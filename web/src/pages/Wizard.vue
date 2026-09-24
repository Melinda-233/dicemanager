<template>
  <div class="wizard">
    <div class="wz-head">
      <h3>{{ wizTitle }} — 第 {{ Math.min(step, stepNames.length) }}/{{ stepNames.length }} 步</h3>
      <ol class="steps">
        <li v-for="s in stepNames" :key="s" :class="{ done: step > stepNames.indexOf(s) + 1,
                                                      now: step === stepNames.indexOf(s) + 1 }">{{ s }}</li>
      </ol>
    </div>

    <!-- 断点续跑入口：管理器重启/断网留下的中间态实例 -->
    <div v-if="pending.length && step === 1 && !loading" class="pending">
      <p class="hint">有 {{ pending.length }} 个未完成的实例，可从中断处继续：</p>
      <div v-for="p in pending" :key="p.id" class="pending-item">
        <span>{{ p.dice }} · {{ p.id }}{{ p.qq ? ' (QQ ' + p.qq + ')' : '' }}
          — 下一步：{{ STEP_NAMES[Math.min(p.next_step, 5) - 1] }}</span>
        <button class="primary" :disabled="busy" @click="resume(p)">继续</button>
        <button class="danger" :disabled="busy" @click="stopPending(p)">停止并删除</button>
      </div>
    </div>

    <p v-if="loading" class="hint">正在加载程序清单…</p>
    <div v-else-if="loadError" class="empty">
      <p>程序清单加载失败：{{ loadError }}</p>
      <button class="primary" @click="load">重试</button>
    </div>
    <p v-else-if="err" class="err">{{ err }}</p>

    <!-- Step1：选程序 + 登录端（支持「先选登录端」的配对模式） -->
    <div v-if="step === 1" class="wz-body">
      <div class="field">
        <label>部署模式</label>
        <select v-model="mode" :disabled="!!pair">
          <option value="dice">先选骰子端（默认）</option>
          <option value="pair">先选登录端 · 配对（登录端 → 兼容骰子端 → 互联）</option>
        </select>
        <p class="hint" v-if="mode === 'pair' && !pair">
          先创建并登录一个登录端（自动写入它的互联配置并拉起），随后只列出与它兼容的骰子端，
          创建时自动关联并继承登录端的地址与令牌。</p>
        <p class="hint" v-else-if="pair && pair.phase === 'dice'">
          登录端 {{ pair.loginId }} 已就绪，请从兼容列表中选择骰子端。</p>
      </div>

      <!-- 配对 · 阶段一：选登录端（新建或复用已有实例） -->
      <div class="field" v-if="mode === 'pair' && (!pair || pair.phase === 'login')">
        <label>登录端程序</label>
        <select v-model="loginChoice" @change="onLoginChoice">
          <option value="" disabled>请选择</option>
          <optgroup label="新建登录端">
            <option v-for="n in loginPrograms" :key="n" :value="'new:' + n">{{ n }}</option>
          </optgroup>
          <optgroup label="已有登录端实例" v-if="existingLogins.length">
            <option v-for="i in existingLogins" :key="i.id" :value="'inst:' + i.id">
              {{ i.dice }} · {{ i.id }}{{ i.qq ? ' (QQ ' + i.qq + ')' : '' }}</option>
          </optgroup>
        </select>
        <p class="hint">选「新建」会先部署并登录该登录端；选「已有实例」直接进入骰子端选择。</p>
      </div>

      <!-- 配对 · 阶段二：选骰子端（只列兼容项） -->
      <div class="field" v-if="mode === 'pair' && pair && pair.phase === 'dice'">
        <label>骰子端程序（已按兼容性过滤）</label>
        <select v-model="dice">
          <option value="" disabled>请选择</option>
          <option v-for="n in diceCandidates" :key="n" :value="n">{{ n }}</option>
        </select>
        <p class="hint" v-if="!diceCandidates.length">
          没有与 {{ pair.loginProg }} 兼容的骰子端程序。</p>
      </div>

      <!-- 默认模式：先选骰子端，再可选关联登录端 -->
      <template v-if="mode === 'dice'">
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
      </template>

      <!-- LLBot v8 的 AUTH TOKEN 提前到第一步：选完程序立刻填，扫码页免操作直接出码 -->
      <div class="field" v-if="needAuthToken">
        <label>AUTH TOKEN（LLBot v8.0.9+ 必填）</label>
        <input v-model="cred.auth_token" placeholder="AUTH TOKEN"/>
        <p class="hint">到 https://auth.luckylillia.com 申请获取；进入扫码页时自动保存生效。</p>
      </div>
      <div class="field" v-if="dice">
        <label>离线程序包（可选）</label>
        <div class="pkg" v-if="pkgInfo">
          <span class="pkg-ok">✓ 本地已有：{{ pkgInfo.size_mb }} MB ·
            {{ pkgInfo.source === 'upload' ? '上传' : '下载缓存' }}于 {{ pkgInfo.updated_at }}</span>
          <button :disabled="pkgBusy" @click="removePkg">删除</button>
        </div>
        <p v-else class="hint">未上传：部署时将从 GitHub 在线下载（国内可能很慢）。</p>
        <p class="hint">上传 zip / tar.gz / tar.xz 等压缩包后，部署将直接解压本地包，不再联网下载；也可用同样的包供多实例复用。</p>
        <input type="file" accept=".zip,.gz,.tgz,.xz,.bz2,.tar" :disabled="pkgBusy" @change="uploadPkg"/>
        <div v-if="pkgUp" class="pkgup">
          <div class="pkgup-bar" :class="{ 'is-indet': !pkgUp.total && !pkgUp.sent, 'is-sent': pkgUp.sent }">
            <i :style="{ width: pkgPercent + '%' }"></i>
          </div>
          <p class="hint">{{ pkgText }}</p>
        </div>
        <p v-if="pkgMsg" class="hint">{{ pkgMsg }}</p>
      </div>
      <p v-if="manifest.prerequisite" class="hint">前置依赖：{{ manifest.prerequisite }}</p>
      <div class="ops">
        <button class="primary" :disabled="!canCreate || busy" @click="create">
          下一步：{{ createLabel }}</button>
      </div>
    </div>

    <!-- Step2：部署 -->
    <div v-else-if="step === 2" class="wz-body">
      <p v-if="busy">{{ pkgInfo ? '正在解压本地程序包部署 ' + dice + '，请稍候…'
                          : '正在下载部署 ' + dice + '，请稍候（首次可能耗时数分钟）…' }}</p>
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
          <button class="primary" @click="doLogin">
            {{ needAuthToken && !tokenSaved ? '保存 TOKEN 并获取二维码' : '我已完成扫码' }}</button>
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
      <div v-else-if="loginType === 'webui'" class="field">
        <p class="hint">该程序需在其自带 WebUI 完成登录，dicemanager 不代劳：</p>
        <pre class="preview" v-if="manual">{{ manual }}</pre>
        <div class="ops"><button class="primary" :disabled="busy" @click="doLogin">我已了解，继续</button></div>
      </div>
      <div v-else class="field">
        <p class="hint">该程序无需在此登录，点击继续。</p>
        <div class="ops"><button class="primary" :disabled="busy" @click="doLogin">继续</button></div>
      </div>
      <div class="field" v-if="needAuthToken && !tokenSaved">
        <label>AUTH TOKEN（必填）</label>
        <input v-model="cred.auth_token" placeholder="AUTH TOKEN（LLBot v8.0.9+ 需申请）"/>
        <p class="hint">到 https://auth.luckylillia.com 申请获取。第一步已填则自动保存，此处仅作补填兜底。</p>
      </div>
      <p class="hint" v-if="needAuthToken && tokenSaved">TOKEN 已保存，二维码生成中；扫码后点击「我已完成扫码」继续。</p>
    </div>

    <!-- Step4：互联配置 -->
    <div v-else-if="step === 4" class="wz-body">
      <div class="field">
        <label>连接方向</label>
        <select v-model="conn.direction">
          <option value="forward">正向 WS：本程序监听端口，等待对端连接</option>
          <option value="reverse">反向 WS：本程序主动连接对端</option>
        </select>
        <p class="hint">不确定就保持默认：两端一个监听、一个连接即可。</p>
      </div>
      <div class="field">
        <label>地址（host:port，留空使用端口分配的默认值）</label>
        <input v-model="conn.addr" placeholder="例如 127.0.0.1:3001"/>
      </div>
      <div class="field">
        <label>互联 Token（两端必须一致，留空自动生成/沿用）</label>
        <input v-model="conn.token" placeholder="留空自动生成"/>
      </div>
      <div class="ops">
        <button class="primary" :disabled="busy" @click="doConn">确认写入互联配置</button>
      </div>
      <pre class="preview" v-if="preview">{{ preview }}</pre>
      <p v-if="manual" class="warn">{{ manual }}</p>
    </div>

    <!-- Step5：启动 -->
    <div v-else-if="step === 5" class="wz-body">
      <p v-if="busy">启动中…</p>
      <template v-else-if="!started">
        <p>配置已就绪，点击启动实例。</p>
        <div class="ops">
          <button class="primary" @click="startInst">启动</button>
          <button @click="goOverview">暂不启动，回到总览</button>
        </div>
      </template>
      <template v-else>
        <p>已下发启动命令，总览图出现新节点即完成。</p>
        <div class="ops">
          <button class="primary" @click="goOverview">完成，回到总览</button>
          <button @click="reset">再建一个</button>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onUnmounted } from 'vue'
