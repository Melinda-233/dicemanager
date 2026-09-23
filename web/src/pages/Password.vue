<template>
  <div class="chpw">
    <h3>修改管理密码</h3>
    <input v-model="oldPwd" type="password" placeholder="当前密码" @keyup.enter="doChange"/>
    <input v-model="newPwd" type="password" placeholder="新密码（至少 6 位）"/>
    <input v-model="newPwd2" type="password" placeholder="再次输入新密码" @keyup.enter="doChange"/>
    <button :disabled="busy" @click="doChange">{{ busy ? '提交中…' : '修改密码' }}</button>
    <p v-if="ok" class="ok">{{ ok }}</p>
    <p v-if="err" class="err">{{ err }}</p>
  </div>
</template>
<script setup>
import { ref } from 'vue'
import { changePassword } from '../api'
const oldPwd = ref(''), newPwd = ref(''), newPwd2 = ref('')
const busy = ref(false), ok = ref(''), err = ref('')
const doChange = async () => {
  if (busy.value) return
  err.value = ''; ok.value = ''
  if (!oldPwd.value) { err.value = '请输入当前密码'; return }
  if (newPwd.value.length < 6) { err.value = '新密码至少 6 位'; return }
  if (newPwd.value !== newPwd2.value) { err.value = '两次输入的新密码不一致'; return }
  busy.value = true
  try {
    await changePassword(oldPwd.value, newPwd.value)
    ok.value = '密码已修改，本次登录凭据已自动更新；其他已登录会话将失效。'
    oldPwd.value = newPwd.value = newPwd2.value = ''
  } catch (e) { err.value = e.message }
  finally { busy.value = false }
}
</script>
<style scoped>
.chpw { max-width: 360px; margin: 60px auto; display: flex; flex-direction: column; gap: 10px; }
.ok { color: #2e7d32; }
.err { color: #e5484d; }
</style>
