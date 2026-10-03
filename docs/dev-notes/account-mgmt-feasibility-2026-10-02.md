# 账号管理功能 · 可行性评估与开发步骤（2026-10-02）

> 需求：登录分为管理员 / 用户两类。管理员管理账号列表、分配每个账号的 bot 数量；
> 用户管理自己名下的登录端与应用端。
>
> 结论先行：**可行，但不是「登录页加个角色分支」的量级，属于鉴权核心 + 数据模型的重构。**
> 现有代码没有用户/角色/配额/归属任何概念（`roadmap-2026-09-26.md:31`把「多用户」列为
> "缓，需求出现再做"），本次需求即触发该项。建议分两期交付，先把地基换掉再叠功能。

---

## 一、现状盘点（含证据）

| 维度 | 现状 | 证据 |
| --- | --- | --- |
| 鉴权模型 | **单密码 = 全局唯一 token**。凭据是一个 dict，不是用户表 | `api/auth.py:30-53`（`self._cred = d`）、`:80`（`login()` 只收 password） |
| 身份注入 | `require_auth` **返回 None**，下游拿不到"我是谁" | `api/auth.py:129-130` |
| 路由鉴权 | 34 个路由全靠 **router 级** `dependencies=[Depends(require_auth)]`，无一手写 Depends | `api/rest.py:28`，路由总数 35 |
| 前端登录态 | 等价于布尔值（token 空/非空），无用户信息 | `api/rest.py:46-49` 只返回 `{token}`；`web/src/api.js:30-31` |
| 实例归属 | `Instance` 18 个字段，**无 owner/user_id** | `core/registry.py:30-68` |
| 配额机制 | **完全不存在**（grep quota/limit/max 全部命中无关项） | 仅 `LOGIN_MAX_FAILS` / `MAX_PKG_BYTES` / `MAX_RESTARTS` |
| 登录端/应用端 | **派生计算，无落盘字段**，后端无派生函数 | `web/src/pages/Overview.vue:332-343`；测试口径 `tests/test_manifest_integrity.py:50-58` |
| 操作审计 | **零命中** | grep `audit|审计` 无结果 |
| 落盘 | 纯 JSON 文件，无数据库 | `core/atomicio.py:29-37` 唯一写入口 `atomic_write_json` |

### 值得保留的基建（不要重写）

- PBKDF2 200k轮 + `hmac.compare_digest` 恒定时间比较（`api/auth.py:22-28`）
- 原子写 + 三层锁（`core/atomicio.py`、`core/locks.py`，`instance_lock` 可重入）
- WS token 走 `Sec-WebSocket-Protocol` 子协议头，不落 access log（`api/auth.py:107-119`）
- 鉴权集中挂载：改一处`require_auth` 签名，全站生效
- 明文密码只在首次启动打印一次、不落盘（`api/auth.py:39,63` + `api/app.py:54-57`）

---

## 二、六个真正的技术难点

### 难点 1：FastAPI router 级依赖拿不到返回值（最容易踩）

`router = APIRouter(dependencies=[Depends(require_auth)])` 中，`require_auth` 的**返回值不会注入到路由函数**。
所以"把 `require_auth` 改成返回 `CurrentUser`"并不能自动让 34 个路由知道当前用户。

**方案（推荐）**：双层依赖并存。
- 保留 router 级 `require_auth` 作为粗校验（不重复解析，FastAPI 依赖有缓存）；
- 需要身份的路由显式加 `Depends(current_user)`，从 `request.state` 取已解析的用户对象。

`current_user` 内部走 `Request` + `request.state.user`（由 `require_auth` 写入），避免二次 token 查表。

### 难点 2：单 token → 多用户 token 的索引与生命周期

`self._cred["token"]` 单值贯穿 `login` / `verify_http` / `ws_handshake` / `verify_ws` 四处，全部要改为
**token → user 的查找**。同时：

- `issued_at` 是全局单一的（`api/auth.py:20,98`），不支持"单用户强制下线"。建议顺手改成 **per-session token 表**，顺便解决"无会话"问题。
- 限速是**全局单桶** `self._fails`（`api/auth.py:53`，进程内list，重启清空）。多用户后**一个用户的失败会锁死所有人**，必须改成按 `(ip, username)` 分桶。

