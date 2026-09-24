<template>
  <div class="overview">
    <div class="resmon">
      <span>内存 {{ resmon.used_mb ?? '-' }} / {{ resmon.total_mb ?? '-' }} MB</span>
      <div class="bar"><div class="fill" :class="{alert: resmon.alert}"
        :style="{width: ((resmon.ratio||0)*100).toFixed(1) + '%'}"/></div>
    </div>
    <p v-if="!nodes.length && !connected" class="hint">正在连接服务端…</p>
    <p v-else-if="!nodes.length" class="hint">
      还没有骰子实例 —— 点上方「新建骰子」开始，或直接访问 <code>#/wizard</code>。
    </p>
    <svg viewBox="0 0 800 420" class="topo">
      <text x="170" y="24" class="col-title" text-anchor="middle">登录端</text>
      <text x="630" y="24" class="col-title" text-anchor="middle">应用端（骰子）</text>
      <text v-if="!loginNodes.length" x="170" y="215" class="col-empty" text-anchor="middle">（暂无）</text>
      <text v-if="!diceNodes.length" x="630" y="215" class="col-empty" text-anchor="middle">（暂无）</text>
      <line v-for="e in edges" :key="e.src+e.dst"
            :x1="pos(e.src).x" :y1="pos(e.src).y"
            :x2="pos(e.dst).x" :y2="pos(e.dst).y" :class="e.state"/>
      <g v-for="n in nodes" :key="n.id"
         :transform="`translate(${pos(n.id).x},${pos(n.id).y})`" @click="sel = n">
        <rect x="-70" y="-26" width="140" height="52" rx="8"
              :class="{dead: !n.process_alive}"/>
        <text y="-6">{{ n.dice }}{{ n.arch === 'allinone' ? '（整合包）' : '' }}</text>
        <text y="14" class="sub">{{ n.state }} : {{ n.port || '-' }}</text>
        <text v-for="(w, i) in n.warnings" :key="i" y="40" class="warn">{{ w }}</text>
      </g>
    </svg>
    <div v-if="sel" class="panel">
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
        <button class="danger" @click="del">删除</button>
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
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { connectWS } from '../ws'
// 注意：下方已有同名 ref `resmon`（内存水位），此处不再导入 api 的 resmon()，否则重复声明导致构建失败
import { opInstance, delInstance, listManifests, linkInstance, instanceWebui, wizardStep } from '../api'

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
const pos = id => {
  const node = nodes.value.find(n => n.id === id)
  if (!node) return { x: 400, y: 215 }
  const col = isLogin(node) ? loginNodes.value : diceNodes.value
  const j = col.findIndex(n => n.id === id)
  const n = col.length
  const y = n <= 1 ? 215 : 60 + j * (310 / (n - 1))
  return { x: isLogin(node) ? 170 : 630, y }
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
  return e ? ({ 'solid-green': '已连接', 'dashed-gray': '已配置未连接',
                'solid-red': '连接失败' })[e.state] : '—'
})
// 可关联的登录端候选：manifest 的 compatible_login 声明 ∩ 当前存活节点
const linkCandidates = computed(() => {
  if (!sel.value) return []
  const opts = (manifests.value[sel.value.dice]?.compatible_login || [])
    .filter(o => o !== 'builtin')
  return nodes.value.filter(n => opts.includes(n.dice) && n.id !== sel.value.id)
})
watch(sel, () => { webuiInfo.value = null; connMsg.value = ''; linkChoice.value = '' })

const op = o => opInstance(sel.value.id, o)
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
rect.dead { fill: #f3f3f3; opacity: .6; }
text.warn { fill: #d97706; font-size: 10px; }
text.col-title { fill: #888; font-size: 15px; font-weight: 600; }
text.col-empty { fill: #bbb; font-size: 12px; }
.bar { width: 320px; height: 12px; background: #eee; border-radius: 6px; }
.fill { height: 100%; background: #42b883; border-radius: 6px; }
.fill.alert { background: #e5484d; }
button.danger { color: #e5484d; }
.panel { max-width: 560px; }
.sel-info { margin-bottom: 8px; }
.ops { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 6px 0; }
.tok { cursor: pointer; background: #f6f8fa; padding: 2px 6px; border-radius: 4px; }
</style>
