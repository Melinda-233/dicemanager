# Dice-Next 接入 QQ 官方机器人（原生适配器）说明

> 面向：想给 Dice-Next 接官方机器人通道的骰主 / 运维。
> 结论先行：**Dice-Next 3.x 程序内部自带 QQ 官方机器人适配器（`type=qq_official`），
> 不需要 NapCat 之类的协议端，也不需要另外跑一个「官方↔OneBot」桥接程序。**
> 面板要做的事只有一件：把 AppID / AppSecret 写进它的 `config/adapters.json`。
>
> 本文的协议事实来自 [qq-official-bot-protocol.md](qq-official-bot-protocol.md)（2026-11-10 抓取）；
> 上游代码事实来自 Dice-Next 上游 main 分支快照（本地临时目录 `.scratch/dn/`，不入库），
> 对应上游仓库路径为 `server/src/adapter/qq_official_adapter.h`、`server/src/main.cpp`、
> `server/src/adapter/qq_official_protocol.h`。文中标「未验证」的条目不要当结论用。

---

## 1. 两种通道怎么选

Dice-Next 在面板里有两个接入通道（`manifests/dicenext.json` 的 `bot_modes`）：

| | OneBot 协议通道（`onebot`） | 官方机器人通道（`official`） |
| --- | --- | --- |
| 谁连 QQ | 协议登录端（NapCat / SnowLuma / LLBot / Lagrange），面板要配互联 | Dice-Next 自己直连官方网关 |
| 需要登录端 | 需要，且要写一份「互联配置」（WS 地址 + token） | **不需要**，也不写互联配置 |
| 账号形态 | 一个真实 QQ 号（有被风控/冻结的可能） | 开放平台机器人（AppID 身份，无 QQ 密码） |
| 消息能力 | 协议端能给的都能给（全量群消息、主动推送、撤回…） | 受官方能力限制：默认只收 @机器人 的消息、被动回复有时间/次数窗口 |
| 适合谁 | 自用群、功能要全 | 想避免协议端封号风险、能接受官方限制 |

要点：官方通道**不是**「把官方协议翻译成 OneBot 再喂给 Dice-Next」，而是在 Dice-Next 内部
直接实现官方 API（WS 网关、intents、被动回复、富媒体上传都在 C++ 适配器里）。
所以 DiceManager 侧没有、也不应该有桥接进程。

## 2. 快速上手（5 步）

1. **建机器人**：到 QQ 开放平台（<https://q.qq.com>）实名创建机器人应用，在「开发设置」
   拿到 **AppID** 与 **AppSecret**。AppSecret 只显示一次，丢了只能重新生成。
2. **配 IP 白名单**：「开发设置 → IP 白名单」填**这台服务器出口的公网 IP**（云服务器填控制台
   显示的弹性 IP；换机房/换出口要回来改，否则换 token 会被拒）。
3. **申请事件权限**：群聊 @ 消息属于 `GROUP_AND_C2C_EVENT`（intents `1<<25`），
   不是默认就有，要在开放平台申请；没申请到时 WS 会以 4013/4014 断开（见 §7）。
4. **面板向导**：创建实例 → 应用端 → 选 `dicenext` → 「接入通道」选 **QQ 官方机器人** →
   第三步填 AppID + AppSecret → 点「测试连通性」（会真的去换一次 access_token）→
   「保存官方机器人配置」。这一步只写配置文件，不启动程序。
5. **启动并在群里 @ 它**：部署 → 启动 → 把机器人拉进群（个人主体只能拉进自己是群主/管理员的群）
   → 在群里 `@机器人 /r 1d100`，或私聊它（个人主体私聊要先把自己的 QQ 号加进
   「开发体验用户」白名单）。

面板不会向你要「登录端」或「互联 token」——向导识别到官方通道后直接跳过这两步
（`services/wizard.py:46-54` 的 `next_step()` 在官方通道返回 5；`:185-221` 的第三步写配置、
第四步直接返回 `skipped`）。

