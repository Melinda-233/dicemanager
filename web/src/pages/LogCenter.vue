<template>
  <div class="logcenter">
    <div class="toolbar">
      <!-- 实例选择：加状态点与分组（运行中/已停止），裸列表在实例一多就分不清
           哪个在跑。另外空态要显式说明，不能留一个空白下拉框让用户干瞪眼。 -->
      <select v-model="instanceId" @change="onPickInstance" class="pick">
        <option value="" disabled>选择实例以查看日志…</option>
        <optgroup v-if="running.length" label="运行中">
          <option v-for="i in running" :key="i.id" :value="i.id">
            ● {{ i.dice }}<template v-if="qqOf(i)"> · {{ qqOf(i) }}</template> · {{ shortId(i.id) }}
          </option>
        </optgroup>
        <optgroup v-if="stopped.length" label="已停止">
          <option v-for="i in stopped" :key="i.id" :value="i.id">
            ○ {{ i.dice }}<template v-if="qqOf(i)"> · {{ qqOf(i) }}</template> · {{ shortId(i.id) }}
          </option>
        </optgroup>
      </select>
      <span v-if="!instances.length" class="hint">还没有实例</span>
      <!-- 子视图切换：日志流 / 实例文件只读浏览（D 项）。两个互斥，用独立 v-if 各自渲染。 -->
      <div class="subtabs">
        <button :class="{active: view === 'logs'}" @click="view = 'logs'">日志</button>
        <button :class="{active: view === 'files'}" @click="view = 'files'">文件</button>
      </div>
    </div>

    <!-- ===== 日志视图 ===== -->
    <template v-if="view === 'logs'">
      <div class="toolbar">
        <input v-model="keyword" placeholder="关键字过滤（当前实例）" @change="applyFilter"/>
        <button @click="togglePause">{{ paused ? '继续跟随' : '暂停跟随' }}</button>
        <button @click="copyAll">复制</button>
        <button @click="download">下载</button>
      </div>
      <!-- 跨实例聚合检索（拓展10）：ring + 磁盘日志尾部，不依赖当前选中实例 -->
      <div class="toolbar">
        <input v-model="gq" class="gq" placeholder="跨实例全局搜索（回车执行，至少 2 字符）"
               @keyup.enter="globalSearch"/>
        <button @click="globalSearch">搜索</button>
      </div>
      <div v-if="searching" class="hint">搜索中…</div>
      <div v-if="gResults" class="gres">
        <p class="hint">全局搜索「{{ gResults.q }}」：{{ gResults.results.length }} 条结果
          <button class="x" @click="gResults = null">收起</button></p>
        <div v-for="(r, i) in gResults.results" :key="i" class="g-row">
          <span class="g-inst" :title="r.instance">{{ r.dice }}</span>
          <span class="g-line">{{ r.line }}</span>
          <button v-if="r.instance !== instanceId" @click="jumpTo(r.instance)">查看该实例</button>
        </div>
        <p v-if="!gResults.results.length" class="hint">没有匹配的日志行。</p>
      </div>
      <div class="term" ref="term">
        <div v-for="l in lines" :key="l.seq" :class="{err: l.error}">{{ l.text }}</div>
      </div>
    </template>

    <!-- ===== 文件视图（D 项，只读） ===== -->
    <template v-else>
      <div class="toolbar">
        <button @click="goUp" :disabled="!fPath">上一级</button>
        <span class="crumb">
          <span class="link" @click="loadFiles('')">根目录</span>
          <template v-for="(c, i) in breadcrumb" :key="i">
            <span class="sep">/</span><span class="link" @click="goCrumb(c.path)">{{ c.name }}</span>
          </template>
        </span>
        <span class="spacer" />
        <button @click="fNewFile">新建文件</button>
        <button @click="fNewFolder">新建文件夹</button>
        <button @click="fUpload">上传</button>
        <span v-if="fLoading" class="hint">加载中…</span>
      </div>
      <input ref="fileInput" type="file" class="hide" @change="fOnFile" />
      <div v-if="fErr" class="hint warn">{{ fErr }}</div>

      <!-- 文件内容查看（文本） -->
      <div v-if="fContent" class="fileview">
        <div class="toolbar">
          <span class="hint">{{ fContent.path }}（{{ fmtSize(fContent.size) }}）</span>
          <button @click="fContent = null">返回列表</button>
          <button v-if="!fEditing" @click="startEdit">编辑</button>
          <template v-else>
            <button class="primary" @click="saveEdit">保存</button>
            <button @click="cancelEdit">取消</button>
          </template>
          <button @click="fDownloadCurrent">下载</button>
        </div>
        <pre v-if="!fEditing" class="ftext">{{ fContent.text }}</pre>
        <textarea v-else ref="editArea" v-model="fEditText" class="fedit" spellcheck="false"></textarea>
      </div>

      <!-- 目录列表 -->
      <div v-else class="filelist">
        <div v-for="e in fEntries" :key="e.name" class="frow" @click="openEntry(e)">
          <span class="ficon">{{ e.is_dir ? '📁' : '📄' }}</span>
          <span class="fname">{{ e.name }}</span>
          <span class="fmeta" v-if="!e.is_dir">{{ fmtSize(e.size) }} · {{ fmtTime(e.mtime) }}</span>
          <span class="factions">
            <button v-if="!e.is_dir" class="fbtn" @click.stop="fDownload(e)">下载</button>
            <button class="fbtn" @click.stop="fRename(e)">重命名</button>
            <button class="fbtn danger" @click.stop="fDelete(e)">删除</button>
          </span>
        </div>
        <p v-if="!fLoading && !fErr && !fEntries.length" class="hint">空目录</p>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { connectWS } from '../ws'
