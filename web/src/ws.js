export function connectWS(path, onMsg, onOpen) {
  let ws, closed = false, retry = 0
  const open = () => {
    if (closed) return
    const token = localStorage.getItem('dm_token') || ''
    ws = new WebSocket(
      `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${path}?token=${token}`)
    ws.onopen = () => { retry = 0; onOpen && onOpen() }
    ws.onmessage = e => onMsg(JSON.parse(e.data))
    ws.onclose = e => {
      if (e.code === 4401) {          // 鉴权失败：停止重连风暴，回登录页
        closed = true; location.hash = '#/login'; return
      }
      setTimeout(open, Math.min(1000 * 2 ** retry++, 30000))
    }
  }
  open()
  return { send: s => ws && ws.readyState === 1 && ws.send(s),
           close: () => { closed = true; ws && ws.close() } }
}
