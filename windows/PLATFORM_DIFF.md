# 平台差异对照表 — Linux 版 vs Windows 版

> 本表逐文件标注「同 / 改 / 新 / 弃」并给出 Windows 化的具体改造点。
> 规划阶段的核心交付物，实施阶段按此表逐项落地。
> 标注口径：
> - **同** = 可直接拷贝，无需改动
> - **改** = 需针对 Windows 调整（路径 / API / 命令 / 默认值）
> - **新** = Linux 版没有，Windows 版新增
> - **弃** = Linux 版有但 Windows 版不实现（及理由）

---

## 总览：Linux 特定调用清单

Linux 版共 38 个 Python 文件，Linux 硬绑定调用集中在以下 5 类：

| 类别 | 涉及文件 | Windows 化策略 |
|---|---|---|
| 路径硬编码 `/var/lib`、`/var/log`、`/opt`、`/tmp` | context.py、locks.py、manifests/*.json、app.py | 默认值改 `%LOCALAPPDATA%\dicemanager`，环境变量覆盖保留 |
| POSIX 进程信号 `killpg` / `SIGTERM` / `SIGKILL` | process.py | `CREATE_NEW_PROCESS_GROUP` + `taskkill /T /PID` |
| POSIX 文件锁 `fcntl.flock` | locks.py | 已有 `msvcrt.locking` 双实现，验证多实例场景；可选升级 pywin32 |
| 防火墙 `ufw` / `firewalld` | firewall.py | 单机本地工具监听 127.0.0.1，直接弃用 |
| Shell 启动脚本 `.sh` / `linuxqq-deb` 依赖 | manifests/*.json、adapters/*.py | exe 改 `.exe` / `.bat`，prerequisite 改 `windowsqq` |

---

## core/ 层

| 文件 | 标注 | 改造点 |
|---|---|---|
| `atomicio.py` | **同** | `mkstemp + os.replace + fsync` 全部跨平台 API，直接拷贝 |
| `locks.py` | **改** | 现有 `msvcrt.locking` 分支已可用（锁 1 字节）。改造点：① `_LOCK_DIR` 默认 `/tmp/dicemanager` 改为 `%LOCALAPPDATA%\dicemanager\locks`；② 验证多面板实例并发场景下 msvcrt 锁的可靠性（Linux 版注释称"退化为进程内锁"是误判，实际是跨进程锁，需复核）；③ 可选升级为 `pywin32` 的 `win32file.LockFileEx`，更贴近 flock 语义 |
| `process.py` | **改** | ① `start_new_session=_POSIX` → Windows 用 `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP`（等价语义，便于后续 `taskkill /T` 杀进程树）；② `stop()` 中 `os.killpg(os.getpgid(pid), SIGTERM)` → `subprocess.run(["taskkill", "/T", "/PID", str(pid)])`（taskkill /T 杀整个进程树，与 killpg 等价）；③ `signal.SIGKILL` 兜底 → `taskkill /F /T /PID`；④ 日志滚动、ring buffer、滑动窗口自动重启逻辑全部保留 |
| `ports.py` | **同** | 端口分配表逻辑与平台无关 |
| `registry.py` | **同** | 实例状态机、墓碑删除逻辑与平台无关 |
| `packages.py` | **同** | `zipfile` + 魔数识别 + sha256 校验全部跨平台 |
| `metrics.py` | **同** | `psutil` 跨平台，`psutil.Process(pid).memory_info().rss` 在 Windows 同样可用 |
| `logutil.py` | **改** | 仅改路径默认值：`%LOCALAPPDATA%\dicemanager\logs`；滚动逻辑（50MB / 7 天）保留 |
| `exports.py` | **同** | 备份产物目录管理逻辑与平台无关 |
| `scanner.py` | **改** | ① `_under()` 和 `match_program_dir()` 写死 `path.startswith(b + "/")`，Windows 需兼容 `\` 和 `/`，改用 `Path.is_relative_to()`（Python 3.9+）；② `psutil.process_iter` 跨平台，无需改；③ 安装根路径默认值改 `%LOCALAPPDATA%\dicemanager\programs` |
| `adopt.py` | **改** | ① `_conn_listen_pid_procfs` 是 Linux `/proc` 专属，加 `if os.name != "posix": return None` 平台判断；② `psutil.net_connections` / `Process.children` / `wait_procs` 跨平台，保留；③ `kill_process_tree` 用 psutil 跨平台，保留 |
| `scheduler.py` | **改** | 定时任务守护原为 threading + 时间检查，Windows 单机场景无需 Windows 任务计划程序，保留 threading 实现即可 |
| `backup.py` | **同** | tarfile 备份逻辑跨平台（pyproject.toml 锁 Python 3.12，tarfile filter= 新 API 可用） |
| `firewall.py` | **弃** | ufw/firewalld 是 Linux 专属。单机本地工具监听 127.0.0.1，外部无法访问，无需开端口。直接删除此文件，`open_port()` 调用点改为 no-op 或返回 None |

## adapters/ 层

所有适配器的 `build_start_cmd()` 需根据 manifest 的 `exe` 字段构造 Windows 命令。
**核心原则**：exe 字段在 manifest 里已是 Windows 形态（带 `.exe` / `.bat` 后缀），
适配器只需 `Path(instance.dir) / self.m["exe"]` 拼接，无需平台分支。

| 文件 | 标注 | 改造点 |
|---|---|---|
| `base.py` | **改** | ① 删 `from core.firewall import open_port`，改为本地 no-op `def open_port(port): return None`（firewall 弃用，监听 127.0.0.1 无需开端口，expose_webui 调用仍可用）；② 契约接口 `deploy / build_start_cmd / configure_login / write_conn_config` 与平台无关，保留 |
| `sealdice.py` | **改** | `build_start_cmd` 返回 `[sealdice-core.exe, --address=0.0.0.0:port]`；serve.yaml/dice.yaml 端点写入逻辑保留（YAML 跨平台） |
| `shiki.py` | **改** | 启动命令改 Windows exe；AutoLogin.yml / config.txt 双版本识别保留 |
| `olivadice.py` | **改** | OPK 组合部署，启动命令改 .exe；缺核阻断 / 缺件告警逻辑保留 |
| `dicenext.py` | **改** | `config/adapters.json` 写入逻辑保留；启动命令改 .exe |
| `napcat.py` | **改** | `NapCat.sh` → `NapCat.bat`（manifest exe 字段改）；WebUI API 写配置逻辑保留 |
| `lagrange.py` | **改** | `Implementations[]` 正/反向 WS 写入保留；启动命令改 .exe |
| `lagrange_milky.py` | **改** | Milky.HttpServer + Signer Token 写入保留；启动命令改 .exe |
| `lagrange_base.py` | **改** | 预置配置 / 执行位 / 二维码读盘 / 账号回读逻辑保留；执行位命令改 .bat |
| `llbot.py` | **改** | JSON5 配置热更新保留；启动命令改 .exe；v8 AUTH TOKEN 流程保留 |
| `snowluma.py` | **改** | `launcher.sh` → `launcher.bat`；WebUI 登录流程保留 |
| `yogurt.py` | **改** | 启动命令改 .exe |

## manifests/ 层

所有 manifest 改 3 个字段：`install_root`、`exe`、`prerequisite`。
**命名约定**：Windows 版 manifest 文件名加 `_win` 后缀（如 `sealdice_win.json`），
便于将来若做单仓库多平台清单合并时不冲突。

| 文件 | 标注 | 改造点 |
|---|---|---|
| `sealdice_win.json` | **改** | `install_root: %LOCALAPPDATA%\dicemanager\programs`；`exe: sealdice-core.exe`；其余字段（webui_default_port、compatible_login、bot_modes、release_page、download_strategy）保留 |
| `napcat_win.json` | **改** | `exe: NapCat.bat`；`prerequisite: windowsqq`（Windows QQ 客户端 .exe） |
| `shiki_win.json` | **改** | exe 改 Windows 形态 |
| `olivadice_win.json` | **改** | exe 改 .exe |
| `dicenext_win.json` | **改** | exe 改 .exe |
| `lagrange_win.json` | **改** | exe 改 .exe |
| `lagrange_milky_win.json` | **改** | exe 改 .exe |
| `llbot_win.json` | **改** | exe 改 .exe |
| `snowluma_win.json` | **改** | exe 改 .bat |
| `yogurt_win.json` | **改** | exe 改 .exe |

> `install_root` 用 `%LOCALAPPDATA%` 字面量占位，由 `core/pathutil.py`（新增）
> 在加载 manifest 时展开为真实路径，避免硬编码用户名。

## services/ 层

| 文件 | 标注 | 改造点 |
|---|---|---|
| `wizard.py` | **同** | 五步向导状态机、断点续跑、冲突弹窗逻辑与平台无关 |
| `resume.py` | **同** | 拉回 RUNNING 但已死实例的逻辑靠 `pm.launch()`，与平台无关 |

## api/ 层

| 文件 | 标注 | 改造点 |
|---|---|---|
| `context.py` | **改** | `STATE_DIR` / `LOG_DIR` 默认值从 `/var/lib/dicemanager`、`/var/log/dicemanager` 改为 `pathutil.default_state_dir()` / `default_log_dir()`（`%LOCALAPPDATA%\dicemanager` 与 `...\logs`）；环境变量 `DM_STATE_DIR` / `DM_LOG_DIR` 覆盖保留 |
| `auth.py` | **改** | `AUTH_FILE` 默认值从 `/var/lib/dicemanager/auth.json` 改为 `default_state_dir() / "auth.json"`；其余 PBKDF2 + Bearer token + 恒定时间比较跨平台无改动 |
| `app.py` | **同** | 第9-18行 umask 0o077 段已有 `os.name == "posix"` 判断，Windows 自动跳过，无需改动；第12行 `/var/lib/dicemanager` 写死路径在 posix 分支内，Windows 不执行；`_dist` 挂载逻辑跨平台 |
| `rest.py` | **同** | REST 路由、二次确认删除、`_under()` 用 `os.path.realpath` + `startswith(os.sep)` 已跨平台，无需改 |
| `ws_overview.py` | **同** | 拓扑 + 资源水位 WS 通道与平台无关 |
| `ws_logs.py` | **同** | 日志 tail WS 通道与平台无关 |
| `ws_login.py` | **同** | 二维码/滑块验证推送与平台无关 |

## web/ 层

| 文件 | 标注 | 改造点 |
|---|---|---|
| 整个 `web/` | **同** | Vue3 前端复用。仅 `vite.config` 的 backend proxy host 不同，可由 `.env.development` 配置；构建产物 `web/dist` 挂载逻辑同 |

## deploy/ 层（新增）

| 文件 | 标注 | 改造点 |
|---|---|---|
| `start_dev.bat` | **新** | 双击启动：`python -m api.app`，监听 127.0.0.1:8765；首次启动密码打印在控制台 |
| `README_win.md` | **新** | Windows 单机本地启动说明：前置条件、一键启动、密码获取、目录位置、已知限制、与 Linux 版差异摘要 |
| `start_dev.ps1` | **未做** | PowerShell 版启动脚本（可选，功能等价 .bat，单机本地工具不强制） |
| `install_service.ps1` | **未做** | 用 nssm 注册 Windows 服务（可选扩展点，仅留作未来"服务器共享版"升级路径） |

## tests/ 层

| 文件 | 标注 | 改造点 |
|---|---|---|
| `test_core_win.py` | **新** | 10 项断言覆盖 pathutil/locks/process/scanner/adopt/context/auth/app 全部 Windows 化改造点 |
| `test_manifest_integrity_win.py` | **新** | 8 项断言：加载 `*_win.json`、`platform: ["win32"]`、exe 带 `.exe/.bat` 后缀、install_root 用 `%LOCALAPPDATA%`、NapCat.bat / DiceNext.exe 必备文件回归 |
| `test_scanner.py` | **同** | Linux 版跨平台设计（用 `as_posix()` 拼接），直接拷贝，5 项断言 Windows 全通过 |
| `smoke_local_win.py` | **新** | Windows 端到端冒烟，对应 Linux 版 `tests/smoke_local.py`；7 项（auth 迁移+限速、清单加载、registry 状态机、ports 分配、wizard step5、路由顺序、Windows 路径接管） |
| `conftest.py` | **同** | 仓库根加 sys.path，跨平台直接拷贝 |
| `_verify_core_win.py` / `_verify_stage2_win.py` / `_verify_stage3_win.py` | **新** | 三阶段验收脚本，各阶段完成后验证 import 链与平台分支正确性 |

---

## 改造工作量估算

| 类别 | 文件数 | 工作量 |
|---|---|---|
| **同**（直接拷贝） | 14 | 极低 |
| **改**（路径/命令/默认值） | 20 | 中（多为机械替换） |
| **新**（Windows 专属） | 4 | 低 |
| **弃**（删除） | 1 | 极低 |
| **合计** | 39 | 中等 |

> 关键风险点：
> 1. `scanner.py` 路径分隔符兼容——必须用 `Path.is_relative_to()` 重写，正则分支不可靠
> 2. `process.py` 进程树终止——`taskkill /T` 在某些情况下杀不干净孙子进程，需回归测试
> 3. `locks.py` msvcrt 锁——Linux 版注释称"退化为进程内锁"需复核，可能误导