import { listInstances, downloadLog, searchLogs,
         listInstanceFiles, getInstanceFileContent, downloadInstanceFile,
         writeInstanceFile, mkdirInstanceFile, renameInstanceFile, deleteInstanceFile } from '../api'

const instances = ref([]), instanceId = ref(''), lines = ref([])
const keyword = ref(''), paused = ref(false), term = ref(null)
const gq = ref(''), gResults = ref(null), searching = ref(false)
const view = ref('logs')          // 'logs' | 'files'

// 文件视图状态
const fPath = ref(''), fEntries = ref([]), fErr = ref('')
const fLoading = ref(false), fContent = ref(null)
const fEditing = ref(false), fEditText = ref('')
const fileInput = ref(null), editArea = ref(null)

let sock
const render = m => {
  if (m.type !== 'line') return
  lines.value.push(m)
  if (lines.value.length > 2000) lines.value.shift()
  if (!paused.value) nextTick(() =>
    term.value && (term.value.scrollTop = term.value.scrollHeight))
}
// 运行中 / 已停止分组：只靠文字列表在实例多时无法区分状态
const running = computed(() => instances.value.filter(i => i.state === 'RUNNING'))
const stopped = computed(() => instances.value.filter(i => i.state !== 'RUNNING'))
const shortId = id => (id || '').length > 22 ? id.slice(0, 12) + '…' + id.slice(-6) : id
// QQ 号：登录端有 qq 字段（且 accounts 里也有一份），应用端没有 QQ 这个概念。
// 两个来源都取、多个账号用顿号列出 —— 挑日志时按 QQ 认实例比按 ID 认快得多。
const qqOf = i => {
  const seen = []
  const push = v => {
    const t = String(v ?? '').trim()
    if (t && !seen.includes(t)) seen.push(t)
  }
  push(i.qq)
  for (const a of i.accounts || []) push(a.qq)
  return seen.join('、')
}

const reconnect = () => {
  sock?.close(); lines.value = []
  if (!instanceId.value) return
  sock = connectWS(`/ws/logs/${instanceId.value}`, render)
}
// 切换实例时把过滤条件一并清掉：后端的 filter 是按实例解析的，
// 留着上一个实例的关键词会在新实例上匹配不到东西 —— 表现为
// 「换了实例但搜不到内容」，且没有任何提示说为什么。
const onPickInstance = () => {
  keyword.value = ''
  gResults.value = null
  paused.value = false
  reconnect()
  if (view.value === 'files') loadFiles('')   // 切到别的实例时文件视图也要重新列
}
const togglePause = () => { paused.value = !paused.value
  sock?.send(paused.value ? 'pause' : 'resume') }