### 难点 3：配额并发超卖

`POST /api/instances` → `wizard.create_instance`（`api/rest.py:61-69` / `services/wizard.py:21-43`）
是"计数 + 分配 iid + 写盘"的临界区，但**它不持有任何实例锁**（实例还不存在），
目前只靠 `atomic_write_json` 的进程内 `_write_lock` + `port_allocation_lock` 保护。
并发创建会突破配额。

**方案**：在 `core/locks.py` 新增 `quota_lock()`（跨进程文件锁，参照 `port_allocation_lock` `core/locks.py:77-80`），
把「配额校验 + 实例登记」包在同一临界区。rest 层与 wizard 层**双层校验**（wizard 也可能被内部调用）。

### 难点 4：登录端/应用端角色在后端不存在

配额要按角色区分计数，但后端目前没有 `is_login_program()`。
必须新增，且口径必须与前端 `Overview.vue:332-343` 和 `tests/test_manifest_integrity.py:50-58` **完全一致**
（登录端 = 出现在任意骰子端 manifest 的 `compatible_login` 里且≠ `builtin`），否则会与既有测试打架。

⚠️ 注意与"纯清单驱动、不写程序名分支"的项目铁律对齐——派生必须走 manifest，不能硬编码 `napcat in (...)`。

### 难点 5：三个已存在的越权通道

| 位置 | 问题 |
| --- | --- |
| `api/rest.py:97-135` `link_login` | 用户 A 的骰子端可关联用户 B 的登录端，且**token 自动同步**（`wizard.py::_login_account_info`） |
| `api/rest.py:493-501` 删除级联 | 删登录端会级联解除**其他用户**实例的 links |
| `api/rest.py:861` `/panel/restart` | **任何登录用户都能重启整个面板**，多用户后必须收进管理员位，否则分权无意义 |

外加 `api/ws_overview.py:119` 遍历 `registry.all()` **全量推送拓扑**，普通用户会看到别人的实例。

### 难点 6：既有测试与双副本

- `tests/test_auth_password.py:36` 硬断言 `auth.json` 字段集合 `{password_hash, salt, token, issued_at}`——**改数据模型必挂**，须同步改。
- `windows/` 是**完整独立副本**（`windows/api/auth.py`、`windows/core/registry.py` 等都在），不是引用共享。只改主目录会导致 Windows 版行为分叉。

---

## 三、需要你拍板的 3 个决策点

在动手前必须定，否则返工：

1. **"bot 数量"指什么？**
   - (A) **实例数配额**（创建时校验，校验点在 `create_instance`）——推荐，好控制
   - (B) **QQ 账号数配额**（校验点在登录完成后；但 `accounts` 由适配器 `list_accounts()` 从运行时配置**回读**，创建时不可控）
   - 建议 A，并允许按角色分别设上限（登录端 X 个 + 应用端 Y 个）

2. **登录端与应用端是否分别设配额？**
   需求文字只说"bot 数量"，但用户要管两类端。若分别设，后端必须新增角色派生（难点 4）。

3. **`official` 通道（SealDice 官方机器人，免登录端）如何计入？**
   不占登录端配额但占应用端配额。建议在 `core/quota.py` 里**显式定义**，不要隐式推导。

---

## 四、开发步骤（分两期）

### Phase 0 · 设计与决策（0.5 天）

1. 敲定上面 3 个决策点，写入本文件「决策」节。
2. 设计新 `auth.json` 结构（建议保留原字段做迁移源）：

```json
{
  "version": 2,
  "users": {
    "admin": {
      "role": "admin",
      "password_hash": "...", "salt": "...",
      "issued_at": 1234.0, "display_name": "管理员"
    },
    "alice": {
      "role": "user",
      "password_hash": "...", "salt": "...",
      "issued_at": 1234.0, "display_name": "Alice",
      "quota": {"total": 5, "login": 2, "app": 3}
    }
  },
  "sessions": { "<token>": {"username": "alice", "issued_at": 1234.0} }
}
```

