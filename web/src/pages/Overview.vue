<template>
  <div class="overview">
    <div class="resmon">
      <span>内存 {{ resmon.used_mb ?? '-' }} / {{ resmon.total_mb ?? '-' }} MB</span>
      <div class="bar"><div class="fill" :class="{alert: resmon.alert}"
        :style="{width: ((resmon.ratio||0)*100).toFixed(1) + '%'}"/></div>
      <button @click="scan">扫描安装目录</button>
    </div>
    <div v-if="scanRes" class="scanbox">
      <p class="hint">安装根：{{ scanRes.roots.join('、') }}
        （owned = 实例受管；orphan = 匹配程序名但无实例记录，可清理；
          external = 无关目录，仅展示）</p>
      <div v-for="d in scanRes.dirs" :key="d.path" class="scan-row">
        <span v-if="d.kind === 'owned'" class="kind-owned">✓ {{ d.path }}
          — {{ d.dice }} · {{ d.id }}（{{ d.state }}）</span>
        <template v-else-if="d.kind === 'orphan'">
          <span class="kind-orphan">⚠ {{ d.path }} — 游离目录（更新于 {{ d.mtime }}）</span>
          <button class="danger" @click="delOrphan(d.path)">删除</button>
        </template>
        <span v-else class="kind-ext">{{ d.path }} — 无关目录（{{ d.mtime }}）</span>
      </div>
      <div v-for="p in scanRes.procs" :key="p.pid" class="scan-row">
        <span :class="p.owner ? 'kind-owned' : (p.killable ? 'kind-orphan' : 'kind-ext')">PID {{ p.pid }}
          {{ p.cmd || p.exe }}{{ p.owner ? ' — 实例 ' + p.owner : (p.killable ? ' — 游离进程' : ' — 无关进程') }}</span>
        <button v-if="!p.owner && p.killable" class="danger" @click="killProc(p.pid)">结束</button>
      </div>
      <p v-if="!scanRes.dirs.length && !scanRes.procs.length" class="hint">
        安装根下没有目录与相关进程。</p>
    </div>
    <p v-if="!nodes.length && !connected" class="hint">正在连接服务端…</p>
    <p v-else-if="!nodes.length" class="hint">
      还没有骰子实例 —— 点上方「新建骰子」开始，或直接访问 <code>#/wizard</code>。
    </p>
    <div class="layout">
      <div class="topo-wrap">
    <svg :viewBox="'0 0 ' + vbW + ' 420'" class="topo">
      <text :x="colX.login" y="24" class="col-title" text-anchor="middle">登录端</text>
      <text :x="colX.dice" y="24" class="col-title" text-anchor="middle">应用端（骰子）</text>
      <text v-if="!loginNodes.length" :x="colX.login" y="215" class="col-empty" text-anchor="middle">（暂无）</text>
      <text v-if="!diceNodes.length" :x="colX.dice" y="215" class="col-empty" text-anchor="middle">（暂无）</text>
      <line v-for="e in edges" :key="e.src+e.dst"
            :x1="pos(e.src).x" :y1="pos(e.src).y"
            :x2="pos(e.dst).x" :y2="pos(e.dst).y" :class="e.state"/>
      <!-- 连线中点标注状态，颜色随线；白描边保证压在线上也可读 -->
      <text v-for="e in edges" :key="'t'+e.src+e.dst"
            :x="(pos(e.src).x + pos(e.dst).x) / 2"
            :y="(pos(e.src).y + pos(e.dst).y) / 2 - 7"
            :class="['edge-label', e.state]" text-anchor="middle">{{
        stateText(e.state) }}</text>
      <g v-for="n in nodes" :key="n.id"
         :transform="`translate(${pos(n.id).x},${pos(n.id).y})`" @click="sel = n">
        <rect x="-70" y="-26" width="140" height="52" rx="8"
              :class="{dead: !n.process_alive}"/>
        <text y="-6">{{ n.dice }}{{ n.arch === 'allinone' ? '（整合包）' : '' }}</text>
        <text y="14" class="sub">{{ n.state }} : {{ n.port || '-' }}</text>
        <text v-for="(w, i) in n.warnings" :key="i" y="40" class="warn">{{ w }}</text>
      </g>
    </svg>
    <div class="legend">
      <span><i class="lg-line lg-green"/>已连接</span>
      <span><i class="lg-line lg-gray"/>已配置未连接</span>
      <span><i class="lg-line lg-red"/>连接失败</span>
    </div>
      </div>
      <aside v-if="sel" class="panel">
        <div class="panel-top">
          <b>实例操作</b>
          <button class="x" title="关闭" @click="sel = null">×</button>
        </div>
        <div class="sel-info">
        <b>{{ sel.dice }}</b> · {{ sel.id }} · {{ live.state }} · 端口 {{ live.port || '-' }}
        <span v-if="live.qq"> · QQ {{ live.qq }}</span>
        <span v-if="linkTarget"> · 已连 {{ linkTarget.dice }}（{{ edgeState }}）</span>
      </div>
      <div class="ops">
        <button @click="op('start')">启动</button>
        <button @click="op('stop')">停止</button>
        <button @click="op('restart')">重启</button>
        <button @click="openWebui">打开 WebUI</button>
        <button @click="goLogs">查看日志</button>
        <button @click="pickBackup">上传备份</button>
        <button class="danger" @click="del">删除</button>
        <input ref="backupInput" type="file" hidden
               accept=".zip,.tgz,.tar,.tar.gz,.tar.xz,.tar.bz2" @change="doBackup"/>
      </div>
      <p v-if="webuiInfo" class="hint">
        WebUI 登录令牌：<code class="tok" title="点击复制" @click="copyToken">{{
          webuiInfo.token || '日志中未发现令牌，请进 WebUI 查看' }}</code>（点击复制）</p>
      <div class="ops">
        <template v-if="linkTarget">
          <button @click="reconn">重写互联配置</button>
          <button class="danger" @click="unlink">解除连接</button>
        </template>
        <template v-else-if="linkCandidates.length">
          <select v-model="linkChoice">
            <option value="" disabled>选择登录端并关联…</option>
            <option v-for="c in linkCandidates" :key="c.id" :value="c.id">
              {{ c.dice }} · {{ c.id }}{{ c.qq ? ' (QQ ' + c.qq + ')' : '' }}</option>
          </select>
          <button :disabled="!linkChoice" @click="link">关联登录端</button>
        </template>
        <span v-else class="hint">该程序没有可关联的登录端（未声明兼容矩阵或暂无候选实例）。</span>
      </div>
      <p v-if="connMsg" class="hint">{{ connMsg }}</p>
      </aside>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { connectWS } from '../ws'