## 3. 官方协议要点（够用版）

完整细节（每个字段、每个错误码的来源 URL）在 [qq-official-bot-protocol.md](qq-official-bot-protocol.md)，
这里只留接入必须知道的。

### 3.1 凭证 → access_token

```
POST https://api.bot.qq.com/app/getAppAccessToken
Content-Type: application/json

{"appId": "<机器人 AppID>", "clientSecret": "<机器人 AppSecret>"}
```

- 成功返回 `{"access_token": "...", "expires_in": "7200"}`。注意 **`expires_in` 是字符串**，
  解析必须 `int()`（`core/qqofficial.py` 同款处理）。
- **失败也返回 HTTP 200**，只能看响应体 `code`：`100001` 请求过频、`100007` AppID 无效或机器人
  被封/删、`100016` AppID 与 AppSecret 不匹配、`10004` 机器人不存在。
- 有效期 7200 秒（2 小时）；有效期内重复获取返回同一个 token；距过期 ≤60 秒时才签新 token。
- 之后所有官方 API 的请求头是 `Authorization: QQBot <access_token>`（大小写敏感）。
- 域名：官方 2026-08-10 起统一为 `api.bot.qq.com`；旧域名 `bots.qq.com` 仍可用（官方 SDK botpy
  还在用）。上游 Dice-Next 代码走 `api.bot.qq.com`
  （快照 `qq_official_adapter.h:1294`）；面板的连通性自检两个域名依次试
  （`core/qqofficial.py:23-26`）。

### 3.2 事件通道：WS 网关（本适配器用它）

1. `GET /gateway` 拿网关地址（如 `wss://api.bot.qq.com/websocket/`）；
2. 连上后服务端发 `op 10 Hello`（`heartbeat_interval: 45000` 毫秒）；
3. 客户端发 `op 2 Identify`：`token: "QQBot <access_token>"`、`intents`、`shard: [0,1]`；
4. 收 `op 0 t=READY` 得到 `session_id` 与会话信息；
5. 按 `heartbeat_interval` 发 `op 1` 心跳，服务端回 `op 11 ACK`；
6. 断线用 `op 6 Resume` 续会话，否则重新 Identify。

另一个选项是 Webhook：**必须公网 HTTPS**，且端口只能是 80/443/8080/8443，还要做 Ed25519 验签
（`X-Signature-Ed25519` / `X-Signature-Timestamp`，验签体是 `timestamp + 原始 body`）。
面板部署出来的形态给不出公网入口，所以面板写配置时固定用 `receive_mode: "gateway"`。

### 3.3 intents：按位订阅，没权限就断连

官方把事件按位掩码分组订阅。与本通道相关的位：

| 位 | 值 | 事件 |
| --- | --- | --- |
| `1<<25` | 33554432 | `GROUP_AND_C2C_EVENT`：C2C 单聊、群 @ 消息、群全量消息、加群/加好友等 |
| `1<<26` | 67108864 | `INTERACTION`：按钮/交互回调 |
| `1<<30` | 1073741824 | `PUBLIC_GUILD_MESSAGES`：频道公域消息（默认有权限） |

Dice-Next 实际 Identify 的 intents 是
`(1<<25) | (1<<26) | (1<<30) | (members ? (1<<24) : 0)`
（快照 `qq_official_protocol.h:102`，并由 `subscribeGroupMemberEvents_` 决定最后一位，
见 `qq_official_adapter.h:1351`）。

- `1<<25` / `1<<26` **需要在开放平台申请**。没申请到就 Identify，会以 `4013`（无效 intent）或
  `4014`（无权限）断开——表现就是「程序一直重连、日志里没消息」。
- `1<<24` 这一位的官方语义**未验证**（协议笔记的 intents 表里没有它），只知道它由
  `subscribe_group_member_events` 开关控制。不要据此推断具体事件名。