3. **迁移方案（不可省）**：旧 `auth.json` 的单条凭据 → 变成 `admin` 账号；
   旧 `instances.json` 实例（无 `owner`）→ 归属 `admin`。否则升级后管理员看不到已有 bot。

### Phase 1 · 鉴权重构（不改对外行为）（1.5～2 天）

| 文件 | 改动 |
| --- | --- |
| `api/auth.py` | `Auth` 内部换模型：`users` + `sessions`；新增 `create_user/delete_user/set_quota/reset_password`；限速改按 `(ip, username)` 分桶；`verify_http` 改为**返回 `CurrentUser`**；`ws_handshake` 改为 token→user 查找并返回用户名 |
| `api/auth.py` | `require_auth` 把 `CurrentUser` 写入 `request.state.user`；新增 `current_user`（读 state）与 `require_admin` 依赖 |
| `api/rest.py` | `LoginReq` 加`username`；`/login` 返回 `{token, username, role}`；新增 `admin` 路由组（`/api/admin/accounts` CRUD） |
| `api/app.py` | 启动横幅改为打印新建账号的一次性密码（保留"仅首次打印、不落盘"契约） |
| `tests/test_auth_password.py` | 改字段集合断言 + 补多用户用例 |

**出口标准**：单管理员场景行为与改造前完全一致，`pytest` 全绿。

### Phase 2 · 数据模型与配额（1.5 天）

| 文件 | 改动 |
| --- | --- |
| `core/registry.py` | `Instance` 加 `owner: str \| None = None`；`create()` 加 `owner` 参数；新增 `count_by_owner(owner)`；`_migrate_links` 旁加 owner 迁移兜底 |
| `core/roles.py`（新建） | `is_login_program(dice, adapters)` 后端角色派生，口径对齐既有测试 |
| `core/quota.py`（新建） | 配额模型与判定，显式处理 `official` 通道边界 |
| `core/locks.py` | 新增 `quota_lock()` |
| `services/wizard.py` | `create_instance()` 透传 `owner` + 内部配额校验 |

⚠️ 遵守既有迁移模式：`Instance.__dataclass_fields__` 过滤（`core/registry.py:125-127`）会让缺字段的老实例**不崩但owner 为 None**，None 语义须定义为"管理员/系统所有"。

### Phase 3 · 越权收口（1.5～2 天）

| 文件 | 改动 |
| --- | --- |
| `api/rest.py` | 34 个路由逐个加归属校验；`list_instances` 按 owner 过滤；`link_login` 校验目标同属；删除级联按 owner 过滤；`/panel/restart` 收进管理员位 |
| `api/ws_overview.py` | 拓扑推送按 owner 过滤 |
| `api/ws_logs.py` / `api/ws_login.py` | 日志/登录事件按 owner 过滤 |
| `core/quota.py` | 顺手接入审计（`core/logutil` 可复用，roadmap 拓展 9「操作审计」正好一起做） |

### Phase 4 · 前端（1～1.5 天）

前端极轻——**9 个源文件、约 2100 行，唯一依赖是 `vue`**，没有 router/pinia/axios，改造面积极小。

| 文件 | 改动 |
| --- | --- |
| `web/src/api.js` | `login()` 改收 `{username, password}`；新增 `getRole()/getUsername()`；`setToken` 一并存 user 信息并广播（`dm-auth` 事件是唯一信号源，**漏了菜单不刷新**）；新增账号管理接口封装。⚠️ 3 处裸 fetch/XHR（`rawUpload`/`downloadLog`/`exportBackup`）不走统一封装，要单独处理 |
| `web/src/App.vue` | 导航 4 个硬编码 `<a>` 改数组 `computed` 遍历 + 按角色过滤；**`page` computed 必须加路由守卫**，否则普通用户手敲 `#/wizard` 仍能进 |
| `web/src/pages/Login.vue` | 拆管理员/用户双入口 |
| `web/src/pages/Accounts.vue`（新建） | 账号列表：增删、改配额、重置密码 |
| `web/src/pages/Password.vue` | 建议仅管理员可见（改密是全局高危操作） |
| `windows/web/src/...` | **全量同步**，否则双平台行为分叉 |

### Phase 5 · 交付前验证（项目铁律）

真实构建 + 真机验证，不能只看测试绿：