// 注意：下方已有同名 ref `resmon`（内存水位），此处不再导入 api 的 resmon()，否则重复声明导致构建失败
import { opInstance, delInstance, listManifests, linkInstance, instanceWebui, wizardStep,
         scanInstallRoots, deleteOrphanDir, killOrphanProc, uploadBackup } from '../api'

const nodes = ref([]), edges = ref([]), sel = ref(null), resmon = ref({})
const manifests = ref({})               // 程序清单：删除/关联等行为由 manifest 声明驱动
const connected = ref(false)             // WS 是否已连上：用于区分「连接中」与「真的没有实例」
const webuiInfo = ref(null), connMsg = ref(''), linkChoice = ref('')
let sock
listManifests().then(m => (manifests.value = m)).catch(() => {})   // 拉不到不阻塞总览
// ---------- 两栏布局：登录端在左、应用端在右 ----------
// 登录端程序 = 在任意骰子端 manifest 的 compatible_login 里出现过的程序
// （纯清单驱动，与 Wizard 配对模式同口径，不写程序名分支）
const loginSet = computed(() => {
  const s = new Set()
  for (const m of Object.values(manifests.value))
    (m.compatible_login || []).forEach(o => { if (o !== 'builtin') s.add(o) })
  return s
})
const isLogin = n => loginSet.value.has(n.dice)
const loginNodes = computed(() => nodes.value.filter(isLogin))
const diceNodes = computed(() => nodes.value.filter(n => !isLogin(n)))
// 侧栏打开时收窄画布（列距拉近、viewBox 变窄），节点/文字保持原大——
// 而不是让 SVG 随容器等比缩小（那样节点和字都会变小）
const compact = computed(() => !!sel.value)
const vbW = computed(() => compact.value ? 560 : 800)
const colX = computed(() => compact.value ? { login: 120, dice: 440 } : { login: 170, dice: 630 })
const pos = id => {
  const node = nodes.value.find(n => n.id === id)
  if (!node) return { x: (colX.value.login + colX.value.dice) / 2, y: 215 }
  const col = isLogin(node) ? loginNodes.value : diceNodes.value
  const j = col.findIndex(n => n.id === id)
  const n = col.length
  const y = n <= 1 ? 215 : 60 + j * (310 / (n - 1))
  return { x: isLogin(node) ? colX.value.login : colX.value.dice, y }
}
onMounted(() => {
  sock = connectWS('/ws/overview', m => {
    if (m.type !== 'overview') return
    nodes.value = m.payload.nodes; edges.value = m.payload.edges
    resmon.value = m.payload.resmon
  }, () => (connected.value = true))
})
onUnmounted(() => sock?.close())

