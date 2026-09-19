# DiceManager — 骰子管理器

在 Linux 服务器上统一管理多个 QQ 骰子程序的 Web 管理器。支持 **5 个骰子程序**的
一键部署、登录承载、互联配置与进程守护，通过拓扑总览实时掌握每个骰子的运行与连接状态。

## 支持的程序

| 程序 | 架构 | 登录方式 | 互联说明 |
|---|---|---|---|
| [海豹 SealDice](https://sealdice.com/download) | 独立程序 | 外置登录端 | WS 正向/反向均可，配置自动写入 serve.yaml |
| LLBot | 独立程序 | 二维码（v8.0.9+ 需申请 AUTH TOKEN） | ob11/milky/satori 多端口，JSON5 配置热更新 |
| NapCatQQ | 独立程序 | 二维码（WebUI） | WebUI API 写互联配置，不可达时给出手动兜底指引 |
| 溯洄 Dice! | 整合包（allinone） | 账号密码（推荐 PAD/WATCH 协议） | 自包含，无外部 WS 配置 |
| 青果 OlivaDice | 整合包（allinone） | 内置 | 内置客户端，OPK 组合部署，缺核阻断启动 |

## 功能特性

- **五步向导**：建档分配端口 → 部署（同名冲突弹窗二选一）→ 登录（二维码实时推送 / 滑块验证链接转发）→ 互联配置写入 → 启动。支持断点续跑，进程崩溃后向导自动恢复中间态。
- **拓扑总览**：独立程序型骰子与其登录端渲染为双节点连线——实线绿已连接、虚线灰已配置未连接、红连接失败；整合包渲染为单节点。2 秒刷新，含全局内存水位告警条。
- **日志中心**：实时 tail + 历史回放（进程重启不丢日志）、关键字过滤、错误行高亮、暂停跟随、复制与下载。
- **端口管理**：按角色批量分配（webui/ob11/milky/satori），默认端口占用自动 +1 重试，实例删除时整体释放；文件锁 + 原子写保证多进程安全。
- **进程守护**：自动重启（5 次 / 5 分钟退避），双限日志滚动（50MB 或 7 天）。
- **安全**：随机管理密码 + Bearer token（恒定时间比较），WS 经查询参数鉴权；启动命令一律后端构建，杜绝任意命令执行；首次启动密码打印在控制台，凭据持久化到本地。

## 技术栈

后端 FastAPI + Vue3 前端（Vite 构建），WebSocket 三通道（总览 / 日志 / 登录），
Python 3.10+。

## 快速开始

```bash
# 1. 上传代码并安装依赖
cd /opt/dicemanager
pip install -r requirements.txt

# 2. 构建前端
cd web && npm i && npm run build && cd ..

# 3. 启动（首次启动密码打印在控制台）
python -m api.app        # 监听 127.0.0.1:8765

# 4. 浏览器访问 → 输入密码登录
```

> 生产环境请配置 Nginx TLS 反向代理（WebSocket 需加 `Upgrade`/`Connection` 头），
> 并用 systemd 常驻运行。

## 目录结构

```
dice-manager/
├── requirements.txt
├── core/        # 核心层：原子写、锁、端口分配、实例注册表、进程管理
│   ├── atomicio.py     # mkstemp + os.replace + fsync 原子写；JSON5 兼容读
│   ├── locks.py        # 端口/目录/实例三类临界区（threading + fcntl 双层）
│   ├── ports.py        # allocate_many 批量分配 + release_owner 整体释放
│   ├── registry.py     # 实例状态机 UNDEPLOYED→…→RUNNING；墓碑式删除
│   └── process.py      # Popen + tail 线程 + 环形缓冲 + 自动重启
├── adapters/    # 适配器层：每个骰子程序一份，实现统一契约
│   ├── base.py         # deploy / build_start_cmd / configure_login / write_conn_config
│   ├── sealdice.py     # serve.yaml 与 1.x 单文件 dice.yaml 双形态端点写入
│   ├── llbot.py        # JSON5 配置热更新；v8 需 AUTH TOKEN
│   ├── napcat.py       # WebUI API 写配置 + 手动兜底 WriteResult
│   ├── shiki.py        # AutoLogin.yml / config.txt 双版本识别；删实例保留存档
│   └── olivadice.py    # OPK 组合部署，缺核阻断 / 缺件告警
├── services/    # 服务层
│   ├── wizard.py       # 五步向导状态机（断点续跑 + 冲突弹窗）
│   └── login.py        # 登录编排：进程承载 + WS 推送通道
├── api/         # Web 服务层
│   ├── rest.py         # REST：向导 / 实例操作 / 快照 / 二次确认删除
│   ├── ws_overview.py  # 通道1：拓扑 + 资源水位（2s）
│   ├── ws_logs.py      # 通道2：日志 tail（回放 + 过滤 + 错误标记）
│   ├── ws_login.py     # 通道3：二维码/滑块验证推送，refresh/skip_login
│   └── app.py          # 组装 + lifespan + 监听 127.0.0.1:8765
├── manifests/   # 5 个程序清单：下载地址、端口、配置路径、兼容矩阵
└── web/         # Vue3 前端：Overview（拓扑）/ LogCenter（日志）/ Wizard（向导）
```

## 数据与运行时位置

| 路径 | 内容 |
|---|---|
| `/var/lib/dicemanager/instances.json` | 实例注册表（含墓碑） |
| `/var/lib/dicemanager/ports.json` | 端口分配表 |
| `/var/lib/dicemanager/auth.json` | 管理凭据（持久化） |
| `/var/log/dicemanager/{id}.log(.1)` | 各实例日志（50MB / 7 天双限滚动） |
| `/opt/{程序名}` 及序号后缀 | 程序安装目录 |

## 配置说明（`api/context.py`）

```python
STATE_DIR = Path("/var/lib/dicemanager")   # 状态持久化目录
LOG_DIR   = Path("/var/log/dicemanager")   # 日志目录
resmon_alert = 0.90                        # 内存 ≥90% 红色告警
resmon_warn  = 0.80                        # 部署前预估 ≥80% 黄牌提示
```

程序安装根目录由 manifests 的 `install_root` 决定，默认 `/opt`。

## 常见问题

- **登录二维码不动了**：日志中心查看进程输出；点向导内「刷新二维码」会重启登录进程。
- **弹出滑块验证**：管理器只转发验证链接，不代做——请在浏览器手动完成。
- **LLBot 提示需要 AUTH TOKEN**：LLBot v8.0.9+ 强制要求，到快速登录平台申请后填入向导。
- **NapCat 提示手动配置**：WebUI API 不可达（token 失效/端口未回读成功）时的兜底，按提示在 NapCat WebUI 手动创建后回填。
- **删除实例端口被占用**：分配表按 owner 释放；若 30 天内有墓碑记录，同名目录/端口受保护。
- **忘记管理密码**：删除 `/var/lib/dicemanager/auth.json` 后重启，生成新密码。

## License

MIT
