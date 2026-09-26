<template>
  <div class="wizard">
    <div class="wz-head">
      <h3>{{ wizTitle }} — 第 {{ curStepIdx }}/{{ stepNames.length }} 步</h3>
      <ol class="steps">
        <li v-for="(s, i) in stepNames" :key="s" :class="{ done: curStepIdx > i + 1,
                                                      now: curStepIdx === i + 1 }">{{ s }}</li>
      </ol>
    </div>

    <!-- 断点续跑入口：管理器重启/断网留下的中间态实例 -->
    <div v-if="pending.length && step === 1 && !loading" class="pending">
      <p class="hint">有 {{ pending.length }} 个未完成的实例，可从中断处继续：</p>
      <p class="hint">（断点续跑按默认「骰子端优先」流程走；配对模式暂不支持续跑。）</p>
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
          <option value="dice">标准：先建骰子端，稍后可关联登录端</option>
          <option value="pair">配对：先建登录端，再选兼容骰子端（自动互联）</option>
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
        <!-- 接入通道：程序声明了多种通道时才出现（目前仅海豹支持官方机器人） -->
        <div class="field" v-if="botModes.length > 1">
          <label>接入通道</label>
          <select v-model="botMode">
            <option v-for="b in botModes" :key="b.id" :value="b.id">{{ b.label }}</option>
          </select>
          <p class="hint" v-if="officialMode">
            官方通道由 {{ dice }} 自己对接 QQ 官方服务：<b>不需要协议登录端</b>，面板也不会写入
            OneBot 互联配置。启动后进 WebUI「添加账号 → 平台 QQ → QQ 官方机器人」，
            用 AppID + AppSecret 或扫码完成接入（需在开放平台把本机公网 IP 填进白名单）。
            <a v-if="officialGuide" :href="officialGuide" target="_blank" rel="noreferrer">官方接入手册</a>
          </p>
        </div>
        <div class="field" v-if="loginOptions.length && !officialMode">
          <label>登录端（{{ loginCandidates.length ? '可留空稍后关联' : '暂无实例可选' }}）</label>
          <select v-model="loginRef">
            <option value="">暂不关联</option>
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
        <button v-if="pkgUp" class="danger" @click="cancelPkg">取消上传</button>
        <div v-if="pkgUp" class="pkgup">
          <div class="pkgup-bar" :class="{ 'is-indet': !pkgUp.total && !pkgUp.sent, 'is-sent': pkgUp.sent }">
            <i :style="{ width: pkgPercent + '%' }"></i>
          </div>
          <p class="hint">{{ pkgText }}</p>
        </div>
        <p v-if="pkgMsg" class="hint">{{ pkgMsg }}</p>
      </div>
      <!-- 缓存管理：程序包下载/上传后永久驻留，这里集中展示占用与是否仍被实例使用。
           有死缓存/超龄备份时默认展开提醒，否则收起（管理功能不该压过新建流程） -->
      <details class="cache-box" :open="unusedRows.length > 0">
        <summary>本地缓存（{{ pkgTotalMb }} MB）{{ unusedRows.length
          ? ' · ' + unusedRows.length + ' 个未使用' : ' · 点击管理' }}</summary>
        <p v-if="!pkgRows.length" class="hint">暂无本地缓存。</p>
        <div v-for="r in pkgRows" :key="r.dice" class="pkg">
          <span :class="r.in_use ? 'pkg-ok' : 'pkg-idle'">
            {{ r.dice }} · {{ r.size_mb }} MB ·
            {{ r.source === 'upload' ? '上传' : '下载' }}于 {{ r.updated_at }} —
            {{ r.in_use ? '已有实例在用' : '无实例使用（死缓存，可安全删除）' }}</span>
          <button :disabled="pkgBusy" @click="removePkgOf(r.dice)">删除</button>
        </div>
        <div class="ops" v-if="unusedRows.length">
          <button class="danger" :disabled="pkgBusy" @click="cleanUnused">
            清理 {{ unusedRows.length }} 个未使用包（释放约 {{ unusedMb }} MB）</button>
        </div>
        <p class="hint">删除只是清掉种子包，已部署的实例不受影响；再次部署该程序需重新下载或上传。</p>
        <p v-if="pkgMsg2" class="hint">{{ pkgMsg2 }}</p>
      </details>
      <!-- 备份产物：手动导出 / 升级前快照 / 定时备份都落在 exports/，此前没有任何回收入口 -->
      <details class="cache-box" :open="expStaleRows.length > 0">
        <summary>备份文件（{{ expTotalMb }} MB）{{ expStaleRows.length
          ? ' · ' + expStaleRows.length + ' 份超龄' : ' · 点击管理' }}</summary>
        <p v-if="!expRows.length" class="hint">暂无备份文件。</p>
        <div v-for="r in expRows" :key="r.name" class="pkg">
          <span :class="r.age_days >= pruneDays ? 'pkg-idle' : 'pkg-ok'">
            {{ r.name }} · {{ r.size_mb }} MB · {{ r.mtime }}（已存放 {{ r.age_days }} 天）</span>
          <button :disabled="busy" @click="delExportFile(r.name)">删除</button>
        </div>
        <div class="ops" v-if="expRows.length">
          <label class="inline">清理超过
            <input type="number" min="1" max="365" v-model.number="pruneDays" style="width:64px"/>
            天的备份</label>
          <button class="danger" :disabled="busy" @click="pruneOldExports">
            清理 {{ expStaleRows.length }} 份（释放约
            {{ Math.round(expStaleRows.reduce((s, r) => s + (r.size_mb || 0), 0)) }} MB）</button>
        </div>
        <p class="hint">升级前会自动留一份整目录快照，定时备份也在这里；确认回滚无需要的旧备份可安全清理。</p>
        <p v-if="expMsg" class="hint">{{ expMsg }}</p>
      </details>
      <p v-if="manifest.prerequisite" class="hint">前置依赖：{{ manifest.prerequisite }}</p>
      <div class="ops">
        <button class="primary" :disabled="!canCreate || busy" @click="create">
          下一步：{{ createLabel }}</button>
      </div>
    </div>

    <!-- Step2：部署 -->
    <div v-else-if="step === 2" class="wz-body">
      <p v-if="busy">{{ deployMsg || (pkgInfo ? '正在解压本地程序包部署 ' + dice + '，请稍候…'
                          : '正在下载部署 ' + dice + '，请稍候（首次可能耗时数分钟）…') }}</p>
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

    <!-- Step4：互联配置。
         正向/反向在两端是镜像语义：登录端 forward=开 WS 服务端口，骰子端 forward=主动连入
         ——两端同选「正向」即可连通。旧文案把 forward 一刀切成「本程序监听」，对骰子端
         恰好说反，按文案选会配出「两端都拨号/都监听」的死局。 -->
    <div v-else-if="step === 4" class="wz-body">
      <div v-if="loginTarget" class="conn-peer">
        对端：{{ loginTarget.dice }} · {{ loginTarget.id }}{{ loginTarget.qq
          ? '（QQ ' + loginTarget.qq + '）' : '' }}
        <span v-if="peerOb11Port"> · ob11 ws {{ peerOb11Port }}</span>
      </div>
      <p v-else-if="needsLoginEnd" class="warn">
        尚未关联登录端：写入的地址 {{ defaultAddr }} 当前没有程序监听，启动后会持续连接失败。
        可先继续（稍后在总览关联登录端并「重写互联配置」），但建议现在就回去关联。
      </p>
      <div class="field">
        <label>WS 模式</label>
        <select v-model="conn.direction">
          <option value="forward">正向 WS — {{ isLoginEnd
            ? '本程序监听端口，等待骰子端连入' : '本程序主动连接登录端' }}（推荐）</option>
          <option value="reverse">反向 WS — {{ isLoginEnd
            ? '本程序主动连接骰子端' : '本程序监听端口，等待登录端连入' }}</option>
        </select>
        <p class="hint">两端选同一模式即可连通（一端监听、另一端连接）；保持默认「正向」适配
          绝大多数部署，登录端与骰子端都这么选即互通。</p>
      </div>
      <details class="adv-box">
        <summary>高级：地址 / Token（默认自动推导，一般无需修改）</summary>
        <div class="field">
          <label>地址 host:port</label>
          <input v-model="conn.addr" :placeholder="'留空 = ' + defaultAddr"/>
        </div>
        <div class="field">
          <label>互联 Token（两端必须一致）</label>
          <input v-model="conn.token" placeholder="留空自动生成 / 沿用登录端的"/>
        </div>
      </details>
      <div class="ops">
        <button class="primary" :disabled="busy" @click="doConn">写入互联配置</button>
      </div>
      <pre class="preview" v-if="preview">{{ preview }}</pre>
      <p v-if="manual" class="warn">{{ manual }}</p>
    </div>

    <!-- Step5：启动 -->
    <div v-else-if="step === 5" class="wz-body">
      <!-- 官方通道：连接动作在程序自身 WebUI 完成，这里把前置清单一次说清 -->
      <div v-if="officialMode" class="official-tip">
        <b>官方机器人通道 · 接入清单</b>
        <ol>
          <li>到 <a href="https://q.qq.com" target="_blank" rel="noreferrer">QQ 开放平台</a>
            实名创建机器人应用，取到 <code>AppID</code> 与 <code>AppSecret</code>。</li>
          <li>「开发设置 → IP 白名单」填本机<b>公网</b> IP（云服务器填控制台显示的 IP）。</li>
          <li>启动后打开下方 WebUI：添加账号 → 平台选「QQ」→ 连接方式选「QQ 官方机器人」，
            用 AppID + AppSecret 或扫码接入。</li>
          <li>指令须在开放平台配置（默认前缀 <code>/</code>），使用场景勾「QQ 群 / 频道私信 / QQ 频道」，
            <b>不要勾「消息列表」</b>。</li>
          <li>提交审核（自测报告 + 隐私协议）通过后「上线机器人」；先用沙盒群自测。</li>
        </ol>
        <p class="hint">优点：不走协议端，无风控封号风险。代价：官方接口能力受限，
          依赖主动消息、群管、本地媒体的功能可能不可用。</p>
      </div>
      <p v-if="busy">启动中…</p>
      <template v-else-if="!started">
        <p>配置已就绪，点击启动实例。</p>
        <div class="ops">
          <button class="primary" @click="startInst">启动</button>
          <button @click="goOverview">暂不启动，回到总览</button>
        </div>
      </template>
      <template v-else>
        <p>已启动 <code>{{ instanceId }}</code>，总览图出现新节点即完成。</p>
        <div v-if="webuiInfo" class="start-info">
          <a class="webui-link" :href="webuiUrl" target="_blank" rel="noreferrer">
            打开 WebUI（{{ webuiUrl }}）</a>
          <p v-if="webuiInfo.token" class="hint">
            WebUI 令牌：<code class="tok">{{ webuiInfo.token }}</code>（登录页粘贴使用）</p>
          <p v-else class="hint">令牌将从启动日志中回读，稍后可在总览查看。</p>
        </div>
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
         deletePackage, deleteUnusedPackages, listExports, deleteExport, pruneExports,
         createInstance, wizardStep, delInstance, deployProgress, instanceWebui } from '../api'

