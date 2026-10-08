<template>
  <div class="logcenter">
    <div class="toolbar">
      <!-- 实例选择：加状态点与分组（运行中/已停止），裸列表在实例一多就分不清
           哪个在跑。另外空态要显式说明，不能留一个空白下拉框让用户干瞪眼。 -->
      <select v-model="instanceId" @change="onPickInstance" class="pick">
        <option value="" disabled>选择实例以查看日志…</option>
        <optgroup v-if="running.length" label="运行中">
          <option v-for="i in running" :key="i.id" :value="i.id">
            ● {{ i.dice }} · {{ shortId(i.id) }}
          </option>
        </optgroup>
        <optgroup v-if="stopped.length" label="已停止">
          <option v-for="i in stopped" :key="i.id" :value="i.id">
            ○ {{ i.dice }} · {{ shortId(i.id) }}
          </option>
        </optgroup>
      </select>
      <span v-if="!instances.length" class="hint">还没有实例</span>
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
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { connectWS } from '../ws'
import { listInstances, downloadLog, searchLogs } from '../api'

const instances = ref([]), instanceId = ref(''), lines = ref([])
const keyword = ref(''), paused = ref(false), term = ref(null)
const gq = ref(''), gResults = ref(null), searching = ref(false)
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
.pick { min-width: 260px; max-width: 380px; }
.gres { margin: 8px 0; padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px; }
.gres .x { padding: 1px 8px; font-size: 12px; float: right; }
.g-row { display: flex; gap: 8px; align-items: baseline; margin: 2px 0; font-size: 12.5px; }
.g-inst { flex-shrink: 0; color: var(--brand); font-weight: 600; min-width: 72px; }
.g-line { flex: 1; font-family: ui-monospace, Menlo, Consolas, monospace;
          white-space: pre-wrap; word-break: break-all; }
.g-row button { padding: 0 8px; font-size: 12px; flex-shrink: 0; }
</style>