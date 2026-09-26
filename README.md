# DiceManager — 骰子管理器

在 Linux 服务器上统一管理多个 QQ 骰子程序的 Web 管理器。支持 **4 个骰子端 + 6 个登录端**
（共 10 份程序清单）的一键部署、登录承载、互联配置与进程守护，通过拓扑总览实时掌握
每个骰子的运行与连接状态。

## 支持的程序

### 骰子端（4）

| 程序 | 架构 | 登录方式 | 互联说明 |
|---|---|---|---|
| [海豹 SealDice](https://sealdice.com/download) | 独立程序 | 外置登录端 | WS 正向/反向均可，配置自动写入 serve.yaml |
| 溯洄 Dice! | 独立程序 | 外置登录端（离线上传） | AutoLogin.yml / config.txt 双版本识别 |
| 青果 OlivaDice | 整合包（allinone） | 内置 | 内置客户端，OPK 组合部署，缺核阻断启动 |
| Dice!Next | 独立程序 | 外置登录端 | OneBot v11 适配器写入 config/adapters.json |

### 登录端（6）

| 程序 | 架构 | 登录方式 | 说明 |
|---|---|---|---|
| NapCatQQ | 独立程序 | 二维码（WebUI） | WebUI API 写互联配置，不可达时给出手动兜底指引 |
| Lagrange.OneBot | 独立程序 | 二维码（stdout 字符画 + 落盘 qr-*.png） | 无头部署需预置配置，否则等按键 |
| Lagrange.Milky | 独立程序 | 二维码 | Milky 协议端口，与 OneBot 分开分配 |
| LLBot | 独立程序 | 二维码（v8.0.9+ 需申请 AUTH TOKEN） | ob11/milky/satori 多端口，JSON5 配置热更新 |
| SnowLuma | 独立程序 | WebUI | launcher.sh 启动，WebUI 内完成登录 |
| Yogurt | 独立程序 | 二维码 | 可作为海豹的登录端 |

> 骰子端 / 登录端的划分由清单驱动：登录端 = 在任意骰子端 manifest 的
> `compatible_login` 里出现过的程序，代码里没有程序名分支。新增程序只需
> 加 `manifests/<name>.json` + `adapters/<name>.py` + 适配器注册一行。

## 功能特性

- **五步向导**：建档分配端口 → 部署（同名冲突弹窗二选一）→ 登录（二维码实时推送 / 滑块验证链接转发）→ 互联配置写入 → 启动。支持断点续跑，进程崩溃后向导自动恢复中间态。
- **拓扑总览**：独立程序型骰子与其登录端渲染为双节点连线——实线绿已连接、虚线灰已配置未连接、红连接失败；整合包渲染为单节点。2 秒刷新，含全局内存水位告警条。
- **日志中心**：实时 tail + 历史回放（进程重启不丢日志）、关键字过滤、错误行高亮、暂停跟随、复制与下载。
- **端口管理**：按角色批量分配（webui/ob11/milky/satori），默认端口占用自动 +1 重试，实例删除时整体释放；文件锁 + 原子写保证多进程安全。
- **进程守护**：自动重启（5 次 / 5 分钟退避），双限日志滚动（50MB 或 7 天）。
- **磁盘回收**：程序包缓存标注是否有实例在用并支持一键清理死缓存；备份产物（升级前快照 / 定时备份）集中展示，可按天批量清理。
- **安全**：随机管理密码（PBKDF2 哈希存储，登录失败限速）+ Bearer token（恒定时间比较），WS 经查询参数鉴权；启动命令一律后端构建，杜绝任意命令执行；首次启动密码打印在控制台，凭据持久化到本地；manifest 可选 `sha256` 字段校验安装包完整性。

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
│   ├── locks.py        # 端口/目录/实例三类临界区（RLock + 引用计数文件锁）
│   ├── ports.py        # allocate_many 批量分配 + release_owner 整体释放
│   ├── registry.py     # 实例状态机 UNDEPLOYED→…→RUNNING；墓碑式删除
│   ├── process.py      # Popen + tail 线程 + 环形缓冲 + 自动重启
│   ├── packages.py     # 程序包缓存（魔数识别 + 完整性校验 + 死缓存清理）
│   ├── metrics.py      # 资源采样（含子进程，进程句柄复用）
│   ├── firewall.py     # ufw/firewalld 端口放行
│   ├── logutil.py      # 实例日志滚动与读取（50MB / 7 天双限）
│   ├── exports.py      # 备份产物目录（清单 / 单删 / 按天清理）
│   ├── scanner.py      # 安装根扫描：游离目录与游离进程
│   ├── scheduler.py    # 定时任务守护（每日重启 / 备份）
│   └── backup.py       # 备份导出与导入（full 整目录 / data 应用数据）
├── adapters/    # 适配器层：每个程序一份，实现统一契约
│   ├── base.py         # deploy / build_start_cmd / configure_login / write_conn_config
│   ├── sealdice.py     # serve.yaml 与 1.x 单文件 dice.yaml 双形态端点写入
│   ├── shiki.py        # AutoLogin.yml / config.txt 双版本识别；删实例保留存档
│   ├── olivadice.py    # OPK 组合部署，缺核阻断 / 缺件告警
│   ├── dicenext.py     # config/adapters.json 的 OneBot v11 适配器条目
│   ├── napcat.py       # WebUI API 写配置 + 手动兜底 WriteResult
│   ├── lagrange.py     # 无头部署预置配置；二维码落盘 qr-{uin}.png
│   ├── lagrange_milky.py # Milky 协议变体
│   ├── llbot.py        # JSON5 配置热更新；v8 需 AUTH TOKEN
│   ├── snowluma.py     # launcher.sh 启动 + WebUI 登录
│   └── yogurt.py       # Yogurt 登录端
├── services/    # 服务层
│   ├── wizard.py       # 五步向导状态机（断点续跑 + 冲突弹窗）
│   ├── login.py        # 登录流程（二维码/滑块链接转发）
│   └── resume.py       # 面板重启后自动拉回 RUNNING 但已死的实例
├── api/         # Web 服务层
│   ├── rest.py         # REST：向导 / 实例操作 / 程序包 / 备份产物 / 二次确认删除
│   ├── auth.py         # 随机密码 + PBKDF2 + Bearer token（恒定时间比较）
│   ├── ws_overview.py  # 通道1：拓扑 + 资源水位（2s）
│   ├── ws_logs.py      # 通道2：日志 tail（回放 + 过滤 + 错误标记）
│   ├── ws_login.py     # 通道3：二维码/滑块验证推送，refresh/skip_login
│   └── app.py          # 组装 + lifespan + 监听 127.0.0.1:8765
├── manifests/   # 10 份程序清单（4 骰子端 + 6 登录端）：下载地址、端口、配置路径、兼容矩阵
└── web/         # Vue3 前端：Overview（拓扑）/ LogCenter（日志）/ Wizard（向导）
```

## 数据与运行时位置

| 路径 | 内容 |
|---|---|
| `/var/lib/dicemanager/instances.json` | 实例注册表（含墓碑） |
| `/var/lib/dicemanager/ports.json` | 端口分配表 |
| `/var/lib/dicemanager/auth.json` | 管理凭据（持久化） |
| `/var/lib/dicemanager/packages/` | 程序包缓存（无 TTL，可一键清理未使用） |
| `/var/lib/dicemanager/exports/` | 备份产物：升级前快照 / 定时备份（向导「备份文件」区回收） |
| `/var/log/dicemanager/{id}.log(.1)` | 各实例日志（50MB / 7 天双限滚动） |
| `/opt/{程序名}` 及序号后缀 | 程序安装目录 |

## 配置说明（`api/context.py`）

```python
STATE_DIR = Path("/var/lib/dicemanager")   # 状态持久化目录
LOG_DIR   = Path("/var/log/dicemanager")   # 日志目录
resmon_alert = 0.90                        # 内存 ≥90% 红色告警（由 /ws/overview 每 2s 推送）
```

程序安装根目录由 manifests 的 `install_root` 决定，默认 `/opt`。
Windows 开发 / 非 root 运行可用环境变量覆盖路径：`DM_STATE_DIR`（默认 `/var/lib/dicemanager`）、
`DM_LOG_DIR`（默认 `/var/log/dicemanager`）。

## 开发与测试

```bash
pip install -r requirements-dev.txt
ruff check .                          # Lint（紧凑单行风格已豁免，见 pyproject.toml）
mypy                                  # 类型检查（core + services + api + adapters）
pytest tests/                         # 进程守护回归测试（自动重启线程/计数/seq 一致性）
python tests/smoke_local.py           # Windows 可跑的本地冒烟（fcntl 自动打桩）
```

推送后由 GitHub Actions（`.github/workflows/ci.yml`）跑同一套检查 + 前端构建。

## 常见问题

- **登录二维码不动了**：日志中心查看进程输出；点向导内「刷新二维码」会重启登录进程。
- **弹出滑块验证**：管理器只转发验证链接，不代做——请在浏览器手动完成。
- **LLBot 提示需要 AUTH TOKEN**：LLBot v8.0.9+ 强制要求，到快速登录平台申请后填入向导。
- **NapCat 提示手动配置**：WebUI API 不可达（token 失效/端口未回读成功）时的兜底，按提示在 NapCat WebUI 手动创建后回填。
- **删除实例端口被占用**：分配表按 owner 释放；若 30 天内有墓碑记录，同名目录/端口受保护。
- **忘记管理密码**：删除 `/var/lib/dicemanager/auth.json` 后重启，生成新密码。

## License

MIT
