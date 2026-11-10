# Dice-Next（骰子!Next）深度分析：架构、适配器契约、QQ 官方 Bot 现状

> 分析对象：<https://github.com/DiceZone/Dice-Next.git>
> 克隆位置：`E:\gito_daze\dicemanager\.scratch\Dice-Next`
> 提交：`614a591 chore: record successful build 973`（`--depth 1`，2026-11-10 抓取）
> 方法：只读通读源码 + 仓库文档 + GitHub API 查 release。**本文所有结论都能在下表的文件:行 里找到依据**；凡推断或未能核实的地方都显式标注「推断」/「未核实」。
> 下文源码路径均相对 Dice-Next 仓库根目录（如 `server/src/adapter/onebot_v11_adapter.h:549`）。

---

## 0. 结论摘要

1. **Dice-Next 原生支持 QQ 官方机器人，不需要桥接。** 适配器类型 `qq_official` 已内置（`server/src/common/types.h:98`），支持「网关 WebSocket」与「Webhook」两种接收模式，凭据只需 AppID + AppSecret。仓库还配套了完整仓库文档 `docs/qq-official-rich-replies.md`。
2. **桥接方案不是必需品，而是一条兼容路径。** 只有在「必须继续复用 DiceManager 现有 `adapters/dicenext.py` 的 OneBot v11 配置通道」或「需要 OneBot 生态里 Dice-Next 官方适配器没做的动作」时才有意义。
3. 若真要做桥接，OneBot v11 侧有三个**硬约束**（都已在源码核实）：
   - **id 必须是 JSON 数字**。`user_id`/`group_id`/`time` 走 `j.value(..., 0LL)`（`onebot_v11_adapter.h:1017, 1028, 1033, 1034, 934`），传入字符串会抛 `type_error`，被外层 `catch` 记一条 WARN 后**整条消息丢弃**（`onebot_v11_adapter.h:1312-1315`）。
   - **出站 id 也会被重新数字化**：`parseId()` 用 `std::stoll` 解析，非数字返回 `0`（`onebot_v11_adapter.h:1727-1730`）。所以「把 `group_openid` 直接当 `group_id` 用」会让所有出站消息发往 `group_id=0`。桥接程序**必须自建稳定的数字 id 映射表**。
   - **必须应答主动查询**：连上后 Dice-Next 立即发 `get_login_info` + `get_group_list` + `get_friend_list`，之后每 60 秒重复一次群/好友列表（`onebot_v11_adapter.h:301-304, 536, 711, 785-791`）。
4. **反向 WS 的语义容易被误读**：Dice-Next 是**服务端**，`endpoint` 填**纯端口字符串**（`std::stoi`，`onebot_v11_adapter.h:551`），桥接程序是**客户端**。且普通配置下它**不校验 access_token、不校验路径、绑定 0.0.0.0、只允许一条连接**（`onebot_v11_adapter.h:549-600, 660-712`；原因见 §3.6）。
5. OpenID ≠ QQ 号。官方 API 不会给出真实 QQ 号，因此**桥接无法伪装出真 QQ 号**；Dice-Next 自己对官方 Bot 走的是「OpenID → 内部虚拟 13 位数字 id」的绑定体系（`server/src/core/identity/identity_binding.h:64-77`）。桥接若照抄这个思路（自造数字 id）即可自洽，但 WebUI 里看到的群号/用户号会是合成值。
6. 版本：`v3.0.0-beta.973`（`server/build_counter.txt` = `3.0:973`），Linux 官方构建基于 `ubuntu:22.04` → **glibc 基线 2.35**（`cross-compile/docker/Dockerfile.linux-amd64:5`）。

---

## 1. 总体架构

### 1.1 语言 / 构建 / 依赖

| 项 | 事实 | 依据 |
| --- | --- | --- |
| 语言 | C++20（`CMAKE_CXX_STANDARD`），C 用于第三方 | `server/CMakeLists.txt` |
| 构建 | CMake ≥ 3.20，vcpkg 清单模式 | `server/CMakeLists.txt:2`、`server/vcpkg.json` |
| 版本 | `project(dice-next-server VERSION 3.0.0 ...)` + `server/build_counter.txt` = `3.0:973` | `server/CMakeLists.txt:2` |
| 关键依赖 | drogon（HTTP/WebSocket 框架）、sqlite_orm、spdlog、nlohmann-json、yaml-cpp、quickjs-ng、Lua | `BUILD.md:10`、`server/CMakeLists.txt:242-259` |
| 许可 | AGPL-3.0-or-later | `LICENSE` |
| 构建目标 | `dice-next-server`（主体）、`dice-next`（启动器/包管理器，Windows 名为 `dice-next-launcher` 但输出名 `dice-next`） | `server/CMakeLists.txt:214, 268-269` |

官方只对 **Windows 10/11 x64 + MSVC 2022** 提供构建说明（`BUILD.md`）；Linux/macOS 走 `cross-compile/` 下的 Docker + 脚本。

### 1.2 进程形态（单二进制 + start.sh + 启动器）

发布包不是裸二进制，而是「启动器 + 主体 + 资源」：

```
DiceNext-beta-.../
├── start.sh              # 仅 Linux/macOS：exec ./dice-next "$@"
├── dice-next             # 启动器（前台 supervisor，无三方 .so 依赖）
├── dice-next-server      # 真正的骰娘主体（drogon 服务）
├── lib/                  # Linux 动态 vcpkg 依赖，启动器负责塞进 LD_LIBRARY_PATH
├── i18n/  decks/  card-templates/  web/dist/  docs/
├── data/                 # helpdoc、plugins 等默认资源
└── （首次运行自动创建 config/）
```