const STEP_NAMES = ['选程序', '部署', '登录', '互联', '启动']   // 默认模式（断点续跑文案也用它）

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
const pkgBusy = ref(false), pkgMsg = ref(''), pkgMsg2 = ref('')
// 缓存管理视图：全部本地包 / 未被任何实例使用的那部分（可安全删除的死缓存）
const pkgRows = computed(() => Object.values(pkgs.value))
const unusedRows = computed(() => pkgRows.value.filter(r => !r.in_use))
const sumMb = rows => Math.round(rows.reduce((s, r) => s + (r.size_mb || 0), 0))
const pkgTotalMb = computed(() => sumMb(pkgRows.value))
const unusedMb = computed(() => sumMb(unusedRows.value))
// 备份产物（exports/）：升级前快照 + 定时备份，长期不回收会堆到几百 MB
const expRows = ref([]), expMsg = ref(''), pruneDays = ref(30)
const expTotalMb = computed(() => Math.round(expRows.value.reduce((s, r) => s + (r.size_mb || 0), 0)))
const expStaleRows = computed(() => expRows.value.filter(r => r.age_days >= pruneDays.value))
// 独立加载：失败不牵连主流程（旧服务端可能还没这个端点）
const loadExports = () => listExports().then(e => (expRows.value = e || [])).catch(() => {})
// sock 必须是 ref：script setup 里 let 变量不会随赋值同步到模板上下文，
// 旧写法下「刷新二维码」按钮拿到的永远是初始的 null
const sock = ref(null)
let instanceId = null