import { connectWS } from '../ws'
import { listManifests, listInstances, listPending, listPackages, uploadPackage,
         deletePackage, createInstance, wizardStep, delInstance } from '../api'

const STEP_NAMES = ['选程序', '部署', '登录', '互联', '启动']   // 默认模式（断点续跑文案也用它）
const STEP_COUNT = STEP_NAMES.length

const step = ref(0), manifests = ref({}), instances = ref([])
const dice = ref(''), loginRef = ref('')
// 配对模式：phase='login' 先部署+登录登录端；'dice' 选兼容骰子端（创建时自动 login_ref 关联）
const mode = ref('dice'), pair = ref(null), loginChoice = ref('')
let loginHandled = false            // 登录完成去向只处理一次（「我已完成扫码」与 WS completed 会竞态双触发）
const loading = ref(true), loadError = ref(''), busy = ref(false), err = ref('')
const conflict = ref(false), conflictDir = ref(''), qr = ref({}), verifyUrl = ref('')
const cred = ref({ qq: '', password: '', protocol: 'ANDROID_PAD', auth_token: '' })
const tokenSaved = ref(false)   // AUTH TOKEN 已落盘并重启过进程（LLBot v8 扫码前置条件）
const conn = ref({ direction: 'forward', addr: '', token: '' })
const preview = ref(''), manual = ref('')
const pending = ref([])          // 中间态实例（断点续跑入口）
const started = ref(false)       // Step5 是否已下发启动命令
const pkgs = ref({})             // {dice: {exists,size_mb,source,updated_at}}
const pkgBusy = ref(false), pkgMsg = ref('')
// sock 必须是 ref：script setup 里 let 变量不会随赋值同步到模板上下文，
// 旧写法下「刷新二维码」按钮拿到的永远是初始的 null
const sock = ref(null)
let instanceId = null