- `start.sh` 由打包脚本生成，内容是 `exec "$ROOT/dice-next" "$@"`（`cross-compile/scripts/package-linux.sh:102-110`）。
- 启动器是**前台 supervisor**，注释原文：`Foreground POSIX supervisor. systemd/launchd watch this process, not an untracked detached successor.`（`server/src/platform/launcher_posix.cpp:1-3`）；它还会识别容器环境并关掉自更新（`launcher_posix.cpp:17-56`）。
- 启动器通过 `LD_LIBRARY_PATH` 指向包内 `lib/`（`launcher_posix.cpp:57-62`）。
- 包内 README 明确：systemd 的 `ExecStart` 应写 `start.sh` 或 `dice-next`，**不要**直接写 `dice-next-server`（`cross-compile/scripts/package-linux.sh:113-124`）。
- **运行时工作目录必须是包根/`server/`**：它按相对路径找 `config/`、`i18n/`、`data/`（`BUILD.md`、`server/src/main.cpp:667-690` 等处的相对路径）。

### 1.3 端口与 WebUI 配置接口

- HTTP/WebUI 默认 `0.0.0.0:18088`：默认值在 `server/src/config/config_manager.cpp:28`，读取在 `server/src/main.cpp:324`；配置文件 `server/config/server.json` 键为 `host/port/api_key/db_path/log_level`。README 另有「端口被占用会顺延到可用端口」的说法（`README.md:33`）。
- 适配器相关的 HTTP 接口（都在 18088 上，即 WebUI 同源）：

| 方法与路径 | 作用 | 依据 |
| --- | --- | --- |
| `GET /api/adapters` | 列出适配器（含实时 `status`/`loginId`/官方 Bot 的 `qqNumber`、`shareUrl`） | `server/src/service/api_service.h:2845-2868` |
| `POST /api/adapters` | 新建适配器；`enabled` 默认 **true**（API 层），按 `type` 分支校验 | `api_service.h:2869-2955` |
| `PUT|PATCH /api/adapters/{id}` | 修改 | `api_service.h:2956-3116` |
| `DELETE /api/adapters/{id}` | 删除 | `api_service.h:2956-3116` |
| `POST /api/adapters/{id}/test` | 连通性测试 | `api_service.h:3117` |
| `POST /api/adapters/{id}/reconnect` | 重连 | `api_service.h:3168` |
| `POST /api/adapters/{id}/openopq/{action}` | 内置登录控制 | `api_service.h:3150` |
| `POST /api/adapters/{id}/qq-official-actions` | 官方 Bot 的 `qq_*` 动作透传 | `api_service.h:6336` |
| `POST /qq-official/webhook` | **不在 `/api` 下**的开放平台回调入口 | `api_service.h:2820-2844` |
| `POST /api/adapters/qq-official/qr/start`、`GET .../qr/{sid}` | 官方机器人扫码/分享入口 | `api_service.h:2808-2819` |

> 注：`/api/*` 受 WebUI 登录态约束，这正是 webhook 路由被刻意挂在根路径的原因（源码注释在 `api_service.h:2819`：「不能放在 /api 下，否则登录拦截会把开放平台的回调挡成 401」）。

### 1.4 配置 / 数据目录

| 路径 | 内容 | 依据 |
| --- | --- | --- |
| `config/adapters.json` | 适配器表（顶层是数组，见 §2.2），是 SQLite 的**导入/导出镜像** | `server/src/main.cpp:3087-3193` |
| `config/server.json` | host/port/api_key/db_path/log_level | `server/config/server.json` |
| `config/webui.json` | 管理口令 `{"password": ""}` | `config/webui.json` |
| `config/dice.json`、`persona.json`、`backup.json`、`events.json`、`i18n.json`、`hot_reload.json` | 骰点规则、人格、备份、事件、i18n、热重载开关 | `config/` 目录 |
| `data/dice.db` | 主库（适配器行、身份绑定、群设置、日志索引…） | `server/config/server.json` 的 `db_path`、`server/src/storage/database.h:87-96` |
| `data/logs/` | `app/dice-<时间>-<pid>.log` 运行日志；`legacy-log-*.txt` 跑团记录导出；`crash_*.txt` 崩溃报告 | `server/src/common/logger.cpp:535-544`、`main.cpp:1298-1307`、`main.cpp:367` |
| `data/mod`、`data/plugins/js` | Lua mod / JS 插件 | `main.cpp:917, 1288` |
| `data/decks`、`data/rules`、`data/rulepacks`、`data/knowledge/entries.json` | 牌堆、规则、规则包、知识库 | `main.cpp:667-690` |
| `data/assets/`、`data/chat/images/` | 图片资源与聊天图片 | `main.cpp:3684-3689` |
| `data/audit/mcp.db` | MCP 审计库 | `main.cpp:3815` |

**适配器正文只存在 `data/dice.db` 里**，`adapters.json` 是启动时与 DB 同步的镜像（见 §2.3）。这一点对本项目（DiceManager 直接改 json）是决定性的。

### 1.5 日志

- spdlog，控制台 + 文件双 sink（`server/src/common/logger.h:4-5`）。
- 文件落在 **`data/logs/app/dice-<YYYYmmdd-HHMMSS>-<pid>.log`**，源码注释说明与跑团 `data/logs/*.txt` 分开（`logger.cpp:535-544`）。
- 级别由 `server.json` 的 `log_level` 控制；另有 `docs/runtime-logging.md` 描述运行期日志体系。

### 1.6 版本与发布

| 项 | 事实 | 依据 |
| --- | --- | --- |
| tag 规则 | `v<version>-beta.<build>`，如 `v3.0.0-beta.973` | `.github/workflows/release.yml:591` |
| 资产命名 | `DiceNext-beta-<ver>-<build3>-<os>-<arch>-<北京时间>.zip|tar.gz`（如 `DiceNext-beta-3.0.0-973-linux-amd64-2026-11-07-....tar.gz`） | `package.ps1:91`、`cross-compile/scripts/package-linux.sh:62`、`package-macos.sh:48` |
| 平台 | windows-amd64、windows-arm64（zip）；linux-amd64、linux-arm64（tar.gz）；macos-arm64（tar.gz，仅 Apple Silicon） | GitHub API：`v3.0.0-beta.973` 共 5 个资产 |
| Linux glibc | 构建镜像 `ubuntu:22.04` → **glibc ≥ 2.35** | `cross-compile/docker/Dockerfile.linux-amd64:5`（arm64 同） |
| 发布渠道 | GitHub Releases（Beta 通道），包内附 QQ 群反馈渠道 | `package-linux.sh:120` |