const manifest = computed(() => manifests.value[dice.value] || {})
const pkgInfo = computed(() => pkgs.value[dice.value] || null)
const loginType = computed(() => manifest.value.login_type || 'none')
const loginOptions = computed(() =>
  (manifest.value.compatible_login || []).filter(o => o !== 'builtin'))
// 接入通道：清单驱动（manifest.bot_modes）。官方通道由程序自身对接官方服务，
// 不需要登录端、也不需要面板写互联配置 —— 向导据此跳过「登录」「互联」两步。
const botModes = computed(() => manifest.value.bot_modes || [])
const botMode = ref('onebot')
const officialMode = computed(() =>
  botMode.value === 'official' && botModes.value.some(b => b.id === 'official'))
const officialGuide = computed(() =>
  (botModes.value.find(b => b.id === 'official') || {}).guide_url || '')
// 登录端候选 = 已创建且程序类型在兼容矩阵里的实例（login_ref 在后端即实例 id，用于画连线）
const loginCandidates = computed(() =>
  instances.value.filter(i => loginOptions.value.includes(i.dice)))
// 默认选中项要在切换程序时同步，否则默认骰子（未触发 change）永远拿不到该标记
const needAuthToken = computed(() => !!manifest.value.auth_token_conditional)

// ---------- 向导步进（面向有基础用户）----------
// 本程序是否 WS 服务端角色：持有 ob11 端口（登录端）→ 正向=监听；骰子端 → 正向=主动连入
const isLoginEnd = computed(() => !!manifest.value.ob11_default_port)
// none/external 没有独立登录环节：Step3 自动跳过，步骤条同步少一步
const skipLoginStep = computed(() =>
  loginType.value === 'none' || loginType.value === 'external' || officialMode.value)