### 3.4 群聊与单聊事件

- `GROUP_AT_MESSAGE_CREATE`：群里 @机器人 的消息。`content` 已自动去掉 @ 前缀；
  带 `group_openid`、`author.member_openid`、`author.member_role`。**群默认只推这种。**
- `C2C_MESSAGE_CREATE`：单聊消息，从 `author.user_openid` 取会话。
- `GROUP_MESSAGE_CREATE`：群主在客户端打开「接收全量消息」后，每条群消息都推（内容里别人的 @
  会变成 `<@openid>`）。不打开就只有 @ 消息。
- `openid` 是**按 AppID 隔离**的：同一个 QQ 用户在不同机器人下 openid 不同，换 AppID 后旧 id 全部失效。

### 3.5 被动回复窗口与去重（最容易踩的坑）

机器人**不能随便主动发消息**：只能在「收到某条消息后」于窗口内回复有限次数。

| 场景 | 窗口 | 次数 |
| --- | --- | --- |
| 单聊 | 官方两处文档分别写 60 分钟 / 5 分钟（冲突；实现按 5 分钟保守） | 4 次 |
| 群聊 | 5 分钟 | 5 次 |
| 频道 | 5 分钟 | — |

- 被动回复要带 `msg_id`（同一条消息第几次回复用 `msg_seq` 自增）；也支持用 `event_id`。
- 超出窗口/次数奖励 `40034005` / `40034128`；同 `msg_id` + `msg_seq` 重复发送会失败 `40054005`。
- 同一条消息**可能重复推送**，适配器必须按 `msg_id` 去重，否则骰子会重复响应。

### 3.6 发送消息与富媒体

```
POST /v2/users/{user_openid}/messages      # 单聊
POST /v2/groups/{group_openid}/messages    # 群聊
POST /channels/{channel_id}/messages       # 频道
```

- `msg_type`：`0` 文本、`2` markdown（此时 `content` 必须为空）、`6` 输入中（仅单聊）、
  `7` 富媒体、`8` 图文卡片。
- 发图片/语音/文件**必须先上传**：`POST /v2/{users|groups}/{open_id}/files`
  （`file_type`：1 图片 / 2 视频 / 3 语音 / 4 文件，body 传 `url`），拿到 `file_info` 后再
  `msg_type=7` + `media.file_info` 发送。单聊与群聊的上传接口**互不通用**；`file_info` 有有效期。
- 群消息里带链接可能被拒（`40054010`）。
- 发消息要求机器人 WS 在线，否则 `304018`。

### 3.7 Markdown / 按钮会自动降级

官方客户端对 markdown、按钮的渲染能力不一致。Dice-Next 的处理是（快照
`qq_official_adapter.h:2175-2194`）：HTTP `400/403/422` 视为「格式被拒」，先降级到「去掉 TeX 与
交互、保留卡片」，再退到**纯文本**；连续多次被拒后进入冷却，短时间内全部走预渲染纯文本
（日志里会写「未来 10 分钟使用预渲染纯文本」）。面板写入的默认值就是最保守的
`qq_rich_replies: "off"` + `qq_interactions: "links"`。

## 4. Dice-Next 侧：配置文件与字段

### 4.1 文件位置

`config/adapters.json`（清单里的 `config_path`），结构是：

```json
{
  "adapters": [
    {
      "name": "qq_official",
      "type": "qq_official",
      "enabled": true,
      "app_id": "<机器人 AppID>",
      "app_secret": "<机器人 AppSecret>",
      "receive_mode": "gateway",
      "qq_rich_replies": "off",
      "qq_interactions": "links"
    }
  ]
}
```

> `app_secret` 是明文落盘的——这是上游的格式，面板只能把文件权限收到 `0600`
> （`adapters/dicenext.py:164`），挡不住同机 root。示例里用占位符，不要在文档/工单里贴真值。

