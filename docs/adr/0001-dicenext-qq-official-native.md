# 0001. Dice-Next 走 QQ 官方机器人通道时复用其原生官方适配器

- 状态：已采纳
- 日期：2026-11-10
- 相关：`manifests/dicenext.json`、`adapters/dicenext.py`、`services/wizard.py`、
  `core/qqofficial.py`、`web/src/pages/Wizard.vue`、`docs/notes/dicenext-qq-official.md`

## 背景

DiceManager 要支持「不用协议登录端、直接接 QQ 官方机器人」的部署形态。目标程序是 Dice-Next：
它上游 3.x 的 main 分支已经**程序内置**了 QQ 官方机器人适配器
（上游路径 `server/src/adapter/qq_official_adapter.h`），官方 API 的 WS 网关、intents、
被动回复、富媒体上传、markdown 降级都在它自己的 C++ 代码里实现，配置落在
`config/adapters.json` 的一条 `type=qq_official` 条目上。

需要决定的是：面板这一层怎么把「官方通道」变成用户点得下去的流程。两条路：

1. 复用上游原生适配器，面板只写配置（本文采纳）；
2. 在 DiceManager 里自己写一个「QQ 官方 ↔ OneBot v11」桥接程序（或 sidecar 进程），
   让 Dice-Next 仍走它熟悉的 OneBot 适配器。

## 决定

**复用 Dice-Next 原生官方适配器。** 面板的职责收窄到三件事：

- 清单声明通道：`manifests/dicenext.json` 的 `bot_modes` 增加 `official`，
  声明 `needs_login: false` 与 `credentials`（AppID / AppSecret）；
- 向导写配置：`adapters/dicenext.py:135` 的 `configure_official()` 把凭据写进实例目录下的
  `config/adapters.json`（`type=qq_official`），文件权限 `0600`；
- 连通性自检：`api/rest.py:77` 的 `POST /api/official/verify` → `core/qqofficial.py:49`
  用 AppID/AppSecret 换一次 access_token，把官方 `code` 翻成人话。

面板**不**启动任何额外进程、**不**做协议转换、**不**写 OneBot 互联配置
（`services/wizard.py:248-254` 对官方通道直接返回 `skipped`）。

## 备选方案

### A. 桥接程序：官方 API ↔ OneBot v11

在 DiceManager 里维护一个进程，连官方 WS，把事件翻译成 OneBot v11 上报给 Dice-Next。

- 否决理由 1：**要重新实现一遍上游已经做完的事**——intents 位、被动回复窗口与 `msg_seq`、
  富媒体先上传、markdown/按钮降级、openid 与会话映射、去重。这些在上游 C++ 适配器里已有
  并且随官方 API 演进维护；桥接版等于把同一份协议实现抄两遍，两边都会漂移。
- 否决理由 2：**翻译层必然丢能力**。OneBot v11 没有「被动回复窗口」的概念，桥接进程只能自己
  编一套队列/重试语义；官方新增事件（如 2026-10 的 `GROUP_MESSAGE_CREATE`）也要等桥接层跟上。
- 否决理由 3：多一个常驻进程 = 多一个崩溃面、多一份日志、多一个升级不同步的版本组合。
- 否决理由 4：用户看到的能力差异无法解释——同一条 `/r` 在 OneBot 通道和官方通道表现不同，
  排障要跨三个程序。

### B. Sidecar 只做鉴权/白名单检查

只把「换 token / 判错误码」拆成 sidecar，事件仍走上游。

- 否决理由：本期已经用 `core/qqofficial.py` 在面板进程内完成了这件事（一次 HTTP POST，
  无状态、无进程），sidecar 没有收益；反而多一个部署单元和端口。

### C. 什么都不做，让用户自己去 Dice-Next WebUI 配

- 否决理由：官方适配器要求 AppID/AppSecret，且上游启动时以 `adapters.json` 为准
  **反向覆盖**内部适配器表（上游 `main.cpp:3159-3195`，`:3190` 删除 JSON 里没有的行）。
  用户在其 WebUI 里手加的条目会被下一次启动/面板写入吃掉——「让用户自己配」会变成
  一个反复失效的路径。面板写文件才是稳定入口。

## 后果

### 正面

- 少一个进程、少一层协议转换：消息路径是「官方 WS → Dice-Next 适配器 → 骰子内核」。
- 官方能力不丢：官方后续新增的事件/字段，只要上游适配器跟进，面板无需改动。
- 部署形态简单：官方通道实例不需要登录端，向导第 3/4 步直接跳过，
  不会留下一条「无人监听的 OneBot 端点」（`services/wizard.py:186-221` 的注释即为此）。
- 凭据处理边界清晰：面板只写不读（`official_status()` 只回 AppID），
  连通性自检不返回 token/secret（`core/qqofficial.py:49-103`）。

### 负面 / 代价

- **绑定 Dice-Next 的配置 schema**：`app_id`/`app_secret`/`receive_mode`/`qq_rich_replies`/
  `qq_interactions` 等键名与取值来自上游 `main.cpp:3113-3122` 的映射。上游改字段名或改
  `adapters.json` 结构，面板要跟着改；这类改动不会在面板侧报错，只会表现为「程序起来后
  没启用官方适配器」。
- **启动时反写文件**：上游会规范化并回写 `adapters.json`（`:3192`），
  因此面板不能假设自己写下的内容长期不变；也不能把同一目录给两个实例。
- **AppSecret 明文落盘**：上游格式如此，面板只能 `chmod 0600` 缩小暴露面；
  且面板不回显 secret，Secret 重置后必须重填。
- **能力受官方限制**：群默认只收 @ 消息、被动回复窗口/次数、主动消息频控——这些不是面板
  能补的，文档里必须让用户预期到（见 `docs/notes/dicenext-qq-official.md` §6）。

### 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 上游改字段名，静默失效 | `adapters/dicenext.py` 里字段集中在 `_OFFICIAL_DEFAULTS` 与 `configure_official()`；升级上游时对照 `main.cpp` 的 `rowFromConfig`/`configFromRow` |
| 用户在其 WebUI 手改被覆盖 | 保存结果里的 `manual` 文案明确告知；文档 §4.3 单独说明 |
| intents 未申请导致「连上但没消息」 | 排障表把 4013/4014 与「收不到群消息」分开列；自检只覆盖换 token，不覆盖 intents（未做的边界要写明） |
| Secret 泄露 | 不回显、文件 0600、文档示例用占位符；不在日志里打印 secret |

## 参考

- 协议事实：[../notes/qq-official-bot-protocol.md](../notes/qq-official-bot-protocol.md)
- 适配说明与排障：[../notes/dicenext-qq-official.md](../notes/dicenext-qq-official.md)
- 术语：[../../GLOSSARY.md](../../GLOSSARY.md)
- 上游快照（临时目录，不入库）：`.scratch/dn/qq_official_adapter.h`、`.scratch/dn/main.cpp`、
  `.scratch/dn/qq_official_protocol.h`