// 后端步号 1-5；跳过登录时把 >3 的步号在显示层左移一位
const curStepIdx = computed(() => {
  const s = Math.min(step.value, 5)
  return Math.min(skipLoginStep.value && s > 3 ? s - 1 : s, stepNames.value.length)
})
// 当前实例在列表中的记录（进入 Step4 时 refreshLists 已带回）
const curInst = computed(() =>
  instanceId ? instances.value.find(i => i.id === instanceId) || null : null)
// 互联对端：实例记录里的 login_ref 优先，配对模式/表单选择兜底
const loginTarget = computed(() => {
  const ref = curInst.value?.login_ref || loginRef.value ||
    (pair.value?.phase === 'dice' ? pair.value.loginId : '')
  return ref ? instances.value.find(i => i.id === ref) || null : null
})
const peerOb11Port = computed(() =>
  (loginTarget.value?.allocated_ports || {}).ob11)
// 骰子端（兼容矩阵非空）却没关联登录端 → Step4 明确警告，别让用户造出连不通的实例
const needsLoginEnd = computed(() =>
  loginOptions.value.length > 0 && !loginTarget.value)
// 与后端 wizard.step4 同口径的默认地址预览：自己有 ob11 用自己的，否则对端的
const defaultAddr = computed(() => {
  const own = (curInst.value?.allocated_ports || {}).ob11 || manifest.value.ob11_default_port
  return `127.0.0.1:${own || peerOb11Port.value || 3001}`
})
// Step5 启动完成后的 WebUI 直达信息
const webuiInfo = ref(null)
const webuiUrl = computed(() => webuiInfo.value?.port
  ? `http://${location.hostname}:${webuiInfo.value.port}` : '')

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
  if (mode.value !== 'pair' || !pair.value) {
    const base = skipLoginStep.value ? STEP_NAMES.filter(s => s !== '登录') : STEP_NAMES
    // 官方通道连互联步也省掉：选程序 → 部署 → 启动
    return officialMode.value ? base.filter(s => s !== '互联') : base
  }
  return pair.value.phase === 'login'
    ? ['选登录端', '部署', '登录']                    // 登录端启动在登录完成后自动进行
    : skipLoginStep.value
      ? ['选骰子端', '部署', '互联', '启动']
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
  loadExports()                               // 首次进向导就要看到备份产物占用
}
load()

