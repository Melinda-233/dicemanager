# DiceManager 开发与运维手册

> 版本基线：仓库 `main` 分支当前状态（含 SnowLuma / Dice!Next 支持、Milky 相关字段预留、ruff+mypy+CI 工程化）。
> 本文档面向三类读者：**运维**（部署/排障）、**使用者**（理解向导与总览）、**贡献者**（新增一个骰子或登录端程序）。

---

## 目录

1. [项目定位](#1-项目定位)
2. [逻辑架构](#2-逻辑架构)
3. [数据与状态模型](#3-数据与状态模型)
4. [功能清单](#4-功能清单)
5. [HTTP / WebSocket 接口契约](#5-http--websocket-接口契约)
6. [适配模块编写规则](#6-适配模块编写规则)
7. [新增一个程序的完整流程](#7-新增一个程序的完整流程)
8. [部署与运维](#8-部署与运维)
9. [开发、测试与 CI](#9-开发测试与-ci)
10. [已知缺陷与改进清单](#10-已知缺陷与改进清单)
11. [附录](#11-附录)

---

## 1. 项目定位

DiceManager 是一个**自托管的 QQ 骰子（TRPG 骰娘）程序管理器**。它在 Linux 服务器上解决三件本来很烦的事：

| 痛点 | DiceManager 的做法 |
|---|---|
| 装一个骰子要拉包、解压、改配置、改端口、配 OneBot 连接 | 五步向导全流程托管，端口自动分配，互联配置自动写入 |
| 多开（多个 QQ 号 / 多个骰子）时端口互相抢占、目录互相覆盖 | 全局端口分配表 + 同名目录自动编号 + 每实例独立日志流 |
| 进程崩了不知道、日志散落各处、连接断了对不上 | 进程守护（崩溃自动重启 + 熔断）、日志中心、总览拓扑图实时显示链路状态 |

**它不做什么**：不实现 QQ 协议、不代做滑块验证、不改骰子程序的业务逻辑。它的职责边界是「把程序正确地跑起来并让两端连上」。

### 两类角色

系统里的每个实例都归属于两类角色之一，这是理解全部设计的钥匙：

- **骰子端**（dice）：真正干活的程序，如 SealDice、Dice!、Dice!Next、Olivadice。部分自带登录能力，部分通过 OneBot 连外部登录端。
- **登录端**（login）：负责登录 QQ 并把消息转成 OneBot 协议，如 NapCat、LLBot、SnowLuma。

两者的关联字段是 `instance.login_ref`（值就是登录端实例的 id），向导第 1 步可选，用于总览图画连线 + 两端 token 自动一致。

---

## 2. 逻辑架构

### 2.1 分层

```
┌─────────────────────────────────────────────────────────────┐
│  web/  Vue 3 SPA（无路由库，hash 手写路由）                  │
│  App.vue(导航)  pages/{Login,Overview,Wizard,LogCenter}      │
│  api.js(REST 封装 + token)  ws.js(WS 封装 + 重连/鉴权降级)   │
└───────────────────────────┬─────────────────────────────────┘
                            │ REST + 3 条 WebSocket
┌───────────────────────────▼─────────────────────────────────┐
│  api/  FastAPI 接入层                                        │
│  app.py        组装 + lifespan（恢复扫描/墓碑清理/密码横幅） │
│  rest.py       REST 端点（向导/实例/包/备份产物/manifests）  │
│  ws_overview.py 通道1：拓扑+资源水位，2s 周期                │
│  ws_logs.py     通道2：日志 tail（回放+实时+暂停+过滤）      │
│  ws_login.py    通道3：登录事件（二维码/滑块/完成）          │
│  auth.py       Bearer 鉴权 + WS 查询参数鉴权                 │
│  context.py    AppContext 依赖注入容器（模块级单例）         │
└───────────────────────────┬─────────────────────────────────┘
┌───────────────────────────▼─────────────────────────────────┐
│  services/  编排层（只做流程，不碰 IO 细节）                 │
│  wizard.py     五步向导状态机 + 端口/目录/token 编排         │
│  resume.py     面板重启后拉回 RUNNING 但已死的实例           │
└───────────────────────────┬─────────────────────────────────┘
┌───────────────────────────▼─────────────────────────────────┐
│  core/  基础设施（与业务无关，可独立测试）                   │
│  registry.py  实例注册表 + 状态机 + mtime 读缓存 + 墓碑      │
│  ports.py     端口分配表（系统占用探测 + 文件锁互斥）        │
│  process.py   进程守护（ring 缓冲/日志滚动/自动重启熔断）    │
│  packages.py  程序包缓存（魔数识别 + 完整性校验 + 元数据）   │
│  exports.py   备份产物目录（清单/单删/按天清理）             │
│  locks.py     RLock + 引用计数文件锁（可重入）               │
│  atomicio.py  原子写（mkstemp + fsync + os.replace）         │
│  logutil.py   统一日志（控制台 + 滚动文件，敏感信息只走控制台）│
└───────────────────────────┬─────────────────────────────────┘
┌───────────────────────────▼─────────────────────────────────┐
│  adapters/  适配层（每个程序一个适配器，唯一允许出现程序差异的地方）│
│  base.py      抽象基类 + 公共契约 + 下载/解压/校验             │
│  __init__.py  manifest 加载校验 + 类注册表 + 策略白名单       │
│  sealdice / napcat / llbot / shiki / olivadice / snowluma / dicenext │
└───────────────────────────┬─────────────────────────────────┘
┌───────────────────────────▼─────────────────────────────────┐
│  manifests/*.json  声明式清单（同目录 7 个，与适配器一一对应）│
└─────────────────────────────────────────────────────────────┘
```

依赖方向严格单向：`api → services → core ← adapters`。**`core/`、`api/`、`services/` 里不允许出现任何具体程序名的分支判断**——所有行为差异必须通过 manifest 字段声明、由适配器实现。

### 2.2 七个关键设计决策

理解这几条，读代码会快很多：

**① 启动命令只能由后端构建。**
前端从不传命令，`build_start_cmd(instance)` 是唯一的命令来源。这从根本上杜绝了「通过 API 执行任意命令」这一类漏洞，代价是每个新程序都要实现这个方法。

**② 适配器实例被缓存共享，绝不能存请求态。**
`api/context.py` 和 `services/wizard.py` 各维护一份 `{程序名: 适配器实例}` 缓存（进程生命周期内复用）。因此适配器里写 `self.xxx = ...` 保存某次请求的数据是线程不安全的，会串号。所有状态必须走 `instance` 参数或落盘。（历史上 NapCat 适配器就踩过这个坑。）

**③ 锁必须可重入。**
调用链普遍是「REST/services 先取 `instance_lock` → 再调 `registry.transition/update/remove`，而这些方法内部又取同一把锁」。所以 `core/locks.py` 用的是 `RLock` + 引用计数文件锁，而不是不可重入的 `Lock` + 每次新开 fd 的 `flock`（后者会自己等自己死锁）。**新增持锁代码不要另起一套。**

**④ 落盘一律原子写。**
`atomic_write_json` = 「读 → mutate 函数 → mkstemp 到同目录 → fsync → os.replace」。同目录是必须的（跨设备 `os.replace` 会失败）。读时若遇到 JSON5（LLBot 的配置带注释/尾逗号），传 `source_json5=True` 自动回退 `json5`。

**⑤ ring 存 `(seq, line)` 而不是纯字符串。**
`ManagedProcess.ring` 是 `deque[(seq, line)]`，`seq` 全局单调递增。作用：WS 通道「先回放快照、再收实时行」的天然缝隙用 seq 去重来填——回放时记下 `max_seq`，随后从队列里补给只发 `seq > max_seq` 的行。没有这个机制就会丢行或重复。

**⑥ 进程退出码语义要分清。**
常驻进程（`start`）崩溃会触发滑动窗口自动重启；一次性命令（`run_once`，如 LLBot 的 `--update`）退出码非 0 是正常的，**绝不参与熔断**。两者输出都进同一条日志流，日志中心能看到全部。

**⑦ 两端的 OneBot token 必须一致。**
由登录端生成 → 落盘到 `instance.conn_token` → 骰子端经 `login_ref` 继承同一个值。SnowLuma 这类自行生成 token 的登录端，通过可选的 `get_conn_token()` 钩子被 `ws_overview` 周期回读。

---

## 3. 数据与状态模型

### 3.1 磁盘布局

| 路径 | 内容 | 可覆盖的环境变量 |
|---|---|---|
| `/var/lib/dicemanager/instances.json` | 实例注册表（含墓碑记录） | `DM_STATE_DIR` |
| `/var/lib/dicemanager/ports.json` | 端口分配表 `{端口: "实例id:角色"}` | `DM_STATE_DIR` |
| `/var/lib/dicemanager/auth.json` | 管理凭据（密码哈希 + API token） | `DM_STATE_DIR` |
| `/var/lib/dicemanager/packages/` | 程序包缓存 `<dice>.<ext>` + `<dice>.meta.json` | `DM_STATE_DIR` |
| `/var/lib/dicemanager/exports/` | 备份产物：手动导出 / 升级前快照 `*-preupgrade-*` / 定时备份 `*-sched-*`。只增不减，需定期在向导「备份文件」清理 | `DM_STATE_DIR` |
| `/var/log/dicemanager/<实例id>.log` | 每实例独立日志（50MB 滚动 + 7 天保留） | `DM_LOG_DIR` |
| `/var/log/dicemanager/<实例id>.log.1` | 上一代日志副本（重启后用于回填 ring） | `DM_LOG_DIR` |
| `/var/log/dicemanager/dicemanager.log` | 管理器自身日志（10MB × 3 滚动） | `DM_LOG_DIR` |
| `/tmp/dicemanager/*.lock` | 文件锁 | `DM_LOCK_DIR` |
| `/opt/<程序名>[-2...]` | 实例安装目录 | 由 manifest `install_root` 决定 |

### 3.2 状态机

```
        ┌──────────────┐
        │  UNDEPLOYED  │◄────────────┐
        └──────┬───────┘             │
               │ step2               │
        ┌──────▼───────┐             │
        │  DEPLOYING   │─────────────┤
        └──────┬───────┘             │
               │ 部署完成            │
        ┌──────▼───────┐             │
   ┌───►│ AWAIT_LOGIN  │─────────────┤
   │    └──────┬───────┘             │
   │           │ step3 / 无需登录    │ 任意状态 → ERROR
   │    ┌──────▼───────┐             │ （特批，不受迁移表约束）
   │    │  CONFIGURED  │─────────────┤
   │    └──────┬───────┘             │
   │           │ step5               │
   │    ┌──────▼───────┐             │
   └────┤   RUNNING    │─────────────┘
        └──────────────┘
```

| 当前状态 | 允许迁移到 |
|---|---|
| `UNDEPLOYED` | `DEPLOYING`, `AWAIT_LOGIN` |
| `DEPLOYING` | `AWAIT_LOGIN`, `CONFIGURED`, `UNDEPLOYED` |
| `AWAIT_LOGIN` | `CONFIGURED`, `RUNNING`, `UNDEPLOYED` |
| `CONFIGURED` | `RUNNING`, `UNDEPLOYED` |
| `RUNNING` | `CONFIGURED`, `UNDEPLOYED` |
| `ERROR` | `DEPLOYING`, `AWAIT_LOGIN`, `UNDEPLOYED` |

非法迁移会抛 `ValueError("非法迁移 X → Y")`。`ERROR` 是特批目标（任何状态都能进），用于适配器报告不可恢复的错误；出边允许从错误恢复——重跑部署（→ `DEPLOYING`）、重跑登录（→ `AWAIT_LOGIN`）或直接回滚（→ `UNDEPLOYED`）。

**断点续跑映射**（`Wizard.next_step`，供前端「继续」按钮用）：

| 状态 | 下一步 |
|---|---|
| `UNDEPLOYED` / `DEPLOYING` | 2（部署） |
| `AWAIT_LOGIN` | 3（登录） |
| 其它 | 5（启动） |

### 3.3 实例记录字段

| 字段 | 说明 |
|---|---|
| `id` | `<程序名>-<8位随机>`，全系统唯一，同时用作日志文件名与端口 owner |
| `dice` | 程序名（manifest 的 `name`） |
| `arch` | `standalone` / `allinone` |
| `dir` | 安装目录绝对路径 |
| `port` | 主端口（引导用途，通常 = `allocated_ports["webui"]`） |
| `actual_port` | **实际**端口，由 WS 周期从日志回读（NapCat 端口被占会自动 +1） |
| `allocated_ports` | `{角色: 端口}`，角色 ∈ `webui` / `ob11` / `milky` / `satori` |
| `qq` | 已登录的 QQ 号，配置文件或日志回读落盘 |
| `login_ref` | 关联的登录端实例 id |
| `state` | 见状态机 |
| `warnings` | 部署期告警（如 Olivadice 缺子模块），总览图会画出来 |
| `webui_token` | 从启动日志回读的 WebUI 令牌 |
| `conn_token` / `conn_addr` / `conn_direction` | 互联配置三要素（两端一致性的持久化落点） |
| `first_run_done` | 首启一次性动作是否已执行（如 LLBot `--update`） |
| `created_at` | 创建时间 |

### 3.4 墓碑

删除实例不是真删记录，而是把记录改名成 `__tombstone__<id>` 并加 `removed_at`，保留 30 天，防止「删掉再建时端口/目录被误分配」。清理发生在**管理器启动时**（`lifespan` 调 `purge_tombstones()`），即长期不重启的管理器不会中途清理——不影响正确性。

---

## 4. 功能清单

### 4.1 总览页

- **拓扑图**：实例按环形布局画节点，首节点从正上方开始；有关联登录端的实例画连线。
- **连线状态三态**：实线绿 = 已连接、虚线灰 = 已配置未连接、实线红 = 连接失败。判定来自适配器的 `health_check()`。
- **节点信息**：程序名（整合包标注）+ 状态 + 端口；进程已死则节点变灰；`warnings` 以橙色小字画在节点下方（不换行、最长两行由样式的 `y=40` 决定）。
- **资源水位条**：内存 used/total，≥90%（`ctx.resmon_alert`）变红。阈值可配置。
- **扫描安装目录**（`GET /api/scan`）：比对安装根（manifest 的 `install_root`）下的一级目录与相关进程 vs 注册表，分三类展示——`owned`（实例受管）/ `orphan`（目录名匹配程序名但无实例记录，如手工部署残留、删除失败残留，提供删除按钮）/ `external`（无关软件如 alist、containerd，仅展示不提供删除）。游离进程同样标注，可结束（pid 的 exe/cwd 必须落在安装根下且不属于实例/管理器，`POST /api/scan/kill`）。删除走 `DELETE /api/scan/dir`，三重校验在 `core/scanner.py::check_deletable`（**受保护目录必须 Path 对 Path 比较**——str/Path 混比曾放行受管目录，2026-09-25 事故）。
- **实例操作**：选中节点后出现「启动 / 停止 / 重启 / 打开 WebUI / 查看日志 / 上传备份 / 删除」。
- **删除的二次确认**：第一次 confirm 确认删实例；第二次 confirm 询问是否连程序目录一起删。若该程序的 manifest 声明了 `delete_keeps_save: true`，第二次询问的文案会提示「建议保留存档目录」，并把 `keep_save=true` 传给后端。
- **删除链路顺序（2026-09-25 修正）**：停进程 → 释放端口 → 删目录（rmtree 失败会收集错误并**先于墓碑**抛 500，实例记录保留可直接重试）→ 全部成功才 `registry.remove()` 写墓碑。旧实现 `rmtree(ignore_errors=True)` 静默吞错且记录已删，失败即成无主孤儿目录；`keep_save` 删空后残留的空壳目录也会一并清掉。
- **数据来源**：`/ws/overview`，2 秒一推。断线由前端自动重连（指数退避，上限 30s）。

### 4.2 新建向导（五步）

| 步骤 | 前端动作 | 后端动作 | 关键产物 |
|---|---|---|---|
| 1 选程序 | 拉 `/manifests`（下拉选项含整合包/独立程序 + 内存预估 + 前置依赖）、`/instances`（登录端候选）、`/packages`（本地包状态）；可上传离线包 | `create_instance()`：分配全部端口、定目录（同名自动 `-2`、`-3`…）、建档 `UNDEPLOYED` | 实例 id |
| 2 部署 | 直接触发 | `adapter.deploy()`：本地包优先 → 无则按策略下载 → 校验 → 解压 → 归一化顶层目录 → `verify_required()`；成功后 `AWAIT_LOGIN` | 安装目录 |
| 3 登录 | 按 `login_type` 渲染四种界面（二维码 / 账号密码 / WebUI 指引 / 无需登录） | `configure_login()`；需要则启动登录进程并开 `/ws/login` 推送 | 进程 + `qq` 回读 |
| 4 互联 | 选方向（正向/反向）、地址、token（均可留空自动） | `write_conn_config()` 写入程序侧配置 + 返回预览与人工指引 | `conn_*` 三字段 |
| 5 启动 | 显式点击「启动」（不再自动启动） | `prepare_start()`（首启一次性）→ `build_start_cmd()` → 启动 → `RUNNING` | 运行中实例 |

**冲突处理（第 2 步）**：目标目录已存在且缺必备文件时，`deploy()` 返回 `"conflict"`，前端弹窗二选一——「直接使用（校验必备文件）」或「新建序号文件夹」，选择结果作为 `use_existing` 回传后端。

**离线程序包**：向导第 1 步可直接上传 `zip / tar.gz / tar.xz / tar.bz2 / tar`（2GB 上限）。包按魔数识别真实格式（不看扩展名）、做完整性校验后原子落盘到包缓存，**所有同程序实例共用**。上游不提供可运行包的程序（如 Dice!）靠它落地。

### 4.3 登录页

- 三种交互：`qrcode`（二维码图片 + 刷新二维码 + 滑块验证链接）、`account`（QQ + 密码 + 协议）、`webui`（展示指引，等用户自己去程序自带面板登录）。
- 二维码与滑块链接来自 `/ws/login/{id}` 推送，数据由适配器的 `extract_qrcode()` / `extract_verify()` 从进程日志中正则提取。
- 完成判定：`health_check()` 返回「进程存活 **且** 链路连通」→ 标记 `CONFIGURED` 并推送 `completed`，前端跳第 4 步。
- 用户也可主动 `skip_login` 强行推进。
- 滑块验证**只转发不代做**（把 ticket url 显示出来让用户自己点）。

### 4.4 日志中心

- 实例下拉切换；每实例一条 WS，切换即重连并清空。
- 功能：历史回放（ring 快照，重启不丢）、实时跟随、暂停/继续跟随（发 `pause`/`resume`）、关键字过滤（发 `filter:<kw>`，**服务端过滤**，正则转义后大小写不敏感）、错误行高亮（`ERROR|FATAL|Traceback|panic`）、复制全部、下载完整日志文件。
- 前端最多保留 2000 行；后端 ring 也是 2000 行；磁盘日志 50MB 滚动、保留 7 天。

### 4.5 程序包管理

- 列表 `/api/packages`：每个程序本地包的存在性、大小、来源（上传/下载缓存）、更新时间。
- 上传：原始字节流（`application/octet-stream`），**不用 multipart**（免依赖），浏览器直接 `fetch(file)` 流式发送。
- 删除：删掉该程序所有已知扩展名的包 + 元数据。
- 死缓存清理：`DELETE /api/packages/unused` 清掉「没有任何实例在用」的包（列表里的 `in_use` 据此标注）。
- 部署时优先级：本地包 > 在线下载（下载成功后也会缓存进本地，供后续复用）。

### 4.5.1 备份产物（exports/）

三类产物都落在 `<state>/exports/`，**只增不减**，需要人工或按天清理：

| 产物 | 命名 | 生成时机 |
|---|---|---|
| 升级前快照 | `<dice>-<id>-preupgrade-<ts>.tar.gz` | 每次点「升级」前自动整目录备份 |
| 定时备份 | `<dice>-<id>-sched-<scope>-<ts>.tar.gz` | 定时任务 kind=backup |
| 手动导出 | `<dice>-<id>-<scope>-<ts>.tar.gz` | 面板导出（发完即删，不常驻） |

- 定时任务的 keep 滚动**只认 `-sched-` 标记**，不会误删升级前快照（升级快照同样含 `<id>-` 子串）。
- 回收入口在向导「备份文件」区：单项删除，或按天数批量清理（默认 30 天）。

### 4.6 进程守护

| 行为 | 参数 |
|---|---|
| 环形缓冲 | 2000 行（`RING_MAX`） |
| 日志文件滚动 | 单文件 50MB，超出后转存 `.log.1` 并重开 |
| 日志保留 | 7 天（mtime 超期触发轮转；启动时清理过旧的 `.log.1`） |
| 停止方式 | POSIX 下 `SIGTERM` 到进程组 → 等 10s → `SIGKILL` |
| 崩溃自动重启 | 指数退避 1s 起（上限 300s），每次崩溃 +1 |
| 熔断 | 300s 滑动窗口内 ≥5 次重启则**停止**自动重启并写日志 |
| 窗口清零 | 上次启动后稳定运行超过 300s，重启窗口清零 |

所有子进程都以 `start_new_session=True` 启动（POSIX），因此信号能打到整个进程组，不会留下孤儿子进程。

### 4.7 面板重启后的实例自动恢复

实例进程是**面板的子进程**（`start_new_session` 只脱离了终端会话，仍是 systemd 单元的成员），因此
`systemctl restart dicemanager` 会连带杀掉全部实例，而注册表里状态仍停留在 `RUNNING`——历史上表现
为「面板显示在跑，实际全部离线」，只能人工逐个点启动（`services/resume.py` 的来历）。

启动时 `lifespan` 会起一个守护线程执行 `resume_running_instances()`：

| 环节 | 口径 |
|---|---|
| 触发范围 | **仅 `RUNNING`**。用户主动 stop 会把状态落回 `CONFIGURED`，因此不会被强行拉起；`AWAIT_LOGIN`（待扫码）与 `ERROR`（待排查）同理不拉 |
| 执行方式 | 后台线程，先等 2s（`DM_RESUME_DELAY`）让 HTTP 端口起来，实例之间错开 1.5s（`DM_RESUME_STAGGER`） |
| 启动路径 | 复用 `Wizard.start_instance`，与 REST `start`、向导 Step5 **完全同一条实现**，没有第二个启动入口 |
| 失败处理 | 逐实例独立 try/except：一个失败（目录被删、二进制缺失）不影响其余；失败原因同时写进面板日志与该实例自身的日志 |
| 总开关 | `DM_AUTO_RESUME=0`（排障时不希望被自动拉起干扰） |
| 可观测 | 面板日志 `[resume] 实例 xxx 已自动拉起` / `实例 xxx 自动拉起失败：原因`；成功恢复的实例日志里会有 `[manager] 面板重启后已自动恢复运行` |

> 「用户主动停下的实例不得复活」是这条功能的红线，`tests/test_resume.py` 用五个用例锁死了
> CONFIGURED / AWAIT_LOGIN / ERROR / UNDEPLOYED 一律不拉，只有 RUNNING 才拉。

---

## 5. HTTP / WebSocket 接口契约

### 5.1 REST

所有 `/api/*`（除登录）都需要请求头 `Authorization: Bearer <token>`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` | **无鉴权**。`{password}` → `{token}`；失败有频率限制 |
| GET | `/api/manifests` | 前端所需字段的白名单视图（见 §6.2） |
| POST | `/api/instances` | 建实例：`{dice, arch, login_ref?, confirm_dir?}` |
| GET | `/api/instances` | 列表，每条附 `process` 探针（alive/restarts/pid/uptime） |
| GET | `/api/pending` | 中间态实例（`DEPLOYING`/`AWAIT_LOGIN`）+ `next_step` |
| POST | `/api/instances/{id}/wizard` | `{step, payload}` → `{result: ok/conflict/error, ...}` |
| POST | `/api/instances/{id}/{op}` | `op ∈ start / stop / restart`；已在运行返回 409 |
| DELETE | `/api/instances/{id}` | `?confirm=true&remove_dir=&keep_save=`；缺 confirm 返回 400 |
| POST | `/api/instances/{id}/backup` | **备份导入**：原始字节流上传 zip/tar 系（2GB 上限）。流程：运行中先停机 → 魔数识别 + 完整性校验 → 解压覆盖到实例目录（只覆盖包内文件、不删包外文件；zip-slip 整体前置校验；顶层目录歧义按「与已有路径命中多者胜、平手剥离」消解，见 `core/backup.py`）→ 自动重启。恢复失败也会把实例拉回运行态 |
| GET | `/api/packages` | 本地包列表 |
| POST | `/api/packages/{dice}` | 原始字节流上传 |
| DELETE | `/api/packages/{dice}` | 删除本地包 |
| DELETE | `/api/packages/unused` | 一键清理「无任何实例在用」的死缓存 → `{removed, freed_mb}` |
| GET | `/api/exports` | 备份产物清单（名称/大小/修改时间/已存放天数） |
| DELETE | `/api/exports/prune?days=30` | 清理超过 N 天未修改的备份 → `{removed, freed_mb}`；`days<1` 返回 400 |
| DELETE | `/api/exports/{name}` | 删除单个备份（名称含 `/` 或 `..` 一律拒绝，防目录穿越） |
| GET | `/api/logs/{id}/download` | 下载日志文件（`FileResponse`） |

> 内存水位没有独立 REST 端点：总览的资源数据由 `/ws/overview` 每 2s 推送
> （`payload.resmon`），REST 侧不再重复提供（2026-09-26 移除 `/api/resmon`）。

> 路由顺序坑：`/packages/unused`、`/exports/prune` 这类「字面量子路径」必须注册在
> `/packages/{dice}`、`/exports/{name}` 之前，否则会被当成参数值吞掉。

静态资源：若 `web/dist` 存在则挂载在 `/`（SPA，`html=True`）；不存在时服务照常启动，只在控制台警告「仅提供 API」。

### 5.2 WebSocket

三条通道都用**查询参数**鉴权：`/ws/xxx?token=<token>`。鉴权失败以 `4401` 关闭。

| 通道 | 周期/触发 | 服务端 → 客户端 | 客户端 → 服务端 |
|---|---|---|---|
| `/ws/overview` | 2s | `{type:"overview", payload:{nodes, edges, resmon}}` | —— |
| `/ws/logs/{id}` | 回放 + 实时 | `{type:"line", seq, text, error}` | `pause` / `resume` / `filter:<kw>` |
| `/ws/login/{id}` | 回放 + 实时 | `{type:"qrcode"/"verify"/"completed"/"skipped"/"restarted", payload}` | `refresh` / `skip_login` |

前端重连策略：收到 `4401` 立即停止重连并跳登录页；握手被拒时浏览器只暴露 `1006`，连续 3 次后在客户端判定为鉴权失效。

### 5.3 鉴权要点

- 管理密码首次启动随机生成，**只打印到控制台**（systemd 下是 journal），不写日志文件。
- 密码以哈希形式持久化在 `auth.json`；旧版本遗留的明文密码在加载时自动迁移为哈希，**用户无感**。
- 登录失败有速率限制（短窗口内多次失败会被拒）。
- 前端 `api.js` 遇到 401 会清 token 并回登录页；日志下载走 `fetch + blob`（裸 `<a href>` 带不上 Authorization 头）。

---

## 6. 适配模块编写规则

**这是本手册最重要的一节。** 新增一个程序只允许改两个地方（外加注册表一行），凡是突破这个边界的实现方式都应当重新设计。

### 6.1 铁律

1. **只动 `manifests/<name>.json` + `adapters/<name>.py`，再到 `adapters/__init__.py` 的 `classes` 字典注册一行。**
2. **`core/`、`api/`、`services/` 里禁止出现程序名分支。** 有差异就加 manifest 字段，由适配器读。
3. **适配器实例是共享单例，禁止写 `self` 保存请求态。**
4. **manifest 里的字符串必须单行。** JSON 不支持相邻字符串字面量拼接，写成两行会让整个 manifest 解析失败 → `load_registry` 抛错 → **服务启动即挂**。
5. **新增 `download_strategy` 必须同步 `adapters/__init__.py::ALLOWED_STRATEGY`**，否则同样启动即挂。单测直接实例化适配器会绕过这道校验，所以务必用 `load_registry` 做端到端加载用例兜底。

### 6.2 manifest 字段全表

manifest 是标准 JSON，但**允许整行 `//` 注释**（加载时按行剥离）。行尾注释不支持。

#### 必需字段（缺任一 → 启动报错）

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | str | 程序名。必须与 `adapters/__init__.py` 的 `classes` 键一致，也决定实例目录名与包缓存文件名 |
| `arch` | str | `standalone`（独立程序）或 `allinone`（整合包，前端标注不同）。其它值报错 |
| `multi_account` | bool | 是否支持多账号（当前仅用于前端提示） |
| `exe` | str | 可执行文件名（相对 `instance.dir`）。有的适配器用它做 glob 兜底 |
| `install_root` | str | 安装根，实例目录 = `<install_root>/<name>[-N]` |
| `required_files` | list[str] | 部署后必须存在的文件（相对 `instance.dir`）。缺 → 抛错/冲突 |
| `download_strategy` | str | 见下表，必须在白名单内 |

#### 下载策略（`download_strategy`）

| 值 | 行为 |
|---|---|
| `direct` | 直接用 `download` 字段的固定 URL |
| `resolve_latest_via_api` | 用 `release_page` 推出 `owner/repo`，调 GitHub API 取 latest release；`asset_name_pattern`（正则）优先精确选资产，否则退回 `asset_suffix`（默认 `.zip`）后缀匹配 |
| `manual` | 上游不提供可直接运行的程序包，本地无包时给出明确引导（附 `prerequisite` 文案），要求用户先上传离线包 |
| `olivos_bundle_or_opk` | Olivadice 专用：`opk_modules` + `download_opk_pattern` 逐个拉 `.opk` |

> 走 GitHub 的请求统一经过 `mirror_url()`，受 `DM_GITHUB_MIRROR` 控制。

#### 行为字段（被代码消费）

| 字段 | 消费点 | 说明 |
|---|---|---|
| `login_type` | `services/wizard.py` + 前端 | `qrcode` / `account` / `webui` / `external` / `none`。决定第 3 步界面，也作为 `needs_login` 的默认判据（`qrcode`/`account` 默认为需要登录） |
| `compatible_login` | 前端 | 兼容的登录端程序名列表；`builtin` 表示自带登录（不出现下拉） |
| `auth_token_conditional` | 前端 | 为真时登录页强制显示 AUTH TOKEN 输入框（LLBot v8.0.9+） |
| `webui_default_port` / `ob11_default_port` / `milky_default_port` / `satori_default_port` | `wizard.create_instance` | 按角色申请端口。全部可选，缺省则不申请该角色端口 |
| `config_path` | 适配器 | 程序配置文件的相对路径（适配器自己读，非框架强制） |
| `post_start_action` | LLBot 适配器 | 首启一次性 CLI 动作（如 `--update`），与 `first_run_done` 配合只做一次 |
| `prerequisite` | `base._resolve_download` / 前端 | 前置依赖说明（如 `linuxqq-deb`）。`manual` 策略会把这句话拼进错误提示 |
| `asset_name_pattern` | `base._resolve_download` | 正则选 release 资产（tar.gz 类程序必填） |
| `asset_suffix` | 同上 | 正则未命中时的后缀兜底 |
| `sha256` | `base.deploy` | 可选。提供则校验包哈希，不一致时区分「本地包错版本」与「下载失败」两种提示 |
| `save_keep_dir` | `api/rest.py` | 删除时保留的存档目录名。兼容三种声明：`"config"`（字符串）/ `["config","data"]`（数组）/ `true`（等同默认 `config`）。**不要写 `false`**，走 else 分支 |
| `delete_keeps_save` | 前端 | 为真时删除确认框文案提示「建议保留存档目录」，并传 `keep_save=true` |
| `approx_memory_mb` | 前端 | 下拉里的内存预估，纯展示 |
| `opk_dir` / `opk_modules` / `download_opk_pattern` | Olivadice 适配器 | OPK 组合部署专用 |

#### 文档性字段（当前无代码消费，保留备用）

`multi_account`、`recommended_protocols`、`config_strategy`、`release_page`（`resolve_latest_via_api` 策略会解析它，`direct`/`manual` 下纯文档）、`download_page_official`、`error_keywords`、`webui_port_bump_limit`、`onebot_config`、`framework_repo`。

#### 透出白名单

`GET /api/manifests` **只透出白名单字段**（`api/rest.py` 里那个 tuple），且白名单只放
前端真正消费的字段——曾在此透出但前端从不读取的 `multi_account`、`recommended_protocols`
已于 2026-09-26 移出，避免误导后来者：

```
arch, login_type, compatible_login,
webui_default_port, ob11_default_port, approx_memory_mb, auth_token_conditional,
prerequisite, delete_keeps_save
```

**新增一个需要前端使用的 manifest 字段，必须在白名单里登记**，否则前端永远拿不到（历史上 `delete_keeps_save` 就踩过）。

### 6.3 适配器钩子全表

基类 `BaseAdapter` 在 `adapters/base.py`。签名如下（`instance` 是 `registry.Instance`；有些地方传的是 `SimpleNamespace` 同名属性的字典适配物，所以**只做属性访问**，别假设它是 `Instance` 类型）。

#### 必须实现（`@abstractmethod`）

```python
def build_start_cmd(self, instance) -> list[str]
```
返回完整命令 argv。**唯一**的命令来源，不存在从外部传入命令的路径。
例：`[str(Path(instance.dir) / self.m["exe"]), f"--address=127.0.0.1:{instance.port}"]`

```python
def configure_login(self, instance, credentials: dict) -> dict
```
登录步的入口。返回字典，约定键：
- `needs_login: bool` —— 是否需要停在登录页并开 `/ws/login`。缺省按 `login_type ∈ (qrcode, account)` 推断。
- `conflict: str` —— 非空则当作冲突处理，前端弹出该消息（LLBot 缺 AUTH TOKEN 用的就是它）。
- `manual: str` —— 展示给用户的操作指引（`webui` 型登录端用）。
- 其它键原样透传给前端（如 `qq`）。

```python
def write_conn_config(self, instance, mode: str, direction: str,
                      addr: str, token: str) -> WriteResult
```
写互联配置。`direction` 为 `"forward"`（本程序监听，等对端连）或 `"reverse"`（本程序主动连对端）；`mode` 目前是 `"ob11"`；`addr` 形如 `127.0.0.1:3001`。
返回 `WriteResult(ok, manual, path)`：`ok=False` 时 `manual` 必填（作为错误信息）；`manual` 同时用来传达「需重启才生效」这类提示。

#### 可选覆盖

| 钩子 | 默认行为 | 何时覆盖 |
|---|---|---|
| `deploy(instance) -> "ok" \| "conflict"` | 锁目录 → 已有目录则校验必备文件 → 取包（本地优先）→ 校验 sha256 → 解压 → 归一化顶层目录 → 校验必备文件 | 部署流程本身特殊（如 Olivadice 的 OPK 组合、缺核阻断、缺件告警） |
| `verify_required(instance) -> list` | 返回缺失的 `required_files` | 缺件判定要更复杂时 |
| `prepare_start(instance, runner) -> bool` | 返回 `False`（不做事） | 启动前需要改配置或执行一次性命令。返回 `True` 表示「做了一次性动作」，调用方会落盘 `first_run_done`。`runner(cmd, cwd, label)` 用于跑一次性命令并把输出汇入本实例日志 |
| `health_check(instance, is_alive) -> dict` | 有 ob11 端口则 TCP 探测，返回 `{alive, conn: ok/down/none}` | 需要更准的判定（SealDice 读 `serve.yaml` 的 `state`，Dice!Next 探 WebUI 端口） |
| `get_actual_port(lines) -> int \| None` | `None` | 真实端口只能从日志回读时（NapCat 端口冲突自动 +1） |
| `get_webui_token(lines) -> str \| None` | `None` | WebUI 令牌只打印在启动日志里 |
| `detect_account(instance) -> str \| None` | `None` | 能从配置文件回读 QQ 号时（首选，最可靠） |
| `account_from_logs(lines) -> str \| None` | 不存在该属性 | 只能从日志锚点抠 QQ 号时 |
| `get_conn_token(instance) -> str \| None` | **不存在该属性** | 登录端自己生成 OneBot token 时（SnowLuma）。这是**鸭子类型探测**（`hasattr`），不是基类方法 |
| `extract_qrcode(line) -> dict \| None` | 正则匹配 `data:image/png;base64,` 或含 `qrcode` 的 URL | 二维码形态不同时 |
| `extract_verify(line) -> dict \| None` | 匹配 `ticket url: <url>` | 滑块验证的日志锚点不同时 |

#### 基类可复用的工具

| 成员 | 用途 |
|---|---|
| `self.m` | manifest 字典（构造时注入） |
| `_acquire_archive()` | 取包：本地缓存优先，否则下载并缓存 |
| `_verify_sha256(archive, expected)` | 校验 |
| `_extract(archive, instance)` | 解压 + 顶层目录归一化；tar 用 `filter="data"` 防路径穿越 |
| `_resolve_download()` | 按策略解析下载 URL |
| `mirror_url(url)` | GitHub 镜像前缀（模块级函数） |
| `self.tcp_probe(host, port, timeout)` | TCP 连通探测（staticmethod） |
| `self.gen_token(n)` | 生成互联 token（staticmethod） |

#### 日志行回调的签名契约（重要）

进程日志有两个消费入口，**签名必须一致**：

```python
proc.on_line(cb)      # cb(seq: int, line: str) → 返回一个函数，用于 off_line 注销
```

`core/process.py` 在尾部线程与一次性命令两条路径上都按 `cb(seq, line)` 调用。回调抛出的任何异常都会被静默吞掉（`except Exception: pass`），所以签名写错不会报错，只会「什么都不发生」。
`ring` 的结构同样是 `(seq, line)` 对，`seq` 单调递增、全局唯一，用于回放与实时的去重。

### 6.4 三种形态的适配器要点

拿到一个新程序，先判断它属于哪一类，再照葫芦画瓢：

**A. 骰子端 · 独立程序**（`login_type: external`，范例 `shiki.py` / `dicenext.py`）
- `configure_login` 返回 `{"needs_login": False}`
- `write_conn_config` 写自己的适配器/端点配置（Dice!Next 写 `config/adapters.json`；Dice! 因上游格式未确认只给指引）
- `health_check` 若在正向模式下自己不监听端口，就探 WebUI 端口当存活信号

**B. 登录端**（`login_type: qrcode|webui`，范例 `napcat.py` / `snowluma.py` / `llbot.py`）
- `write_conn_config` 通常需要处理「正向 = 自己监听」与「反向 = 自己连出」两种条目结构
- 往往需要 `get_webui_token` / `get_actual_port` / `detect_account`
- 若它自己生成 token，实现 `get_conn_token` 让骰子端继承
- 条目写入要**按名去重**（统一用 `name: "dicemanager"`，命中即替换），并保留用户自建条目

**B'（LLBot 实战要点，2026-09-24 故障沉淀）**
- 官方 zip 根二进制叫 `llbot`（启动器，内部 exec `bin/llbot/node llbot.js`）——manifest `exe`
  必须同名，且 `required_files` **必须非空**（曾因空列表把误传的 GitHub 源码包放行成「部署成功」）
- zip 解压不保留执行位：`prepare_start` 里对 exe / `bin/llbot/node` / `bin/pmhq/pmhq` 补 `chmod +x`
- 二维码三路输出：stdout 字符画（多行、还原不可靠）、**落盘 PNG** `bin/llbot/data/temp/login-qrcode.png`、
  生成服务 URL（`create-qr-code` 带连字符，基类正则不匹配）→ `extract_qrcode` 覆写为**读 PNG 转 base64**
- v8.0.9+ 登录前置条件是 AUTH TOKEN 文件 `bin/llbot/data/auth_token.txt`（缺失直接报错退出），
  `configure_login` 负责落盘并返回 `restart: True`，前端保存后自动 `refresh` 重启进程再扫码
- `ws_login` 的自动拉起**仅在 `AWAIT_LOGIN` 状态**触发：浏览器挂着登录页会反复重连 WS，
  部署中/错误态若也拉起，`prepare_start` 会建出残缺目录，让随后的部署误判 conflict（踩过）

**C. 整合包**（`arch: allinone`，`login_type: none`，范例 `olivadice.py`）
- 登录内置于程序，`write_conn_config` 直接返回空 `WriteResult(ok=True)`
- 部署流程特殊，通常覆盖 `deploy()`；缺核心模块要**阻断**，缺子模块只**告警**
- ⚠️ 告警写进 `instance.warnings` 后**必须由调用方显式落盘**（`wizard.run_step` 第 2 步会做）。适配器里改的只是副本——历史上有过「告警改了临时副本、从未落盘、总览永远看不到」的 bug

### 6.5 契约测试建议

新增/修改适配模块后，至少覆盖：

1. **manifest 契约**：字段存在性、类型、`arch` 与 `download_strategy` 合法。
2. **`load_registry` 端到端加载**：从磁盘读真实 manifest 解析（能抓到 JSON 语法错误与策略未登记）。见 `tests/test_shiki_offline.py`。
3. **部署路径**：本地包优先、缺必备文件要报错、`manual` 策略要给出引导。
4. **配置写入**：用临时目录造出目标配置文件，写入后回读断言结构；重复写要幂等（按名去重）。
5. **日志解析**：把真实日志粘贴进测试，断言 `get_actual_port` / `detect_account` 等能命中。

参考现有测试：`tests/test_pkg_deploy.py`、`tests/test_shiki_offline.py`、`tests/test_snowluma_dicenext.py`、`tests/test_patch_regress.py`。

---

## 7. 新增一个程序的完整流程

以新增 `foo` 为例（假设它是独立骰子端，`https://github.com/x/foo` 提供 linux 包）：

**Step 1｜先搞清楚上游形态**（最关键，别跳）
- 发行物是 zip 还是 tar.gz？资产名长什么样？主程序叫什么？
- 它自己登录 QQ 吗？还是连 OneBot？连的话配置文件在哪、什么格式？
- 默认端口各是多少（WebUI / OneBot）？
- 有哪些「第一次才能做」的动作？

**Step 2｜写 `manifests/foo.json`**
```json
// manifests/foo.json
{
  "name": "foo", "arch": "standalone", "multi_account": false,
  "exe": "foo", "install_root": "/opt",
  "webui_default_port": 12345, "ob11_default_port": 3001,
  "login_type": "external",
  "compatible_login": ["napcat", "snowluma", "llbot"],
  "config_path": "config/adapters.json",
  "release_page": "https://github.com/x/foo/releases",
  "download_strategy": "resolve_latest_via_api",
  "asset_name_pattern": "foo-.*-linux-amd64-.*\\.tar\\.gz",
  "required_files": ["foo"],
  "approx_memory_mb": 150
}
```
> 注意：字符串单行；正则里的反斜杠在 JSON 中要写成 `\\`。

**Step 3｜写 `adapters/foo.py`**
```python
"""foo：一句话说明形态与要点（含已核实的事实来源）"""
from pathlib import Path

from adapters.base import BaseAdapter, WriteResult
from core.atomicio import atomic_write_json


class FooAdapter(BaseAdapter):
    def build_start_cmd(self, instance) -> list[str]:
        return [str(Path(instance.dir) / self.m["exe"])]

    def configure_login(self, instance, credentials) -> dict:
        return {"needs_login": False}
        # 若自身登录：return {"ok": True}（二维码走 /ws/login 推送）

    def write_conn_config(self, instance, mode, direction, addr, token) -> WriteResult:
        forward = direction != "reverse"
        entry = {
            "name": "dicemanager",
            "type": "onebot_v11",
            "connection_mode": "forward_ws" if forward else "reverse_ws",
            "endpoint": (f"ws://{addr}/" if forward else addr.split(":")[-1]),
            "access_token": token or "",
            "enabled": True,
        }
        path = Path(instance.dir) / self.m["config_path"]

        def _m(cfg: dict) -> dict:
            cfg.setdefault("adapters", [])
            cfg["adapters"] = [e for e in cfg["adapters"] if e.get("name") != "dicemanager"]
            cfg["adapters"].append(entry)
            return cfg

        atomic_write_json(path, _m)
        return WriteResult(ok=True, path=str(path),
                           manual="已写入 config/adapters.json，面板启用后生效。")

    def health_check(self, instance, is_alive: bool = False) -> dict:
        port = instance.allocated_ports.get("ob11") or instance.actual_port
        if not port:
            return {"alive": is_alive, "conn": "none"}
        return {"alive": is_alive,
                "conn": "ok" if self.tcp_probe("127.0.0.1", port) else "down"}
```

**Step 4｜注册**
```python
# adapters/__init__.py
from adapters import dicenext, foo, llbot, napcat, ...   # 加 foo

classes = {..., "foo": foo.FooAdapter}                   # 加一行
```

**Step 5｜如有新策略，同步白名单**
```python
ALLOWED_STRATEGY = ("direct", "resolve_latest_via_api", "olivos_bundle_or_opk", "manual", "...")
```

**Step 6｜如前端要用新字段，登记白名单**（`api/rest.py` 的 `/manifests`）

**Step 7｜加测试**
- manifest 契约 + `load_registry` 加载
- `write_conn_config` 的正/反向写入与去重
- 日志解析（有的话）
- 部署路径（本地包 / 缺件 / manual 引导）

**Step 8｜验证**
```bash
ruff check . && mypy && pytest tests/ -v
python tests/smoke_local.py
```

**Step 9｜真机验证**（不可省）
部署到服务器 → 向导走完五步 → 总览出现节点且连线变绿 → 日志中心有输出 → 发一条 QQ 消息确认链路通。

---

## 8. 部署与运维

### 8.1 一键部署

```bash
sudo bash deploy/install.sh                # 完整（含前端构建）
sudo bash deploy/install.sh --swap         # 额外建 2G swap（2G 内存机器建议）
sudo bash deploy/install.sh --skip-build   # 已有 web/dist，跳过 npm 构建
```

脚本做的事：可选 swap → 装系统依赖 → 检查 Python ≥3.10 → 拉代码到 `/opt/dicemanager` → 建 venv 装依赖 → 构建前端 → 建 `/var/lib`、`/var/log` 目录 → 装并启用 systemd 服务 → 配置 nginx → 输出访问方式与「如何查看管理密码」。

可注入的变量：`DM_REPO`、`DM_BRANCH`、`DM_GITHUB_MIRROR`。

**首次拿到密码**：
```bash
journalctl -u dicemanager -n 50 | grep '\[auth\]'
```

### 8.2 运行时配置

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `DM_STATE_DIR` | `/var/lib/dicemanager` | 注册表/端口表/凭据/包缓存 |
| `DM_LOG_DIR` | `/var/log/dicemanager` | 实例日志 + 管理器日志 |
| `DM_LOCK_DIR` | `/tmp/dicemanager` | 文件锁目录 |
| `DM_GITHUB_MIRROR` | 空（直连） | GitHub 镜像前缀，如 `https://ghfast.top`，等价于拼成 `mirror/https://github.com/...` |

改 systemd 服务：
```ini
[Service]
User=root
WorkingDirectory=/opt/dicemanager
ExecStart=/opt/dicemanager/venv/bin/python -m api.app
Restart=always
Environment="PYTHONUNBUFFERED=1"
# Environment="DM_GITHUB_MIRROR=https://ghfast.top"
```
改完 `systemctl daemon-reload && systemctl restart dicemanager`。

### 8.3 Nginx

管理器自身只监听 `127.0.0.1:8765`，由 nginx 对外并提供 TLS。

**WebSocket 必须带 `Upgrade` / `Connection` 头**，否则总览、日志、登录二维码三条通道全部不动。`deploy/nginx-dicemanager.conf` 已含 `map $http_upgrade $connection_upgrade` 与 3600s 超时。

模板 `client_max_body_size 2048m` 已对齐「离线包上限 2GB」（2026-09-22 前是 8m，会导致大包上传被 nginx 直接 413）。

### 8.4 更新部署

```bash
# 后端（.py 改动）
cd /opt/dicemanager && git fetch --depth=1 origin main && git reset --hard origin/main
/opt/dicemanager/venv/bin/pip install -r requirements.txt
systemctl restart dicemanager

# 前端（web/ 改动，无需重启服务）
# 本机构建产物覆盖过去即可
# web/dist → /opt/dicemanager/web/dist
```

判断规则：**改 `.py` 要重启，改前端只覆盖 dist**。nginx 已设 `Cache-Control: no-store`，前端更新即时生效。

### 8.5 常见故障

| 现象 | 排查方向 |
|---|---|
| `/ws/*` 返回 404 | 依赖版本漂移。`fastapi`/`starlette` 必须锁在 `requirements.txt` 的区间内——新版下「静态文件挂载在 `/`」会吞掉 WS 路由 |
| 总览一直「正在连接服务端…」 | nginx 缺 WebSocket 头；或 token 已被 401 清掉（看浏览器控制台与 `journalctl`） |
| 二维码不出现 | 该程序是否真的把二维码打进了 stdout；`extract_qrcode` 的正则是否匹配它的格式；另见 §10 的已知缺陷 |
| 部署卡住/超时 | 国内直连 GitHub 慢 → 设 `DM_GITHUB_MIRROR`，或改用「离线程序包」上传 |
| 端口被别的服务占用 | 分配器会从默认端口 +1 探测，直到 `ports.json` 与系统占用都空闲；真实端口可能不同于 `webui_default_port`，以总览显示的为准 |
| 进程反复重启最终停下 | 触发熔断（300s 内 5 次）。看日志找根因，修好后手动「启动」即可，`restarts` 计数不复位是正常现象 |
| 界面正常但功能全 500 | 检查 `/var/lib/dicemanager` 与 `/var/log/dicemanager` 的权限；服务以 `User=root` 运行则一般无碍 |
| 启动即挂，日志说 manifest 非法 | 某个 manifest 语法错误（最常见：字符串被拆成两行）或 `download_strategy` 未登记 |

---

## 9. 开发、测试与 CI

### 9.1 依赖

`requirements.txt`（运行时）：fastapi / starlette / uvicorn（**三者都锁了上限**，原因见 §8.5）、psutil、PyYAML、json5。
`requirements-dev.txt`（开发）：pytest、ruff、mypy（大版本区间锁定，避免 latest 漂移）。

### 9.2 静态检查

```bash
ruff check .     # E/F/W/I/B/SIM；刻意豁免 E701/E702（本项目紧凑单行风格）
mypy             # 只查 core/ + services/，platform=linux
```

`pyproject.toml` 里的关键取舍：
- `platform = "linux"`：让 `fcntl` / `killpg` / `SIGKILL` 参与检查而不报「Windows 上不存在」。**代价**是平台条件导入不能写成 `if posix: import fcntl else: fcntl = None`（会被判赋值错误 + else 分支永不可达），必须显式声明成 `ModuleType | None` 再在使用处 `assert` 收窄。
- `files = ["core", "services"]`：adapters/api 大量依赖 FastAPI/pydantic 动态特性，信噪比低，暂未纳入。

### 9.3 测试

```bash
pytest tests/ -v              # 回归测试
python tests/smoke_local.py   # Windows 可跑的本地冒烟（自动打桩 fcntl）
```

已知测试文件：`test_process.py`（进程守护：重启后线程不翻倍、计数、seq 一致）、`test_patch_regress.py`、`test_pkg_deploy.py`、`test_shiki_offline.py`、`test_snowluma_dicenext.py`，以及两个冒烟脚本。

**Windows 本地注意事项**：`fcntl` 不可用，`smoke_local.py` 会打桩；`process.py` 和 `locks.py` 都做了 POSIX 守卫（`_POSIX` / `msvcrt`）。所以本地能跑通不等于线上没问题——**真机验证不可省**。

### 9.4 CI

`.github/workflows/ci.yml`，两个 job 都固定 `ubuntu-24.04`（`ubuntu-latest` 将于 2026-10-19 迁移到 Ubuntu 26，避免环境突变）：

- **backend**：`pip install -r requirements-dev.txt` → `ruff check .` → `mypy` → `pytest tests/ -v`
- **frontend**：`npm ci --prefix web && npm run build --prefix web`

排障提示：GitHub Actions 里**前一步失败会让后续步骤显示 skipped（不是通过）**。修完 lint 必须把 mypy + pytest 也本地重跑一遍，否则失败只是被推迟到下次推送。job/steps 结果用 `/actions/runs/{id}/jobs` 查；日志下载接口会 302 到 Azure blob 并丢鉴权头（401），别浪费时间，直接本地用同版本工具复现。

### 9.5 前端

```bash
cd web && npm run build     # 或直接用 node 跑 vite
```

两条血泪约定：
- **`<script setup>` 里 `let` 变量不会同步到模板**——模板要读的状态必须用 `ref`（`Wizard.vue` 的 `sock` 曾因此永久为 null）。
- **模板全局白名单不含 `location`**——模板里写 `location.hash` 会被编译成 `_ctx.location`，点击时才报错。一律包成方法调用。

前端改动后务必**真实构建一次**（`py_compile` / 类型检查通过 ≠ 页面正常）。

---

## 10. 已知缺陷与改进清单

### 10.1 待确认 / 待修复

> ✅ **2026-09-22 修复记录**：下表前三项已修复——hook 签名统一为 `cb(seq, line)`（实时推送恢复）、
> nginx 模板调到 `2048m` 对齐 2GB 上限、`ERROR` 增加出边（→ `DEPLOYING` / `AWAIT_LOGIN` / `UNDEPLOYED`）。

| 级别 | 问题 | 影响 | 位置 |
|---|---|---|---|
| ~~高~~ | ~~日志行回调签名不一致~~ | ✅ 已修复（`api/ws_logs.py`、`api/ws_login.py` hook 改为双参数） | — |
| ~~中~~ | ~~nginx `client_max_body_size 8m` 与 2GB 上限矛盾~~ | ✅ 已修复（模板改为 `2048m`） | — |
| ~~中~~ | ~~`ERROR` 状态无出边~~ | ✅ 已修复（`TRANSITIONS` 增加恢复边） | — |
| 中 | Dice!（shiki）的 OneBot 连接配置**只给指引、不写文件** | 用户必须手工填写，且两端可能填错导致连不上 | `adapters/shiki.py` |
| 低 | 管理器自身端口 `8765` 硬编码 | 与既有站点冲突时只能改代码 | `api/app.py` |
| 低 | 内存告警阈值 `resmon_alert=0.90` 是代码常量（原计划用于「部署前预估黄牌」的 `resmon_warn=0.80` 从未实现，已于 2026-09-26 移除） | 无法按机器配置 | `api/context.py` |
| 低 | 墓碑清理只在启动时执行 | 长期不重启的管理器不清理 | `core/registry.py` + `api/app.py` |

### 10.2 可考虑的演进方向

1. **多架构 / 多平台包选择**：当前 `asset_name_pattern` 只能匹配一个平台；若要同时支持 amd64 与 arm64，需要让 manifest 声明平台矩阵、由运行时按 `platform.machine()` 选择。
2. **实例配置的可视化编辑**：目前改配置要么走向导重跑，要么手工进服务器。
3. **备份与还原**：「导入」（上传压缩包 → 停机 → 覆盖解压 → 重启，总览页「上传备份」按钮）已实现；「导出」（把实例目录打成压缩包下载）待做。
4. **通知渠道**：进程熔断、连接断开目前只能靠人看总览。可接 Webhook / 邮件。
5. **mypy 覆盖面扩大**：先把 `adapters/` 纳入（给适配器加 `manifest: dict[str, Any]` 的类型标注后信噪比会好转）。
6. **协议扩展**：manifest 已预留 `milky_default_port` / `satori_default_port` 两个端口角色与 `compatible_login` 机制，接入 Milky 协议端时主要工作在于「连接配置的读写形态与 OneBot 不同」——需要给适配器区分协议类型，不能复用现有 OneBot 写法。

---

## 11. 附录

### 11.1 当前支持的程序

| 程序 | 角色 | arch | login_type | 默认端口 | 备注 |
|---|---|---|---|---|---|
| `sealdice` | 骰子 | standalone | external | webui 3211 | 双形态配置（1.x 单文件 / 0.99.x 分文件） |
| `shiki`（Dice!） | 骰子 | standalone | external | —— | **必须离线上传程序包**（上游只发行平台 dll） |
| `dicenext` | 骰子 | standalone | external | webui 18088 / ob11 6700 | tar.gz，二进制名 glob 兜底 |
| `olivadice` | 骰子（整合） | allinone | none | —— | OPK 组合部署，缺核阻断 |
| `napcat` | 登录端 | standalone | qrcode | webui 6099 / ob11 3001 | 端口冲突自动 +1，真实端口从日志回读 |
| `snowluma` | 登录端 | standalone | webui | webui 5099 / ob11 3001 | 自带 WebUI 登录；`get_conn_token` 回读 token |
| `llbot` | 登录端 | standalone | qrcode | webui 3080 / ob11 3001 | 支持 milky/satori 端口；首启 `--update`；v8.0.9+ 需 AUTH TOKEN |

### 11.2 目录结构

```
dicemanager/
├─ adapters/          适配层（base + 每程序一个 + __init__ 注册与校验）
├─ api/               FastAPI（app / rest / ws_* / auth / context）
├─ core/              基础设施（registry / ports / process / packages / locks / atomicio / logutil）
├─ services/          编排（wizard / login）
├─ manifests/         7 个声明式清单
├─ tests/             回归测试 + 本地冒烟
├─ web/               Vue 3 源码 + dist 构建产物
├─ deploy/            install.sh / systemd unit / nginx conf / 部署说明
├─ .github/workflows/ CI
├─ pyproject.toml     ruff + mypy 配置
├─ pytest.ini
├─ requirements.txt / requirements-dev.txt
└─ README.md
```

### 11.3 术语表

| 术语 | 含义 |
|---|---|
| **实例（instance）** | 一个被管理的程序部署，id 形如 `sealdice-a1b2c3d4` |
| **骰子端 / 登录端** | 见 §1「两类角色」 |
| **正向 WS** | 本程序监听端口，等对端连进来 |
| **反向 WS** | 本程序主动连对端 |
| **互联配置** | 两端的地址 + token 约定，第 4 步写入 |
| **中间态** | `DEPLOYING` / `AWAIT_LOGIN`，可断点续跑 |
| **墓碑** | 删除后保留 30 天的记录，防端口/目录误分配 |
| **ring** | 进程的内存环形日志缓冲，2000 行，元素是 `(seq, line)` |
| **熔断** | 300s 内重启 5 次的自动重启抑制 |
| **OPK** | OlivOS 插件包格式 |
| **OPK 组合部署** | Olivadice 由多个 .opk 模块拼起来，缺核心阻断、缺子模块告警 |

---

*本手册基于当前代码基线整理。修改架构或新增程序时，请同步更新对应章节——尤其是 §6 的字段表与钩子表。*