---

## 2. 适配器系统

### 2.1 类型与连接模式

`server/src/common/types.h:93-124`：

| 枚举 | 字符串（json 的 `type`） | 平台语义 | 连接形态 |
| --- | --- | --- | --- |
| `kUnknown = 0` | —（API 里会回显成 `onebot_v11`） | — | — |
| `kOneBotV11 = 1` | `onebot_v11` | QQ，经 OneBot v11 协议端（NapCat / LLOneBot / Lagrange…） | 正向 WS / 反向 WS |
| `kMilky = 2` | `milky` | QQ，经 Milky 协议端 | HTTP API + WS 事件（强制 forward） |
| `kQQOfficial = 3` | `qq_official` | **QQ 官方机器人**（开放平台） | 网关 WS / Webhook |
| `kDiscord = 4` | `discord` | Discord | Bot Token + 网关 |
| `kKook = 5` | `kook` | KOOK | Bot Token + WS |
| `kOpenOPQ = 6` | `openopq` | 内置登录（实验性，DN 自己拉起 openopq，本质还是 OneBot v11 反向 WS） | 由 `openopq.transport` 决定 |

`ConnectionMode`（`types.h:128-141`）：`0=forward_ws`、`1=reverse_ws`、`2=http`。

**没有 `onebot_v12`，没有 `guild` 独立类型**（Discord 有频道概念；官方 Bot 的频道事件复用了 `kChannel` 消息类型）。

> `http` 模式在 schema 里合法，但 OneBot v11 适配器**未实现**：`start()` 对非 forward/reverse 直接返回 false 并报 `connection mode '<x>' not implemented yet (use forward_ws or reverse_ws)`（`onebot_v11_adapter.h:87-104`）。Milky 更是被强制成 forward（`main.cpp:3102-3105`）。→ **`http` 是死值，别选。**

### 2.2 `config/adapters.json` 完整 schema

顶层是**数组**，每个元素一条适配器。权威读取逻辑是 `server/src/main.cpp:3087-3126`（`rowFromConfig`），落盘写出逻辑是 `main.cpp:3127-3157`（`configFromRow`，全蛇形）。

| 字段（文件） | 别名（API/驼峰） | 类型 | 默认值 | 适用 type | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | `id` | int | 无 | 全部 | `>0` 且库中已存在则**更新该行**，否则插入新行（`main.cpp:3171-3184`） |
| `name` | | string | `"Bot"` | 全部 | 适配器显示名；DiceManager 用 `link_id` 保证「一连多」互不覆盖 |
| `type` | | string | `"onebot_v11"` | 全部 | 见 §2.1 |
| `connection_mode` | `connectionMode` | string | `"forward_ws"` | onebot_v11 | `forward_ws` / `reverse_ws` / `http`(未实现) |
| `endpoint` | | string | `""` | onebot_v11 / milky / openopq | **正向**：`ws://host:port/`；**反向**：**纯端口号**（如 `"6700"`，`std::stoi` 解析） |
| `access_token` | `accessToken` | string | `""` | onebot_v11 / milky / discord / kook | 正向作 `Authorization: Bearer`；**反向不校验**（§3.1） |
| `enabled` | | bool | **`false`** | 全部 | 启动时只注册 `enabled=true` 的行（`main.cpp:3242-3247`），再 `startAll()`（`main.cpp:4954`） |
| `heart_api_key` | `heartApiKey` | string | `""` | openopq / milky | 内置登录与 Milky 的 BDC 骰娘 Key |
| `persona_selection` | `personaSelection` | string | `"all"` | 全部 | 人格策略 |
| `selectable_persona_ids` | `selectablePersonaIds` | array | `[]` | 全部 | 人格白名单 |
| `default_persona_id` | `defaultPersonaId` | int | `0` | 全部 | 默认人格 |
| `event_endpoint` | `eventEndpoint` | string | `""` | milky | Milky 事件 WS 地址 |
| `openopq.transport` | | string | `"forward_ws"` | openopq | 决定连接模式 |
| `openopq.account` | | string | — | openopq | 要登录的 QQ 号 |
| `openopq.console_port` | `consolePort` | int | 自动分配 | openopq | 管理端口 |
| `app_id` | `appId` | string | `""` | **qq_official** | 必填 |
| `app_secret` | `appSecret` | string | `""` | **qq_official** | 必填（只在开放平台生成时显示一次） |
| `qq_number` | `qqNumber` | string | `""` | qq_official | 展示用（OpenAPI 不返回真实 QQ 号） |
| `receive_mode` | `receiveMode` / `qqReceiveMode` | string | `"gateway"` | qq_official | `gateway` 或 `webhook`（仅 `webhook` 走 HTTP 回调） |
| `force_verify_image_resource` | `forceVerifyImageResource` | bool | `false` | qq_official | 强制校验图片资源可达性 |
| `subscribe_group_member_events` | `subscribeGroupMemberEvents` | bool | `false` | qq_official | 追加 GUILD_MEMBERS intent（`1<<24`） |
| `auto_acknowledge_interactions` | `autoAcknowledgeInteractions` | bool | **`true`** | qq_official | 自动确认按钮交互 |
| `qq_rich_replies` | `qqRichReplies` | string | `"off"` | qq_official | `off`/`markdown`/`math`（经 `qq_rich::style()` 归一） |
| `qq_interactions` | `qqInteractions` | string | `"links"` | qq_official | `off`/`links`/`buttons` |

最小可用示例（字段名与默认值均已按源码核对）：

