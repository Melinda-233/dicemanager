<template>
  <div class="wizard">
    <h3>新建骰子 — 第 {{ step }}/5 步</h3>
    <div v-if="step === 1">
      <select v-model="dice" @change="needAuthToken = !!manifests[dice]?.auth_token_conditional">
        <option v-for="(m, n) in manifests" :key="n" :value="n">
          {{ n }}（{{ m.arch === 'allinone' ? '整合包' : '独立程序' }}）</option>
      </select>
      <select v-if="loginOptions.length" v-model="loginRef">
        <option v-for="o in loginOptions" :key="o" :value="o">登录端：{{ o }}</option>
      </select>
      <button :disabled="!dice" @click="create">下一步</button>
    </div>
    <div v-if="step === 2">
      <p>正在下载部署…</p>
      <div v-if="conflict" class="dialog">
        <p>同名文件夹已存在：{{ conflictDir }}</p>
        <button @click="resolve(true)">直接使用（校验必备文件）</button>
        <button @click="resolve(false)">新建序号文件夹</button>
      </div>
    </div>
    <div v-if="step === 3">
      <div v-if="loginType === 'qrcode'">
        <img v-if="qr.url" :src="qr.url" alt="二维码"/>
        <img v-if="qr.base64" :src="qr.base64" alt="二维码"/>
        <button @click="sock?.send('refresh')">刷新二维码</button>
        <a v-if="verifyUrl" :href="verifyUrl" target="_blank">
          需要滑块验证：请手动完成（只转发不代做）</a>
      </div>
      <div v-if="loginType === 'account'">
        <input v-model="cred.qq" placeholder="QQ 账号"/>
        <input v-model="cred.password" type="password" placeholder="密码"/>
        <select v-model="cred.protocol">
          <option value="ANDROID_PAD">ANDROID_PAD（推荐）</option>
          <option value="ANDROID_WATCH">ANDROID_WATCH（推荐）</option>
          <option value="ANDROID_PHONE">ANDROID_PHONE</option>
        </select>
      </div>
      <input v-if="needAuthToken" v-model="cred.auth_token"
             placeholder="AUTH TOKEN（LLBot v8.0.9+ 需申请）"/>
      <button @click="doLogin">提交登录</button>
    </div>
    <div v-if="step === 4">
      <pre class="preview" v-if="preview">{{ preview }}</pre>
      <p v-if="manual" class="warn">{{ manual }}</p>
      <button @click="doStep(4, {})">确认写入互联配置</button>
    </div>
    <div v-if="step === 5">
      <p>启动中… 总览图出现新节点即完成</p>
      <button @click="location.hash = '#/overview'">完成</button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { connectWS } from '../ws'
import { api, listManifests, createInstance, wizardStep } from '../api'

const step = ref(0), manifests = ref({}), dice = ref(''), loginRef = ref('')
const conflict = ref(false), conflictDir = ref(''), qr = ref({}), verifyUrl = ref('')
const cred = ref({}), preview = ref(''), manual = ref(''), needAuthToken = ref(false)
let instanceId = null, sock = null

const manifest = computed(() => manifests.value[dice.value] || {})
const loginType = computed(() => manifest.value.login_type || 'none')
const loginOptions = computed(() =>
  (manifest.value.compatible_login || []).filter(o => o !== 'builtin'))

listManifests().then(m => { manifests.value = m; dice.value = Object.keys(m)[0] })

const create = async () => {                    // Step1：建档 + 端口分配
  const r = await createInstance({ dice: dice.value, arch: manifest.value.arch,
                                   login_ref: loginRef.value })
  instanceId = r.id                             // 原实现把整个响应对象当 id 传，全部 404
  step.value = 2; doStep(2, {})
}
const doStep = async (n, payload) => {
  const r = await wizardStep(instanceId, n, payload)
  if (r.result === 'conflict') {
    conflict.value = true; conflictDir.value = r.dir || r.message || ''; return }
  if (r.manual) manual.value = r.manual
  step.value = n + 1
  if (step.value === 3) {
    if (r.needs_login === false) return doStep(3, {})   // 无需登录的程序直接跳过登录页
    if (loginType.value === 'qrcode') openLoginWS()
  }
  if (step.value === 4) preview.value = r.preview || ''
  if (step.value === 5) doStep(5, {})
}
const resolve = useExisting =>                  // 冲突二选一：重发 step2（后端已实现）
  wizardStep(instanceId, 2, { use_existing: useExisting })
    .then(() => { conflict.value = false; step.value = 3
                  if (loginType.value === 'qrcode') openLoginWS() })
const openLoginWS = () => {
  sock = connectWS(`/ws/login/${instanceId}`, m => {
    if (m.type === 'qrcode') qr.value = m.payload
    if (m.type === 'verify') verifyUrl.value = m.payload.url
    if (m.type === 'completed') step.value = 4
    if (m.type === 'skipped') step.value = 4
  })
}
const doLogin = () => {                          // Step3：登录（含条件必填校验）
  if (needAuthToken.value && !cred.value.auth_token)
    return alert('LLBot v8.0.9+ 必须填写 AUTH TOKEN')
  doStep(3, { qq: cred.value.qq, credentials: cred.value })
}
</script>

<style scoped>
.dialog { border: 1px solid #e5484d; padding: 12px; border-radius: 8px; }
.warn { color: #d97706; }
pre.preview { background: #f6f6f6; padding: 10px; border-radius: 6px; }
</style>
