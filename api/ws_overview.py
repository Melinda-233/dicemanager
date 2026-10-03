"""通道 1：总览推送（拓扑 + 资源水位，2s 周期）

直接消费 registry.all() 的记录（此前 all() 后又按 id get() 一次，纯浪费且在
实例被删时会 KeyError 打死整条推送循环）。每条记录独立兜底：单实例异常只影响
自己，不放大为整条 WS 断连。actual_port 由本循环从 ring 延迟回填——启动瞬间
进程尚未打印端口，向导 step5 的即时回读恒为 None（已移除）。"""
import asyncio
import logging
from types import SimpleNamespace

import psutil
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.auth import auth
from api.context import ctx

router = APIRouter()
logger = logging.getLogger(__name__)

EDGE_STATES = {"ok": "solid-green",        # 实线绿：已连接
               "none": "dashed-gray",      # 虚线灰：已配置未连接
               "down": "solid-red"}        # 红：连接失败

# 增量扫描游标（审查 #19）：此前每 2s 对每个存活实例全量重扫 2000 行 ring 跑 3 组正则。
# 记录每实例上次消费到的 seq，之后每跳只扫新增行。游标按 (进程对象, seq) 记，
# 进程对象重建（面板删除后 ring 重置 seq 从 0 起）时自动作废旧游标做一次全量补扫。
_scan_cursor: dict[str, tuple] = {}


def _consume_new_lines(proc) -> list[tuple[int, str]]:
    """返回自上次消费以来的新增日志行，保持 (seq, line) 二元组格式。

    关键契约：get_actual_port / get_webui_token 的 lines 参数按 (seq, line) 二元组解包
    （见 adapters/*.py 与 tests/test_patch_regress.py 的 ring() 约定），绝不能剥成纯字符串——
    此前曾 return [ln for _, ln in out]，导致对字符串做 `for _, line in` 解包抛
    ValueError，整轮 overview 被 except 跳过，节点从总览payload消失（llbot 等繁忙机器人
    几乎每轮都触发）。"""
    prev = _scan_cursor.get(proc.id)
    last = prev[1] if prev and prev[0] is proc else -1
    out = [(s, ln) for s, ln in proc.ring if s > last]
    if out:
        _scan_cursor[proc.id] = (proc, out[-1][0])
        return out
    return []


def _backfill_from_logs(rec: dict, lines: list[tuple[int, str]]) -> dict:
    """进程存活时从日志回读实际端口 / WebUI token / QQ 号，变化才写盘。

    NapCat 的真实 WebUI 端口（占用时自动 +1）与登录令牌只在启动日志里出现一次，
    这里_periodic 回读是它们的唯一落盘入口；写盘前比对旧值，避免 2s 周期的写放大。
    lines 为自上次消费以来的新增日志行（增量扫描，见 _scan_cursor）——端口/token
    是行级锚点，命中过的结果已持久化到实例记录，无需重复全量扫。

    lines 恒为 (seq, line) 二元组序列：适配器 get_actual_port / get_webui_token 按
    二元组解包，此处签名必须与 _consume_new_lines 保持一致，否则 mypy arg-type 报错
    （CI 门禁），且历史上正是剥成纯字符串导致解包抛 ValueError、节点整条消失。
    """
    adapter = ctx.get_adapter(rec["dice"])
    upd: dict = {}
    if lines:
        port = adapter.get_actual_port(lines)
        if port and port != rec.get("actual_port"):
            upd["actual_port"] = port
        token = adapter.get_webui_token(lines)
        if token and token != rec.get("webui_token"):
            upd["webui_token"] = token
        if not rec.get("qq"):
            qq = adapter.account_from_logs(lines) if hasattr(adapter, "account_from_logs") else None
            if qq:
                upd["qq"] = qq
    # 登录端：周期回读已登录的全部 QQ 账号（list_accounts 钩子），供多账号/按账号分发展示
    if hasattr(adapter, "list_accounts"):
        try:
            accs = adapter.list_accounts(SimpleNamespace(**rec)) or []
            sig = repr([(a.get("qq"), a.get("port"), a.get("token")) for a in accs])
            old = repr([(a.get("qq"), a.get("port"), a.get("token"))
                        for a in (rec.get("accounts") or [])])
            if sig != old:
                upd["accounts"] = accs
                # 首个账号作为展示用 qq（若尚未设置）
                if accs and not rec.get("qq"):
                    upd["qq"] = accs[0].get("qq")
        except Exception:
            pass
    # SnowLuma 等登录端的 OneBot accessToken 由它自己生成并落在 onebot.json，
    # 回读后写入 conn_token，骰子端经 login_ref 继承，保证两端 token 一致
    # （文件读取开销小，不走增量；QQ 号文件侧检测同理——文件可能在任意时刻出现）
    if hasattr(adapter, "get_conn_token"):
        ct = adapter.get_conn_token(SimpleNamespace(**rec))
        if ct and ct != rec.get("conn_token"):
            upd["conn_token"] = ct
    if not rec.get("qq") and hasattr(adapter, "detect_account"):
        qq = adapter.detect_account(SimpleNamespace(**rec))
        if qq:
            upd["qq"] = qq
    if upd:
        ctx.registry.update(rec["id"], **upd)
        return {**rec, **upd}
    return rec


