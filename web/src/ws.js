const toLogin = () => {
  if (!location.hash.startsWith('#/login')) location.hash = '#/login'
}

export function connectWS(path, onMsg, onOpen) {
  let ws, closed = false, retry = 0
  const open = () => {
    if (closed) return
    const token = localStorage.getItem('dm_token') || ''
    if (!token) {                     // 未登录：别连，直接回登录页（否则握手被拒=1006，会无限重连）
      closed = true; toLogin(); return
    }
    // token 走 Sec-WebSocket-Protocol 子协议头：不进 URL → 不会落 nginx/uvicorn access log
    // （原先 ?token= 查询参数会连 token 一起写进访问日志）
    ws = new WebSocket(
      `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${path}`, token)
    ws.onopen = () => { retry = 0; onOpen && onOpen() }
    ws.onmessage = e => onMsg(JSON.parse(e.data))
    ws.onclose = e => {
      if (e.code === 4401) {          // 鉴权失败：停止重连风暴，回登录页
        closed = true; toLogin(); return
      }
      // 握手被拒时浏览器只给 1006（看不到 4401），连续失败若干次后判定为鉴权失效。
      // 阈值放宽到 5：服务重启窗口期（几秒）的网络抖动不该误踢正在操作的用户
      if (e.code === 1006 && ++retry >= 5) {
        closed = true; toLogin(); return
      }
      setTimeout(open, Math.min(1000 * 2 ** retry++, 30000))
    }
  }
  open()
  return { send: s => ws && ws.readyState === 1 && ws.send(s),
           close: () => { closed = true; ws && ws.close() } }
}