const manifest = computed(() => manifests.value[dice.value] || {})
const pkgInfo = computed(() => pkgs.value[dice.value] || null)
const loginType = computed(() => manifest.value.login_type || 'none')
const loginOptions = computed(() =>
  (manifest.value.compatible_login || []).filter(o => o !== 'builtin'))
// 登录端候选 = 已创建且程序类型在兼容矩阵里的实例（login_ref 在后端即实例 id，用于画连线）
const loginCandidates = computed(() =>
  instances.value.filter(i => loginOptions.value.includes(i.dice)))
// 默认选中项要在切换程序时同步，否则默认骰子（未触发 change）永远拿不到该标记
const needAuthToken = computed(() => !!manifest.value.auth_token_conditional)

// ---------- 配对模式 ----------
// 登录端程序 = 在任意骰子端 manifest 的 compatible_login 里出现过的程序（纯清单驱动，无名字分支）
const loginPrograms = computed(() => {
  const s = new Set()
  for (const m of Object.values(manifests.value))
    (m.compatible_login || []).forEach(o => { if (o !== 'builtin') s.add(o) })
  return Object.keys(manifests.value).filter(n => s.has(n))
})
const existingLogins = computed(() =>
  instances.value.filter(i => loginPrograms.value.includes(i.dice)))
const diceCandidates = computed(() => Object.keys(manifests.value).filter(n =>
  (manifests.value[n].compatible_login || []).includes(pair.value?.loginProg)))