const applyFilter = () => sock?.send(`filter:${keyword.value}`)
const copyAll = () => navigator.clipboard.writeText(lines.value.map(l => l.text).join('\n'))
const download = () => instanceId.value && downloadLog(instanceId.value).catch(() => {})

const globalSearch = async () => {
  if (!gq.value || gq.value.trim().length < 2) return
  searching.value = true
  try {
    gResults.value = await searchLogs(gq.value.trim())
  } catch (e) {
    gResults.value = { q: gq.value, results: [] }
  } finally {
    searching.value = false
  }
}
const jumpTo = id => { instanceId.value = id; reconnect() }

// ---------- 文件只读浏览（D 项） ----------
const loadFiles = async (p = '') => {
  if (!instanceId.value) return
  fLoading.value = true; fErr.value = ''; fContent.value = null
  try {
    const r = await listInstanceFiles(instanceId.value, p)
    fEntries.value = r.entries
    fPath.value = r.path
  } catch (e) { fErr.value = e.message } finally { fLoading.value = false }
}
const openEntry = e => {
  const p = fPath.value ? `${fPath.value}/${e.name}` : e.name
  if (e.is_dir) loadFiles(p)
  else viewFile(p)
}
const viewFile = async path => {
  fLoading.value = true; fErr.value = ''
  try {
    fContent.value = await getInstanceFileContent(instanceId.value, path)
  } catch (e) { fErr.value = e.message; fContent.value = null } finally { fLoading.value = false }
}
const goUp = () => {
  const parts = (fPath.value || '').split('/').filter(Boolean)
  parts.pop(); loadFiles(parts.join('/'))
}
const goCrumb = p => loadFiles(p)
const breadcrumb = computed(() => {
  const acc = [], out = []
  for (const p of (fPath.value || '').split('/').filter(Boolean)) {
    acc.push(p); out.push({ name: p, path: acc.join('/') })
  }
  return out
})
const fDownload = e => {
  const p = fPath.value ? `${fPath.value}/${e.name}` : e.name
  downloadInstanceFile(instanceId.value, p, e.name).catch(e => fErr.value = e.message)
}
const fDownloadCurrent = () =>
  downloadInstanceFile(instanceId.value, fContent.value.path, fContent.value.path.split('/').pop())
    .catch(e => fErr.value = e.message)

// ---------- 受限写：编辑 / 新建 / 上传 / 重命名 / 删除（限制在实例目录内）----------
const startEdit = () => {
  fEditText.value = fContent.value.text
  fEditing.value = true
  nextTick(() => editArea.value?.focus())
}
const cancelEdit = () => { fEditing.value = false }
const saveEdit = async () => {
  try {
    await writeInstanceFile(instanceId.value, fContent.value.path, fEditText.value, 'text')
    fContent.value = await getInstanceFileContent(instanceId.value, fContent.value.path)
    fEditing.value = false; fErr.value = ''
  } catch (e) { fErr.value = e.message }
}
const fNewFile = async () => {
  const name = window.prompt('新文件名（可含子目录，如 config/sub.txt）：')
  if (!name) return
  try {
    await writeInstanceFile(instanceId.value, name.trim(), '', 'text')
    await loadFiles(fPath.value); fErr.value = ''
  } catch (e) { fErr.value = e.message }
}
const fNewFolder = async () => {
  const name = window.prompt('新文件夹名：')
  if (!name) return
  try {
    await mkdirInstanceFile(instanceId.value, name.trim())
    await loadFiles(fPath.value); fErr.value = ''
  } catch (e) { fErr.value = e.message }
}
const fRename = async (e) => {
  const newName = window.prompt('重命名为：', e.name)
  if (!newName || newName.trim() === e.name) return
  const p = fPath.value ? `${fPath.value}/${e.name}` : e.name
  try {
    await renameInstanceFile(instanceId.value, p, newName.trim())
    await loadFiles(fPath.value); fErr.value = ''
  } catch (err) { fErr.value = err.message }
}
const fDelete = async (e) => {
  if (!window.confirm(`确定删除 ${e.is_dir ? '文件夹' : '文件'}「${e.name}」？此操作不可恢复。`)) return
  const p = fPath.value ? `${fPath.value}/${e.name}` : e.name
  try {
    await deleteInstanceFile(instanceId.value, p)
    await loadFiles(fPath.value); fErr.value = ''
  } catch (err) { fErr.value = err.message }
}
const fUpload = () => fileInput.value?.click()
const fOnFile = async (ev) => {
  const f = ev.target.files?.[0]
  if (!f) return
  try {
    const buf = await f.arrayBuffer()
    const bytes = new Uint8Array(buf); let bin = ''
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i])
    const p = fPath.value ? `${fPath.value}/${f.name}` : f.name
    await writeInstanceFile(instanceId.value, p, btoa(bin), 'base64')
    await loadFiles(fPath.value); fErr.value = ''
  } catch (e) { fErr.value = e.message }
  ev.target.value = ''
}

