<template>
  <div class="login">
    <h3>登录 DiceManager</h3>
    <input v-model="pwd" type="password" placeholder="管理密码（首次启动打印在服务端控制台）"
           @keyup.enter="doLogin"/>
    <button :disabled="busy" @click="doLogin">{{ busy ? '登录中…' : '登录' }}</button>
    <p v-if="err" class="err">{{ err }}</p>
  </div>
</template>
<script setup>
import { ref } from 'vue'
import { login } from '../api'
const pwd = ref(''), busy = ref(false), err = ref('')
const doLogin = async () => {
  if (!pwd.value || busy.value) return
  busy.value = true; err.value = ''
  try {
    await login(pwd.value)
    location.hash = '#/overview'
  } catch (e) { err.value = e.message }
  finally { busy.value = false }
}
</script>
<style scoped>
.login { max-width: 360px; margin: 60px auto; display: flex; flex-direction: column; gap: 10px; }
.err { color: #e5484d; }
</style>