const canCreate = computed(() => {
  if (mode.value === 'dice') return !!dice.value
  return pair.value?.phase === 'dice' ? !!dice.value : !!loginChoice.value
})
const createLabel = computed(() => {
  if (mode.value !== 'pair') return pkgInfo.value ? '解压本地包部署' : '下载部署'
  if (!pair.value || pair.value.phase === 'login')
    return loginChoice.value.startsWith('inst:') ? '进入骰子端选择' : '部署并登录该登录端'
  return pkgInfo.value ? '解压本地包部署（自动关联登录端）' : '下载部署（自动关联登录端）'
})
const stepNames = computed(() => {
  if (mode.value !== 'pair' || !pair.value) return STEP_NAMES
  return pair.value.phase === 'login'
    ? ['选登录端', '部署', '登录']                    // 登录端启动在登录完成后自动进行
    : ['选骰子端', '部署', '登录', '互联', '启动']
})
const wizTitle = computed(() => {
  if (mode.value !== 'pair' || !pair.value) return '新建骰子'
  return pair.value.phase === 'login' ? '配对 · 第一步：登录端' : '配对 · 第二步：骰子端'
})

const load = async () => {
  loading.value = true; loadError.value = ''
  try {
    const [m, inst, pend, pk] = await Promise.all(
      [listManifests(), listInstances(), listPending(), listPackages()])
    manifests.value = m || {}; instances.value = inst || []; pending.value = pend || []
    pkgs.value = Object.fromEntries((pk || []).filter(p => p.exists).map(p => [p.dice, p]))
    dice.value = Object.keys(manifests.value)[0] || ''
    step.value = dice.value ? 1 : 0           // 原实现从未把 step 推进到 1，页面永远停在 0/5
  } catch (e) {
    loadError.value = e.message || String(e)
  } finally {
    loading.value = false
  }
}
load()

// 静默刷新实例/待续跑列表（启动完成后待续跑条目应消失）
const refreshLists = () =>
  Promise.all([listInstances(), listPending()])
    .then(([i, p]) => { instances.value = i || []; pending.value = p || [] })
    .catch(() => {})

// 程序包：上传后部署直接解压本地包，不再联网下载
const pkgUp = ref(null)             // 上传进度 {loaded, total, sent, startAt}
const fmtMB = b => (b / 1048576).toFixed(1)
const pkgPercent = computed(() => {
  const u = pkgUp.value
  if (!u) return 0
  if (u.sent) return 100                          // 已发完 → 满格（停在「校验中」）
  return u.total ? Math.min(99, Math.round(u.loaded / u.total * 100)) : 100
})
const pkgText = computed(() => {
  const u = pkgUp.value
  if (!u) return ''
  // 请求体发完后后端还要落盘并做整体校验，大包可达数十秒，必须说明，否则像卡死
  if (u.sent) return '已发送完毕，服务端正在落盘并校验（大包可能耗时较久，请勿关闭页面）…'
  const secs = (Date.now() - u.startAt) / 1000
  const rate = secs > 1 ? ` · ${fmtMB(u.loaded / secs)} MB/s` : ''
  const size = `已上传 ${fmtMB(u.loaded)}${u.total ? ` / ${fmtMB(u.total)} MB` : ' MB'}`
  return u.total ? `${size}（${pkgPercent.value}%${rate}）` : size + rate
})

const uploadPkg = e => {
  const file = e.target.files[0]
  e.target.value = ''                          // 允许重选同一文件再次触发 change
  if (!file) return
  pkgBusy.value = true; pkgMsg.value = ''
  pkgUp.value = { loaded: 0, total: file.size, sent: false, startAt: Date.now() }
  uploadPackage(dice.value, file, ({ loaded, total, sent }) => {
    const cur = pkgUp.value                    // sent 一旦为真不再回退；loaded 取单调最大值
    pkgUp.value = { loaded: Math.max(loaded, cur?.loaded || 0),
                    total: total || cur?.total || file.size,
                    sent: !!(sent || cur?.sent),
                    startAt: cur?.startAt || Date.now() }
  })
    .then(info => {
      pkgs.value = { ...pkgs.value, [dice.value]: info }
      pkgMsg.value = `已上传 ${info.size_mb} MB，部署时将直接解压该包。`
    })
    .catch(ex => { pkgMsg.value = ''; err.value = ex.message || String(ex) })
    .finally(() => { pkgBusy.value = false; pkgUp.value = null })
}

const removePkg = () => {
  pkgBusy.value = true; pkgMsg.value = ''
  deletePackage(dice.value)
    .then(() => {
      const next = { ...pkgs.value }; delete next[dice.value]; pkgs.value = next
      pkgMsg.value = '已删除本地包，下次部署将在线下载。'
    })
    .catch(ex => { err.value = ex.message || String(ex) })
    .finally(() => { pkgBusy.value = false })
}

