"""通道 3：登录推送（qrcode/verify/completed；refresh/skip_login）"""
import asyncio
from fastapi import WebSocket, WebSocketDisconnect, APIRouter
from api.context import ctx
from api.auth import auth
from core.registry import State

router = APIRouter()

@router.websocket("/ws/login/{inst_id}")
async def ws_login(ws: WebSocket, inst_id: str):
    if not auth.verify_ws(ws):
        return await ws.close(code=4401)
    await ws.accept()
    inst = ctx.registry.get(inst_id)
    adapter = ctx.get_adapter(inst.dice)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    # 类型与提取器绑定：事件类型由这里声明，不再从 payload 形状反推
    EXTRACTORS = (("qrcode", adapter.extract_qrcode),
                  ("verify", adapter.extract_verify))

    def hook(line: str):
        """tail 线程 → 事件循环：线程安全投递（asyncio.Queue 非线程安全）。"""
        def _put():
            for tag, fn in EXTRACTORS:
                if item := fn(line):
                    if queue.full(): queue.get_nowait()
                    queue.put_nowait({"type": tag, "payload": item})
        loop.call_soon_threadsafe(_put)

    proc = ctx.pm.get(inst_id)
    for line in list(proc.ring):        # 回放历史
        for tag, fn in EXTRACTORS:
            if item := fn(line):
                await ws.send_json({"type": tag, "payload": item})
    proc.on_line(hook)

    def _mark_configured():
        try:
            ctx.registry.transition(inst_id, State.CONFIGURED)
        except ValueError:
            pass                        # 幂等：已是 CONFIGURED/RUNNING 等场景

    get_task = None
    recv_task = None
    try:
        while True:
            if get_task is None:
                get_task = asyncio.ensure_future(queue.get())
            if recv_task is None:
                recv_task = asyncio.ensure_future(ws.receive_text())
            done, _ = await asyncio.wait({get_task, recv_task}, timeout=2.0,
                                         return_when=asyncio.FIRST_COMPLETED)
            if recv_task in done:
                cmd = recv_task.result()
                recv_task = None
                if cmd == "refresh":
                    proc.stop()
                    ctx.pm.launch(inst_id, adapter.build_start_cmd(inst), inst.dir)
                    await ws.send_json({"type": "restarted"})
                elif cmd == "skip_login":
                    _mark_configured()
                    await ws.send_json({"type": "skipped"})
            if get_task in done:
                await ws.send_json(get_task.result())
                get_task = None

            if recv_task is not None and not recv_task.done() and \
               (get_task is None or not get_task.done()):
                # 空闲周期才做健康检查（原实现每收到一行日志都探测一次，纯浪费）
                hc = adapter.health_check(inst, is_alive=proc.is_alive())
                if hc["alive"] and hc["conn"] == "ok":  # 完成判定：存活 + 链路连通
                    _mark_configured()
                    await ws.send_json({"type": "completed"})
                    return
    except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        proc.off_line(hook)
        for t in (get_task, recv_task):
            if t and not t.done(): t.cancel()
