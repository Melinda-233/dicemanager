<template>
  <!-- form 包裹：浏览器密码管理器据此识别登录表单，Enter 提交也走原生 submit
       （原先只有 @keyup.enter，密码管理器不认、且无 form 语义） -->
  <form class="login" @submit.prevent="doLogin">
    <h3>登录 DiceManager</h3>
    <div class="tabs">
      <button type="button" :class="{ on: mode === 'admin' }" @click="switchTo('admin')">管理员登录</button>
      <button type="button" :class="{ on: mode === 'user' }" @click="switchTo('user')">用户登录</button>
    </div>
    <label v-if="mode === 'user'">
      <span>用户名</span>
      <input v-model.trim="username" placeholder="管理员分配的用户名"/>
    </label>
    <label>
      <span>密码</span>
      <input v-model="pwd" type="password"
             :placeholder="mode === 'admin' ? '管理密码（首次启动打印在服务端控制台）' : '密码'"/>
    </label>
    <button type="submit" class="go" :disabled="busy || !pwd || (mode === 'user' && !username)">
      {{ busy ? '登录中…' : '登录' }}
    </button>
    <p v-if="err" class="err" role="alert">{{ err }}</p>
  </form>
</template>
<script setup>
import { ref } from 'vue'
import { login } from '../api'
// 两个入口只是预填用户名：真正的角色由服务端签发的 token 决定，
// 这里不设 role 参数——客户端声明角色等于把权限校验交给前端。
const mode = ref('admin')
const username = ref('')
const pwd = ref('')
const busy = ref(false)
const err = ref('')
const switchTo = m => { mode.value = m; err.value = ''; if (m === 'admin') username.value = '' }
const doLogin = async () => {
  if (!pwd.value || busy.value) return
  if (mode.value === 'user' && !username.value) return
  busy.value = true; err.value = ''
  try {
    const r = await login(mode.value === 'admin' ? 'admin' : username.value, pwd.value)
    // 选了「用户登录」但服务端说你是管理员（或反之）时如实提示，不静默放行
    const wantAdmin = mode.value === 'admin'
    if ((r.role === 'admin') !== wantAdmin) {
      err.value = wantAdmin
        ? `该账号是普通用户，请用「用户登录」；如需管理员请联系面板管理员`
        : `「${r.username}」是管理员账号，请用「管理员登录」`
      pwd.value = ''
      return
    }
    location.hash = '#/overview'
  } catch (e) {
    err.value = e.message
  } finally { busy.value = false }
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
</style>