const goOverview = () => (location.hash = '#/overview')

const reset = () => {
  sock.value?.close(); sock.value = null
  step.value = 1; err.value = ''; conflict.value = false
  qr.value = {}; verifyUrl.value = ''; preview.value = ''; manual.value = ''
  cred.value = { qq: '', password: '', protocol: 'ANDROID_PAD', auth_token: '' }
  conn.value = { direction: 'forward', addr: '', token: '' }
  started.value = false; tokenSaved.value = false
  loginRef.value = ''; instanceId = null
  pair.value = null; loginChoice.value = ''; loginHandled = false
  refreshLists()
}

// 断点续跑：跳到该实例下一步（二维码登录需重开推送通道）——续跑只支持默认模式，退出配对态
const resume = p => {
  sock.value?.close(); sock.value = null
  err.value = ''; conflict.value = false; preview.value = ''; manual.value = ''
  started.value = false; tokenSaved.value = false   // 续跑实例的 token 落盘状态未知，重新判定
  pair.value = null; mode.value = 'dice'; loginHandled = false
  instanceId = p.id
  dice.value = p.dice
  loginRef.value = p.login_ref || ''
  step.value = Math.min(p.next_step || 1, 5)
  if (step.value === 3 && loginType.value === 'qrcode') openLoginWS()
}

// 停止并删除未完成实例（如下载失败卡在部署中的）：未完成实例无存档，目录一并清理
const stopPending = p => guard(async () => {
  await delInstance(p.id, true, true, false)
  await refreshLists()
})

const guard = async fn => {                    // 统一转圈 + 报错，避免失败后界面无反馈卡死
  busy.value = true; err.value = ''
  try { await fn() } catch (e) { err.value = e.message || String(e) } finally { busy.value = false }
}

// 配对阶段一选「新建」时把 dice 指向登录端程序：manifest / 离线包 / AUTH TOKEN 字段全部对准它
const onLoginChoice = () => {
  if (loginChoice.value.startsWith('new:')) dice.value = loginChoice.value.slice(4)
}

const create = () => guard(async () => {
  if (mode.value === 'pair') {
    // 阶段二：创建骰子端，自动 login_ref 关联登录端（第 4 步据此继承地址与令牌）
    if (pair.value && pair.value.phase === 'dice') {
      const r = await createInstance({ dice: dice.value,
                                       arch: manifest.value.arch || 'standalone',
                                       login_ref: pair.value.loginId })
      instanceId = r.id
      step.value = 2
      return doStep(2, {})
    }
    const [kind, v] = loginChoice.value.split(':')
    if (kind === 'inst') {                       // 已有登录端实例 → 直接进入骰子端选择
      const inst = instances.value.find(i => i.id === v)
      if (!inst) throw new Error('登录端实例不存在，请刷新页面重试')
      pair.value = { phase: 'dice', loginProg: inst.dice, loginId: v }
      dice.value = ''
      enterPhaseStepOne()
      return
    }
    // 阶段一：新建登录端，部署/登录复用同一套步骤机
    pair.value = { phase: 'login', loginProg: v, loginId: null }
    const r = await createInstance({ dice: v, arch: manifests.value[v].arch || 'standalone' })
    instanceId = r.id
    step.value = 2
    return doStep(2, {})
  }
  const r = await createInstance({ dice: dice.value, arch: manifest.value.arch || 'standalone',
                                   login_ref: loginRef.value || null })
  instanceId = r.id
  step.value = 2
  await doStep(2, {})
})

// 阶段切换 / 新流程开始时回到第 1 步：清掉上一阶段全部中间态
const enterPhaseStepOne = () => {
  err.value = ''; conflict.value = false
  qr.value = {}; verifyUrl.value = ''; preview.value = ''; manual.value = ''
  cred.value = { qq: '', password: '', protocol: 'ANDROID_PAD', auth_token: '' }
  conn.value = { direction: 'forward', addr: '', token: '' }
  started.value = false; tokenSaved.value = false
  sock.value?.close(); sock.value = null
  loginHandled = false
  step.value = 1
}

