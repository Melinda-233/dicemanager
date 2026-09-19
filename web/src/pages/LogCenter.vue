<template>
  <div class="logcenter">
    <div class="toolbar">
      <select v-model="instanceId" @change="reconnect">
        <option v-for="i in instances" :key="i.id" :value="i.id">{{ i.dice }} / {{ i.id }}</option>
      </select>
      <input v-model="keyword" placeholder="关键字过滤" @change="applyFilter"/>
      <button @click="togglePause">{{ paused ? '继续跟随' : '暂停跟随' }}</button>
      <button @click="copyAll">复制</button>
      <button @click="download">下载</button>
    </div>
    <div class="term" ref="term">
      <div v-for="l in lines" :key="l.seq" :class="{err: l.error}">{{ l.text }}</div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import { connectWS } from '../ws'
import { listInstances, downloadLog } from '../api'

const instances = ref([]), instanceId = ref(''), lines = ref([])
const keyword = ref(''), paused = ref(false), term = ref(null)
let sock
const render = m => {
  if (m.type !== 'line') return
  lines.value.push(m)
  if (lines.value.length > 2000) lines.value.shift()
  if (!paused.value) nextTick(() =>
    term.value && (term.value.scrollTop = term.value.scrollHeight))
}
const reconnect = () => {
  sock?.close(); lines.value = []
  if (!instanceId.value) return
  sock = connectWS(`/ws/logs/${instanceId.value}`, render)
}
const togglePause = () => { paused.value = !paused.value
  sock?.send(paused.value ? 'pause' : 'resume') }
const applyFilter = () => sock?.send(`filter:${keyword.value}`)
const copyAll = () => navigator.clipboard.writeText(lines.value.map(l => l.text).join('\n'))
const download = () => instanceId.value && downloadLog(instanceId.value).catch(() => {})

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
</style>