const fmtSize = n => {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}
const fmtTime = ts => {
  const d = new Date(ts * 1000), p = x => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

onMounted(async () => {
  instances.value = await listInstances()
  instanceId.value = new URLSearchParams(location.hash.split('?')[1] || '')
                      .get('instance') || instances.value[0]?.id || ''
  reconnect()
})
onUnmounted(() => sock?.close())
</script>

<style scoped>
.term { font-family: monospace; background: #111; color: #ddd;
        height: 70vh; overflow-y: auto; padding: 8px; white-space: pre-wrap; }
.term .err { color: #ff7b72; background: #3a1d1f; }
.gq { flex: 1; min-width: 240px; }
.pick { min-width: 320px; max-width: 460px; }
.gres { margin: 8px 0; padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px; }
.gres .x { padding: 1px 8px; font-size: 12px; float: right; }
.g-row { display: flex; gap: 8px; align-items: baseline; margin: 2px 0; font-size: 12.5px; }
.g-inst { flex-shrink: 0; color: var(--brand); font-weight: 600; min-width: 72px; }
.g-line { flex: 1; font-family: ui-monospace, Menlo, Consolas, monospace;
          white-space: pre-wrap; word-break: break-all; }
.g-row button { padding: 0 8px; font-size: 12px; flex-shrink: 0; }

/* 子视图切换：与日志中心的其余按钮区分开，避免误以为是日志过滤动作 */
.subtabs { margin-left: auto; display: flex; gap: 4px; }
.subtabs button { padding: 4px 14px; border: 1px solid var(--border);
                  background: var(--bg); color: var(--text); border-radius: 6px; cursor: pointer; }
.subtabs button.active { background: var(--brand); color: #fff; border-color: var(--brand); }

/* 文件浏览器 */
.crumb { font-size: 13px; color: var(--muted); display: inline-flex; align-items: center; gap: 2px; }
.crumb .link { color: var(--brand); cursor: pointer; }
.crumb .link:hover { text-decoration: underline; }
.crumb .sep { color: var(--muted); margin: 0 2px; }
.filelist { border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.frow { display: flex; align-items: center; gap: 10px; padding: 7px 12px;
        border-bottom: 1px solid var(--border); cursor: pointer; }
.frow:last-child { border-bottom: none; }
.frow:hover { background: var(--bg-hover); }
.ficon { width: 22px; text-align: center; }
.fname { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fmeta { font-size: 12px; color: var(--muted); flex-shrink: 0; }
.fbtn { padding: 1px 10px; font-size: 12px; flex-shrink: 0; }
.fileview { border: 1px solid var(--border); border-radius: 8px; }
.ftext { font-family: ui-monospace, Menlo, Consolas, monospace; margin: 0;
         padding: 12px; max-height: 64vh; overflow: auto;
         white-space: pre-wrap; word-break: break-all; font-size: 12.5px; }

/* 文件视图工具条与受限写控件 */
.toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.toolbar .spacer { flex: 1; }
.hide { display: none; }
.factions { margin-left: auto; display: inline-flex; gap: 6px; flex-shrink: 0; }
.fbtn.danger { color: #ff7b72; border-color: #ff7b7255; }
.fbtn.danger:hover { background: #ff7b7222; }
.primary { background: var(--brand); color: #fff; border-color: var(--brand); }
.fedit { width: 100%; min-height: 60vh; box-sizing: border-box; margin: 0; padding: 12px;
         border: none; resize: vertical; background: var(--bg); color: var(--text);
         font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12.5px;
         white-space: pre-wrap; line-height: 1.5; }
</style>