```json
[
  {
    "name": "dicemanager",
    "type": "onebot_v11",
    "connection_mode": "reverse_ws",
    "endpoint": "6700",
    "access_token": "",
    "enabled": true
  },
  {
    "name": "qq_official",
    "type": "qq_official",
    "enabled": true,
    "app_id": "102xxxxx",
    "app_secret": "xxxxxxxx",
    "receive_mode": "gateway",
    "qq_rich_replies": "off",
    "qq_interactions": "links"
  }
]
```

> DiceManager 的 `adapters/dicenext.py:88-91, 147-148` 写的正是这套字段（`connection_mode`/`endpoint`/`enabled`/`app_id`/`app_secret`/`receive_mode`/`qq_rich_replies`/`qq_interactions`），**与 Dice-Next 当前 schema 一致**，这批字段名没有踩空。

### 2.3 导入导出语义（对 DiceManager 至关重要）

`server/src/main.cpp:3158-3193`：

1. 若库为空 / 标记需要导出 → 把 DB 全量写成 `adapters.json`。
2. 否则以 **`adapters.json` 为准**做 upsert：
   - 有 `id` 且库中存在 → `update`；
   - 否则 → `insert`（新 id）；
   - **json 里没出现的 DB 行会被删除**（`for (const auto& [id, row] : existing) if (!configuredIds.count(id)) st->remove<AdapterRow>(id);`）。
3. 归一化后再写回 json（`configFromRow`，字段名全蛇形）。

由此可得三条实践结论：

- 外部程序**可以**直接改 `adapters.json`（上游启动时以它为准覆盖内部表），但必须**整体写入完整数组**，否则未列出的适配器会被删掉。
- 写入的条目若省略 `id`，每次启动都会**新建一行**（id 递增），旧行因不在本次列表里被删掉 —— 结果上等价于重建，但 WebUI 里的适配器 id 会漂移。建议固定 `name` 由 DiceManager 去重（现状即如此），或自行持久化 `id`。
- `enabled` 缺省是 `false`（文件层），**务必显式写 `true`**，否则只是入库不启动。
- 是否需要重启才生效：**未核实**（`config/hot_reload.json` 存在，但适配器表在启动阶段读取完毕；本笔记未追踪该文件的监听范围）。

---

## 3. OneBot v11 适配器契约

源码：`server/src/adapter/onebot_v11_adapter.h`（1831 行）。

### 3.1 反向 WS（Dice-Next 当服务端）

| 项 | 行为 | 依据 |
| --- | --- | --- |
| 谁连谁 | **协议端/桥接连 Dice-Next**（Dice-Next 是服务端） | `startReverseWs()`，`:549` |
| `endpoint` 语义 | **纯端口号**，`std::stoi(endpoint_)`；不是 URL。**写 `9000` 对，写 `0.0.0.0:9000` 会 `stoi` 成 0** | `:551` |
| 监听地址 | `trantor::InetAddress(port, loopbackOnly_)`；`listenLoopback` 默认 `false` → **绑 0.0.0.0** | `:557`、`:68` |
| 监听路径 | **不校验**。握手只解析 `Authorization` 与 `Sec-WebSocket-Key` 头，`GET /` 与 `GET /ws` 都能连 | `:660-712` |
| 鉴权 | `requireToken_` 默认 **false** → **不校验 token**；为 true 时要求 `Authorization: Bearer <access_token>`，否则回 `401` | `:69`、`:697-704` |
| 非 WS 请求 | 回 `426 Upgrade Required` 并断开 | `:689-695` |
| 并发 | **同时只接受一条**主连接，多连直接 `forceClose` | `:565-583` |
| 连上后 | 立即发 `get_login_info`，并 `get_group_list` + `get_friend_list` | `:711-715` |
| 周期 | 每 **60s** 重发 `get_group_list`/`get_friend_list` | `:785-791` |
| 自检 | `get_status` 轮询**仅** `managedPeer_=true` 时启用（普通配置不会发） | `:794-806` |

### 3.2 正向 WS（Dice-Next 当客户端）

- `endpoint` 是 URL（默认 `ws://localhost:6700`，构造时去尾斜杠），路径取 `wsPath`（默认 `/`，非 `/` 开头会自动补）（`:65-73`）。
- 有 token 时加 `Authorization: Bearer <token>`（`:530-531`）。
- 断线重连带退避；连上后同样发 `get_login_info` + 列表查询（`:535-545`）。

### 3.3 入站事件格式

入口 `onRawMessage()`（`:898`），解析在 `:906-1082`：

- 仅处理 `post_type` = `message` / `message_sent`（`:910-923`）；`notice` / `request` 另行走 `handlePlatformEvent()`（`:1083-1086`）。
- 自身消息判定：`post_type == "message_sent"` **或**（`post_type=="message"` 且 `user_id == self_id`）（`:913-915`）。默认 `s_respondSelf=false` → 自身消息被忽略（`:916-923`）。
- 字段：

| 字段 | 要求 | 依据 |
| --- | --- | --- |
| `post_type` | 字符串，`message`/`message_sent`/`notice`/`request` | `:910` |
| `self_id` | 可选；用于多账号定向与自身判定 | `:914, 931` |
| `user_id` | **必须是 JSON 数字**（`j.value("user_id", 0LL)`），字符串会抛异常丢弃 | `:1017, 1028, 1033` |
| `group_id` | **必须是 JSON 数字** | `:1034` |
| `time` | **必须是数字**（秒级时间戳） | `:934` |
| `message_type` | `private` / `group` / `guild`（缺省按 `group`） | `:1013-1036` |
| `sub_type` | 只透传，不参与分流 | `:1330` |
| `message_id` / `message_seq` | 两者都认，数字或字符串都行（走 `jsonField` 兼容） | `:928-929` |
| `sender.nickname` / `sender.card` / `sender.role` | 昵称进 `senderName`，名片与角色进 `extra` | `:1043-1052` |
| `channel_id` / `guild_id` | `message_type=="guild"` 时必需 | `:1026-1031` |

