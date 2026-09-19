<template>
  <div class="app">
    <nav>
      <a href="#/overview">总览</a> | <a href="#/logs">日志中心</a> |
      <a href="#/wizard">新建骰子</a>
      <template v-if="!token"> | <a href="#/login">登录</a></template>
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
import { getToken } from './api'

const token = ref(getToken())
const hash = ref(location.hash)
const onAuth = () => (token.value = getToken())
onMounted(() => {
  addEventListener('hashchange', () => (hash.value = location.hash))
  addEventListener('dm-auth', onAuth)
})
onUnmounted(() => removeEventListener('dm-auth', onAuth))
const page = computed(() => hash.value.startsWith('#/logs') ? LogCenter
  : hash.value.startsWith('#/wizard') ? Wizard
  : hash.value.startsWith('#/login') ? Login
  : token.value ? Overview
  : Login)                            // 未登录时默认落在登录页，而不是先报一堆 401
</script>
