# DiceManager Windows 版部署说明（单机本地工具）

> Linux 服务器版见 `../README.md` 与 `../deploy/install.sh`。Windows 版定位为
> 玩家在自己电脑上跑的单机本地工具：无需 systemd / nginx / 防火墙规则，双击即用。

## 1. 前置条件

| 项目 | 要求 |
|---|---|
| 系统 | Windows 10 1809+ / Windows 11 / Windows Server 2019+ |
| Python | **必须 3.10+**（代码使用 `X \| None` 语法，3.9 及以下会直接语法错误） |
| 内存 | 2G 可用（建议同时只跑 1-2 个骰子） |
| 端口 | 默认监听 `127.0.0.1:8765`，仅本机访问，无需在防火墙放行 |
| Node.js | **可选**，仅构建前端（`web/dist`）时需要。已有 dist 时双击即用 |

## 2. 一键启动

**方式一：双击 exe（推荐，无需预装 Python）**

双击 `dist\dicemanager.exe`：
1. **首次启动**会弹原生 Windows 密码设置框，自设管理密码（≥6 位，需输入两次确认）
2. 设置完成后**无 CMD 窗口**，程序进入后台运行
3. 任务栏**右下角**出现骰子图标
4. 右键骰子图标 →「打开面板」即可打开 `http://127.0.0.1:8765`
5. 右键菜单还提供「重启服务」「退出」

**方式二：开发模式（需 Python）**

双击 `deploy\start_dev.bat`，或在项目根目录打开 PowerShell：

```powershell
python -m api.app
```

> 开发模式会保留 CMD 窗口 + 控制台日志；首次启动若 auth.json 不存在，
> 会在控制台随机生成密码并打印一行 `[auth] 本次管理密码: xxx`。
> 忘记密码：删除 `<项目根>/data/auth.json` 后重启即可重置。

浏览器打开 `http://127.0.0.1:8765` 输入密码即可。

## 3. 首次部署准备（装依赖 + 构建前端）

仓库不带 `web/dist`，首次启动会打印 warning「前端构建产物不存在」，页面是空白 API 模式。
完整体验需要构建前端：

```powershell
# 1. 装 Python 依赖（建议先建 venv）
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

# 2. 构建前端（可选，仅需要 UI 时）
cd web
npm install
npm run build
cd ..
```

之后双击 `deploy\start_dev.bat` 即可。

## 4. 目录位置

| 路径 | 内容 |
|---|---|
| `<项目根>\dicemanager_win` | 程序本体（开发模式）/ exe 双击场景下为 exe 所在目录 |
| `<项目根>/data` | instances.json / ports.json / auth.json / schedules.json / metrics/ |
| `<项目根>/data/logs` | 各实例日志（与面板日志分开） |
| `<项目根>/data/locks` | 跨进程锁（msvcrt） |
| `<项目根>/package/<程序名>` | 骰子程序安装目录（由 pathutil.default_install_root() 决定） |
| `<项目根>/data/exports` | 备份导出产物 |

> 项目根的位置：
> - **开发模式**：`dicemanager_win/`（与代码同级目录）
> - **打包模式**（PyInstaller 双击 exe）：exe 所在目录（data 目录紧邻 exe）
>
> 自定义位置：设置环境变量 `DM_STATE_DIR` / `DM_LOG_DIR` 后启动即可（与 Linux 版同口径）。

## 5. 升级方式

```powershell
cd <项目根>
git pull
# 如改了依赖：pip install -r requirements.txt
# 如改了前端：cd web && npm install && npm run build && cd ..
```

重启 `start_dev.bat` 即生效。

## 6. 已知环境限制

- **msvcrt 文件锁**：Windows 用 `msvcrt.locking` 替代 `fcntl.flock`，多实例并发部署同一程序
  时锁可靠性略低于 Linux（msvcrt 是进程级劝告锁）。极高并发场景可升级 `pywin32` 的
  `win32file.LockFileEx`，但单机本地工具一般无需。
- **taskkill /T 终止进程树**：Linux 用 `os.killpg`，Windows 用 `taskkill /T /PID`。
  若骰子程序自身 fork 出独立子进程（罕见），可能杀不干净——这种情况会回退到
  `psutil.Process.children(recursive=True)` 逐个 kill。
- **监听 127.0.0.1**：单机本地工具不对外暴露，无需防火墙规则。如需局域网访问，
  自行改 `api/app.py` 的 `host`，并放行 Windows 防火墙 8765 端口。
- **未构建前端时后端仍能启动**（打印 warning），但页面空白 —— 需先 `npm run build`。
- **fastapi / starlette / uvicorn 版本上限**：与 Linux 版对齐（见 `requirements.txt`），
  不要随意 `pip install -U`：fastapi 0.141+ 会吞掉 WebSocket 路由导致总览/日志/扫码登录全 404。

