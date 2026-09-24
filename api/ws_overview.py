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

def _backfill_from_logs(rec: dict) -> dict:
    """进程存活时从日志 ring 回读实际端口 / WebUI token / QQ 号，变化才写盘。

    NapCat 的真实 WebUI 端口（占用时自动 +1）与登录令牌只在启动日志里出现一次，
    这里_periodic 回读是它们的唯一落盘入口；写盘前比对旧值，避免 2s 周期的写放大。
    """
    adapter = ctx.get_adapter(rec["dice"])
    ring = ctx.pm.get(rec["id"]).ring
    upd: dict = {}
    port = adapter.get_actual_port(ring)
    if port and port != rec.get("actual_port"):
        upd["actual_port"] = port
    token = adapter.get_webui_token(ring)
    if token and token != rec.get("webui_token"):
        upd["webui_token"] = token
    # SnowLuma 等登录端的 OneBot accessToken 由它自己生成并落在 onebot.json，
    # 回读后写入 conn_token，骰子端经 login_ref 继承，保证两端 token 一致
    if hasattr(adapter, "get_conn_token"):
        ct = adapter.get_conn_token(SimpleNamespace(**rec))
        if ct and ct != rec.get("conn_token"):
            upd["conn_token"] = ct
    if not rec.get("qq"):
        qq = adapter.detect_account(SimpleNamespace(**rec))
        if not qq and hasattr(adapter, "account_from_logs"):
            qq = adapter.account_from_logs(ring)
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
                alive = ctx.pm.is_alive(rec["id"])
                if alive:
                    rec = _backfill_from_logs(rec)
                nodes.append({"id": rec["id"], "dice": rec["dice"],
                              "arch": rec["arch"],              # allinone → 单节点渲染
                              "state": rec["state"],
                              "process_alive": alive,
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
    if not auth.verify_ws(ws):
        return await ws.close(code=4401)
    await ws.accept()
    try:
        await overview_loop(ws)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