### 4.2 字段表（上游 `main.cpp:3113-3122` 读、`:3127-3147` 写回）

| 字段（snake_case） | 取值 | 面板默认 | 说明 |
| --- | --- | --- | --- |
| `name` | 字符串 | `qq_official` | 适配器显示名，面板用保留名便于识别自己写的条目 |
| `type` | `qq_official` | 固定 | 上游据此分派到官方适配器 |
| `enabled` | bool | `true` | 关掉等于不启用 |
| `app_id` | 字符串 | 必填 | 机器人 AppID |
| `app_secret` | 字符串 | 必填 | 机器人 AppSecret |
| `qq_number` | 字符串 | 不写 | 上游会读；具体用途**未验证**，面板不伪造 |
| `receive_mode` | `gateway` \| `webhook` | `gateway` | 事件通道；webhook 需公网入口，面板形态给不出 |
| `qq_rich_replies` | `off` \| `markdown` \| `math` | `off` | 富文本回复方式（上游 `configure()` 默认即 `off`） |
| `qq_interactions` | `off` \| `links` \| `buttons` | `links` | 按钮/交互方式 |
| `force_verify_image_resource` | bool | 不写 | 强制校验图片资源（上游可读；默认与语义**未验证**） |
| `subscribe_group_member_events` | bool | 不写 | 是否订阅成员事件（对应 Identify 里的 `1<<24`） |
| `auto_acknowledge_interactions` | bool | 不写 | 自动回 ACK（上游默认 `true`） |

面板只写「最少必要 + 最保守默认」这一组；用户想开 markdown/按钮，直接在 Dice-Next 自己的
WebUI 里改，面板不会覆盖非面板写入的其他字段（但会整体重写这个文件，见 §4.3）。

### 4.3 启动时：`adapters.json` 是权威来源（会反向覆盖数据库）

上游启动流程（快照 `main.cpp:3159-3195`）：

1. 读 `config/adapters.json`，按 `id` 更新或插入内部适配器表；
2. 对「JSON 里没有的适配器行」执行删除（`:3190` `st->remove<dice::AdapterRow>(id)`）；
3. 把规范化后的条目**回写** JSON（`:3192`），所以文件里会多出 `id` 等字段。

推论（写文档/做支持时都要记住）：

- **面板直接写文件是有效路径**：程序启动时以文件为准，不用去调它的 WebUI 接口。
- **用户在 Dice-Next WebUI 里手动加的适配器，会被下一次面板写入或下次启动覆盖/删除。**
  面板保存时会先把旧 `type=qq_official` 或 `name=qq_official` 的条目过滤掉再 append
  （`adapters/dicenext.py:151-160`），这是有意的：同一个机器人只该有一条。
- 反过来，**不要把一个安装目录同时给两个实例用**：两边都改同一个 `adapters.json`，
  启动时会互相把对方的条目删掉。

鉴权与连接细节在上游代码里：换 token 走 `api.bot.qq.com` 的 `/app/getAppAccessToken`
（`qq_official_adapter.h:1294`），消息发送拼 `/v2/users|/v2/groups|/channels`
（`:2140-2166`），被动回复 `msg_id` + `msg_seq` 也在这里。

## 5. DiceManager 侧：面板做了什么

