"""通道 1：总览推送（拓扑 + 资源水位，2s 周期）"""
import asyncio
import psutil
from fastapi import WebSocket, WebSocketDisconnect, APIRouter
from api.context import ctx
from api.auth import auth

router = APIRouter()

EDGE_STATES = {"ok": "solid-green",        # 实线绿：已连接
               "none": "dashed-gray",      # 虚线灰：已配置未连接
               "down": "solid-red"}        # 红：连接失败

async def overview_loop(ws: WebSocket):
    while True:
        nodes, edges = [], []
        for rec in ctx.registry.all():
            inst = ctx.registry.get(rec["id"])
            alive = ctx.pm.is_alive(inst.id)
            nodes.append({"id": inst.id, "dice": inst.dice,
                          "arch": inst.arch,              # allinone → 单节点渲染
                          "state": inst.state,
                          "process_alive": alive,
                          "port": inst.actual_port or inst.allocated_ports.get("webui"),
                          "warnings": inst.warnings})
            if inst.login_ref:                            # 独立程序型才有连线
                try:
                    hc = ctx.get_adapter(inst.dice).health_check(inst, is_alive=alive)
                    edges.append({"src": inst.id, "dst": inst.login_ref,
                                  "state": EDGE_STATES.get(hc["conn"], "dashed-gray")})
                except Exception:
                    edges.append({"src": inst.id, "dst": inst.login_ref,
                                  "state": "solid-red"})
        vm = psutil.virtual_memory()
        try:
            await ws.send_json({"type": "overview", "payload": {
                "nodes": nodes, "edges": edges,
                "resmon": {"ratio": vm.used / vm.total,
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
