# DiceManager Web 应用测试报告

- **测试日期**：2026-10-03
- **测试对象**：本机主版（`E:/gito_daze/dicemanager`），FastAPI + Vue3
- **测试环境**：Windows / Python 3.13 / Edge 无头（1440×900）
- **启动方式**：`DM_STATE_DIR=.dmtest_state DM_METRICS=0 uvicorn api.app:app --host 127.0.0.1 --port 8765`

---

## 一、结论速览

| 维度 | 结果 |
|---|---|
| 静态检查 | ruff ✅ / mypy 41 文件 ✅ |
| 后端测试 | pytest **292 passed, 1 skipped** ✅ |
| WS 通道测试 | `test_ws_channels.py` **10/10 passed** ✅ |
| 接口层专项（账号功能） | 89 项，**78 通过**；11 项初判失败中 **10 项为脚本预期写错**，1 项定位到真缺陷 |
| 真实浏览器 UI | 登录/权限/账号管理/响应式/可访问性 **均通过** |
| **缺陷** | **1 个 P0 越权（必须修）** + 2 个 P2 体验问题 |

**账号功能整体可用**：登录双入口、角色化菜单、路由守卫、配额双维度计数与拦截、
会话生命周期（改密轮换 / 强制下线 / 登出）、删除账号后实例归属转移 —— 全部验证通过。

---

## 二、P0 缺陷：`instance_op` 缺失归属校验（越权）

### 现象

普通用户可以对**其他用户（含管理员）的实例**执行 `start` / `stop` / `restart`。

### 复现（干净环境实测）

```
victim  实例: napcat-12fc9f80
attacker 实例: sealdice-c1d1948f

[对照组] attacker 访问 victim 实例的只读端点 → 全部正确 404
  GET    /instances/{id}/webui    -> 404 {"detail": "实例不存在: napcat-12fc9f80"}
  GET    /instances/{id}/metrics  -> 404 {"detail": "实例不存在: napcat-12fc9f80"}
  DELETE /instances/{id}          -> 404 {"detail": "实例不存在: napcat-12fc9f80"}

[问题端点] attacker 操作 victim 实例 → 全部放行
  POST /instances/{id}/start    -> 409  （本机缺二进制；鉴权已通过才走到启动）
  POST /instances/{id}/stop     -> 200 {"ok": true}          ← 越权成功
  POST /instances/{id}/restart  -> 409
```

> 409 不是拦截，是「鉴权已通过 → 进入启动逻辑 → 本机找不到可执行文件」。
> 在 Linux 生产环境（程序已部署）上，`start` 会**真的把别人的骰子启动起来**。

### 根因

`api/rest.py:576` 的 `instance_op` 拿到了 `user` 并调用了 `_u(user)`，
但**从未调用 `_own()` / `_inst_or_404()`**：

```python
@router.post("/instances/{inst_id}/{op}")
def instance_op(inst_id: str, op: str, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    user = _u(user)          # ← 身份拿到了，但没用它做归属判断
    if op not in VALID_OPS:
        raise HTTPException(404, f"未知操作: {op}")
    with instance_lock(inst_id):
        ...  # 直接停/启进程
```

而 `api/rest.py:335` 的 `_inst_or_404` 文档字符串明确写着：

> 所有按 inst_id 操作的端点都走这里，越权与不存在同样 404

**`instance_op` 是 15 个按 inst_id 操作的端点中唯一漏掉的一个。**
其余全部正确（`webui` / `metrics` / `backup` / `diagnose` / `upgrade` /
`upgrade-check` / `deploy-progress` / `wizard` / `link` / `export` / `logs/download` / `delete`）。

### 影响

- 任意登录用户可**停掉他人正在运行的骰子/登录端**（ Denial of Service）
- 可**启动他人未部署的实例**，消耗服务器端口与资源
- 可反复 `restart` 打断他人游戏会话
- 属水平越权，违反项目自身「越权与不存在同样 404，不泄露他人实例是否存在」的设计约定

### 修复方案（1 行）

