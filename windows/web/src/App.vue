<template>
  <div class="app">
    <nav>
      <span class="brand">DiceManager</span>
      <a href="#/overview" :class="{ on: !token || hash.startsWith('#/overview') || !hash }">总览</a>
      <a href="#/logs" :class="{ on: hash.startsWith('#/logs') }">日志中心</a>
      <a href="#/wizard" :class="{ on: hash.startsWith('#/wizard') }">新建骰子</a>
      <a v-if="!token" href="#/login" :class="{ on: hash.startsWith('#/login') }">登录</a>
      <template v-else>
        <a href="#/password" :class="{ on: hash.startsWith('#/password') }">修改密码</a>
        <span class="spacer"/>
      </template>
    </nav>
    <component :is="page" />
  </div>
</template>
<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import Overview from './pages/Overview.vue'
import LogCenter from './pages/LogCenter.vue'
import Wizard from './pages/Wizard.vue'
import Login from './pages/Login.vue'
import Password from './pages/Password.vue'
import { getToken, setToken, needsSetup } from './api'

const token = ref(getToken())
const hash = ref(location.hash)
const onAuth = () => (token.value = getToken())
const onHash = () => (hash.value = location.hash)
onMounted(async () => {
  addEventListener('hashchange', onHash)
  addEventListener('dm-auth', onAuth)
  // 首次启动 / 密码文件被移除（忘记密码时的重置办法）：即使 localStorage 还残留旧 token，
  // 也必须清掉并把路由压回登录页——否则会停在总览页（甚至 #/logs、#/wizard），
  // 而「设置管理密码」界面只在登录页里，用户永远看不到。
  try {
    if (await needsSetup()) {
      setToken('')
      if (!location.hash.startsWith('#/login')) location.hash = '#/login'
    }
  } catch { /* 探测失败不阻断渲染：登录页自身还会再探一次并给出可见提示 */ }
})
onUnmounted(() => {
  removeEventListener('hashchange', onHash)
  removeEventListener('dm-auth', onAuth)
})
// 未登录（含首次设置）时一切路由都落登录页：#/logs、#/wizard 此前不校验 token，
// 未登录也能直接停在日志中心/向导壳上，导致看不到设置密码界面。
const page = computed(() =>
  !token.value ? Login
  : hash.value.startsWith('#/logs') ? LogCenter
  : hash.value.startsWith('#/wizard') ? Wizard
  : hash.value.startsWith('#/password') ? Password
  : hash.value.startsWith('#/login') ? Login
  : Overview)
</script>