## 7. 故障排查

| 现象 | 排查 |
|---|---|
| 双击 bat 一闪而过 | 命令行执行 `python -m api.app` 看完整报错；多半是缺 Python 或缺依赖 |
| 浏览器空白页 | 未构建前端，执行 `cd web && npm install && npm run build` |
| 总览/日志/扫码登录全 404 | fastapi/starlette/uvicorn 版本超上限，重装 `pip install -r requirements.txt` |
| 部署骰子失败 | 看控制台日志 + `<项目根>/data/logs/<实例id>.log` |
| 骰子进程关不掉 | 任务管理器看 PID，或 `taskkill /F /T /PID <pid>` 兜底 |
| 忘记密码 | 删除 `<项目根>/data/auth.json` 后重启 |

## 8. 打包为单 exe（PyInstaller，默认带 UI + 托盘后台运行）

把 Python 解释器 + 依赖 + 代码 + manifests + 前端 UI + 托盘 + 密码弹窗 全部打成
`dist\dicemanager.exe`（约 25MB），用户**无需预装 Python**，双击即用。

**核心特性**：
- `--windowed` 模式：无 CMD 窗口，程序后台运行
- 首次启动弹原生 Windows 密码设置框（自设管理密码，不打印明文到控制台）
- 任务栏右下角骰子图标，右键菜单：打开面板 / 重启服务 / 退出
- uvicorn 在子线程跑，托盘消息循环在主线程

```powershell
# 1. 装打包依赖（pyinstaller + 托盘 + 密码弹窗所需库）
pip install pyinstaller pystray Pillow pywin32

# 2. 确保前端已构建（web/dist 存在）
#    仓库自带 web/dist（Linux 版构建产物直接复用，前端源码跨平台）
#    若要重新构建：cd web && npm install && npm run build && cd ..

# 3. 双击打包脚本，或命令行执行
deploy\build_exe.bat

# 或手动执行（注意 --windowed 替代 --console）
python -m PyInstaller --noconfirm --onefile --windowed --name dicemanager ^
    --add-data "manifests;manifests" ^
    --add-data "web/dist;web/dist" ^
    --hidden-import uvicorn.logging ^
    --hidden-import uvicorn.protocols.http.auto ^
    --hidden-import uvicorn.protocols.websockets.auto ^
    --hidden-import uvicorn.protocols.websockets.wsproto ^
    --hidden-import uvicorn.protocols.websockets.websockets_impl ^
    --hidden-import uvicorn.lifespan.on ^
    --hidden-import uvicorn.lifespan.off ^
    --hidden-import psutil._pswindows ^
    --hidden-import pystray._win32 ^
    --hidden-import pywintypes ^
    --hidden-import win32gui ^
    --hidden-import win32con ^
    --hidden-import win32api ^
    --collect-submodules api ^
    --collect-submodules core ^
    --collect-submodules adapters ^
    --collect-submodules services ^
    --collect-submodules PIL ^
    launcher.py
```

产物在 `dist\dicemanager.exe`，可拷贝到任意 Windows 机器双击运行。

**注意事项**：
- 双击 exe → 首次弹密码框（自设 ≥6 位）→ 后台运行 → 任务栏右下角骰子图标
- 右键骰子图标 →「打开面板」打开 `http://127.0.0.1:8765`
- 「退出」会停掉整个进程（uvicorn 子线程 + 托盘）；「重启服务」会 spawn 新 exe 后退出当前
- 状态目录在 `<项目根>/data`（开发期为 dicemanager_win/data，打包后为 exe 同级 data）
- 若 `web\dist` 不存在时打包，build_exe.bat 会打印 warning，打包出的 exe 仅提供 API
  （访问根路径返回 404），需 UI 时先 `cd web && npm install && npm run build` 再重新打包。

## 9. 与 Linux 版的差异（核心）

详见 `../PLATFORM_DIFF.md`，摘要：

- **路径**：`/opt` / `/var/lib` → `<项目根>/data`
- **守护**：systemd → 双击 bat（前台进程，关窗即停）
- **防火墙**：ufw / firewalld → 弃用（监听 127.0.0.1）
- **进程终止**：`killpg` + SIGTERM → `taskkill /T /PID`
- **文件锁**：`fcntl` → `msvcrt`
- **进程组**：`start_new_session` → `CREATE_NEW_PROCESS_GROUP`
- **manifest**：`*_win.json`（exe 加 `.exe`/`.bat` 后缀，prerequisite 改 `windowsqq`）