// 静默刷新实例/待续跑列表（启动完成后待续跑条目应消失）+ 备份产物占用
const refreshLists = async () => {
  try {
    const [i, p] = await Promise.all([listInstances(), listPending()])
    instances.value = i || []; pending.value = p || []
  } catch { /* 静默：刷新失败不阻断页面 */ }
  loadExports()
}

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
  pkgJob.value = uploadPackage(dice.value, file, ({ loaded, total, sent }) => {
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
    .finally(() => { pkgBusy.value = false; pkgUp.value = null; pkgJob.value = null })
}

const pkgJob = ref(null)             // 当前上传任务（Promise.abort() 取消 XHR）
const cancelPkg = () => { pkgJob.value?.abort?.() }

const removePkgOf = name => {
  pkgBusy.value = true; pkgMsg.value = ''; pkgMsg2.value = ''
  deletePackage(name)
    .then(() => {
      const next = { ...pkgs.value }; delete next[name]; pkgs.value = next
      const msg = `已删除 ${name} 的本地包，下次部署将在线下载。`
      if (name === dice.value) pkgMsg.value = msg
      else pkgMsg2.value = msg
    })
    .catch(ex => { err.value = ex.message || String(ex) })
    .finally(() => { pkgBusy.value = false })
}
const removePkg = () => removePkgOf(dice.value)

// 一键清理死缓存：没有任何实例在用的种子包（菜单入口按需触发，不做自动删除）
const cleanUnused = () => guard(async () => {
  pkgMsg2.value = ''
  const r = await deleteUnusedPackages()
  const next = { ...pkgs.value }
  ;(r.removed || []).forEach(n => delete next[n])
  pkgs.value = next
  pkgMsg2.value = r.removed && r.removed.length
    ? `已清理 ${r.removed.join(' / ')}，释放约 ${r.freed_mb} MB。`
    : '没有需要清理的未使用缓存。'
})

// 备份产物回收：升级每次留一份整目录快照，定时备份也在同一目录，长期只增不减
const delExportFile = name => guard(async () => {
  expMsg.value = ''
  await deleteExport(name)
  expRows.value = expRows.value.filter(r => r.name !== name)
  expMsg.value = `已删除备份 ${name}。`
})
const pruneOldExports = () => guard(async () => {
  expMsg.value = ''
  const r = await pruneExports(pruneDays.value)
  const names = r.removed || []
  expRows.value = expRows.value.filter(x => !names.includes(x.name))
  expMsg.value = names.length
    ? `已清理 ${names.length} 份超过 ${pruneDays.value} 天的备份，释放约 ${r.freed_mb} MB。`
    : `没有超过 ${pruneDays.value} 天的备份。`
})

const goOverview = () => (location.hash = '#/overview')

const reset = () => {
  sock.value?.close(); sock.value = null
  step.value = 1; err.value = ''; conflict.value = false
  qr.value = {}; verifyUrl.value = ''; preview.value = ''; manual.value = ''
  cred.value = { qq: '', password: '', protocol: 'ANDROID_PAD', auth_token: '' }
  conn.value = { direction: 'forward', addr: '', token: '' }
  started.value = false; tokenSaved.value = false; webuiInfo.value = null
  loginRef.value = ''; instanceId = null
  pair.value = null; loginChoice.value = ''; loginHandled = false
  botMode.value = 'onebot'
  stopDeployPoll(); deployMsg.value = ''
  refreshLists()
}

// 断点续跑：跳到该实例下一步（二维码登录需重开推送通道）——续跑只支持默认模式，退出配对态
const resume = p => {
  sock.value?.close(); sock.value = null
  err.value = ''; conflict.value = false; preview.value = ''; manual.value = ''
  started.value = false; tokenSaved.value = false   // 续跑实例的 token 落盘状态未知，重新判定
  pair.value = null; mode.value = 'dice'; loginHandled = false
  stopDeployPoll(); deployMsg.value = ''
  instanceId = p.id
  dice.value = p.dice
  loginRef.value = p.login_ref || ''
  botMode.value = p.bot_mode || 'onebot'          // 续跑官方通道实例才能跳过登录/互联步
  step.value = Math.min(p.next_step || 1, 5)
  if (step.value === 3) enterStep3().catch(e => { err.value = e.message || String(e) })
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
                                   login_ref: loginRef.value || null,
                                   bot_mode: officialMode.value ? 'official' : 'onebot' })
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
  started.value = false; tokenSaved.value = false; webuiInfo.value = null
  sock.value?.close(); sock.value = null
  loginHandled = false
  step.value = 1
}