// WS 每 2s 全量替换 nodes，sel 里存的是点击时刻的快照（login_ref/qq 会过期）——
// 展示一律读 selLive（按 id 回当前节点），操作用 sel.id（稳定）
const selLive = computed(() => nodes.value.find(n => n.id === sel.value?.id) || sel.value)
const live = computed(() => selLive.value || {})
const linkTarget = computed(() =>
  live.value.login_ref ? nodes.value.find(n => n.id === live.value.login_ref) : null)
const edgeState = computed(() => {
  const e = edges.value.find(e => e.src === sel.value?.id || e.dst === sel.value?.id)
  return e ? stateText(e.state) : '—'
})
// 连线状态 → 中文文案（edge.state 的取值即样式类名，与 ws_overview 保持一致）
const stateText = s => ({ 'solid-green': '已连接', 'dashed-gray': '已配置未连接',
                          'solid-red': '连接失败' })[s] || s
// 可关联的登录端候选：manifest 的 compatible_login 声明 ∩ 当前存活节点
const linkCandidates = computed(() => {
  if (!sel.value) return []
  const opts = (manifests.value[sel.value.dice]?.compatible_login || [])
    .filter(o => o !== 'builtin')
  return nodes.value.filter(n => opts.includes(n.dice) && n.id !== sel.value.id)
})
watch(sel, () => { webuiInfo.value = null; connMsg.value = ''; linkChoice.value = '' })

// 启停操作：后端在启动/重启时会顺带开放 WebUI 端口（绑定修正 + ufw），有提示就展示
const op = o => guard(async () => {
  const r = await opInstance(sel.value.id, o)
  connMsg.value = r?.webui_note || ''
})
// 模板里不能直接用 location（会编译成 _ctx.location），一律包成方法
const goLogs = () => { location.hash = `#/logs?instance=${sel.value.id}` }
const del = () => {
  // 是否建议保留存档目录由 manifest 声明（delete_keeps_save），不再按程序名硬编码
  const keeps = !!manifests.value[sel.value.dice]?.delete_keeps_save
  if (!confirm(`确认删除 ${sel.value.dice} 实例 ${sel.value.id}？`)) return
  const removeDir = confirm(keeps
    ? '同时删除程序文件夹？\n（该程序建议保留存档目录）'
    : '同时删除程序文件夹？')
  delInstance(sel.value.id, true, removeDir, removeDir && keeps)
    .then(() => (sel.value = null))
}

const guard = async fn => {              // 面板操作统一报错出口，失败不静默
  try { await fn() } catch (e) { connMsg.value = e.message || String(e) }
}

// ---------- 上传备份：停机 → 覆盖导入 → 自动重启（后端 /instances/{id}/backup） ----------
const backupInput = ref(null)
const pickBackup = () => backupInput.value?.click()
const doBackup = e => guard(async () => {
  const f = e.target.files?.[0]
  e.target.value = ''                    // 清空以便可重复选择同一文件
  if (!f || !sel.value) return
  if (!confirm(`把备份导入到 ${sel.value.dice} 实例 ${sel.value.id}？\n\n`
    + '· 运行中的实例会先停止，完成后自动重启\n'
    + '· 包内文件覆盖实例目录中的同名文件（包外文件不受影响）\n'
    + '· 支持 zip / tar.gz / tar.xz / tar.bz2 / tar，上限 2GB')) return
  connMsg.value = '上传中 0%'
  const r = await uploadBackup(sel.value.id, f, p => {
    connMsg.value = p.sent ? '上传完成，服务端正在解压恢复…'
      : `上传中 ${p.total ? Math.round(p.loaded / p.total * 100) : 0}%`
  })
  connMsg.value = `备份导入完成：${r.format} 格式，恢复 ${r.files} 个文件`
    + (r.restart_error ? `；⚠ 自动重启失败：${r.restart_error}（请手动点「启动」）`
       : r.restarted ? '，实例已重启' : '；实例原本未运行，保持停止')
})

