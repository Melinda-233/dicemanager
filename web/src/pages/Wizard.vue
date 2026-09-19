<template>
  <div class="wizard">
    <div class="wz-head">
      <h3>新建骰子 — 第 {{ Math.min(step, 5) }}/5 步</h3>
      <ol class="steps">
        <li v-for="s in STEP_NAMES" :key="s" :class="{ done: step > STEP_NAMES.indexOf(s) + 1,
                                                       now: step === STEP_NAMES.indexOf(s) + 1 }">{{ s }}</li>
      </ol>
    </div>

    <p v-if="loading" class="hint">正在加载程序清单…</p>
    <div v-else-if="loadError" class="empty">
      <p>程序清单加载失败：{{ loadError }}</p>
      <button class="primary" @click="load">重试</button>
    </div>
    <p v-else-if="err" class="err">{{ err }}</p>

    <!-- Step1：选程序 + 登录端 -->
    <div v-if="step === 1" class="wz-body">
      <div class="field">
        <label>骰子程序</label>
        <select v-model="dice">
          <option v-for="(m, n) in manifests" :key="n" :value="n">
            {{ n }}（{{ m.arch === 'allinone' ? '整合包' : '独立程序'
            }}{{ m.approx_memory_mb ? ' · 约 ' + m.approx_memory_mb + ' MB' : '' }}）</option>
        </select>
      </div>
      <div class="field" v-if="loginOptions.length">
        <label>登录端（可留空，稍后再关联）</label>
        <select v-model="loginRef">
          <option value="">不关联</option>
          <option v-for="o in loginCandidates" :key="o.id" :value="o.id">
            {{ o.dice }} · {{ o.id }}{{ o.qq ? ' (QQ ' + o.qq + ')' : '' }}</option>
        </select>
        <p class="hint">兼容：{{ loginOptions.join(' / ') }}
          <span v-if="!loginCandidates.length">（当前还没有已创建的登录端实例）</span></p>
      </div>
      <p v-if="manifest.prerequisite" class="hint">前置依赖：{{ manifest.prerequisite }}</p>
      <div class="ops">
        <button class="primary" :disabled="!dice || busy" @click="create">下一步：下载部署</button>
      </div>
    </div>

    <!-- Step2：部署 -->
    <div v-else-if="step === 2" class="wz-body">
      <p v-if="busy">正在下载部署 {{ dice }}，请稍候（首次可能耗时数分钟）…</p>
      <div v-if="conflict" class="dialog">
        <p>同名文件夹已存在：<code>{{ conflictDir }}</code></p>
        <div class="ops">
          <button @click="resolve(true)">直接使用（校验必备文件）</button>
          <button @click="resolve(false)">新建序号文件夹</button>
        </div>
      </div>
    </div>

    <!-- Step3：登录 -->
    <div v-else-if="step === 3" class="wz-body">
      <div v-if="loginType === 'qrcode'" class="qr">
        <img v-if="qr.url" :src="qr.url" alt="二维码"/>
        <img v-if="qr.base64" :src="qr.base64" alt="二维码"/>
        <div class="ops">
          <button @click="sock?.send('refresh')">刷新二维码</button>
          <button class="primary" @click="doLogin">我已完成扫码</button>
        </div>
        <a v-if="verifyUrl" :href="verifyUrl" target="_blank" rel="noreferrer">
          需要滑块验证：请手动完成（只转发不代做）</a>
      </div>
      <div v-else-if="loginType === 'account'" class="field">
        <label>QQ 账号</label><input v-model="cred.qq" placeholder="QQ 账号"/>
        <label>密码</label><input v-model="cred.password" type="password" placeholder="密码"/>
        <label>协议</label>
        <select v-model="cred.protocol">
          <option value="ANDROID_PAD">ANDROID_PAD（推荐）</option>
          <option value="ANDROID_WATCH">ANDROID_WATCH（推荐）</option>
          <option value="ANDROID_PHONE">ANDROID_PHONE</option>
        </select>
        <div class="ops"><button class="primary" :disabled="busy" @click="doLogin">提交登录</button></div>
      </div>
      <div v-else class="field">
        <p class="hint">该程序无需在此登录，点击继续。</p>
        <div class="ops"><button class="primary" :disabled="busy" @click="doLogin">继续</button></div>
      </div>
      <div class="field" v-if="needAuthToken">
        <label>AUTH TOKEN{{ needAuthToken ? '（必填）' : '' }}</label>
        <input v-model="cred.auth_token" placeholder="AUTH TOKEN（LLBot v8.0.9+ 需申请）"/>
      </div>
    </div>

    <!-- Step4：互联配置 -->
    <div v-else-if="step === 4" class="wz-body">
      <pre class="preview" v-if="preview">{{ preview }}</pre>
      <p v-if="manual" class="warn">{{ manual }}</p>
      <div class="ops">
        <button class="primary" :disabled="busy" @click="doStep(4, {})">确认写入互联配置</button>
      </div>
    </div>

    <!-- Step5：启动 -->
    <div v-else-if="step === 5" class="wz-body">
      <p v-if="busy">启动中…</p>
      <p v-else>已下发启动命令，总览图出现新节点即完成。</p>
      <div class="ops">
        <button class="primary" @click="goOverview">完成，回到总览</button>
        <button @click="reset">再建一个</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onUnmounted } from 'vue'
import { connectWS } from '../ws'
import { listManifests, listInstances, createInstance, wizardStep } from '../api'

const STEP_NAMES = ['选程序', '部署', '登录', '互联', '启动']
const STEP_COUNT = STEP_NAMES.length

