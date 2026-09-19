let token = localStorage.getItem('dm_token') || ''
export const getToken = () => token
export const setToken = t => {
  token = t; localStorage.setItem('dm_token', t)
  dispatchEvent(new Event('dm-auth'))          // 通知导航栏刷新登录状态
}

export async function api(path, opts = {}) {
  const r = await fetch(`/api${path}`, {
    ...opts,
    headers: { 'Content-Type': 'application/json',
               ...(token && { Authorization: `Bearer ${token}` }), ...opts.headers },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  })
  if (r.status === 401) { location.hash = '#/login'; throw new Error('未授权') }
  if (!r.ok) {
    let detail = r.statusText
    try { detail = (await r.json()).detail || detail } catch { /* 非 JSON 响应兜底 */ }
    throw new Error(detail)
  }
  return r.json()
}

export const login = async pwd =>
  setToken((await api('/login', { method: 'POST', body: { password: pwd } })).token)
export const listInstances = () => api('/instances')
export const listManifests = () => api('/manifests')
export const createInstance = b => api('/instances', { method: 'POST', body: b })
export const wizardStep = (id, step, payload) =>
  api(`/instances/${id}/wizard`, { method: 'POST', body: { step, payload } })
export const opInstance = (id, op) => api(`/instances/${id}/${op}`, { method: 'POST' })
export const delInstance = (id, confirm, removeDir, keepSave) =>
  api(`/instances/${id}?confirm=${confirm}&remove_dir=${removeDir}&keep_save=${keepSave}`,
      { method: 'DELETE' })
export const resmon = () => api('/resmon')

// 裸 <a href> 下载带不上 Authorization 头（原来必 401），改走 fetch + blob
export async function downloadLog(id) {
  const r = await fetch(`/api/logs/${id}/download`,
                        { headers: token ? { Authorization: `Bearer ${token}` } : {} })
  if (r.status === 401) { location.hash = '#/login'; throw new Error('未授权') }
  if (!r.ok) throw new Error('下载失败')
  const b = await r.blob(), u = URL.createObjectURL(b)
  const a = Object.assign(document.createElement('a'),
                          { href: u, download: `${id}.log` })
  a.click()
  URL.revokeObjectURL(u)
}