1. `cd web && npm-cli.js run build -- --emptyOutDir=false`
2. 本机 uvicorn 全量起（需 `pip install websockets`，否则 `/ws` 404）
3. 无头Edge（`--headless=new --remote-debugging-port`）+ Node22 内置 WebSocket 实测
4. hash 路由带 query（`/?ui=1#/...`）才整页导航
5. ruff + mypy + pytest 全绿后推送（`GH_MSG_FILE` 必须显式传）

---

## 五、工作量汇总

| Phase | 内容 | 人日 |
| --- | --- | --- |
| 0 | 决策 + 设计 + 迁移方案 | 0.5 |
| 1 | 鉴权重构（含 admin 路由组） | 1.5～2 |
| 2 | 数据模型 + 配额 | 1.5 |
| 3 | 越权收口（含 WS） | 1.5～2 |
| 4 | 前端 | 1～1.5 |
| 5 | 构建 + 真机验证 | 0.5 |
| — | **合计** | **6.5～8 人日** |

**若只做一期（不做普通用户自助、仅管理员配额管控）可压到 3～4 人日**，但数据模型仍要一次到位，
否则二期返工。建议不要拆成"先做半套"。

---

## 六、测试验收要点（交给测试同学）

**回归重点**：面板密码只在首次启动打印一次（不落盘）——`test_auth_password.py` 锁定，改密 `#/password` 轮换 token；
重启面板 `services/resume.py` 自动拉回 RUNNING 实例（只认 RUNNING，`DM_AUTO_RESUME=0` 可关）——多用户后要确认 resume **不会**把普通用户的实例误拉起。

**新增用例**：

| 类别 | 用例 |
| --- | --- |
| 正常 | 管理员登录 → 建账号 → 分配配额 → 用户登录 → 创建实例至配额上限 → 第 N+1 个被拒 |
| 边界 | 配额 0；配额恰好用满；`official` 通道不占登录端配额；删除实例后配额释放 |
| 异常 | 越权访问他人实例 ID（应 404/403 而非 500）；改密后旧 token 立即失效；用户被删除后其 token 失效 |
| 权限 | 普通用户访问 `/api/admin/*` → 403；普通用户 `/panel/restart` → 403；普通用户菜单不显示"账号管理"；手敲 `#/wizard` 路由被守卫拦下 |
| 并发 | 同一用户并发创建 N 个，超配额时**不得超卖**（`quota_lock` 有效性） |
| 迁移 | 旧 `auth.json` + 旧 `instances.json` 升级后：旧密码仍能登录、旧实例全部归属 admin、无数据丢失 |
| 兼容性 | 旧 token 在改密后失效；WS 三通道（overview/logs/login）鉴权正常；`windows/` 版行为一致 |

---

## 七、风险清单

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| 只做前端分权、后端仍是单token | **高危**：普通用户改菜单无用，留旧 token 可调全站 API | 后端必须先完成 Phase 1 |
| 配额校验只在 rest 层 | 内部调用绕过配额 | rest + wizard 双层校验 |
| 角色派生口径漂移 | 与 `test_manifest_integrity` 打架、统计错乱 | 派生放 `core/roles.py` 单点实现，前后端与测试共用 |
| 只改主目录漏 `windows/` | 双平台行为分叉 | 同步后在 `windows/PLATFORM_DIFF.md` 记录双份维护项 |
| 数据模型二次改造 | 二期返工 | Phase 2 一次到位（含 official 通道边界） |

---

## 八、下一步建议

1. 先回答第三节的 3 个决策点；
2. 把本功能拆成事项（Phase 0～5各一条），关联本评估文档；
3. Phase 1 完成后即可交付一个"可用但只有管理员"的可验收版本。

> 提醒：改造涉及鉴权与数据隔离，建议把「越权用例」和「迁移不丢数据」列为**上线阻断项**，
> 其余可作为常规回归。建议把本评估结论上传项目资料库，并把遗留问题（配额语义、official 通道口径）
> 创建为待确认事项。
---

## 九、实施结果（2026-10-02 收尾）

Phase 0～4 已完成并通过真机验证。**决策点落地情况**：q-0 采用 A+B 组合拦截；
q-1 双维度配额（总数 + 分别设上限）；q-2 用户可自助增删自己名下实例。