- `message` 字段**两种格式都支持**（`:936-1000`）：
  - **字符串（CQ 码）**：`[CQ:at,qq=123]` 会被抽进 `atList` 并从正文剥离；`[CQ:record,...]` 抽进 `extra.records`。
  - **数组段**：`text` / `at` / `image` / `face` / `file` / `record` 六种，`at.data.qq` 可为字符串或数字，`image` 取 `url`（缺省回落 `file`）。

### 3.4 出站动作（Dice-Next 会发给协议端/桥接的 action）

`grep sendOneBotAction("...")` 全集，共 23 个：

| action | 何时发 | 依据 |
| --- | --- | --- |
| `get_login_info` | 连上、以及拿不到登录号时重试 | `:536, 711, 1263-1284` |
| `get_group_list` | 连上 + 每 60s | `:302, 785` |
| `get_friend_list` | 连上 + 每 60s | `:303, 785` |
| `get_group_info` | 群列表里缺群名时逐群反查 | `:315` |
| `get_group_member_list` | 群成员快照 | `:373` |
| `get_group_member_info` | 单成员信息 | `:281, 376, 400` |
| `get_group_msg_history` | 拉历史（`.log`/补录） | `:370` |
| `send_group_msg` | 群消息（数组段 / CQ 字符串两条路径） | `:164, 429, 434` |
| `send_private_msg` | 私聊 | `:160, 442` |
| `send_guild_channel_msg` | 频道消息 | `:170` |
| `send_group_forward_msg` | 合并转发 | `:466` |
| `upload_group_file` | 群文件（先传本地路径，失败再传 base64） | `:482, 497` |
| `set_group_kick` / `set_group_ban` / `set_group_special_title` / `set_group_card` / `set_group_name` / `set_group_leave` | 群管理 | `:287, 292, 296, 417, 422, 508` |
| `set_group_add_request` / `set_friend_add_request` | 审批 | `:227, 232` |
| `delete_friend` / `group_poke` | 好友与戳一戳 | `:330, 363` |
| `get_status` | **仅** `managedPeer_` 模式 | `:804` |

- 响应关联：按 `echo` 匹配；`status == "failed"` 或 `retcode != 0` 判失败并记 WARN（`:1093-1096`）。
- caps 声明（`:56-61`）：好友、删好友、踢人、禁言、戳一戳、合并转发、群文件、群名片、成员列表、退群 —— 全部 true。

### 3.5 消息内容格式与 @ / 图片

- 出站默认发**数组段** `[{"type":"text","data":{"text":"..."}}]`，由 `buildSegments()` 把文本里的 `[CQ:xxx]` / `[img,...]` 解析成段（`:150-153`）。
- 图片经 `imgsend::resolve()` 按「图片发送方式」转成 `base64://` 或本站 URL（`:150` 附近的 `buildSegments`）。
- 另一条路径 `sendGroupMessageCQ()` 发**字符串 CQ 码**并带 `auto_escape: false`（`:433-435`）。
- @ 提及：入站 `[CQ:at,qq=..]` 或 `at` 段统一进 `msg.atList`，正文里被剥离（`:937-948, 1000`）；出站侧由核心在文本里写 `[CQ:at,qq=..]`，再由 `buildSegments` 转成 `at` 段。
- 自身回声去重：`SelfEchoFilter` 会登记出站文本，收到的 `message_sent` 若能匹配就丢弃，避免自触发死循环（`:1090-1094`）。

### 3.6 关键限制：普通配置下 token / 回环 / 路径都不可控

`server/src/service/api_service.h:359, 414-421` 的 `makeRuntimeAdapter()` 在构造 OneBot 适配器时**只注入 5 个参数**：

```cpp
adapter->configure({{"name", a.name}, {"endpoint", a.endpoint},
                    {"accessToken", a.accessToken}, {"connectionMode", mode},
                    {"message_format", cfg.value("message_format", std::string())}});
```

也就是说 `requireToken` / `listenLoopback` / `wsPath` / `managedPeer` **永远取默认值**（false/false/"/"、false）。这三个开关只有内置登录（openopq）路径才会用到。

→ 对桥接的直接后果：

1. **任何人都能连上反向 WS**（绑 0.0.0.0、不校验 token），谁能连上谁就是骰娘。请用防火墙/安全组把该端口限制到本机或可信网段。
2. 路径随便填都行（`ws://host:6700/` 或 `/ws` 均可）。
3. 一旦有第二条连接，它会直接被踢（这反过来说也降低了被顶替的风险）。

---

## 4. QQ 官方 Bot 支持现状

### 4.1 结论：原生支持，且实现相当完整

- 类型 `qq_official`（`server/src/common/types.h:98`；字符串映射 `:104-124`）。
- 独立实现分四个文件，共约 2500+ 行：
  - `server/src/adapter/qq_official_adapter.h`（2249 行，主体）
  - `server/src/adapter/qq_official_protocol.h`（intents / 动作白名单 / 字段工具）
  - `server/src/adapter/qq_official_webhook.h`（Ed25519 验签）
  - `server/src/adapter/qq_official_name_cache.h`、`qq_gateway_socket.h`（网关 socket 封装）
- 仓库文档：`docs/qq-official-rich-replies.md`（富回复/按钮/状态卡），以及外部文档站 <https://docs.dice.zone/config/adapter.html>（`README.md:60` 引用）。
- `README.md:60` 原文：QQ 可通过 **OneBot v11**、**Milky** 或 **QQ 官方机器人**接入；`:62` 同时声明「Dice!Next 本身不是 QQ 协议端」。

### 4.2 两种接收模式

