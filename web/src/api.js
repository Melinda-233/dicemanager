let token = localStorage.getItem('dm_token') || ''
export const getToken = () => token
export const setToken = t => {
  token = t; localStorage.setItem('dm_token', t)
  dispatchEvent(new Event('dm-auth'))          // 通知导航栏刷新登录状态
}
// 401：凭据失效（服务重启/换密码/旧 token），清掉本地 token 再回登录页
export const unauthorized = () => {
  token = ''; localStorage.removeItem('dm_token')
  dispatchEvent(new Event('dm-auth'))
  if (!location.hash.startsWith('#/login')) location.hash = '#/login'
}

export async function api(path, opts = {}) {
  const r = await fetch(`/api${path}`, {
    ...opts,
    headers: { 'Content-Type': 'application/json',
               ...(token && { Authorization: `Bearer ${token}` }), ...opts.headers },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  })
  if (r.status === 401) { unauthorized(); throw new Error('未授权') }
  if (!r.ok) {
    let detail = r.statusText
    try { detail = (await r.json()).detail || detail } catch { /* 非 JSON 响应兜底 */ }
    throw new Error(detail)
  }
  return r.json()
}

export const login = async pwd =>
  setToken((await api('/login', { method: 'POST', body: { password: pwd } })).token)
// 改密成功后服务端轮换 token（所有旧凭据失效）——必须立刻替换本地 token，否则自己被 401
export const changePassword = async (oldPwd, newPwd) =>
  setToken((await api('/password', { method: 'POST',
                                     body: { old_password: oldPwd, new_password: newPwd } })).token)
export const listInstances = () => api('/instances')
export const listPending = () => api('/pending')
export const listManifests = () => api('/manifests')
export const createInstance = b => api('/instances', { method: 'POST', body: b })
export const wizardStep = (id, step, payload) =>
  api(`/instances/${id}/wizard`, { method: 'POST', body: { step, payload } })
export const opInstance = (id, op) => api(`/instances/${id}/${op}`, { method: 'POST' })
export const delInstance = (id, confirm, removeDir, keepSave) =>
  api(`/instances/${id}?confirm=${confirm}&remove_dir=${removeDir}&keep_save=${keepSave}`,
      { method: 'DELETE' })

// ---------- 总览页连接管理 ----------
// login_ref 传 null 即解除关联；兼容性校验由后端按 manifest 的 compatible_login 做
export const linkInstance = (id, loginRef) =>
  api(`/instances/${id}/link`, { method: 'POST', body: { login_ref: loginRef } })
// WebUI 直连信息：port 可能与分配端口不同（占用时程序自动 +1），token 来自启动日志回读
export const instanceWebui = id => api(`/instances/${id}/webui`)

// ---------- 安装根扫描：游离目录 / 游离进程 ----------
export const scanInstallRoots = () => api('/scan')
export const deleteOrphanDir = path =>
  api(`/scan/dir?path=${encodeURIComponent(path)}`, { method: 'DELETE' })
export const killOrphanProc = pid =>
  api('/scan/kill', { method: 'POST', body: { pid } })

// ---------- 程序包：部署优先解压本地包，免在线下载 ----------
export const listPackages = () => api('/packages')
export const deletePackage = dice => api(`/packages/${dice}`, { method: 'DELETE' })
// 清理「没有任何实例在用」的死缓存：返回 { removed: [...], freed_mb }
export const deleteUnusedPackages = () => api('/packages/unused', { method: 'DELETE' })

// ---------- 备份产物（exports/）：手动导出 / 升级前快照 / 定时备份 ----------
// 与包缓存同源：只增不减，需要一个统一的可见性与回收入口
export const listExports = () => api('/exports')
export const deleteExport = name =>
  api(`/exports/${encodeURIComponent(name)}`, { method: 'DELETE' })