// ---------- 安装根扫描：游离目录 / 游离进程 ----------
const scanRes = ref(null)
const scan = () => guard(async () => {
  connMsg.value = ''
  scanRes.value = await scanInstallRoots()
})
const delOrphan = path => guard(async () => {
  if (!confirm(`⚠ 确认删除游离目录 ${path}？\n此操作不可恢复，请确认其中没有需要保留的数据！`)) return
  await deleteOrphanDir(path)
  connMsg.value = `已删除 ${path}`
  scanRes.value = await scanInstallRoots()
})
const killProc = pid => guard(async () => {
  if (!confirm(`确认结束游离进程 ${pid}？`)) return
  await killOrphanProc(pid)
  connMsg.value = `已结束进程 ${pid}`
  scanRes.value = await scanInstallRoots()
})
// WebUI：端口由后端回读（实际端口优先），URL 用面板同主机名拼接（服务与面板同机）
const openWebui = () => guard(async () => {
  const w = await instanceWebui(sel.value.id)
  if (!w.port) { connMsg.value = '未发现 WebUI 端口：实例未启动，或该程序没有 WebUI。'; return }
  webuiInfo.value = w
  window.open(`http://${location.hostname}:${w.port}`, '_blank', 'noopener')
})
const copyToken = () => {
  if (webuiInfo.value?.token) navigator.clipboard?.writeText(webuiInfo.value.token)
      .catch(() => {})
  connMsg.value = webuiInfo.value?.token ? '令牌已复制到剪贴板' : ''
}
// 连接管理：关联/解除只改 login_ref（拓扑连线随之变化）；重写互联配置复用向导第 4 步，
// 地址与令牌留空 → 后端自动继承登录端的 ob11 端口与 token，保证两端一致
const link = () => guard(async () => {
  await linkInstance(sel.value.id, linkChoice.value)
  connMsg.value = '已关联。建议点「重写互联配置」自动对齐两端的地址与令牌。'
})
const unlink = () => guard(async () => {
  if (!confirm('解除与登录端的连接？（不删除任何实例）')) return
  await linkInstance(sel.value.id, null)
  connMsg.value = '已解除关联。'
})
const reconn = () => guard(async () => {
  const r = await wizardStep(sel.value.id, 4, {})
  connMsg.value = r.result === 'ok' ? `互联配置已重写：${r.preview || ''}` : (r.message || '重写失败')
})
</script>

<style scoped>
line.solid-green { stroke: #42b883; stroke-width: 3; }
line.dashed-gray { stroke: #bbb; stroke-dasharray: 6 4; }
line.solid-red   { stroke: #e5484d; stroke-width: 3; }
/* 连线中点状态标注：填充色与对应线一致，白描边（paint-order 让描边垫底）保证可读 */
text.edge-label { font-size: 11px; paint-order: stroke; stroke: var(--bg, #fff); stroke-width: 4px; }
text.edge-label.solid-green { fill: #2f9d6f; }
text.edge-label.dashed-gray { fill: #999; }
text.edge-label.solid-red   { fill: #e5484d; }
.legend { display: flex; gap: 18px; font-size: 12px; color: #666; margin: 2px 0 6px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.lg-line { display: inline-block; width: 22px; height: 0; border-top: 3px solid; }
.lg-green { border-color: #42b883; }
.lg-gray  { border-color: #bbb; border-top-style: dashed; }
.lg-red   { border-color: #e5484d; }
rect.dead { fill: #f3f3f3; opacity: .6; }
text.warn { fill: #d97706; font-size: 10px; }
text.col-title { fill: #888; font-size: 15px; font-weight: 600; }
text.col-empty { fill: #bbb; font-size: 12px; }
.bar { width: 320px; height: 12px; background: #eee; border-radius: 6px; }
.fill { height: 100%; background: #42b883; border-radius: 6px; }
.fill.alert { background: #e5484d; }
.scanbox {
  margin: 10px 0; padding: 10px 14px; max-width: 720px;
  border: 1px solid var(--border); border-radius: 8px; background: #fafbfc;
}
.scan-row {
  display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
  padding: 3px 0; font-size: 13px; font-family: ui-monospace, Menlo, Consolas, monospace;
}
.kind-owned { color: var(--ok); }
.kind-orphan { color: #d97706; }
.kind-ext { color: #999; }
button.danger { color: #e5484d; }
/* 点击实例后：拓扑居左、操作面板固定宽度靠右成侧栏 */
.layout { display: flex; gap: 16px; align-items: flex-start; }
.topo-wrap { flex: 1; min-width: 0; }
.panel {
  width: 340px; flex-shrink: 0;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: var(--radius); box-shadow: var(--shadow);
  padding: 14px 16px;
}
.panel-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.panel-top .x {
  border: none; background: none; cursor: pointer;
  font-size: 18px; line-height: 1; color: var(--muted); padding: 2px 6px;
}
.panel-top .x:hover { color: var(--text); }
@media (max-width: 900px) {           /* 窄屏回退为上下堆叠 */
  .layout { flex-direction: column; }
  .panel { width: auto; }
}
.sel-info { margin-bottom: 8px; }
.ops { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 6px 0; }
.tok { cursor: pointer; background: #f6f8fa; padding: 2px 6px; border-radius: 4px; }
</style>
