<template>
  <div class="login">
    <!-- 首次启动：设置管理密码 -->
    <template v-if="mode === 'setup'">
      <h3>首次启动 — 设置管理密码</h3>
      <p class="hint">检测到尚未设置管理密码，请设置一个 ≥6 位的密码以保护面板。</p>
      <input v-model="pwd" type="password" placeholder="新管理密码（≥6 位）"
             @keyup.enter="doSetup"/>
      <input v-model="pwd2" type="password" placeholder="再次输入新管理密码"
             @keyup.enter="doSetup"/>
      <button :disabled="busy" @click="doSetup">{{ busy ? '设置中…' : '设置并登录' }}</button>
      <p v-if="err" class="err">{{ err }}</p>
    </template>

    <!-- 正常登录 -->
    <template v-else>
      <h3>登录 DiceManager</h3>
      <input v-model="pwd" type="password" placeholder="管理密码"
             @keyup.enter="doLogin"/>
      <button :disabled="busy" @click="doLogin">{{ busy ? '登录中…' : '登录' }}</button>
      <p v-if="err" class="err">{{ err }}</p>
      <p v-if="hint" class="hint">{{ hint }}</p>
    </template>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { login, needsSetup, setup } from '../api'

const pwd = ref(''), pwd2 = ref('')
const busy = ref(false), err = ref(''), hint = ref('')
// mode: 'login' = 正常登录；'setup' = 首次启动设置密码
// 进入页面时调 /api/needs-setup 探测：true → 切到 setup 界面
const mode = ref('login')

onMounted(async () => {
  try {
    if (await needsSetup()) {
      mode.value = 'setup'
      hint.value = ''
    }
  } catch (e) {
    // 探测失败必须可见：原先这里静默吞掉（hint=''），用户只看到普通登录框，
    // 会误以为「密码文件不存在却没提示设置密码」。
    hint.value = `未能确认服务端是否已设置管理密码（${e.message || '网络错误'}），可先尝试登录。`
  }
})

const doLogin = async () => {
  if (!pwd.value || busy.value) return
  busy.value = true; err.value = ''; hint.value = ''
  try {
    await login(pwd.value)
    location.hash = '#/overview'
  } catch (e) {
    // 服务端首次启动会返 428「首次启动，请先设置管理密码」→ 自动切到 setup 模式
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
.login { max-width: 360px; margin: 60px auto; display: flex; flex-direction: column; gap: 10px; }
.err { color: #e5484d; }
.hint { color: #5a6172; font-size: 13px; line-height: 1.5; }
</style>