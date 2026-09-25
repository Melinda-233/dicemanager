"""通道 3：登录推送（qrcode/verify/completed；refresh/skip_login）

回放与实时事件用 ring 的 (seq, line) 对去重：先注册 hook、再读快照，
回放完冲刷队列时跳过 seq ≤ 快照尾的重复——消除了「快照后注册 hook 前」的丢行窗口。"""
import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.auth import auth
from api.context import ctx
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

    # 二维码去重：Lagrange 把二维码打成多行字符画，每行都会命中提取器；
    # 同一张码只推一次（按连接保存，重连时仍会补发当前这张）
    qr_seen = {"payload": None}

    def _dedup_qr(tag: str, payload) -> bool:
        if tag != "qrcode":
            return False
        if payload == qr_seen["payload"]:
            return True
        qr_seen["payload"] = payload
        return False

    def hook(seq: int, line: str):
        """tail 线程 → 事件循环：线程安全投递（asyncio.Queue 非线程安全）。
        签名须与 process.on_line 约定一致 cb(seq, line)——此前单参数写法
        TypeError 被 except 吞掉，二维码/验证事件实时推送整条失效（2026-09-22 修复）。"""
        def _put():
            for tag, fn in EXTRACTORS:
                if item := fn(line, inst):
                    if _dedup_qr(tag, item):
                        continue
                    if queue.full(): queue.get_nowait()
                    queue.put_nowait((seq, {"type": tag, "payload": item}))
        loop.call_soon_threadsafe(_put)

    proc = ctx.pm.get(inst_id)
    proc.on_line(hook)                  # 先注册 hook，再取快照（顺序不能反，否则丢行）
    snapshot = list(proc.ring)          # 回放历史
    max_seq = snapshot[-1][0] if snapshot else -1
    for _, line in snapshot:
        for tag, fn in EXTRACTORS:
            if item := fn(line, inst):
                if _dedup_qr(tag, item):
                    continue
                await ws.send_json({"type": tag, "payload": item})
    while True:                         # 回放期间 hook 已投递的事件：去重后补发
        try: s, ev = queue.get_nowait()
        except asyncio.QueueEmpty: break
        if s > max_seq: await ws.send_json(ev)

    if inst.state == State.AWAIT_LOGIN.value and not proc.is_alive():
        # 登录页首入：进程尚未拉起，自动启动（原实现必须手点刷新才出码）。
        # 仅 AWAIT_LOGIN 才拉起：部署中/错误态重连（浏览器挂着登录页反复重连）
        # 不得触碰实例目录 —— 曾因 prepare_start 建出残缺目录致部署 conflict（2026-09-24）。
        def runner(cmd, cwd, label):    # 与 REST start 同路：首启写回端口 + 一次性 --update
            ctx.pm.run_once(inst_id, cmd, cwd, label=label)
        try:
            adapter.expose_webui(inst)  # 登录同时开放 WebUI（绑定修正 + ufw，尽力而为）
        except Exception:
            pass
        try:
            if adapter.prepare_start(inst, runner):
                ctx.registry.update(inst_id, first_run_done=True)
            ctx.pm.launch(inst_id, adapter.build_start_cmd(inst), inst.dir)
        except (RuntimeError, OSError):
            pass                        # 已在运行等竞态：不打死 WS，进程状态经总览暴露

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
                    try:
                        adapter.expose_webui(inst)   # 重启前再保证一次 WebUI 可达
                    except Exception:
                        pass
                    ctx.pm.launch(inst_id, adapter.build_start_cmd(inst), inst.dir)
                    await ws.send_json({"type": "restarted"})
                elif cmd == "skip_login":
                    _mark_configured()
                    await ws.send_json({"type": "skipped"})
            if get_task in done:
                _, ev = get_task.result()
                await ws.send_json(ev)
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
