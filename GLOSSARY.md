# 术语表（GLOSSARY）

本仓库是单上下文（single-context）项目，术语表只有这一份，放在仓库根。
约定见 [`docs/agents/domain.md`](docs/agents/domain.md)：

- 一个概念只在这里定义一次；写代码、提 issue、写文档、评审时**沿用下表的词**，
  不要漂移到同义词（例如已有「骰子端」就不要再写「应用端/机器人端」混用）。
- 每条 = 一句人话 + 代码落点（真实存在的文件，尽量带行号）。
- 概念还没定义时，要么是你在造项目不用的词（先别用），要么是真缺口（补在这里）。

状态：**骨架 + 本次官方通道相关术语**。分组下留空的标题是后续要补的位置。

---

## A. 角色与实例（骨架，部分已定义）

| 术语 | 一句话 | 代码落点 |
| --- | --- | --- |
| 骰子端（应用端） | 真正跑骰子程序、处理消息的一端；它要连一个登录端，或者自带接入。 | `services/wizard.py:21` `create_instance(dice, arch, ...)`；清单里 `bot_modes` 决定要不要登录端 |
| 登录端 | 负责登录 QQ 并把消息通过 OneBot 等协议提供给骰子端的一端；一个登录端可同时服务多个骰子端。 | `services/wizard.py:62` `_login_account_info()`；清单字段 `compatible_login`（`manifests/dicenext.json`） |
| 实例（Instance） | 面板里的一次部署：一个目录、一组端口、一个状态，以及它与谁互联。 | `core/registry.py:31` `class Instance`；状态枚举 `core/registry.py:14` `class State` |
| 清单（Manifest） | 某个程序（dicenext、sealdice…）的元数据声明：怎么下载、怎么启动、支持哪些接入通道。 | `manifests/dicenext.json`；后端白名单透出见 `api/rest.py:1347` |
| 适配器（Adapter） | 每个程序一个的 Python 类，封装「部署 / 登录 / 写配置 / 回读账号」这些程序专属动作。 | `adapters/base.py` `class BaseAdapter`；Dice-Next 实现 `adapters/dicenext.py` |
| 向导（Wizard） | 建实例的五步状态机：选程序 → 部署 → 登录/填凭据 → 写互联配置 → 启动；支持断点续跑。 | `services/wizard.py:9` `class Wizard`；前端 `web/src/pages/Wizard.vue` |

## B. 接入通道与协议

| 术语 | 一句话 | 代码落点 |
| --- | --- | --- |
| 互联配置 | 骰子端与登录端之间那条 WS 连接的「地址 + token + 方向」，两端必须一致。 | `adapters/base.py:776` `write_conn_config()`；关联唯一键 `adapters/base.py:1040` `link_id()`；向导第 4 步 `services/wizard.py:248` |
| OneBot v11 | 国内最常见的「协议端 → 骰子程序」事件/调用标准；本仓库用它指常规协议通道（`bot_mode="onebot"`）。 | `core/registry.py:67` `bot_mode` 默认值；互联配置写入见上一行 |
| 官方机器人通道 | 骰子程序自己直连 QQ 开放平台（AppID 身份），不经过协议登录端的一条接入路径（`bot_mode="official"`）。 | `core/registry.py:67`；清单 `manifests/dicenext.json` 的 `bot_modes`；向导 `services/wizard.py:186` |
| AppID | 开放平台给机器人的编号（**不是 QQ 号**），换 token、鉴权都用它。 | 清单 `credentials` 的 `app_id`；`core/qqofficial.py:49` `verify_official_credentials()` |
| AppSecret | 机器人的密钥，只在开放平台「开发设置」显示一次；用于换 access_token。 | `adapters/dicenext.py:135` `configure_official()`（写盘后 `chmod 0600`，面板不回显） |
| 被动回复 | 官方规定「只能回复收到的消息」，且有窗口（群聊 5 分钟/5 次）与 `msg_seq` 计数；超限报 40034005/40034128。 | 上游快照 `.scratch/dn/qq_official_adapter.h:2140-2166`（`msg_id` + `nextReplySeq`）；协议细节见 `docs/notes/qq-official-bot-protocol.md` §4.7 |
| intents | 官方事件订阅的位掩码；没申请到的位会让 WS Identify 直接失败（4013/4014）。 | 上游快照 `.scratch/dn/qq_official_protocol.h:102` `intents(bool members)` |
| 官方通道降级 | 官方客户端不支持 markdown/按钮时，适配器先退到「卡片去 TeX」再退到纯文本；连续被拒后冷却 10 分钟。 | 上游快照 `.scratch/dn/qq_official_adapter.h:2175-2194` |

> 本节的 `.scratch/dn/` 是上游 Dice-Next 源码的**临时快照**（不入库）；对应上游仓库路径为
> `server/src/adapter/qq_official_adapter.h`、`server/src/adapter/qq_official_protocol.h`。
> 行号以快照为准，上游升级后需重新核对。

## C. 部署与运维（骨架，待补）

> 后续要补的词：部署状态机（`State`）、端口分配、实例归属（owner）、配额、升级通道、
> 本地离线程序包。

## D. 待确认

- 上游 `1<<24` intent 的官方语义、`qq_number` 字段用途：见
  [`docs/notes/dicenext-qq-official.md`](docs/notes/dicenext-qq-official.md) §8「未验证清单」。

## 相关文档

- 领域文档约定：[`docs/agents/domain.md`](docs/agents/domain.md)
- 架构决策记录：[`docs/adr/`](docs/adr/)
- QQ 官方机器人接入说明：[`docs/notes/dicenext-qq-official.md`](docs/notes/dicenext-qq-official.md)