| 模式 | 配置 | 机制 | 依据 |
| --- | --- | --- | --- |
| `gateway`（默认） | `receive_mode: "gateway"` | 换 AccessToken → `GET https://api.bot.qq.com/gateway/bot` → 连 WS → `op=2 identify`（token 前缀 `QQBot `、intents、shard `[0,1]`、properties）→ 心跳 `op=1`、恢复 `op=6`、断线 5s 重连 | `qq_official_adapter.h:1331, 1351-1355` |
| `webhook` | `receive_mode: "webhook"` | 开放平台把事件 POST 到 `http://<host>:18088/qq-official/webhook`，按 `X-Bot-Appid` 头找适配器（找不到回 404），用 **Ed25519** 验签（`X-Signature-Timestamp` + 原始 body + `X-Signature-Ed25519`） | `api_service.h:2820-2844`、`qq_official_webhook.h` 全文 |

### 4.3 intents 与事件

`server/src/adapter/qq_official_protocol.h:102`：

```cpp
return (1 << 25) | (1 << 26) | (1 << 30) | (members ? (1 << 24) : 0);
```

→ `GROUP_AND_C2C_EVENT(1<<25)`、`INTERACTION(1<<26)`、`PUBLIC_GUILD_MESSAGES(1<<30)`，可选 `GUILD_MEMBERS(1<<24)`（由 `subscribe_group_member_events` 控制）。

已实现的事件（`qq_official_adapter.h:1363-1400+` 及 `dispatchEvent()`）：

| 事件 | 语义 | 映射 |
| --- | --- | --- |
| `C2C_MESSAGE_CREATE` | 私聊（`user_openid`） | `MessageType::kPrivate` |
| `GROUP_AT_MESSAGE_CREATE` | 群里 @ 机器人（`group_openid` + `member_openid`） | `kGroup`；**即使 payload 无 mentions 也当显式 @** |
| `GROUP_MESSAGE_CREATE` | 群全量消息（需群主开「接收全量消息」） | `kGroup` |
| `AT_MESSAGE_CREATE` | 频道 @ | `kChannel` |
| `GROUP_ADD_ROBOT` / `GROUP_DEL_ROBOT` | 入群/退群 | 事件 |
| `FRIEND_ADD` / `FRIEND_DEL` | 加/删好友 | 事件 |
| `GROUP_MEMBER_ADD` / `GROUP_MEMBER_REMOVE` | 群成员变动 | 事件 |
| `GROUP_MSG_RECEIVE` / `GROUP_MSG_REJECT` / `GROUP_JOIN_REQUEST` | 群消息开关与入群审批 | 事件 |

正文里的 `<@OpenID>` 会被剥离（`qq_official_adapter.h:1383-1400`）。

### 4.4 OpenID 与内部 id 映射（重点）

官方 API 只给 OpenID（`user_openid` / `member_openid` / `group_openid`），**永远不会给真实 QQ 号**。Dice-Next 的处理方式（`server/src/core/identity/identity_binding.h:64-77, 89-110`）：

- `isVirtual(id)`：长度 13 且首位是 `1` 或 `2` → 内部虚拟号。
- `observeOfficial(botId, openId, kind)`：为 OpenID 建立绑定行，分配一个**13 位虚拟数字 id** 作为 `publicId`（老数据是 `QQ:<id>` / `QQGroup:<id>` 前缀别名，会升级迁移）。
- `rawQQ()` 会剥掉 `QQ:` / `QQGroup:` 前缀。
- 结果：**上位机眼里「群号」是个 13 位虚拟数字**，Lua/JS 兼容层继续按「数字 id」跑。

这条设计正好印证：桥接方案也必须自造数字 id，否则 OneBot 适配器的 `parseId`（`onebot_v11_adapter.h:1727-1730`）会把 OpenID 解析成 `0`。

### 4.5 发送与被动回复

`sendTextTo()`（`qq_official_adapter.h:2125-2165`）：

- 目标路径：私聊 `/v2/users/<openid>/messages`、群 `/v2/groups/<openid>/messages`、频道 `/channels/<id>/messages`（`:2141-2146`）。
- 鉴权：`Authorization: QQBot <accessToken>`（`:2159`）。
- **被动回复**：若消息带 `msg_id`，则 `body.msg_id = m.id` 且 `body.msg_seq = nextReplySeq(m.id)`（自增序号，`:1538`、`:2160-2163`）；频道不加 `msg_seq`。
- **事件型被动回复**：无 `msg_id` 时用 `event_id = takePassiveEvent(...)`（`:2164-2166`），用于入群/加好友等事件的被动响应。
- 富媒体（图片/语音/视频/文件）：`msg_type=7` + `media.file_info`，先走 `/files` 上传拿 `file_info`（`:1809-1820`）。
- 输入中状态：`msg_type=6` + `input_notify`（`:448-449`）。

### 4.6 能力白名单

`qqprotocol::actions()`（`qq_official_protocol.h:103-110`）共 50 个 `qq_*` 动作，经 `POST /api/adapters/{id}/qq-official-actions` 透传（`api_service.h:6336`）。族谱：群禁言/审批/黑白名单/成员/群信息、消息撤回、流式消息、菜单、面板（panel）、频道（guild/channel）、交互（interaction）、分享链接、发送消息、输入中、唤醒、富媒体上传（`qq_media_files`/`qq_upload_prepare`/`qq_upload_part_finish`/`qq_material_*`）、网关信息。

### 4.7 已知限制（官方平台侧，非 Dice-Next 缺陷）

- 群消息默认**只有 @ 机器人的**才推送（`GROUP_AT_MESSAGE_CREATE`）；要全量得群主在客户端开启，走 `GROUP_MESSAGE_CREATE`。
- 被动回复有窗口与次数限制；主动消息有配额。**（具体数值未在本仓库源码中核实，属平台规则，详见 `docs/notes/qq-official-bot-protocol.md`）**
- 拿不到真实 QQ 号 → 依赖 QQ 号的功能（如按 QQ 号查人、跨群同人识别）只能退化为 OpenID 维度。

---

## 5. 关键源码位置（速查表）

