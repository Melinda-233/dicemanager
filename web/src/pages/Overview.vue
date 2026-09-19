<template>
  <div class="overview">
    <div class="resmon">
      <span>内存 {{ resmon.used_mb }} / {{ resmon.total_mb }} MB</span>
      <div class="bar"><div class="fill" :class="{alert: resmon.alert}"
        :style="{width: ((resmon.ratio||0)*100).toFixed(1) + '%'}"/></div>
    </div>
    <svg viewBox="0 0 800 400" class="topo">
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
    <div v-if="sel" class="ops">
      <button @click="op('start')">启动</button>
      <button @click="op('stop')">停止</button>
      <button @click="op('restart')">重启</button>
      <button @click="location.hash = `#/logs?instance=${sel.id}`">查看日志</button>
      <button class="danger" @click="del">删除</button>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { connectWS } from '../ws'
// 注意：下方已有同名 ref `resmon`（内存水位），此处不再导入 api 的 resmon()，否则重复声明导致构建失败
import { opInstance, delInstance } from '../api'

const nodes = ref([]), edges = ref([]), sel = ref(null), resmon = ref({})
let sock
const pos = id => {
  const i = nodes.value.findIndex(n => n.id === id)
  if (i < 0) return { x: 400, y: 200 }
  const a = (i / nodes.value.length) * 2 * Math.PI - Math.PI / 2   // 首节点从正上方起
  return { x: 400 + 280 * Math.cos(a), y: 200 + 130 * Math.sin(a) }
}
onMounted(() => {
  sock = connectWS('/ws/overview', m => {
    if (m.type !== 'overview') return
    nodes.value = m.payload.nodes; edges.value = m.payload.edges
    resmon.value = m.payload.resmon
  })
})
onUnmounted(() => sock?.close())

const op = o => opInstance(sel.value.id, o)
const del = () => {
  if (!confirm(`确认删除 ${sel.value.dice} 实例 ${sel.value.id}？`)) return
  const removeDir = confirm('同时删除程序文件夹？\n（Shiki 建议保留 Dice 存档目录）')
  delInstance(sel.value.id, true, removeDir,
              removeDir && sel.value.dice === 'shiki')      // 保留存档目录
    .then(() => (sel.value = null))
}
</script>

<style scoped>
line.solid-green { stroke: #42b883; stroke-width: 3; }
line.dashed-gray { stroke: #bbb; stroke-dasharray: 6 4; }
line.solid-red   { stroke: #e5484d; stroke-width: 3; }
rect.dead { fill: #f3f3f3; opacity: .6; }
text.warn { fill: #d97706; font-size: 10px; }
.bar { width: 320px; height: 12px; background: #eee; border-radius: 6px; }
.fill { height: 100%; background: #42b883; border-radius: 6px; }
.fill.alert { background: #e5484d; }
button.danger { color: #e5484d; }
</style>