```python
def instance_op(inst_id: str, op: str, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    user = _u(user)
    _inst_or_404(inst_id, user)          # ← 新增：归属校验
    if op not in VALID_OPS:
        raise HTTPException(404, f"未知操作: {op}")
```

放在 `VALID_OPS` 校验**之前**，使未知 op 也先过归属，避免用 op 探测实例存在性。
已验证补丁可无冲突应用（未落盘，等你决定）。

### 建议补充的回归测试

`tests/test_auth_http_identity.py` 已有 16 例 HTTP 层身份测试，
但覆盖的是「已正确校验」的端点，**恰好漏了 `instance_op`**。建议加：

```python
def test_instance_op_rejects_other_users_instance(client):
    """instance_op 必须校验归属：普通用户操作他人实例 → 404。"""
    # victim 建实例 → attacker 调 start/stop/restart → 三个都断言 404
```

---

## 三、P2 问题

### P2-1 登录失败提示文案错误

**现象**：密码输错时，页面提示 **「未授权」**，而非「用户名或密码错误」。

**根因**：`web/src/api.js:36` 把**所有** 401 一律当作「登录态失效」：

```javascript
if (r.status === 401) { unauthorized(); throw new Error('未授权') }
```

`POST /api/login` 密码错误返回的也是 401，于是走到 `unauthorized()`，
既抛出「未授权」，又顺带清空本地 token 并强制 `location.hash = '#/login'`。

**双重副作用**：
1. 用户看不到真实原因（后端 `detail` 里的「用户名或密码错误」被丢弃）
2. `Login.vue:42` 精心设计的**角色错配提示**（「该账号是普通用户，请用『用户登录』」）永远不会触发 ——
   因为错配时后端返回 200，前端能正常拿到 `role`；但如果将来改成 401 就会撞上这个问题。
   当前实测确认：走管理员入口登普通账号，提示是「未授权」而非设计的错配文案。

**建议**：区分「登录接口的 401」与「其他接口的 401」。

```javascript
export async function api(path, opts = {}) {
  const r = await fetch(...)
  if (r.status === 401) {
    // 登录失败要如实展示后端原因；其余 401 才当作凭据失效
    if (path === '/login') {
      let detail = '用户名或密码错误'
      try { detail = (await r.json()).detail || detail } catch {}
      const e = new Error(detail); e.status = 401; throw e
    }
    unauthorized(); throw new Error('未授权')
  }
  ...
}
```

### P2-2 密码框未包在 `<form>` 内

Edge 控制台提示 `Password field is not contained in a form`（`Login.vue:12`）。
影响：浏览器密码管理器不识别、提交时不触发回车（当前靠 `@keyup.enter` 兜住了）、
无障碍表单语义不完整。建议把登录区包进 `<form @submit.prevent="doLogin">`。

---

## 四、账号功能验证明细（全部通过）

| 能力 | 验证点 | 结果 |
|---|---|---|
| 登录 | 管理员/用户双入口、角色由服务端 token 决定 | ✅ |
| | 错误密码 / 不存在用户 → 401 且**同文案**（防账号枚举） | ✅ |
| | 限速：同 ip\|username 桶 5 次失败 → 429 | ✅ |
| 账号 CRUD | 创建、保留名 root 拒绝、非法字符拒绝、密码 <6 位拒绝、重名 409、未知角色拒绝 | ✅ 6/6 |
| | 列表含 usage 占用、**绝不含 password_hash** | ✅ |
| 权限边界 | 普通用户访问全部 6 个管理员端点 → 全 403 | ✅ 6/6 |
| | 普通用户 `POST /panel/restart` → 403 | ✅ |
| 归属隔离 | 实例列表按 owner 过滤，管理员可见全部 | ✅ |
| | 越权访问 webui/metrics/backup/diagnose/upgrade/logs → 全 404 | ✅ 9/9 |
| | 越权与不存在**同文案模板**（不泄露存在性） | ✅ |
| 配额 | 登录端**不占** app 名额（napcat+lagrange 建完 usage.app 仍为 0） | ✅ |
| | 第 2 个应用端被拦截，提示「应用端实例已达上限（1/1）」 | ✅ |
| | `-1` → 不限；非法值（字符串/None）回落默认不 500 | ✅ |
| 会话生命周期 | 自助改密 → 旧 token 立即 401、新 token 可用 | ✅ |
| | 管理员改密 → 旧密码失效；强制下线 → token 失效但可用密码重登 | ✅ |
| | 登出后 token 失效 | ✅ |
| 账号删除 | 拒删 admin、拒删最后一个管理员（`_admin_count()<=1`） | ✅ |
| | 删除后该账号无法登录，名下实例**确实转移给 admin** | ✅ |
| 前端 | 管理员菜单含「账号管理」；普通用户菜单**不含** | ✅ |
| | 普通用户手敲 `#/accounts` → 路由守卫拦截并提示 | ✅ |
| | 账号页不回显任何凭据字段 | ✅ |
| | 改配额弹窗含用户名/当前占用/两个输入框；空表单创建按钮禁用 | ✅ |