| 主题 | 位置 |
| --- | --- |
| 适配器类型枚举 / 字符串映射 | `server/src/common/types.h:93-124` |
| 连接模式枚举 | `server/src/common/types.h:128-141` |
| 适配器工厂（含"只注入 5 个参数"的限制） | `server/src/service/api_service.h:359-421` |
| adapters.json → DB（字段与默认值） | `server/src/main.cpp:3087-3126` |
| DB → adapters.json（落盘字段名） | `server/src/main.cpp:3127-3157` |
| adapters.json 导入导出与删行语义 | `server/src/main.cpp:3158-3193` |
| 启动时只注册 enabled 行 | `server/src/main.cpp:3242-3247`、`server/src/adapter/adapter_manager.h:96-110` |
| OneBot v11 配置项与默认值 | `server/src/adapter/onebot_v11_adapter.h:64-85` |
| OneBot 模式分派 / http 未实现 | `server/src/adapter/onebot_v11_adapter.h:87-104` |
| 出站 `sendMessage`（数组段） | `server/src/adapter/onebot_v11_adapter.h:152-174` |
| 出站 send_group_msg 三条路径 | `server/src/adapter/onebot_v11_adapter.h:429-442` |
| 反向 WS 监听 | `server/src/adapter/onebot_v11_adapter.h:549-600` |
| 反向 WS 握手/鉴权/路径无关 | `server/src/adapter/onebot_v11_adapter.h:660-712` |
| 列表刷新 60s | `server/src/adapter/onebot_v11_adapter.h:785-791, 301-304` |
| 入站事件解析（含数字 id 强约束） | `server/src/adapter/onebot_v11_adapter.h:906-1082` |
| 事件异常兜底（丢消息但连接不断） | `server/src/adapter/onebot_v11_adapter.h:1312-1315` |
| `parseId()` 非数字 → 0 | `server/src/adapter/onebot_v11_adapter.h:1727-1730` |
| QQ 官方适配器 configure（字段全集） | `server/src/adapter/qq_official_adapter.h:463-492` |
| 官方网关连接/identify/resume/心跳 | `server/src/adapter/qq_official_adapter.h:1331, 1351-1355` |
| 官方入站事件分派 | `server/src/adapter/qq_official_adapter.h:1363-1400+` |
| 官方发送（被动 msg_id/msg_seq/event_id） | `server/src/adapter/qq_official_adapter.h:2125-2166` |
| 官方富媒体发送 | `server/src/adapter/qq_official_adapter.h:1809-1820` |
| 官方 intents 位 | `server/src/adapter/qq_official_protocol.h:102` |
| 官方动作白名单（50 个） | `server/src/adapter/qq_official_protocol.h:103-110` |
| Webhook 路由与 AppID 匹配 | `server/src/service/api_service.h:2820-2844` |
| Webhook Ed25519 验签 | `server/src/adapter/qq_official_webhook.h`（全文） |
| OpenID → 虚拟 13 位数字 id | `server/src/core/identity/identity_binding.h:64-77, 89-110` |
| 适配器 DB 行结构 | `server/src/storage/database.h:87-96` |
| 日志文件路径 | `server/src/common/logger.cpp:535-544` |
| start.sh 生成 | `cross-compile/scripts/package-linux.sh:102-110` |
| 发布资产命名 | `cross-compile/scripts/package-linux.sh:60-65`、`package.ps1:91` |
| Linux glibc 基线 | `cross-compile/docker/Dockerfile.linux-amd64:5` |

---

## 6. 若要桥接：Dice-Next 侧需要满足的条件

> 前提：桥接 = 「QQ 官方 Bot 事件 → OneBot v11」翻译进程。它同时扮演两个角色：
> - 对 QQ 开放平台：WS 网关客户端或 Webhook 接收方（协议细节见 `docs/notes/qq-official-bot-protocol.md`）；
> - 对 Dice-Next：**OneBot v11 反向 WS 的客户端**（主动连 `ws://<dicenext>:<port>/`）。

### 6.1 Dice-Next 侧的硬性要求（缺一不可）

| # | 要求 | 为什么 | 依据 |
| --- | --- | --- | --- |
| 1 | `connection_mode: "reverse_ws"`，`endpoint` 写**纯端口数字** | 适配器 `std::stoi` 解析端口；写 `host:port` 会变 0 | `onebot_v11_adapter.h:551` |
| 2 | `enabled: true` | 文件层默认 false，不写就不启动 | `main.cpp:3095`、`main.cpp:3242` |
| 3 | 自建 **数字 id** 映射：`group_openid ⇄ 数字 group_id`、`user_openid ⇄ 数字 user_id` | 入站数字强校验；出站 `parseId` 非数字→0 | `:1017/1032/1041`、`:1727-1730` |
| 4 | 入站 `user_id`/`group_id`/`time` 发 **JSON 数字**（不是字符串） | 字符串 → `type_error` → WARN + 丢消息 | `:1312-1315` |
| 5 | 响应 `get_login_info`（返回数字 `user_id` + `nickname`） | 连上即请求，拿不到登录号会反复重试 | `:536, 711, 1263-1284` |
| 6 | 响应 `get_group_list` / `get_friend_list`（60s 一次） | 群名/好友计数缓存；不响应会持续空转 | `:301-304, 785-791` |
| 7 | 每条 action 都要回 `{"status":"ok|failed","retcode":N,"data":...,"echo":<原样>}` | 按 `echo` 关联，失败判据是 status/retcode | `:1095-1110` |
| 8 | 只允许**一条** WS 连接（自己别开多路） | 多连会被踢 | `:565-583` |
| 9 | 群消息要带 `at` 段（`qq = 机器人数字 id`）或 `[CQ:at,qq=<机器人数字 id>]` | 群里靠 atList 判定「在叫骰娘」 | `:937-1000` |

### 6.2 最小实现范围（MVP）

**必须实现**