const doStep = async (n, payload) => {
  if (n === 2) { deployMsg.value = ''; startDeployPoll() }   // 部署期间轮询进度
  try {
    const r = await wizardStep(instanceId, n, payload)
    if (r.result === 'error') throw new Error(r.message || '操作失败')   // 如双击启动的竞态提示
    if (r.result === 'conflict') {
      conflict.value = true; conflictDir.value = r.dir || r.message || ''; return
    }
    if (r.manual) manual.value = r.manual
    if (n === 3) { afterLoginDone(); return }        // 登录完成去向统一收口（含配对阶段切换）
    step.value = n + 1
    if (step.value === 3) await enterStep3()
    if (step.value === 4) {
      preview.value = r.preview || ''
      refreshLists()          // Step4 要展示对端（login_ref）与它的 ob11 端口，先刷新实例列表
    }
  } finally {
    if (n === 2) stopDeployPoll()
  }
}

// 进入第 3 步（登录）的统一入口：
// · qrcode/account → 开 WS 推二维码（LLBot 的 TOKEN 先落盘再拉起）
// · none/external  → 无独立登录环节，自动提交 step3（后端直接转 CONFIGURED）跳过空转页
const enterStep3 = async () => {
  loginHandled = false
  if (loginType.value === 'qrcode') {
    // TOKEN 已在第一步填写：进扫码页前先落盘（进程随后由 WS 自动拉起，天然带上 token）
    if (needAuthToken.value && cred.value.auth_token && !tokenSaved.value) await saveToken()
    openLoginWS()
  } else if (skipLoginStep.value) {
    await doStep(3, {})
  }
}

// ---------- 部署进度轮询：step2 同步部署期间 1s 拉一次，大包下载不再「假死」 ----------
const deployMsg = ref('')
let deployTimer = null
const stopDeployPoll = () => { if (deployTimer) { clearInterval(deployTimer); deployTimer = null } }
const startDeployPoll = () => {
  stopDeployPoll()
  deployTimer = setInterval(async () => {
    try {
      const p = await deployProgress(instanceId)
      if (p.stage === 'download')
        deployMsg.value = `正在下载程序包 ${fmtMB(p.done)}${p.total ? ' / ' + fmtMB(p.total) + ' MB' : ' MB'}…`
      else if (p.stage === 'extract') deployMsg.value = '下载完成，正在解压部署…'
      else if (p.stage === 'prepare') deployMsg.value = '正在准备部署…'
    } catch { /* 轮询失败不打扰主流程 */ }
  }, 1000)
}

// 登录完成后的去向：
// · 普通模式 / 配对阶段二 → 第 4 步互联；
// · 配对阶段一（登录端）→ 先写登录端自身互联配置（默认正向监听 + 自动生成 token），
//   再拉起登录端（扫码时已运行则为 no-op），然后切到「选骰子端」阶段。
//   骰子端第 4 步经 login_ref 继承登录端的 ob11 端口与 token，保证两端一致。
const afterLoginDone = () => {
  if (loginHandled) return                          // 「我已完成扫码」与 WS completed 竞态双触发
  loginHandled = true
  if (officialMode.value) { step.value = 5; return }   // 官方通道：没有互联步，直接进启动
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
  // 就地反馈：WebUI 直达 + 令牌（actual_port 由启动日志回读，等一拍再取更准）
  setTimeout(() => instanceWebui(instanceId)
    .then(w => { webuiInfo.value = w })
    .catch(() => {}), 2500)
  refreshLists()
})

