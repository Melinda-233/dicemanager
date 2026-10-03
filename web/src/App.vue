<template>
  <div class="app">
    <nav>
      <span class="brand">DiceManager</span>
      <template v-for="m in visibleMenus" :key="m.hash">
        <a :href="m.hash" :class="{ on: onMenu(m) }">{{ m.label }}</a>
      </template>
      <a v-if="!token" href="#/login" :class="{ on: hash.startsWith('#/login') }">登录</a>
      <template v-else>
        <span class="who" v-if="user">{{ user.display_name || user.username }}</span>
        <a href="#/password" :class="{ on: hash.startsWith('#/password') }">修改密码</a>
        <a href="#/login" @click.prevent="onLogout">退出</a>
        <span class="spacer"/>
      </template>
    </nav>
    <div v-if="denied" class="denied">没有权限访问该页面</div>
    <component v-else :is="page" />
  </div>
</template>
<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import Overview from './pages/Overview.vue'
import LogCenter from './pages/LogCenter.vue'
import Wizard from './pages/Wizard.vue'
import Login from './pages/Login.vue'
import Password from './pages/Password.vue'
import Accounts from './pages/Accounts.vue'
import { getToken, getUser, logout, needsSetup, setToken } from './api'

const token = ref(getToken())
const user = ref(getUser())
const hash = ref(location.hash)
// 登录/退出都要刷新身份：dm-auth 是 api.js 唯一的登录态信号源，漏接菜单不会更新
const onAuth = () => { token.value = getToken(); user.value = getUser() }
const onHash = () => (hash.value = location.hash)
onMounted(async () => {
  addEventListener('hashchange', onHash)
  addEventListener('dm-auth', onAuth)
  // 首次启动 / 凭据文件被移除（忘记密码时的重置办法）：即使 localStorage 还残留旧
  // token，也必须清掉并把路由压回登录页——否则会停在总览页（甚至 #/logs、#/wizard），
  // 而「设置管理密码」界面只在登录页里，用户永远看不到。
  // 分化 C2：仅 desktop 有首启流程，server 版 needsSetup 恒为 false。
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
const onLogout = () => logout()

const admin = computed(() => !!user.value && user.value.role === 'admin')
// 菜单数据驱动：之前是 4 个硬编码 <a>，没有任何过滤点，加角色只能改结构
const MENUS = [
  { hash: '#/overview', label: '总览' },
  { hash: '#/logs',     label: '日志中心' },
  { hash: '#/wizard',   label: '新建骰子' },
  { hash: '#/accounts', label: '账号管理', adminOnly: true },
]
const visibleMenus = computed(() =>
  MENUS.filter(m => !m.adminOnly || admin.value))
const onMenu = m => hash.value.startsWith(m.hash)

// 路由守卫：之前只判 token 非空，普通用户手敲 #/accounts 也能进（后端会 403 但页面空白）
const ROUTES = {
  '#/logs':     { comp: LogCenter },
  '#/wizard':   { comp: Wizard },
  '#/password': { comp: Password, needAuth: true },
  '#/accounts': { comp: Accounts, needAuth: true, adminOnly: true },
}
const currentRoute = computed(() => {
  const h = hash.value
  const key = Object.keys(ROUTES).find(k => h.startsWith(k))
  return key ? ROUTES[key] : null
})
// denied 是**派生**状态，不是副作用：写在 computed 里赋值会让缓存与依赖关系变得不可预测
const denied = computed(() => {
  const r = currentRoute.value
  return !!r && ((r.needAuth && !token.value) || (r.adminOnly && !admin.value))
})
const page = computed(() => {
  if (hash.value.startsWith('#/login')) return Login
  const r = currentRoute.value
  if (!r) return token.value ? Overview : Login       // 未登录落登录页，而非先报 401
  if (denied.value) return Overview
  return r.comp
})
</script>
<style scoped>
.denied { padding: 2rem; color: var(--text-dim, #888); text-align: center; }
nav .who { color: var(--text-dim, #888); font-size: 13px; margin-right: 4px; }
</style>