| 方向 | 项 |
| --- | --- |
| 入站事件 | `message`/`group`：`GROUP_AT_MESSAGE_CREATE` → `group_id`、`user_id`、`message`（数组段：`at` + `text`）、`sender.nickname`、`message_id`、`time` |
| 入站事件 | `message`/`private`：`C2C_MESSAGE_CREATE` → `user_id`、`message` |
| 出站 action | `get_login_info`、`get_group_list`、`get_friend_list`、`send_group_msg`、`send_private_msg` |
| 出站消息段 | `text` 必做；`image`（`data.file`/`data.url`）建议做；`at` 出站可降级为文本 `@昵称` |
| 未支持 action | 一律回 `retcode: 1404` + `status: "failed"`（**不要沉默**，否则调用方等 echo 超时） |

**建议实现**（否则功能残缺但不致命）

- `get_group_info`（群名反查）、`get_group_member_list` / `get_group_member_info`（成员与角色缓存）
- `send_group_forward_msg`（合并转发在 TRPG 里常用于长文本）
- `upload_group_file`（日志导出）
- `notice` 事件（入群/退群 → `group_increase`/`group_decrease`）

**无法实现（官方平台不支持，直接回失败）**

`set_group_kick`、`set_group_ban`（官方群禁言走 `qq_set_mute` 且有权限门槛）、`set_group_special_title`、`set_group_card`、`set_group_name`、`delete_friend`、`group_poke`、`get_group_msg_history`（官方不提供历史消息拉取接口）。

### 6.3 id 映射方案（推荐）

```
group_openid  "A1B2C3..."  →  2000000001  (13 位、首位 2，模仿 Dice-Next 的虚拟号风格)
member_openid "X9Y8..."    →  1000000001  (首位 1)
```

- 用**持久化 KV**（SQLite 表：`native_id`, `kind`, `openid`, `seq`），分配后永不变更；不要用哈希（会撞车且不可逆查）。
- 映射表丢了 = 群设置/人物卡/权限全部串号，务必与桥接进程一起备份。
- 群名建议由桥接在 `get_group_list` 里回填（官方 `group_openid` 本身不含群名，可用 `qq_group_info` 类接口或让用户手填）。

### 6.4 消息与媒体

- **图片**：官方发送需要「公网可访问 URL」或「先上传换 `file_info` 再 `msg_type=7`」。Dice-Next 出站默认给 `base64://` 段（`onebot_v11_adapter.h:150` 的 `buildSegments` 走 `imgsend::resolve`）——桥接**必须自己把 base64 解码后上传官方富媒体接口**，不能直传。
- **@ 提及**：入站 `<@openid>` 已被官方剥掉（`qq_official_adapter.h:1383-1400`），桥接需要补一个 `at` 段（qq=机器人的数字 id）才能让 Dice-Next 在群里认「这是叫我的」。
- **富文本**：Dice-Next 出站只给纯文本段；官方 Markdown/按钮属于 `qq_official` 适配器专有能力，桥接无法透传（除非桥接自己解析文本里的约定语法）。

### 6.5 安全（必读）

反向 WS 在普通配置下：**绑 0.0.0.0**、**不校验 access_token**、**不校验路径**、**第一条连上的就是骰娘**（§3.1/§3.6）。

- 桥接与 Dice-Next 若同机，用防火墙只放行 127.0.0.1 到该端口；
- 若必须跨机，前面套一层 TLS + 白名单（Dice-Next 自身没有 TLS 选项可用——**未核实**是否有反代场景下的官方建议）；
- 别把该端口暴露到公网：任何人都能连上并让骰娘说任意话、读群列表。

### 6.6 决策建议：桥接 vs 原生

| 维度 | 原生 `qq_official` | 桥接成 OneBot v11 |
| --- | --- | --- |
| 官方 Bot 能力 | 完整（富媒体、按钮/panel、频道、审批、流式、撤回…50 个动作） | 只用得上文本/图片，其余全丢 |
| Dice-Next 功能覆盖 | 全（身份虚拟号、群管理、日志、人物卡富回复） | 受 OneBot 适配器能力限制 |
| QQ 号语义 | 无真实 QQ 号（OpenID → 13 位虚拟号） | 同（桥接也无法造出真 QQ 号） |
| 额外进程 | 无 | 多一个常驻进程 + 一套 id 映射库 |
| 复用 DiceManager 现有通道 | 需要写 `qq_official` 条目（**`adapters/dicenext.py:136-160` 已经支持**） | 复用现成 `onebot_v11` 写入逻辑 |
| 故障面 | 小 | 大（映射、媒体上传、被动回复窗口、action 语义差异） |

**建议**：优先走原生 `qq_official`（DiceManager 只需把 AppID/AppSecret 写进 `config/adapters.json`，`receive_mode` 取 `gateway` 即免公网回调）；仅在「必须复用 OneBot 通道」或「需要 OneBot 侧特定行为」时才做桥接。

---

## 7. 未核实 / 待验证

| 项 | 状态 |
| --- | --- |
| `adapters.json` 变更是否需要重启 Dice-Next 才生效 | **未核实**（启动阶段读取；`config/hot_reload.json` 的实际监听范围未追踪） |
| 官方被动回复的窗口时长与次数上限 | **未核实**（平台规则，不在本仓库源码中；见 `docs/notes/qq-official-bot-protocol.md`） |
| `GET /api/adapters` 的鉴权细节（`server.json.api_key` 与 WebUI 口令的关系） | **未核实**（只确认路由与返回结构） |
| 反向 WS 在反向代理/TLS 前置下的官方推荐做法 | **未核实** |
| Milkey（Milky）适配器的完整契约 | 未展开（本题不需要） |
| Discord / KOOK 适配器细节 | 未展开（本题不需要） |

---

### 附：本次分析用到的外部链接

- 仓库：<https://github.com/DiceZone/Dice-Next>
- 最新 release：<https://github.com/DiceZone/Dice-Next/releases/tag/v3.0.0-beta.973>
- 官方适配器文档（README 引用）：<https://docs.dice.zone/config/adapter.html>
- OneBot v11 标准：<https://github.com/botuniverse/onebot-11>
- 兄弟笔记（官方 Bot 协议细节与事件翻译）：`docs/notes/qq-official-bot-protocol.md`