const doStep = async (n, payload) => {
  const r = await wizardStep(instanceId, n, payload)
  if (r.result === 'error') throw new Error(r.message || '操作失败')   // 如双击启动的竞态提示
  if (r.result === 'conflict') {
    conflict.value = true; conflictDir.value = r.dir || r.message || ''; return
  }
  if (r.manual) manual.value = r.manual
  if (n === 3) { afterLoginDone(); return }        // 登录完成去向统一收口（含配对阶段切换）
  step.value = n + 1
  if (step.value === 3) {
    loginHandled = false                            // 进入新一次登录步
    if (loginType.value === 'qrcode') {
      // TOKEN 已在第一步填写：进扫码页前先落盘（进程随后由 WS 自动拉起，天然带上 token）
      if (needAuthToken.value && cred.value.auth_token && !tokenSaved.value) await saveToken()
      openLoginWS()
    }
  }
  if (step.value === 4) preview.value = r.preview || ''
}

// 登录完成后的去向：
// · 普通模式 / 配对阶段二 → 第 4 步互联；
// · 配对阶段一（登录端）→ 先写登录端自身互联配置（默认正向监听 + 自动生成 token），
//   再拉起登录端（扫码时已运行则为 no-op），然后切到「选骰子端」阶段。
//   骰子端第 4 步经 login_ref 继承登录端的 ob11 端口与 token，保证两端一致。
const afterLoginDone = () => {
  if (loginHandled) return                          // 「我已完成扫码」与 WS completed 竞态双触发
  loginHandled = true
  if (!pair.value || pair.value.phase !== 'login') {
    step.value = 4; preview.value = ''; return
  }
  guard(async () => {
    const lid = instanceId
    const r4 = await wizardStep(lid, 4, {})
    if (r4.result === 'error') throw new Error(r4.message || '登录端互联配置写入失败')
    await wizardStep(lid, 5, {})
    pair.value = { phase: 'dice', loginProg: pair.value.loginProg, loginId: lid }
    dice.value = ''; loginChoice.value = ''
    enterPhaseStepOne()
    refreshLists()
  })
}

// Step4：留空的字段交给后端用默认值/自动生成的 token（两端一致性由后端保证）
const doConn = () => guard(async () => {
  const payload = { direction: conn.value.direction }
  if (conn.value.addr) payload.addr = conn.value.addr.trim()
  if (conn.value.token) payload.token = conn.value.token.trim()
  await doStep(4, payload)
})

const startInst = () => guard(async () => {
  await doStep(5, {})
  started.value = true
  refreshLists()
})

const resolve = useExisting => guard(async () => {   // 冲突二选一：重发 step2（后端已实现）
  await wizardStep(instanceId, 2, { use_existing: useExisting })
  conflict.value = false; step.value = 3
  loginHandled = false                                // 重新进入登录步
  if (loginType.value === 'qrcode') openLoginWS()
})

const openLoginWS = () => {
  sock.value?.close()
  sock.value = connectWS(`/ws/login/${instanceId}`, m => {
    if (m.type === 'qrcode') qr.value = m.payload
    if (m.type === 'verify') verifyUrl.value = m.payload.url
    if (m.type === 'completed' || m.type === 'skipped') afterLoginDone()
  })
}

const saveToken = async () => {                        // AUTH TOKEN 落盘（LLBot v8 出码前置条件）
  const r = await wizardStep(instanceId, 3, { qq: cred.value.qq, credentials: { ...cred.value } })
  if (r.result === 'error' || r.result === 'conflict')
    throw new Error(r.message || r.conflict || 'AUTH TOKEN 保存失败')
  tokenSaved.value = true
  if (r.manual) manual.value = r.manual
}

const doLogin = () => guard(async () => {              // Step3：登录（含条件必填校验）
  if (needAuthToken.value && !cred.value.auth_token) throw new Error('LLBot v8.0.9+ 必须填写 AUTH TOKEN')
  if (loginType.value === 'qrcode' && needAuthToken.value && !tokenSaved.value) {
    await saveToken()                        // 首次提交：保存 TOKEN 并重启进程出码，留在本步扫码
    sock.value?.send('refresh')
    return
  }
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
.pending { margin-bottom: 14px; padding: 10px 14px; border: 1px solid var(--warn); border-radius: 8px; }
.pkg { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }
.pkg-ok { color: var(--ok); }
.pending-item { display: flex; gap: 10px; align-items: center; margin-top: 6px; flex-wrap: wrap; }
.dialog code { font-family: ui-monospace, Menlo, Consolas, monospace; }
pre.preview {
  padding: 12px; margin: 0 0 14px; background: #f6f8fa; border: 1px solid var(--border);
  border-radius: 8px; font-family: ui-monospace, Menlo, Consolas, monospace; white-space: pre-wrap;
}
</style>
