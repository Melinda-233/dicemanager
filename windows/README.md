# DiceManager Windows 版 — 骰子管理器（单机本地）

`dicemanager_win` 是 [dicemanager](../dicemanager)（Linux 服务器版）的 **Windows 单机本地移植**。
在 Windows 10/11 上统一管理本机 QQ 骰子程序的 Web 管理器，支持 **4 个骰子端 + 6 个登录端**
（共 10 份程序清单）的一键部署、登录承载、互联配置与进程守护。

> 本目录为**规划阶段产物**：包含目录骨架、平台差异对照、改造路线图、Windows 版 manifest
> 与配置示例。具体 Python 文件实施见 [ROADMAP.md](ROADMAP.md)。

## 与 Linux 版的关系

| 维度 | Linux 版 (`dicemanager/`) | Windows 版 (`dicemanager_win/`) |
|---|---|---|
| 定位 | 服务器共享版，多账号承载 | 单机本地工具，单用户 |
| 代码组织 | 主仓库 | 独立副本（不共用代码，分支演进） |
| 运行账户 | root 或专用账户 | 当前用户（双击即用） |
| 数据/日志路径 | `/var/lib/dicemanager`、`/var/log/dicemanager` | `<项目根>/data`、`<项目根>/data/logs` |
| 程序安装根 | `/opt` | `<项目根>/package` |
| 守护方式 | systemd + Popen 自动重启 | Popen 自动重启（无需服务化） |
| 防火墙 | ufw / firewalld | 弃用（监听 127.0.0.1，外部不可达） |
| 凭据保护 | `umask 0o077` + 文件 `0o600` | 项目目录天然隔离（双击 exe 落在用户目录下） |
| 服务化 | systemd unit | 不需要；开机自启走启动文件夹快捷方式 |

## 支持的程序

同 Linux 版：4 骰子端（海豹 SealDice / 溯洄 Dice! / 青果 OlivaDice / Dice!Next）+
6 登录端（NapCatQQ / Lagrange.OneBot / Lagrange.Milky / LLBot / SnowLuma / Yogurt）。
Windows 版 manifest 见 `manifests/*_win.json`，exe 字段改为 Windows 形态（`.exe` / `.bat`）。

## 快速开始（规划态，实施后可用）

```bat
:: 1. 安装 Python 3.10+ 与依赖
pip install -r requirements.txt

:: 2. 构建前端
cd web && npm install && npm run build && cd ..

:: 3. 双击启动（首次启动密码打印在控制台）
deploy\start_dev.bat
::    监听 127.0.0.1:8765

:: 4. 浏览器访问 → 输入密码登录
```

> 单机本地用户：直接双击 `deploy\start_dev.bat`，或将其快捷方式放入
> `shell:startup`（启动文件夹）实现开机自启。无需 nssm / Windows 服务。

## 目录结构

```
dicemanager_win/
├── README.md                   # 本文件
├── ROADMAP.md                  # Windows 化路线图（阶段划分）
├── PLATFORM_DIFF.md            # 平台差异对照表（核心交付物）
├── pyproject.toml              # platform=win32，新增可选 pywin32
├── requirements.txt            # 同 Linux 版 + 可选 pywin32
├── requirements-dev.txt
├── conftest.py                 # 占位
├── pytest.ini
├── core/                       # 平台适配层（Windows 化版）
│   ├── atomicio.py             # 同：原子写跨平台
│   ├── locks.py                # 改：_LOCK_DIR 默认值；msvcrt 锁复核
│   ├── process.py              # 改：CREATE_NEW_PROCESS_GROUP + taskkill /T
│   ├── ports.py                # 同
│   ├── registry.py             # 同
│   ├── packages.py             # 同
│   ├── metrics.py               # 同
│   ├── logutil.py               # 改：路径默认值
│   ├── exports.py               # 同
│   ├── scanner.py               # 改：Path.is_relative_to() 替代正则
│   ├── scheduler.py             # 同
│   ├── backup.py                # 同
│   └── pathutil.py              # 新：展开 %LOCALAPPDATA% 等环境变量占位
├── adapters/                   # 适配器：build_start_cmd 改 .exe/.bat
├── services/                   # 同 Linux 版（向导状态机）
├── api/                        # 同 Linux 版，context.py 路径改 %LOCALAPPDATA%
├── manifests/                  # Windows 版清单：install_root/exe/prerequisite 改
├── web/                        # Vue3 前端复用（仅 backend host 配置不同）
├── deploy/                     # Windows 部署：start_dev.bat 等
└── tests/                      # Windows 冒烟测试
```

## 数据与运行时位置

| 路径 | 内容 |
|---|---|
| `<项目根>/data/instances.json` | 实例注册表（含墓碑） |
| `<项目根>/data/ports.json` | 端口分配表 |
| `<项目根>/data/auth.json` | 管理凭据（持久化） |
| `<项目根>/data/packages/` | 程序包缓存（无 TTL，可一键清理未使用） |
| `<项目根>/data/exports/` | 备份产物：升级前快照 / 定时备份 |
| `<项目根>/data/logs/{id}.log(.1)` | 各实例日志（50MB / 7 天双限滚动） |
| `<项目根>/package/` | 程序安装目录（按程序名 + 序号后缀） |

> 项目根：
> - **开发模式**：`dicemanager_win/`（与代码同级，便于排查）
> - **打包模式**（PyInstaller exe 双击）：exe 所在目录（即 `dist/` 或用户拷贝到的目录），data 紧邻 exe
>
> 仍可经环境变量 `DM_STATE_DIR` / `DM_LOG_DIR` 覆盖（用于测试与特殊部署）。

## 配置说明（`api/context.py`）

```python
from core.pathutil import default_state_dir, default_log_dir
STATE_DIR = Path(os.environ.get("DM_STATE_DIR", str(default_state_dir())))
LOG_DIR = Path(os.environ.get("DM_LOG_DIR", str(default_log_dir())))
```

程序安装根目录由 `core.pathutil.default_install_root()` 决定（
`<项目根>/package`），`adapters/__init__.py` 的 `load_registry()` 加载时
统一覆盖 manifest 的 `install_root` 字段。

## 常见问题（规划态预判）

- **首次启动找不到密码**：开发模式下，密码打印在启动 .bat 弹出的控制台窗口，关闭即丢失明文，
  只剩 PBKDF2 哈希。忘记密码请删除 `<项目根>/data/auth.json` 后重启。
- **程序启动后立即退出**：可能是 prerequisite 未安装（如 NapCat 需要 Windows QQ 客户端）。
  向导 Step1 会检查 prerequisite，缺失则给出下载链接。
- **端口被占用**：分配表按 owner 释放；端口冲突自动 +1 重试。
- **关机后实例丢失**：关机会杀掉所有子进程，registry 仍记 RUNNING。重启面板后
  lifespan 会拉回 RUNNING 但已死的实例（与 Linux 版行为一致）。
- **杀进程树不干净**：`taskkill /T` 在极少数情况下杀不干净孙子进程（如程序自身 fork），
  回归测试需覆盖。

## License
MIT
