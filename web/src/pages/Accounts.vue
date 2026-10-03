<template>
  <div class="accounts">
    <h3>账号管理</h3>
    <p class="tip">
      配额 = 该用户名下<b>登录端已登录 QQ 号总数</b>与<b>应用端（骰子端）实例数</b>的上限；
      填 <code>-1</code> 表示不限。管理员账号本身不受配额约束。
    </p>
    <div v-if="err" class="err">{{ err }}</div>
    <div v-if="msg" class="ok">{{ msg }}</div>

    <table>
      <thead>
        <tr>
          <th>用户名</th><th>显示名</th><th>角色</th>
          <th>QQ 号数</th><th>应用端数</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="a in list" :key="a.username">
          <td>
            {{ a.username }}
            <span class="me" v-if="a.username === me">(我)</span>
          </td>
          <td>{{ a.display_name }}</td>
          <td>
            <span :class="a.role">{{ a.role === 'admin' ? '管理员' : '用户' }}</span>
          </td>
          <td>
            <template v-if="a.role === 'admin'">不限</template>
            <template v-else>
              {{ a.usage.login_qq }} / {{ fmtQ(a.quota.login_qq) }}
              <span class="over" v-if="over(a, 'login_qq')">已超限</span>
            </template>
          </td>
          <td>
            <template v-if="a.role === 'admin'">不限</template>
            <template v-else>
              {{ a.usage.app }} / {{ fmtQ(a.quota.app) }}
              <span class="over" v-if="over(a, 'app')">已超限</span>
            </template>
          </td>
          <td class="acts">
            <template v-if="a.role !== 'admin'">
              <button @click="openQuota(a)">改配额</button>
              <button @click="openPwd(a)">改密码</button>
              <button @click="doRevoke(a)">强制下线</button>
              <button class="danger" @click="doDelete(a)">删除</button>
            </template>
            <span v-else class="dim">—</span>
          </td>
        </tr>
      </tbody>
    </table>

    <h4>新建账号</h4>
    <div class="form">
      <input v-model.trim="nu.username" placeholder="用户名（字母/数字/._-，2-32）"/>
      <input v-model.trim="nu.display_name" placeholder="显示名（可留空）"/>
      <input v-model="nu.password" type="password" placeholder="初始密码（至少 6 位）"/>
      <select v-model="nu.role">
        <option value="user">普通用户</option>
        <option value="admin">管理员</option>
      </select>
      <label>QQ 号上限
        <input v-model.number="nu.login_qq" type="number" min="-1" placeholder="3"/>
      </label>
      <label>应用端上限
        <input v-model.number="nu.app" type="number" min="-1" placeholder="5"/>
      </label>
      <button class="go" :disabled="busy || !nu.username || !nu.password" @click="doCreate">
        创建账号
      </button>
    </div>

    <div v-if="dlg" class="mask" @click.self="dlg = null">
      <div class="box">
        <h4>{{ dlg.kind === 'quota' ? `修改配额：${dlg.user.username}` : `修改密码：${dlg.user.username}` }}</h4>
        <template v-if="dlg.kind === 'quota'">
          <label>QQ 号上限<input v-model.number="dlg.login_qq" type="number" min="-1"/></label>
          <label>应用端上限<input v-model.number="dlg.app" type="number" min="-1"/></label>
          <p class="hint">当前占用：QQ {{ dlg.user.usage.login_qq }} · 应用端 {{ dlg.user.usage.app }}</p>
        </template>
        <template v-else>
          <label>新密码<input v-model="dlg.password" type="password" placeholder="至少 6 位"/></label>
          <p class="hint">改密后该用户所有在线会话立即失效，需重新登录。</p>
        </template>
        <div class="row">
          <button @click="dlg = null">取消</button>
          <button class="go" :disabled="busy" @click="submitDlg">确定</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { listAccounts, createAccount, deleteAccount, setAccountQuota,
         setAccountPassword, revokeAccount, getUser } from '../api'

const list = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(false)
const me = ref(getUser()?.username || '')
const dlg = ref(null)
const nu = ref({ username: '', display_name: '', password: '', role: 'user',
                 login_qq: 3, app: 5 })

