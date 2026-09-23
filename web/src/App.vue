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
import { getToken } from './api'

const token = ref(getToken())
const hash = ref(location.hash)
const onAuth = () => (token.value = getToken())
const onHash = () => (hash.value = location.hash)
onMounted(() => {
  addEventListener('hashchange', onHash)
  addEventListener('dm-auth', onAuth)
})
onUnmounted(() => {
  removeEventListener('hashchange', onHash)
  removeEventListener('dm-auth', onAuth)
})
const page = computed(() => hash.value.startsWith('#/logs') ? LogCenter
  : hash.value.startsWith('#/wizard') ? Wizard
  : hash.value.startsWith('#/login') ? Login
  : hash.value.startsWith('#/password') && token.value ? Password
  : token.value ? Overview
  : Login)                            // 未登录时默认落在登录页，而不是先报一堆 401
</script>
