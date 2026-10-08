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
            <!-- 管理程序：两个角色都要给。admin 名下也可能有实例（他人删除账号后
                 实例会转为归属 admin，见 delete_account），同样需要入口。 -->
            <button @click="openInsts(a)">管理程序</button>
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
        <input v-model.number="nu.login_qq" type="number" min="-1" placeholder="留空 = 1"/>
      </label>
      <label>应用端上限
        <input v-model.number="nu.app" type="number" min="-1" placeholder="留空 = 1"/>
      </label>
      <p class="hint">配额按该账号<strong>实际要用的量</strong>填，留空即各 1 个
        （一个 QQ 号 + 一个应用端）；填 -1 表示不限。</p>
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

    <!-- ===== 管理该用户的程序：浮窗 =====
         为什么用浮窗而不是塞进表格：表格已经有 6 列，再加「程序/状态/端口/操作」
         会挤到换行；浮窗能给每个实例一整行放符号按钮。
         布局与总览页的管理应用浮窗一致（点遮罩关闭），两处交互统一。 -->
    <div v-if="idlg" class="mask" @click.self="idlg = null">
      <div class="box ibox" @click.stop>
        <div class="ihead">
          <b>管理程序 · {{ idlg.username }}</b>
          <span class="dim">
            {{ idlg.instances.length }} 个实例
            <button class="lnk" @click="loadInsts">刷新</button>
          </span>
          <button @click="idlg = null">关闭</button>
        </div>
        <p v-if="ierr" class="err">{{ ierr }}</p>
        <p v-if="!idlg.instances.length && !ibusy" class="hint">
          该用户还没有实例。实例由「创建」向导生成，归该用户所有。
        </p>
        <p v-if="ibusy" class="hint">正在加载…</p>
        <table v-else-if="idlg.instances.length" class="itable">
          <thead><tr>
            <th>程序</th><th>状态</th><th>端口</th><th>QQ</th>
            <th class="th-ops">操作</th>
          </tr></thead>
          <tbody>
            <tr v-for="r in idlg.instances" :key="r.id">
              <td>
                <b>{{ r.dice }}</b>
                <span v-if="r.arch === 'allinone'" class="dim">（整合包）</span>
                <div class="dim iid">{{ r.id }}</div>
                <!-- 部署目录默认折叠：路径很长且大多数时候不需要看，
                     出问题时才展开对照。 -->
                <button class="lnk" @click="dirOpen[r.id] = !dirOpen[r.id]">
                  {{ dirOpen[r.id] ? '▾ 收起目录' : '▸ 部署目录' }}
                </button>
                <div v-if="dirOpen[r.id]" class="idir">{{ r.dir || '（未知）' }}</div>
              </td>
              <td>
                <span :class="r.live ? 'on' : 'off'">{{ r.state }}</span>
                <span v-if="r.live && r.mem_mb" class="dim"> · {{ r.mem_mb }}MB</span>
              </td>
              <td>{{ r.port || '-' }}</td>
              <td>{{ r.qq || '-' }}</td>
              <td class="th-ops">
                <button title="启动" :disabled="busy" @click="instOp(r, 'start')">▶</button>
                <button title="停止" :disabled="busy" @click="instOp(r, 'stop')">■</button>
                <button title="重启" :disabled="busy" @click="instOp(r, 'restart')">↻</button>
                <button title="查看日志" @click="instLogs(r)">日志</button>
                <button v-if="r.port" title="打开 WebUI" @click="instWebui(r)">WebUI</button>
                <button class="danger" title="删除实例" :disabled="busy"
                        @click="instDel(r)">×</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { listAccounts, createAccount, deleteAccount, setAccountQuota,
         setAccountPassword, revokeAccount, getUser,
         listUserInstances, opInstance, delInstance, instanceWebui } from '../api'

const list = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(false)
const me = ref(getUser()?.username || '')
const dlg = ref(null)
// 配额不预填：见模板里的说明。留空（undefined）时后端按「不限」处理，
// 比硬塞 3/5 更诚实 —— 预填值会被当成本来就该有的限制。
const nu = ref({ username: '', display_name: '', password: '', role: 'user' })

const fmtQ = v => (Number(v) < 0 ? '不限' : v)
// 留空 → 1（各一个）。不能直接把空值丢给后端：normalize_quota 对无法解析的值
// 会回落到 DEFAULT_QUOTA，那正是用户不想要的「凭空多出来的限制」。
const quotaVal = v => {
  const n = Number(v)
  return (v === '' || v === null || v === undefined || Number.isNaN(n)) ? 1 : n
}
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
      quota: { login_qq: quotaVal(nu.value.login_qq), app: quotaVal(nu.value.app) },
    })
    say(`账号 ${nu.value.username} 已创建`)
    nu.value = { username: '', display_name: '', password: '', role: 'user' }
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

