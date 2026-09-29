# Windows 化路线图

> 分 5 个阶段，每阶段含明确验收标准。规划阶段产出本文件 + 目录骨架 + PLATFORM_DIFF.md，
> 实施阶段按本路线图逐阶段落地。

---

## 阶段 0：骨架搭建 ✅

**目标**：建立 `dicemanager_win/` 目录结构 + 规划文档。

**产出**：
- [x] 目录骨架（core / adapters / services / api / manifests / web / deploy / tests）
- [x] README.md（定位 + 与 Linux 版差异 + 快速开始）
- [x] PLATFORM_DIFF.md（逐模块对照表）
- [x] ROADMAP.md（本文件）
- [x] Windows 版 manifest 示例（sealdice_win.json）
- [x] pyproject.toml / requirements.txt / requirements-dev.txt（Windows 版）
- [x] deploy/start_dev.bat 启动脚本示例

**验收**：目录结构可被 git 跟踪；文档自洽，无悬而未决的设计点。

---

## 阶段 1：平台适配层（core/）✅

**目标**：完成 core 层 Windows 化，使所有平台特定调用走 Windows 路径。

**已完成**：
- [x] 拷贝 10 个无需改动的文件：`__init__.py` / `atomicio.py` / `ports.py` / `registry.py` / `packages.py` / `metrics.py` / `exports.py` / `scheduler.py` / `backup.py` / `logutil.py`
- [x] 新增 `core/pathutil.py`：`expand_windows_vars()` 展开 `%LOCALAPPDATA%` 等占位 + 默认路径常量
- [x] 改 `core/locks.py`：`_LOCK_DIR` 默认值改 `default_lock_dir()`（`%LOCALAPPDATA%\dicemanager\locks`）；修正 msvcrt 锁注释（非"进程内锁"）
- [x] 改 `core/process.py`：新增 `_terminate_tree()`（taskkill /T 替代 killpg）+ `_popen_kwargs()`（CREATE_NEW_PROCESS_GROUP 替代 start_new_session）
- [x] 改 `core/scanner.py`：`_under()` / `match_program_dir()` 改用 `Path.is_relative_to()` 兼容 `\` 和 `/`
- [x] 改 `core/adopt.py`：`_conn_listen_pid_procfs` 加 `os.name != "posix"` 平台判断
- [x] 弃 `core/firewall.py`：未拷贝（监听 127.0.0.1 无需开端口）
- [x] 验证：`py_compile core/*.py` 全通过；`tests/_verify_core_win.py` exit 0（pathutil 展开 + locks 平台分支 + process 标志 + adopt 回退）

**待办（mypy 留待后续阶段装环境后跑）**：
- [ ] `mypy --platform win32 core/` 通过（需先 `pip install mypy`）
- [ ] `pytest tests/test_core_win.py` 正式测试套件

---

## 阶段 2：manifests + adapters 命令改造 ✅

**目标**：所有 manifest 切换到 Windows 形态，适配器构造 Windows 启动命令。

**已完成**：
- [x] 10 份 manifest 改造为 `*_win.json`：install_root 改 `%LOCALAPPDATA%\dicemanager\programs`、exe 加 `.exe`/`.bat`、prerequisite 改 `windowsqq`、asset_name_pattern 改 `win-x64`、required_files 同步
- [x] 拷贝 13 份 adapter 文件（含 base.py / __init__.py / 10 个 adapter + lagrange_base）
- [x] 拷贝 3 份 service 文件（wizard / resume / __init__）
- [x] 改造 `adapters/base.py`：删 `from core.firewall import open_port`，改为本地 no-op `def open_port(port): return None`（firewall 弃用，监听 127.0.0.1 无需开端口）
- [x] **返工 `core/process.py`**：基于最新完整版（384行）重新改造，补 `re_adopt`/`_adopted_pid`/`_port_resolver`/`_try_readopt_now`/`is_alive` 改造/`probe` 改造/`ProcessManager.__init__` port_resolver 参数（resume.py 依赖 re_adopt；旧版 process.py 缺这些导致 Windows 版不完整）
- [x] 验证：`py_compile core+adapters+services` 全通过；`_verify_stage2_win.py` exit 0（manifest 字段 + pathutil 展开 + base.py no-op + process.py re_adopt）

**关键发现（阶段2返工点）**：
- `base.py` 第17行 `from core.firewall import open_port` — PLATFORM_DIFF 原标"同"，实际必须改（firewall 弃用后 import 会失败）
- `process.py` 实际 384 行（非上次读的 315 行），新增 re_adopt 接管逻辑 — resume.py 依赖，旧改造版严重不完整，已基于最新版返工

**待办（阶段3一起做）**：
- [ ] `adapters.load_registry()` 完整验证（需装 yaml/json5 第三方包）
- [ ] 向导 Step1 端到端验证（需 api 层就位）

---

## 阶段 3：api 层 + 部署/守护 ✅

**目标**：api 层 Windows 化，启动脚本可用，端到端跑通。

**已完成**：
- [x] 改 `api/context.py`：STATE_DIR / LOG_DIR 默认值改 `pathutil.default_state_dir()` / `default_log_dir()`（不再写死 `/var/lib`、`/var/log`）
- [x] 改 `api/auth.py`：AUTH_FILE 默认值改 `default_state_dir() / "auth.json"`
- [x] 拷贝 6 份无需改动的 api 文件：`__init__.py` / `app.py`（umask 段已自判 `os.name == "posix"`，Windows 自动跳过）/ `rest.py` / `ws_login.py` / `ws_logs.py` / `ws_overview.py`
- [x] 完善 `deploy/start_dev.bat`：双击启动 `python -m api.app`，监听 127.0.0.1:8765
- [x] 新增 `deploy/README_win.md`：Windows 单机本地启动说明（前置条件、一键启动、密码获取、目录位置、已知限制、与 Linux 版差异摘要）
- [x] 验证：`py_compile core+adapters+services+api` 全通过；`_verify_stage3_win.py` exit 0，20 项断言全通过（含 `build_context()` 成功构建，load_registry 跑通 10 份 manifest）

**未做（单机本地工具定位下不必要）**：
- 不做 systemd / nginx 反代（单机监听 127.0.0.1，无需对外暴露）
- 不做 web/dist 构建（前端构建属可选，后端能启动，UI 构建留给用户按需 `npm run build`）

**端到端冒烟**：见阶段4 `smoke_local_win.py`，全 7 项通过

---

## 阶段 4：测试与打包 ✅

**目标**：补齐 Windows 测试，可选打包为单 exe / 安装包。

**已完成**：
- [x] 新增 `tests/test_core_win.py`：10 项断言覆盖 pathutil/locks/process/scanner/adopt/context/auth/app 全部 Windows 化改造点
- [x] 新增 `tests/test_manifest_integrity_win.py`：8 项断言（含 `platform: ["win32"]`、exe 带 `.exe/.bat` 后缀、install_root 用 `%LOCALAPPDATA%`、NapCat.bat / DiceNext.exe 必备文件回归）
- [x] 拷贝 `tests/test_scanner.py`：5 项断言（Linux 版跨平台设计，用 `as_posix()` 拼接，Windows 直接通过）
- [x] 新增 `tests/smoke_local_win.py`：7 项端到端冒烟（auth 迁移+限速、清单加载、registry 状态机、ports 分配、wizard step5、路由顺序、Windows 路径接管）
- [x] 拷贝 `conftest.py`：仓库根加 sys.path（跨平台）
- [x] 验证：`pytest tests/` **23 passed**（10 core_win + 8 manifest_win + 5 scanner）；`smoke_local_win.py` **SMOKE_LOCAL_WIN_ALL_OK**；三个 `_verify_*_win.py` 全部 exit 0
- [x] **PyInstaller 单 exe 打包（默认带 UI）**：新增 `launcher.py`（入口，处理 `sys._MEIPASS` 路径修正）+ `deploy/build_exe.bat`（打包脚本）；拷贝 Linux 版 `web/`（含 dist 构建产物 + 源码，跳过 node_modules）到 `dicemanager_win/web/`；PyInstaller 加 `--add-data "manifests;manifests"` + `--add-data "web/dist;web/dist"`；产物 `dist/dicemanager.exe`（约 18MB），双击即用，无需预装 Python；验证：启动无「前端构建产物不存在」warning、`GET /` 返回 **200 + 472 字节 index.html**（`<title>DiceManager — 骰子管理器</title>`）、`GET /assets/index-*.js` 返回 **200 + 126390 字节** Vue 应用主 bundle、`/api/manifests` 返回 401（鉴权生效）、auth/resume/metrics 全部正常

**未做（可选，留给用户按需做）**：
- [ ] Inno Setup 安装包（在 PyInstaller exe 基础上包装开始菜单快捷方式 + 控制面板卸载入口）
- [ ] 把 `web/dist` 一并打包（需先 `npm run build`，再在 PyInstaller 命令加 `--add-data "web/dist;web/dist"`）
- [ ] 拷贝 Linux 版其余 20+ 个 test_*.py（涉及具体骰子程序的部署/登录/互联测试，需真实程序包，属集成测试范畴，非 Windows 化范畴）

**验收**：
- [x] `pytest tests/` 通过（23 项全绿）
- [x] `py_compile` 全量语法通过（47 个 .py 文件）
- [x] 三个阶段验证脚本 + smoke 全部 exit 0
- [x] PyInstaller 单 exe 产物 `dist/dicemanager.exe` 双击可运行，无需预装 Python，**默认带 UI**（访问 `http://127.0.0.1:8765` 见完整 DiceManager 面板）
- [ ] （可选）Inno Setup 安装包，带快捷方式 + 卸载入口

---

## 阶段 5：托盘后台运行 + 首次自设密码 ✅

**目标**：exe 双击后无 CMD 窗口、首次弹原生密码设置框、任务栏右下角骰子图标、右键菜单可打开面板/重启/退出。

**产出**：
- [x] **新增 `password_dialog.py`**：用 win32gui + win32con 写原生 Windows 对话框（两个 ES_PASSWORD 输入框 + 确认按钮 + 取消按钮）；校验：空密码拒绝、最少 6 位、两次不一致重弹；不依赖 tkinter（沙箱 Python 无 tkinter，PyInstaller 打包后绝对可用）
- [x] **新增 `tray_icon.py`**：用 Pillow 动态生成 64×64 骰子图标（白底圆角方块 + 6 黑点，无需 .ico 资源）；pystray 创建托盘；右键菜单：打开面板（default，双击托盘也触发）/ 重启服务（spawn 新 exe + os._exit）/ 退出（停托盘 + os._exit）；退出用 os._exit(0) 强制结束整个进程（含 uvicorn 子线程）
- [x] **改造 `launcher.py`**：① frozen 模式 sys.path/cwd 修正（保留）；② **在 import api.* 之前** 检查 AUTH_FILE 存在性 → 不存在则调 `password_dialog.show_password_dialog()` 弹窗获取密码 → 生成 salt + PBKDF2 hash + token 原子写入 auth.json（沿用 auth.py 格式，让后续 Auth() 读到现有文件不再随机生成）→ 用户取消则 `sys.exit(0)` 不启动服务；③ import api.app + tray_icon；④ 子线程跑 uvicorn.Server（install_signal_handlers 改 no-op，子线程不能装 signal handler）；⑤ 主线程跑 `tray_icon.run_tray()` 阻塞
- [x] **重新打包 PyInstaller**：`--windowed` 替代 `--console`（无 CMD 窗口，用 runw.exe bootloader）；新增 hidden imports：`pystray._win32` / `pywintypes` / `win32gui` / `win32con` / `win32api`；新增 `--collect-submodules PIL`；产物 25MB（多 pywin32 + pystray + Pillow ~7MB）
- [x] **更新 `deploy/build_exe.bat`**：模式说明改为「--windowed 无 CMD + 托盘后台」；新增依赖检查（pystray/Pillow/pywin32 自动装）；打包参数同步；末尾用法说明改为「双击 exe → 首次弹密码框 → 托盘后台运行」
- [x] **更新 `deploy/README_win.md`**：第 2 节「一键启动」改为「方式一：双击 exe（推荐）」+「方式二：开发模式」；第 8 节标题改为「打包为单 exe（PyInstaller，默认带 UI + 托盘后台运行）」，补核心特性与手动 PyInstaller 命令（含新 hidden imports）

**验收**：
- [x] `py_compile` 全量语法通过（含新增 password_dialog.py / tray_icon.py / 改造 launcher.py）
- [x] 双击 exe：无 CMD 窗口（tasklist 显示 Console 会话但无 MainWindowTitle）
- [x] exe 启动后进程稳定（两个 dicemanager.exe：bootloader 父 + 实际运行子）
- [x] uvicorn 监听 127.0.0.1:8765：`GET /` 200 + 472 字节 index.html、`GET /docs` 200 + 935 字节 Swagger UI、`GET /api/manifests` 401（鉴权生效）
- [x] auth/resume/metrics 后台线程正常启动
- [x] taskkill /F /IM dicemanager.exe 后无残留进程、8765 端口释放
- [ ] 首次启动密码弹窗 + 托盘右键菜单（GUI 交互，需用户人工双击 exe 验证）

**未做（可选）**：
- [ ] 托盘"打开面板"前轮询 8765 端口就绪状态（当前依赖 uvicorn 启动快，未做轮询）
- [ ] 托盘通知"服务已启动"气泡（pystray.Icon 支持 notify，未接）

---

## 风险与回退点

| 风险 | 触发条件 | 回退方案 |
|---|---|---|
| `taskkill /T` 杀不干净孙子进程 | 程序自身 fork 子进程 | 改用 `psutil.Process(pid).children(recursive=True)` + 逐个 kill |
| msvcrt 锁多实例不可靠 | 多面板实例并发部署同一程序 | 升级 `pywin32` 的 `win32file.LockFileEx` |
| `Path.is_relative_to()` 在 Python 3.9 以下不可用 | 用户装了老 Python | pyproject.toml 锁 Python 3.10+（与 Linux 版对齐） |
| 某 QQ 程序无 Windows 版 | 程序仅 Linux 发行 | manifest 标注 `platform: ["linux"]`，前端隐藏 |