### 9.1 实际改动文件

| 层 | 文件 | 内容 |
| --- | --- | --- |
| 鉴权 | `api/auth.py` | 重写为 v2 模型（`users` + `sessions`）、`CurrentUser`、账号 CRUD、限速分桶 |
| 鉴权 | `core/roles.py`（新） | 登录端/应用端角色派生，单点实现 |
| 鉴权 | `core/quota.py`（新） | 双维度配额计数与判定 |
| 鉴权 | `core/locks.py` | 新增 `quota_lock()` 跨实例临界区 |
| 鉴权 | `core/registry.py` | `Instance.owner` + `owned_by()`（越权与不存在同 KeyError） |
| API | `api/rest.py` | 22 处端点接入身份；新增 `admin_router`、`/api/me`、`/api/logout`；归属与配额收口 |
| API | `api/ws_overview.py` / `ws_logs.py` / `ws_login.py` | 三元组握手 + 按 owner 过滤，越权 close 4404 |
| API | `services/wizard.py` | `create_instance(owner=...)` 透传归属 |
| 前端 | `web/src/api.js` / `App.vue` / `pages/Login.vue` / `pages/Accounts.vue`（新） | 登录态带身份、路由守卫、账号管理页 |

### 9.2 真机验证结果（非仅单测）

| 层 | 手段 | 结果 |
| --- | --- | --- |
| API | 真实 uvicorn + urllib 全链路 | **58/58** |
| WS | websockets 直连三通道 | **15/15** |
| 前端 | 无头 Edge + CDP，真实表单登录 | **18/18** |
| 门禁 | ruff + mypy + pytest | 全绿（详见 9.4） |

### 9.3 期间发现并修复的三个真 bug

**① P0：`Annotated[X, Depends(f)] | None` 使 HTTP 身份全部回落 admin**

写法 `user: Annotated[CurrentUser, Depends(current_user)] | None = None` 下，
FastAPI **完全不注入**（Union 在 Annotated 外不被识别），端点恒拿 `None` → `_u()` 回落
`admin_user()`。后果：HTTP 路径上归属校验、配额拦截、管理员接口全部形同虚设，
**任何登录用户等同管理员**。既有单测因走进程内直调（本就回落 admin）而全绿，
是典型的"测试与生产行为不一致"陷阱。正确写法为
`Annotated[CurrentUser | None, Depends(current_user)] = None`（Union 在内）。

**② `admin_router` 漏挂 `require_auth`**
`require_admin` 只从 `request.state.user` 读身份，写 state 的是 `require_auth`。
少挂一个 → 管理员接口恒 401。现在两个依赖都声明，并用测试锁定。

**③ `quota.usage` 遇非预期 `accounts` 形状直接 500**
`accounts` 规范形状是 `[{qq, token, port, status}]`，但历史数据/第三方适配器可能写成
裸字符串。已加防御：非 dict 元素按 `str(a)` 取值。宁可漏算也不能让 `/api/me` 挂。

### 9.4 回归防护（新增）

- `tests/test_auth_http_identity.py`（16 例）：走 **TestClient 真实 HTTP** 的身份/归属/配额断言。
  这是唯一能捕获 bug ① 的测试形态——直调测试永远发现不了。
  含 AST 静态扫描，禁止全仓库再出现 bug ① 的写法。
- `tests/test_account_quota.py`（17 例）：角色派生、配额计数、越权语义。
- `tests/test_auth_password.py`：新增 10 例多用户场景（v1 迁移保 token、用户隔离、防枚举、
  限速分桶、输入校验、删除保护、改密吊销、踢下线、列表不泄密码材料）。

### 9.5 遗留与后续

- **`windows/` 副本未同步**：`windows/api/auth.py`、`windows/core/registry.py`、
  `windows/web/src/*` 仍为单用户版本，两个平台行为已分叉。需按双份维护约定补齐。
- **`windows/` 副本同步后必须同样跑一遍 HTTP 层测试**，否则同类 P0 会在该副本复现。
- 配额文案已补"请联系管理员在账号管理中提额"指引；`official` 通道只占 app 名额。