// ---------- 管理该用户的程序 ----------
// 操作类端点（启停/删除/WebUI）复用通用实例 API，不必走 admin 前缀：
// 后端 owned_by 对管理员放行全部实例。唯一需要 admin 权限的是「按用户名列实例」。
const idlg = ref(null), ibusy = ref(false), ierr = ref('')
const dirOpen = reactive({})          // 部署目录默认折叠，按实例 id 记展开状态
const openInsts = async a => {
  idlg.value = { username: a.username, instances: [] }
  ierr.value = ''
  await loadInsts()
}
const loadInsts = async () => {
  if (!idlg.value) return
  ibusy.value = true; ierr.value = ''
  try {
    const r = await listUserInstances(idlg.value.username)
    // 期间可能已关闭或切到别的用户 → 丢弃过期结果
    if (!idlg.value || idlg.value.username !== r.username) return
    idlg.value.instances = r.instances || []
  } catch (e) {
    if (idlg.value) ierr.value = e.message || String(e)
  } finally {
    if (idlg.value) ibusy.value = false
  }
}
const instOp = async (r, op) => {
  busy.value = true
  try {
    await opInstance(r.id, op)
    say(`${r.dice}（${r.id}）已${op === 'start' ? '启动' : op === 'stop' ? '停止' : '重启'}`)
    await loadInsts()                  // 状态变了，重拉一次让表格跟上
  } catch (e) { fail(e) }
  finally { busy.value = false }
}
const instLogs = r => { location.hash = `#/logs?instance=${encodeURIComponent(r.id)}` }
const instWebui = async r => {
  busy.value = true
  try {
    const w = await instanceWebui(r.id)
    if (!w.port) { say('未发现 WebUI 端口：实例未启动，或该程序没有 WebUI。'); return }
    window.open(`http://${location.hostname}:${w.port}`, '_blank', 'noopener')
  } catch (e) { fail(e) }
  finally { busy.value = false }
}
const instDel = async r => {
  // 删的是别人的实例，比删自己的更需要明确告知：会停进程、按 manifest 决定
  // 是否保留存档目录，且不可撤销。
  if (!confirm(`删除实例「${r.dice} · ${r.id}」？\n`
             + `归属用户：${idlg.value.username}\n`
             + `会先停止进程；存档目录是否保留由该程序的配置决定。此操作不可撤销。`)) return
  busy.value = true
  try {
    // keep_save 传 true：管理员代人操作时不擅自删用户存档
    await delInstance(r.id, r.id, false, true)
    say(`实例 ${r.dice} 已删除`)
    await loadInsts()
  } catch (e) { fail(e) }
  finally { busy.value = false }
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

/* ---------- 管理程序浮窗 ----------
   复用已有的 .mask/.box，只把卡片放宽并允许纵向滚动：
   一个用户可能有十几个实例，固定高度会把后面的行挤出视口。 */
.ibox { min-width: min(760px, 92vw); max-height: 84vh; }
.ihead { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ihead b { font-size: 14px; font-weight: 500; }
.ihead .dim { flex: 1; }
.ihead .dim button { margin-left: 8px; }
.itable { width: 100%; border-collapse: collapse; }
.itable th, .itable td {
  text-align: left; padding: 7px 8px; font-size: 13px; vertical-align: top;
  border-bottom: 1px solid rgba(128,128,128,.2);
}
.itable th { font-weight: 500; opacity: .7; white-space: nowrap; }
.iid { font-size: 11px; margin: 2px 0; word-break: break-all; }
.idir {
  font-size: 11px; margin-top: 4px; padding: 4px 6px;
  background: rgba(128,128,128,.12); border-radius: 4px;
  word-break: break-all; max-width: 320px;
}
.itable .on { color: #30a46c; }
.itable .off { opacity: .65; }
.th-ops { white-space: nowrap; }
.th-ops button {
  padding: 3px 8px; margin-right: 4px; font-size: 12px;
  min-width: 30px; line-height: 1.4;
}
button.lnk {
  background: none; border: none; padding: 0; margin: 0;
  color: #2f6feb; cursor: pointer; font-size: 12px; text-decoration: underline;
}
</style>