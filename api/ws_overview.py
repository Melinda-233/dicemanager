"""通道 1：总览推送（拓扑 + 资源水位，2s 周期）

直接消费 registry.all() 的记录（此前 all() 后又按 id get() 一次，纯浪费且在
实例被删时会 KeyError 打死整条推送循环）。每条记录独立兜底：单实例异常只影响
自己，不放大为整条 WS 断连。actual_port 由本循环从 ring 延迟回填——启动瞬间
进程尚未打印端口，向导 step5 的即时回读恒为 None（已移除）。"""
import asyncio
from types import SimpleNamespace
import psutil
from fastapi import WebSocket, WebSocketDisconnect, APIRouter
from api.context import ctx
from api.auth import auth

router = APIRouter()

EDGE_STATES = {"ok": "solid-green",        # 实线绿：已连接
               "none": "dashed-gray",      # 虚线灰：已配置未连接
               "down": "solid-red"}        # 红：连接失败

def _backfill_actual_port(rec: dict) -> dict:
    """进程存活时从日志 ring 提取实际端口，变化才写盘（2s 周期下的写放大保护）。"""
    alive_port = ctx.get_adapter(rec["dice"]).get_actual_port(ctx.pm.get(rec["id"]).ring)
    if alive_port and alive_port != rec.get("actual_port"):
        ctx.registry.update(rec["id"], actual_port=alive_port)
        return {**rec, "actual_port": alive_port}
    return rec

async def overview_loop(ws: WebSocket):
    while True:
        nodes, edges = [], []
        for rec in ctx.registry.all():
            try:
                alive = ctx.pm.is_alive(rec["id"])
                if alive:
                    rec = _backfill_actual_port(rec)
                nodes.append({"id": rec["id"], "dice": rec["dice"],
                              "arch": rec["arch"],              # allinone → 单节点渲染
                              "state": rec["state"],
                              "process_alive": alive,
                              "port": rec.get("actual_port")
                                      or rec.get("allocated_ports", {}).get("webui"),
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