export const pruneExports = days => api(`/exports/prune?days=${days}`, { method: 'DELETE' })
// 部署进度（step2 同步部署期间轮询）：{stage: idle|prepare|download|extract, done, total}
export const deployProgress = id => api(`/deploy-progress/${id}`)
// 大文件原始流上传的公共实现：不走 api()（它会把 body JSON 序列化）。
// 用 XHR 而不是 fetch：只有 XHR 有 upload.onprogress。
// fetch + ReadableStream(duplex:'half') 在本站不可用——它要求 HTTP/2/3 或安全上下文，
// 而本服务是 http://IP:8888（HTTP/1.1 明文），Firefox 也尚不支持该写法。
// onProgress({loaded, total, sent})：sent=true 表示请求体已发完、正在等服务端响应。
function rawUpload(url, file, onProgress) {
  let xhrRef = null
  const p = new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhrRef = xhr
    xhr.open('POST', url)
    xhr.setRequestHeader('Content-Type', 'application/octet-stream')
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    xhr.timeout = 0                 // 大包不限时（服务端自己也做 2GB 上限校验）
    const emit = (loaded, total, sent) =>
      onProgress && onProgress({ loaded, total, sent })
    xhr.upload.onprogress = e => emit(e.loaded, e.lengthComputable ? e.total : 0, false)
    // 请求体发完 → 后端落盘 + 校验/解压（可能数十秒），此时进度条应停在满格并转为「处理中」
    xhr.upload.onload = () => emit(file.size, file.size, true)
    xhr.onload = () => {
      let body = null
      try { body = JSON.parse(xhr.responseText) } catch { /* 非 JSON 响应兜底 */ }
      if (xhr.status === 401) { unauthorized(); reject(new Error('未授权')); return }
      if (xhr.status >= 200 && xhr.status < 300) { resolve(body); return }
      reject(new Error((body && body.detail) || xhr.statusText || '上传失败'))
    }
    xhr.onerror = () => reject(new Error('网络中断：上传失败，请重试'))
    xhr.onabort = () => reject(new Error('已取消上传'))
    xhr.ontimeout = () => reject(new Error('上传超时'))
    xhr.send(file)                  // File 直接作为请求体，浏览器自动设置 Content-Length
  })
  p.abort = () => xhrRef && xhrRef.abort()   // 取消上传（调用方 onabort → reject「已取消」）
  return p
}
export const uploadPackage = (dice, file, onProgress) =>
  rawUpload(`/api/packages/${dice}`, file, onProgress)
// 实例备份导入：后端停机 → 解压覆盖到实例目录 → 自动重启（Overview 实例面板）
export const uploadBackup = (id, file, onProgress) =>
  rawUpload(`/api/instances/${id}/backup`, file, onProgress)

// 裸 <a href> 下载带不上 Authorization 头（原来必 401），改走 fetch + blob
export async function downloadLog(id) {
  const r = await fetch(`/api/logs/${id}/download`,
                        { headers: token ? { Authorization: `Bearer ${token}` } : {} })
  if (r.status === 401) { unauthorized(); throw new Error('未授权') }
  if (!r.ok) throw new Error('下载失败')
  const b = await r.blob(), u = URL.createObjectURL(b)
  const a = Object.assign(document.createElement('a'),
                          { href: u, download: `${id}.log` })
  a.click()
  URL.revokeObjectURL(u)
}

// ---------- 备份导出（拓展1）：整目录 / 应用数据两种口径 ----------
export async function exportBackup(id, scope) {
  const r = await fetch(`/api/instances/${id}/export?scope=${scope}`,
                        { headers: token ? { Authorization: `Bearer ${token}` } : {} })
  if (r.status === 401) { unauthorized(); throw new Error('未授权') }
  if (!r.ok) {
    let detail = r.statusText
    try { detail = (await r.json()).detail || detail } catch { /* 非 JSON 兜底 */ }
    throw new Error(detail)
  }
  const b = await r.blob(), u = URL.createObjectURL(b)
  const name = (r.headers.get('content-disposition') || '').match(/filename="?([^";]+)"?/)?.[1]
    || `${id}-${scope}-${new Date().toISOString().slice(0, 10)}.tar.gz`
  const a = Object.assign(document.createElement('a'),
                          { href: u, download: decodeURIComponent(name) })
  a.click()
  URL.revokeObjectURL(u)
  return { files: r.headers.get('X-Export-Files') }
}

// ---------- 互联诊断（拓展2） ----------
export const diagnoseInstance = id => api(`/instances/${id}/diagnose`)

// ---------- 定时任务（拓展7） ----------
export const listSchedules = () => api('/schedules')
export const addSchedule = b => api('/schedules', { method: 'POST', body: b })
export const delSchedule = id => api(`/schedules/${id}`, { method: 'DELETE' })
export const runSchedule = id => api(`/schedules/${id}/run`, { method: 'POST' })

// ---------- 日志聚合检索（拓展10） ----------
export const searchLogs = (q, instId) =>
  api(`/logs/search?q=${encodeURIComponent(q)}` +
      (instId ? `&inst_id=${encodeURIComponent(instId)}` : ''))

// ---------- 升级通道（拓展11） ----------
export const upgradeCheck = id => api(`/instances/${id}/upgrade-check`)
export const upgradeInstance = id => api(`/instances/${id}/upgrade`, { method: 'POST' })