---

## 五、其他维度

### 性能（1440×900 实测）

| 指标 | 实测 | 评价 |
|---|---|---|
| FCP | **744 ms** | 优秀 |
| DOMContentLoaded | 630 ms | 优秀 |
| load | 709 ms | 优秀 |
| 首屏资源 | 159 KB / 3 个请求 | 优秀（无框架臃肿） |
| JS 主包 | 132 KB (gzip 后传输) | 良好 |

### 响应式

375 / 768 / 1440 / 1920 四档均**无横向溢出**。深色模式背景 `rgb(22,24,29)`、
前景 `rgb(230,233,238)`，对比度正常。

### 可访问性

`html[lang]` ✓、无 heading 层级跳跃 ✓、所有表单控件有可访问名 ✓、
所有按钮有可访问名 ✓、无重复 id ✓、存在可聚焦元素 ✓、
无滥用 `tabindex=-1` ✓、无 JS 运行时错误 ✓。

---

## 六、测试中发现的方法论坑（供后续复用）

1. **WS 鉴权头要用 `headers={"sec-websocket-protocol": token}`**，
   不能用 `subprotocols=[token]` —— 后者会与 `accept(subprotocol=...)` 的
   协商逻辑冲突导致握手挂起。参考项目自带 `tests/test_ws_channels.py:32`。
2. **本机 uvicorn 跑 WS 需显式 `--ws websockets`**：`requirements.txt` 里没有
   `websockets` / `wsproto`，auto 探测失败时 WS 端点**静默不响应握手**（不返回 404）。
3. **`/ws/logs` 与 `/ws/login` 的 `inst_id` 是路径参数**：`/ws/logs/{inst_id}`，
   不是 `?inst_id=`。
4. **API 层测试会堆积实例拖慢服务**：`GET /api/instances` 会对每个实例做
   `is_up` 网络探测，20+ 实例时会超时。每轮测试应用独立 `DM_STATE_DIR` 并在
   轮次间重启服务。
5. **首启动密码只在控制台打印一次**（明文不落盘，符合设计）。自动化测试应预置
   已知 `auth.json`（PBKDF2 迭代 200000），或在首次启动后立即捕获横幅。
6. **前端 token 键名**：`dm_token`（token）+ `dm_user`（身份 JSON）。
7. **登录页是双 Tab**，「管理员登录」默认只有密码框；点提交要用 `button.go`
   且排除 `.tabs` 内的按钮，否则会误点 Tab。

---

## 七、附：测试产物

- 截图：`.dmtest_shots/`（23 张，覆盖登录页、错误提示、总览、账号管理、
  权限拦截、弹窗、响应式三档、深色模式）
- 接口报告：`.dmtest_api_report.json`
- WS 报告：`.dmtest_ws_report.json`
- UI 报告：`.dmtest_ui2_report.json` / `.dmtest_ui_report.json`
- 测试脚本：`.dmtest_api.py` / `.dmtest_ws.py` / `.dmtest_ui2.js` / `.dmtest_diag.js` / `.dmtest_p0.py`