const step = ref(0), manifests = ref({}), instances = ref([])
const dice = ref(''), loginRef = ref('')
const loading = ref(true), loadError = ref(''), busy = ref(false), err = ref('')
const conflict = ref(false), conflictDir = ref(''), qr = ref({}), verifyUrl = ref('')
const cred = ref({ qq: '', password: '', protocol: 'ANDROID_PAD', auth_token: '' })
const preview = ref(''), manual = ref('')
// sock 必须是 ref：script setup 里 let 变量不会随赋值同步到模板上下文，
// 旧写法下「刷新二维码」按钮拿到的永远是初始的 null
const sock = ref(null)
let instanceId = null

const manifest = computed(() => manifests.value[dice.value] || {})
const loginType = computed(() => manifest.value.login_type || 'none')
const loginOptions = computed(() =>
  (manifest.value.compatible_login || []).filter(o => o !== 'builtin'))
// 登录端候选 = 已创建且程序类型在兼容矩阵里的实例（login_ref 在后端即实例 id，用于画连线）
const loginCandidates = computed(() =>
  instances.value.filter(i => loginOptions.value.includes(i.dice)))
// 默认选中项要在切换程序时同步，否则默认骰子（未触发 change）永远拿不到该标记
const needAuthToken = computed(() => !!manifest.value.auth_token_conditional)

const load = async () => {
  loading.value = true; loadError.value = ''
  try {
    const [m, inst] = await Promise.all([listManifests(), listInstances()])
    manifests.value = m || {}; instances.value = inst || []
    dice.value = Object.keys(manifests.value)[0] || ''
    step.value = dice.value ? 1 : 0           // 原实现从未把 step 推进到 1，页面永远停在 0/5
  } catch (e) {
    loadError.value = e.message || String(e)
  } finally {
    loading.value = false
  }
}
load()

const goOverview = () => (location.hash = '#/overview')

const reset = () => {
  sock.value?.close(); sock.value = null
  step.value = 1; err.value = ''; conflict.value = false
  qr.value = {}; verifyUrl.value = ''; preview.value = ''; manual.value = ''
  cred.value = { qq: '', password: '', protocol: 'ANDROID_PAD', auth_token: '' }
  loginRef.value = ''; instanceId = null
}

const guard = async fn => {                    // 统一转圈 + 报错，避免失败后界面无反馈卡死
  busy.value = true; err.value = ''
  try { await fn() } catch (e) { err.value = e.message || String(e) } finally { busy.value = false }
}

const create = () => guard(async () => {
  const r = await createInstance({ dice: dice.value, arch: manifest.value.arch || 'standalone',
                                   login_ref: loginRef.value || null })
  instanceId = r.id
  step.value = 2
  await doStep(2, {})
})

const doStep = async (n, payload) => {
  const r = await wizardStep(instanceId, n, payload)
  if (r.result === 'error') throw new Error(r.message || '操作失败')   // 如双击启动的竞态提示
  if (r.result === 'conflict') {
    conflict.value = true; conflictDir.value = r.dir || r.message || ''; return
  }
  if (r.manual) manual.value = r.manual
  step.value = n + 1
  if (step.value === 3) {
    if (r.needs_login === false) return doStep(3, {})   // 无需登录的程序直接跳过登录页
    if (loginType.value === 'qrcode') openLoginWS()
  }
  if (step.value === 4) preview.value = r.preview || ''
  if (step.value === 5) await doStep(5, {})
}

const resolve = useExisting => guard(async () => {   // 冲突二选一：重发 step2（后端已实现）
  await wizardStep(instanceId, 2, { use_existing: useExisting })
  conflict.value = false; step.value = 3
  if (loginType.value === 'qrcode') openLoginWS()
})

const openLoginWS = () => {
  sock.value?.close()
  sock.value = connectWS(`/ws/login/${instanceId}`, m => {
    if (m.type === 'qrcode') qr.value = m.payload
    if (m.type === 'verify') verifyUrl.value = m.payload.url
    if (m.type === 'completed' || m.type === 'skipped') { step.value = 4; preview.value = '' }
  })
}

const doLogin = () => guard(async () => {              // Step3：登录（含条件必填校验）
  if (needAuthToken.value && !cred.value.auth_token) throw new Error('LLBot v8.0.9+ 必须填写 AUTH TOKEN')
  await doStep(3, { qq: cred.value.qq, credentials: { ...cred.value } })
})

onUnmounted(() => sock.value?.close())
</script>

<style scoped>
.wz-head { margin-bottom: 14px; }
.steps { display: flex; gap: 8px; flex-wrap: wrap; margin: 0; padding: 0; list-style: none; }
.steps li {
  padding: 4px 12px; border-radius: 999px; font-size: 12px;
  color: var(--muted); background: #eef1f5; border: 1px solid var(--border);
}
.steps li.done { color: var(--ok); border-color: #bfe3cd; background: #eaf7ef; }
.steps li.now { color: #fff; background: var(--brand); border-color: var(--brand); }
.wz-body { display: block; }
.field { max-width: 420px; margin-bottom: 14px; }
.field input, .field select { margin-bottom: 8px; }
.ops { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 4px; }
.err {
  padding: 8px 12px; margin-bottom: 14px; color: var(--danger);
  background: #fdeced; border: 1px solid #f5c2c4; border-radius: 8px;
}
.warn { color: var(--warn); }
.qr img { max-width: 260px; display: block; margin-bottom: 10px; background: #fff; }
.dialog { padding: 14px; margin-bottom: 14px; border: 1px solid var(--danger); border-radius: 8px; }
.dialog code { font-family: ui-monospace, Menlo, Consolas, monospace; }
pre.preview {
  padding: 12px; margin: 0 0 14px; background: #f6f8fa; border: 1px solid var(--border);
  border-radius: 8px; font-family: ui-monospace, Menlo, Consolas, monospace; white-space: pre-wrap;
}
</style>