def _service_reachable(rec: dict) -> bool:
    """句柄失效但服务端口仍通 → 程序实际在跑（总览显示兜底）。

    llbot 等是 launcher+worker 结构：面板只持有 launcher 的 Popen 句柄，worker(node)
    才是真正监听端口的进程。launcher 重启 / 面板重启后 worker 被 reparent 到 init 时，
    句柄失效会让总览误判「已停止」，但 worker 仍在 3001/3080 上服务。这里用 adapter.is_up
    （端口 TCP 探测，与 health_check 同口径）作真相兜底，避免节点变灰/消失。"""
    try:
        return bool(ctx.get_adapter(rec["dice"]).is_up(SimpleNamespace(**rec)))
    except Exception:
        return False


async def overview_loop(ws: WebSocket, user=None):
    """周期推送总览拓扑。

    user 非None 且非管理员时**只推本人名下实例**——拓扑含QQ 号、连接关系与
    告警，跨用户可见即泄露。
    """
    mine = None if (user is None or user.is_admin) else user.username
    while True:
        nodes, edges = [], []
        for rec in ctx.registry.all():
            if mine is not None and (rec.get("owner") or "admin") != mine:
                continue
            try:
                proc = ctx.pm.get(rec["id"])
                handle_alive = proc.is_alive()
                # 句柄失效但端口仍通（launcher 重启 / 面板重启后 worker reparent）→ 视为存活
                alive = handle_alive or _service_reachable(rec)
                # 日志回读（端口/token/qq）只是增强信息：失败绝不能让节点从总览消失，
                # 独立兜底，异常时保留原 rec（节点照常渲染，仅少回读几项字段）。
                if handle_alive:
                    try:
                        rec = _backfill_from_logs(rec, _consume_new_lines(proc))
                    except Exception as e:
                        logger.warning("backfill %s skipped: %r", rec.get("id"), e)
                nodes.append({"id": rec["id"], "dice": rec["dice"],
                              "arch": rec["arch"],              # allinone → 单节点渲染
                              "state": rec["state"],
                              "process_alive": alive,
                              # 熔断告警：反复崩溃已停止自动重启（start 后自动解除）
                              "crash_looped": proc.crash_looped,
                              # 每实例内存（probe 内 psutil rss，异常时 None）
                              "mem_mb": proc.probe().get("mem_mb"),
                              "port": rec.get("actual_port")
                                      or rec.get("allocated_ports", {}).get("webui"),
                              # 连接管理面板需要：关联目标与登录账号（旧 payload 缺这两项）
                              "login_ref": rec.get("login_ref"),
                              "accounts": rec.get("accounts") or [],
                              "links": rec.get("links") or (
                                  [{"login_ref": rec["login_ref"]}] if rec.get("login_ref") else []),
                              "qq": rec.get("qq"),
                              "warnings": rec.get("warnings", [])})
                links = rec.get("links") or (
                    [{"login_ref": rec["login_ref"]}] if rec.get("login_ref") else [])
                for lk in links:                               # 每条关联一条连线（多连一/一连多）
                    dst = lk.get("login_ref")
                    if not dst:
                        continue
                    inst = SimpleNamespace(**rec)
                    try:
                        hc = ctx.get_adapter(rec["dice"]).health_check(inst, is_alive=alive)
                        edges.append({"src": rec["id"], "dst": dst,
                                      "state": EDGE_STATES.get(hc["conn"], "dashed-gray"),
                                      # 按账号分发：连线标注绑定的 QQ，便于一眼看出一连多/多连一
                                      "account_qq": lk.get("account_qq")})
                    except Exception:
                        edges.append({"src": rec["id"], "dst": dst,
                                      "state": "solid-red"})
            except Exception as e:                             # 单实例异常不拖垮整条推送，但留痕
                logger.warning("overview skip %s: %r", rec.get("id"), e)
                continue
        vm = psutil.virtual_memory()
        try:
            await ws.send_json({"type": "overview", "payload": {
                "nodes": nodes, "edges": edges,
                # 字段需与 REST /api/resmon 对齐：总览页要显示 used_mb / total_mb
                "resmon": {"ratio": vm.used / vm.total,
                           "total_mb": vm.total // 1048576,
                           "used_mb": vm.used // 1048576,
                           "alert": vm.used / vm.total >= ctx.resmon_alert}}})
        except (RuntimeError, WebSocketDisconnect):
            break                                          # 客户端已断开，退出推送循环
        await asyncio.sleep(2.0)

@router.websocket("/ws/overview")
async def ws_overview(ws: WebSocket):
    ok, sub, user = auth.ws_handshake(ws)      # 必须取 user：用于按 owner 过滤推送
    if not ok or user is None:
        return await ws.close(code=4401)
    await ws.accept(subprotocol=sub)
    try:
        await overview_loop(ws, user)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