const resolve = useExisting => guard(async () => {   // 冲突二选一：重发 step2（后端已实现）
  deployMsg.value = ''; startDeployPoll()
  try {
    await wizardStep(instanceId, 2, { use_existing: useExisting })
  } finally {
    stopDeployPoll()
  }
  conflict.value = false; step.value = 3
  await enterStep3()                                 // 登录步统一入口（none/external 自动跳过）
})

const openLoginWS = () => {
  sock.value?.close()
  sock.value = connectWS(`/ws/login/${instanceId}`, m => {
    if (m.type === 'qrcode') qr.value = m.payload
    if (m.type === 'verify') verifyUrl.value = m.payload.url
    if (m.type === 'completed' || m.type === 'skipped') afterLoginDone()
    if (m.type === 'login_failed') {               // 账号登录失败：明确反馈而非卡在原页
      err.value = m.payload?.message || '登录失败，请检查账号信息后重试'
      loginHandled = false
    }
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

onUnmounted(() => { sock.value?.close(); stopDeployPoll() })
</script>

<style scoped>
.wz-head { margin-bottom: 14px; }
.steps { display: flex; gap: 8px; flex-wrap: wrap; margin: 0; padding: 0; list-style: none; }
.steps li {
  padding: 4px 12px; border-radius: 999px; font-size: 12px;
  color: var(--muted); background: var(--code-bg); border: 1px solid var(--border);
}
.steps li.done { color: var(--ok); border-color: var(--step-done-border); background: var(--step-done-bg); }
.steps li.now { color: #fff; background: var(--brand); border-color: var(--brand); }
/* 官方机器人通道：接入清单比向导步骤更需要被看到 */
.official-tip { padding: 8px 12px; border: 1px solid var(--border);
  border-radius: 8px; background: var(--code-bg); margin-bottom: 10px; font-size: 13px; }
.official-tip ol { margin: 6px 0; padding-left: 20px; }
.official-tip li { margin: 3px 0; }
.wz-body { display: block; }
.field { max-width: 420px; margin-bottom: 14px; }
.field input, .field select { margin-bottom: 8px; }
.ops { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 4px; }
.err {
  padding: 8px 12px; margin-bottom: 14px; color: var(--danger);
  background: var(--err-bg); border: 1px solid var(--err-border); border-radius: 8px;
}
.warn { color: var(--warn); }
.qr img { max-width: 260px; display: block; margin-bottom: 10px; background: var(--panel); }
.dialog { padding: 14px; margin-bottom: 14px; border: 1px solid var(--danger); border-radius: 8px; }
.pending { margin-bottom: 14px; padding: 10px 14px; border: 1px solid var(--warn); border-radius: 8px; }
.pkg { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }
.pkg-ok { color: var(--ok); }
.pkg-idle { color: var(--muted); }
.cache-box { padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px; }
.cache-box summary { cursor: pointer; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
.cache-box[open] summary { margin-bottom: 10px; }
.cache-box .pkg { font-size: 13px; }
.conn-peer {
  padding: 8px 12px; margin-bottom: 14px; font-size: 13px;
  border: 1px solid var(--step-done-border, var(--border));
  background: var(--step-done-bg, var(--code-bg)); border-radius: 8px;
}
.adv-box { max-width: 480px; margin-bottom: 14px; }
.adv-box summary { cursor: pointer; font-size: 13px; color: var(--muted); margin-bottom: 8px; }
.adv-box[open] summary { margin-bottom: 10px; }
.start-info { margin-bottom: 12px; }
.webui-link { color: var(--brand); }
.tok { user-select: all; font-family: ui-monospace, Menlo, Consolas, monospace; }
.inline { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.pending-item { display: flex; gap: 10px; align-items: center; margin-top: 6px; flex-wrap: wrap; }
.dialog code { font-family: ui-monospace, Menlo, Consolas, monospace; }
pre.preview {
  padding: 12px; margin: 0 0 14px; background: var(--code-bg); border: 1px solid var(--border);
  border-radius: 8px; font-family: ui-monospace, Menlo, Consolas, monospace; white-space: pre-wrap;
}
</style>
