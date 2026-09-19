"""通道 2：日志 tail（历史回放 + 实时行 + 暂停跟随 + 关键字过滤 + 错误标记）

回放与实时行用 ring 的 (seq, line) 对去重：先注册 hook、再读快照，
回放完冲刷队列时跳过 seq ≤ 快照尾的重复——消除了「快照后注册 hook 前」的丢行窗口。"""
import asyncio, re
from fastapi import WebSocket, WebSocketDisconnect, APIRouter
from api.context import ctx
from api.auth import auth

router = APIRouter()
ERROR_RE = re.compile(r"\b(ERROR|FATAL|Traceback|panic)\b", re.I)  # 通用错误关键字

def compile_filter(kw: str | None):
    return re.compile(re.escape(kw), re.I) if kw else None

@router.websocket("/ws/logs/{inst_id}")
async def ws_logs(ws: WebSocket, inst_id: str):
    if not auth.verify_ws(ws):
        return await ws.close(code=4401)
    await ws.accept()
    proc = ctx.pm.get(inst_id)
    loop = asyncio.get_running_loop()
    paused = False
    filter_re = None
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

    def hook(pair: tuple):
        """tail 线程 → 事件循环：asyncio.Queue 非线程安全，必须经 call_soon_threadsafe；
        队列满时丢最旧，保证不阻塞 tail 线程。投递 (seq, line) 对。"""
        def _put():
            if queue.full(): queue.get_nowait()
            queue.put_nowait(pair)
        loop.call_soon_threadsafe(_put)

    async def send_line(s: int, line: str):
        await ws.send_json({"type": "line", "seq": s, "text": line,
                            "error": bool(ERROR_RE.search(line))})

    proc.on_line(hook)                  # 先注册 hook，再取快照（顺序不能反，否则丢行）
    snapshot = list(proc.ring)          # 历史回放（重启不丢）
    max_seq = snapshot[-1][0] if snapshot else -1
    for s, line in snapshot:
        await send_line(s, line)
    while True:                         # 回放期间 hook 已投递的行：去重后补发真新行
        try: s, line = queue.get_nowait()
        except asyncio.QueueEmpty: break
        if s > max_seq: await send_line(s, line)

    get_task = None                     # 持久任务：避免每轮重建 receive/get 造成的悬挂与丢消息
    recv_task = None
    try:
        while True:
            if get_task is None:
                get_task = asyncio.ensure_future(queue.get())
            if recv_task is None:
                recv_task = asyncio.ensure_future(ws.receive_text())
            done, _ = await asyncio.wait({get_task, recv_task}, timeout=2.0,
                                         return_when=asyncio.FIRST_COMPLETED)
            if recv_task in done:       # 客户端控制指令（先于数据行应用）
                msg = recv_task.result()
                recv_task = None
                if   msg == "pause":  paused = True
                elif msg == "resume": paused = False
                elif msg.startswith("filter:"):
                    filter_re = compile_filter(msg.split(":", 1)[1] or None)
            if get_task in done:
                s, line = get_task.result()
                get_task = None
                if not paused and (filter_re is None or filter_re.search(line)):
                    await send_line(s, line)
    except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
        pass                            # 断开 / send-after-close（RuntimeError）都算正常退出
    finally:
        proc.off_line(hook)             # 断开必须注销
        for t in (get_task, recv_task):
            if t and not t.done(): t.cancel()