| 环节 | 落点 | 行为 |
| --- | --- | --- |
| 清单声明通道 | `manifests/dicenext.json` 的 `bot_modes` | `official` 条目声明 `needs_login: false`、`guide_url`，以及 `credentials`（AppID、AppSecret 两个字段，secret 标 `secret: true`） |
| 是否需要面板写凭证 | `adapters/dicenext.py:127` `requires_official_credentials()` | 以清单有没有 `credentials` 为唯一判据（海豹那种「在程序 WebUI 里配」的通道无需填） |
| 写凭证 | `adapters/dicenext.py:135` `configure_official()` | 组装 `type=qq_official` 条目 → 过滤旧条目 → `atomic_write_json` → `chmod 0600`；返回 `{ok, path, app_id, preview, manual}`，**不回传 secret** |
| 回读状态 | `adapters/dicenext.py:178` `official_status()` | 只回 `configured` / `app_id` / `path` |
| 向导第 3 步 | `services/wizard.py:185-221` | 官方 + 有 credentials：调 `configure_official()` 写文件，然后直接进 `CONFIGURED`；没传凭据但文件已配置则沿用（不逼用户重填 secret） |
| 向导第 4 步 | `services/wizard.py:248-254` | 官方通道直接返回 `skipped`，不写互联配置（否则会留下一条永远连不上的 OneBot 端点） |
| 连通性自检 | `api/rest.py:77` `POST /api/official/verify` → `core/qqofficial.py:49` | 用 AppID/AppSecret 换一次 access_token；永远 HTTP 200，结果看 `{ok, message, code, expires_in}`；不返回 token/secret |
| 前端表单 | `web/src/pages/Wizard.vue:682-790` | 按清单 `credentials` 动态渲染字段，`secret: true` 的用密码框；「测试连通性」调 `verifyOfficialCredentials`（`web/src/api.js:128`）；主按钮在「没保存过」时禁用 |

面板**不**接收 QQ 号、不写 OneBot 互联配置、不参与收发消息——它只负责把两个凭据放进
Dice-Next 的配置文件，剩下全由程序自己完成。

## 6. 限制与坑

1. **主体资格**：个人主体机器人暂不支持公开使用，只能拉进「自己是群主/管理员」的群；
   私聊只能给「开发体验用户号码」白名单里的人。要公开服务得走企业主体。
2. **IP 白名单**：换 token 与调 API 都要求来源 IP 在白名单里。服务器换 IP、上 CDN、
   容器出口 NAT 变化都会让它突然失效——报错通常只是「换 token 失败」。
3. **intents 权限**：`1<<25` 要申请。没权限时表现是「连接反复断开」，不是「能连但没消息」。
   权限被平台收回时也不会立刻报错，要等重连才暴露——所以「昨天还好今天收不到」先查权限。
4. **被动窗口**：默认只能被动回复；想主动推送需要平台能力 + 受频控，骰子的定时任务/主动播报
   在官方通道下不可靠。群消息 5 分钟窗口、5 次回复，长团（跑团）里连续输出容易被截断。
5. **群全量消息**：默认只收 @机器人。要靠群主在客户端打开「接收全量消息」，
   否则骰子「听不到」普通群聊（暗骰、关键词触发都失效）。
6. **富媒体/Markdown**：图片要先上传且单聊/群聊接口不通用；markdown/按钮不支持时自动降级，
   所以官方通道下骰子输出的排版会比 OneBot 通道朴素。
7. **一个目录一个实例**：`adapters.json` 启动时会被反写，同一目录给两个实例会互删条目。
8. **AppSecret 不回显**：面板只存不显示；重新生成 Secret 后必须回来重填一次，否则程序
   拿旧 Secret 换 token 会 `100016`。
9. **沙箱与正式环境**：开放平台有沙箱/正式之分，沙箱域名（`sandbox.api.sgroup.qq.com`）
   与正式域名的映射关系**未验证**；上游代码固定走正式域名，不要假定沙箱可用。

## 7. 排障表

> 先看「测试连通性」结果，再看 Dice-Next 实例日志；两边信息合起来基本能定位。

