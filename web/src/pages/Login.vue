<template>
  <!-- 分化 C2：desktop 首启未设置密码 → 设置界面（server 首启自动生成密码，不走这里） -->
  <form v-if="mode === 'setup'" class="login" @submit.prevent="doSetup">
    <h3>首次启动 — 设置管理密码</h3>
    <p class="hint">检测到尚未设置管理密码，请设置一个 ≥6 位的密码以保护面板。</p>
    <label>
      <span>新管理密码</span>
      <input v-model="pwd" type="password" placeholder="≥6 位"/>
    </label>
    <label>
      <span>确认新密码</span>
      <input v-model="pwd2" type="password" placeholder="再次输入"/>
    </label>
    <button type="submit" class="go" :disabled="busy || !pwd || !pwd2">
      {{ busy ? '设置中…' : '设置并登录' }}
    </button>
    <p v-if="err" class="err" role="alert">{{ err }}</p>
  </form>

  <!-- 正常登录：form 包裹让浏览器密码管理器识别登录表单，Enter 提交走原生 submit -->
  <form v-else class="login" @submit.prevent="doLogin">
    <h3>登录 DiceManager</h3>
    <!-- 分化 C1：角色切换仅 server（多用户）有意义；desktop 只有 admin -->
    <div class="tabs" v-if="isServer">
      <button type="button" :class="{ on: tab === 'admin' }" @click="switchTo('admin')">管理员登录</button>
      <button type="button" :class="{ on: tab === 'user' }" @click="switchTo('user')">用户登录</button>
    </div>
    <label v-if="isServer && tab === 'user'">
      <span>用户名</span>
      <input v-model.trim="username" placeholder="管理员分配的用户名"/>
    </label>
    <label>
      <span>密码</span>
      <input v-model="pwd" type="password"
             :placeholder="isServer ? '管理密码（首次启动打印在服务端控制台）' : '管理密码'"/>
    </label>
    <button type="submit" class="go"
            :disabled="busy || !pwd || (isServer && tab === 'user' && !username)">
      {{ busy ? '登录中…' : '登录' }}
    </button>
    <p v-if="err" class="err" role="alert">{{ err }}</p>
    <p v-if="hint" class="hint">{{ hint }}</p>
  </form>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { getEdition, login, needsSetup, setup } from '../api'
// 两个入口只是预填用户名：真正的角色由服务端签发的 token 决定，
// 这里不设 role 参数——客户端声明角色等于把权限校验交给前端。
const mode = ref('login')            // 'login' | 'setup'
const tab = ref('admin')             // 'admin' | 'user'（仅 server 多用户时可见）
const isServer = ref(false)          // 由 /api/edition 决定，后端是唯一事实来源
const username = ref('')
const pwd = ref(''), pwd2 = ref('')
const busy = ref(false)
const err = ref(''), hint = ref('')

const switchTo = m => { tab.value = m; err.value = ''; if (m === 'admin') username.value = '' }

onMounted(async () => {
  try { isServer.value = (await getEdition()) === 'server' }
  catch { /* 探测失败按 desktop 处理：只是少渲染角色切换，不影响登录 */ }
  try {
    if (await needsSetup()) mode.value = 'setup'
  } catch (e) {
    // 探测失败必须可见：静默吞掉会让用户只看到普通登录框，
    // 误以为「密码文件不存在却没提示设置密码」。
    hint.value = `未能确认服务端是否已设置管理密码（${e.message || '网络错误'}），可先尝试登录。`
  }
})

const doLogin = async () => {
  if (!pwd.value || busy.value) return
  if (isServer.value && tab.value === 'user' && !username.value) return
  busy.value = true; err.value = ''
  try {
    const r = await login(isServer.value && tab.value === 'user' ? username.value : 'admin',
                          pwd.value)
    // 选了「用户登录」但服务端说你是管理员（或反之）时如实提示，不静默放行
    const wantAdmin = !isServer.value || tab.value === 'admin'
    if ((r.role === 'admin') !== wantAdmin) {
      err.value = wantAdmin
        ? `该账号是普通用户，请用「用户登录」；如需管理员请联系面板管理员`
        : `「${r.username}」是管理员账号，请用「管理员登录」`
      pwd.value = ''
      return
    }
    location.hash = '#/overview'
  } catch (e) {
    // 服务端未初始化会返 428「首次启动，请先设置管理密码」→ 自动切到设置模式
    if (/首次启动|设置管理密码/i.test(e.message)) {
      mode.value = 'setup'
      err.value = ''
      pwd.value = pwd2.value = ''
      hint.value = '请设置一个 ≥6 位的管理密码'
    } else {
      err.value = e.message
    }
  } finally { busy.value = false }
}

const doSetup = async () => {
  if (busy.value) return
  err.value = ''
  if (!pwd.value) { err.value = '请输入新管理密码'; return }
  if (pwd.value.length < 6) { err.value = '管理密码至少 6 位'; return }
  if (pwd.value !== pwd2.value) { err.value = '两次输入的密码不一致'; return }
  busy.value = true
  try {
    await setup(pwd.value)
    location.hash = '#/overview'
  } catch (e) { err.value = e.message }
  finally { busy.value = false }
}
</script>
<style scoped>
/* margin 由本规则接管（覆盖 form 默认 margin）；border:0 去掉 form 默认边框 */
.login { max-width: 360px; margin: 60px auto; border: 0;
         display: flex; flex-direction: column; gap: 10px; }
.tabs { display: flex; gap: 8px; }
.tabs button { flex: 1; }
.tabs button.on { border-color: var(--accent, #3b82f6); font-weight: 500; }
label { display: flex; flex-direction: column; gap: 4px; }
label span { font-size: 13px; opacity: .75; }
.go { margin-top: 4px; }
.err { color: #e5484d; }
.hint { color: var(--text-dim, #5a6172); font-size: 13px; line-height: 1.5; }
</style>
