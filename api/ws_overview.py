"""通道 1：总览推送（拓扑 + 资源水位，2s 周期）

直接消费 registry.all() 的记录（此前 all() 后又按 id get() 一次，纯浪费且在
实例被删时会 KeyError 打死整条推送循环）。每条记录独立兜底：单实例异常只影响
自己，不放大为整条 WS 断连。actual_port 由本循环从 ring 延迟回填——启动瞬间
进程尚未打印端口，向导 step5 的即时回读恒为 None（已移除）。"""
import asyncio
from types import SimpleNamespace

import psutil
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.auth import auth
from api.context import ctx

router = APIRouter()

EDGE_STATES = {"ok": "solid-green",        # 实线绿：已连接
               "none": "dashed-gray",      # 虚线灰：已配置未连接
               "down": "solid-red"}        # 红：连接失败

# 增量扫描游标（审查 #19）：此前每 2s 对每个存活实例全量重扫 2000 行 ring 跑 3 组正则。
# 记录每实例上次消费到的 seq，之后每跳只扫新增行。游标按 (进程对象, seq) 记，
# 进程对象重建（面板删除后 ring 重置 seq 从 0 起）时自动作废旧游标做一次全量补扫。
_scan_cursor: dict[str, tuple] = {}


def _consume_new_lines(proc) -> list[str]:
    prev = _scan_cursor.get(proc.id)
    last = prev[1] if prev and prev[0] is proc else -1
    out = [(s, ln) for s, ln in proc.ring if s > last]
    if out:
        _scan_cursor[proc.id] = (proc, out[-1][0])
        return [ln for _, ln in out]
    return []


def _backfill_from_logs(rec: dict, lines: list[str]) -> dict:
    """进程存活时从日志回读实际端口 / WebUI token / QQ 号，变化才写盘。

    NapCat 的真实 WebUI 端口（占用时自动 +1）与登录令牌只在启动日志里出现一次，
    这里_periodic 回读是它们的唯一落盘入口；写盘前比对旧值，避免 2s 周期的写放大。
    lines 为自上次消费以来的新增日志行（增量扫描，见 _scan_cursor）——端口/token
    是行级锚点，命中过的结果已持久化到实例记录，无需重复全量扫。
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
    # SnowLuma 等登录端的 OneBot accessToken 由它自己生成并落在 onebot.json，
    # 回读后写入 conn_token，骰子端经 login_ref 继承，保证两端 token 一致
    # （文件读取开销小，不走增量；QQ 号文件侧检测同理——文件可能在任意时刻出现）
    if hasattr(adapter, "get_conn_token"):
        ct = adapter.get_conn_token(SimpleNamespace(**rec))
        if ct and ct != rec.get("conn_token"):
            upd["conn_token"] = ct
    if not rec.get("qq"):
        qq = adapter.detect_account(SimpleNamespace(**rec))
        if qq:
            upd["qq"] = qq
    if upd:
        ctx.registry.update(rec["id"], **upd)
        return {**rec, **upd}
    return rec

async def overview_loop(ws: WebSocket):
    while True:
        nodes, edges = [], []
        for rec in ctx.registry.all():
            try:
                proc = ctx.pm.get(rec["id"])
                alive = proc.is_alive()
                if alive:
                    rec = _backfill_from_logs(rec, _consume_new_lines(proc))
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
                              "qq": rec.get("qq"),
                              "warnings": rec.get("warnings", [])})
                if rec.get("login_ref"):                       # 独立程序型才有连线
                    # health_check 期望属性访问（allocated_ports/actual_port/dir），
                    # 记录 dict 用 SimpleNamespace 适配，避免回表 get()
                    inst = SimpleNamespace(**rec)
                    try:
                        hc = ctx.get_adapter(rec["dice"]).health_check(inst, is_alive=alive)
                        edges.append({"src": rec["id"], "dst": rec["login_ref"],
                                      "state": EDGE_STATES.get(hc["conn"], "dashed-gray")})
                    except Exception:
                        edges.append({"src": rec["id"], "dst": rec["login_ref"],
                                      "state": "solid-red"})
            except Exception:                                  # 单实例异常不拖垮整条推送
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
    ok, sub = auth.ws_handshake(ws)
    if not ok:
        return await ws.close(code=4401)
    await ws.accept(subprotocol=sub)
    try:
        await overview_loop(ws)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