const fmtQ = v => (Number(v) < 0 ? '不限' : v)
const over = (a, k) => Number(a.quota[k]) >= 0 && a.usage[k] > Number(a.quota[k])
const say = m => { msg.value = m; err.value = ''; setTimeout(() => (msg.value = ''), 3000) }
const fail = e => { err.value = e.message; msg.value = '' }

const load = async () => {
  try { list.value = await listAccounts() }
  catch (e) { fail(e) }               // 403 会由 adminApi 转成「需要管理员权限」
}
onMounted(load)

const doCreate = async () => {
  busy.value = true
  try {
    await createAccount({
      username: nu.value.username, password: nu.value.password,
      role: nu.value.role, display_name: nu.value.display_name,
      quota: { login_qq: nu.value.login_qq, app: nu.value.app },
    })
    say(`账号 ${nu.value.username} 已创建`)
    nu.value = { username: '', display_name: '', password: '', role: 'user',
                 login_qq: 3, app: 5 }
    await load()
  } catch (e) { fail(e) } finally { busy.value = false }
}

const openQuota = a => (dlg.value = { kind: 'quota', user: a,
                                     login_qq: a.quota.login_qq, app: a.quota.app })
const openPwd = a => (dlg.value = { kind: 'password', user: a, password: '' })

const submitDlg = async () => {
  if (!dlg.value) return
  const d = dlg.value
  busy.value = true
  try {
    if (d.kind === 'quota') {
      await setAccountQuota(d.user.username, { login_qq: d.login_qq, app: d.app })
      say(`${d.user.username} 的配额已更新`)
    } else {
      await setAccountPassword(d.user.username, d.password)
      say(`${d.user.username} 的密码已修改，该用户需重新登录`)
    }
    dlg.value = null
    await load()
  } catch (e) { fail(e) } finally { busy.value = false }
}

const doRevoke = async a => {
  if (!confirm(`强制下线「${a.username}」？该用户所有在线会话会立即失效。`)) return
  try { await revokeAccount(a.username); say(`已强制下线 ${a.username}`) }
  catch (e) { fail(e) }
}

const doDelete = async a => {
  if (!confirm(`删除账号「${a.username}」？\n该账号名下的实例将无人可见（不会删除实例本身）。`)) return
  try { await deleteAccount(a.username); say(`账号 ${a.username} 已删除`); await load() }
  catch (e) { fail(e) }
}
</script>

<style scoped>
.accounts { padding: 1.2rem; }
.tip { font-size: 13px; opacity: .75; margin: 0 0 1rem; line-height: 1.6; }
table { width: 100%; border-collapse: collapse; margin-bottom: 1.5rem; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid rgba(128,128,128,.2); font-size: 13px; }
th { font-weight: 500; opacity: .7; }
td.acts button { margin-right: 6px; font-size: 12px; padding: 3px 8px; }
button.danger { color: #e5484d; }
span.me { font-size: 11px; opacity: .6; margin-left: 4px; }
span.dim { opacity: .5; }
span.over { color: #e5484d; font-size: 11px; margin-left: 4px; }
.role.admin { color: #3b82f6; }
.form { display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-end; }
.form input, .form select, .box input { padding: 6px 8px; }
.form label { display: flex; flex-direction: column; font-size: 12px; gap: 3px; }
.form label input { width: 90px; }
h4 { margin: 0 0 .6rem; font-size: 14px; font-weight: 500; }
.err { color: #e5484d; margin-bottom: .6rem; }
.ok { color: #30a46c; margin-bottom: .6rem; }
.mask { position: fixed; inset: 0; background: rgba(0,0,0,.4); display: flex;
        align-items: center; justify-content: center; z-index: 10; }
.box { background: var(--bg, #fff); padding: 1.2rem; border-radius: 8px;
       min-width: 300px; display: flex; flex-direction: column; gap: 10px; }
.box label { display: flex; flex-direction: column; font-size: 13px; gap: 4px; }
.hint { font-size: 12px; opacity: .7; margin: 0; }
.row { display: flex; gap: 8px; justify-content: flex-end; }
code { background: rgba(128,128,128,.15); padding: 1px 4px; border-radius: 3px; }
</style>