| 现象 | 可能原因 | 检查动作 |
| --- | --- | --- |
| 测试连通性报 `100016` | AppID/AppSecret 不匹配；把 QQ 互联的 AppKey 当成 AppSecret；Secret 已重置 | 回开放平台「开发设置」重新复制；确认是「机器人」页的 AppID，不是 QQ 互联应用 ID |
| 测试连通性报 `100007` / `10004` | 机器人被封禁/已删除，或 AppID 填错 | 开放平台确认机器人还在、AppID 与页面一致 |
| 测试连通性报 `100001` | 请求过频（别处也在疯狂换 token） | 等 1 分钟重试；排查是否有另一个程序/实例用同一套凭据 |
| 测试连通性报「连不上官方鉴权接口」 | 服务器出不了公网、DNS/代理问题、IP 白名单 | `curl -sS https://api.bot.qq.com/app/getAppAccessToken` 看是否通；确认出口 IP 已进白名单 |
| 程序启动后日志「连不上网关」/反复重连 | intents 未申请（4013/4014）、IP 白名单、机器人未上线/仅沙箱 | 看日志里的 WS close code；开放平台申请 `GROUP_AND_C2C_EVENT`；核对出口 IP |
| 群里 @ 了没反应 | 消息没被推送（没 @、intents 没权限、机器人不在群、个人主体未获群） | 私聊机器人试；看日志是否收到 `GROUP_AT_MESSAGE_CREATE`；确认群主身份/加群方式 |
| 普通群聊完全收不到 | 群主没开「接收全量消息」；或本来就只推 @ 消息 | 让群主在客户端打开该开关；用 @ 测试 |
| 回复报 400 / 403 | markdown/按钮被拒、内容超长、含链接（`40054010`）、富媒体未上传 | 看日志是否有「降级/预渲染纯文本」；把 `qq_rich_replies` 设 `off` 再试；图片先上传 |
| 回复报 `40034005` / `40034128` | 超出被动回复窗口/次数 | 群聊 5 分钟内最多 5 次；长输出合并成一条或缩短 |
| 回复报 `40054005` | 同 `msg_id` + `msg_seq` 重复发送 | 通常是程序重发；确认没有两个实例共用一个机器人 |
| 回复报 `304018` | 机器人 WS 不在线 | 先修连接（见上一行「连不上网关」），再发 |
| 面板保存后，Dice-Next WebUI 里手加的适配器没了 | 上游启动以 `adapters.json` 为准（`main.cpp:3159-3195`） | 属预期行为；要持久化就写文件（面板就是这么做的），别只在其 WebUI 里加 |
| 换 Secret 后突然 `100016` | 面板存的是旧 Secret，且不回显 | 向导里重填 AppSecret 并保存 |

## 8. 未验证清单（不要据此下结论）

- `qq_number` 字段在上游的具体用途。
- intents `1<<24` 的官方语义（只知道由 `subscribe_group_member_events` 控制）。
- `force_verify_image_resource` / `auto_acknowledge_interactions` 的精确行为与默认值。
- 沙箱域名与正式域名的映射关系；「个人主体是否已开放公开使用」随平台政策变化。
- 单聊被动回复窗口：官方两处文档冲突（60 分钟 vs 5 分钟），实现按 5 分钟保守。
- `READY.d.user.id` 是否等于机器人 QQ 号（AppID 不是 QQ 号）。
- 官方错误码表查不到的 `11244` / `11298`。

## 9. 相关文件

- 协议细节与来源：[qq-official-bot-protocol.md](qq-official-bot-protocol.md)
- 决策记录：[../adr/0001-dicenext-qq-official-native.md](../adr/0001-dicenext-qq-official-native.md)
- 术语表：[../../GLOSSARY.md](../../GLOSSARY.md)
- 清单：`manifests/dicenext.json`；适配器：`adapters/dicenext.py`
- 向导状态机：`services/wizard.py`；自检接口：`api/rest.py`；鉴权模块：`core/qqofficial.py`
- 前端：`web/src/pages/Wizard.vue`、`web/src/api.js`

官方文档入口：
<https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/getting-started.html>、
<https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html>、
<https://bot.q.qq.com/wiki/develop/api-v2/changelog.html>；
事件/发送/富媒体/webhook 的逐条链接见协议笔记第 10 节的表。
