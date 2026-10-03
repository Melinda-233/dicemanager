@echo off
REM DiceManager Windows 版 PyInstaller 打包脚本
REM 产出：dist\dicemanager.exe（单 exe，约 25MB，用户无需预装 Python）
REM 用法：双击本 .bat 或在命令行 deploy\win\build_exe.bat
REM 模式：--windowed（无 CMD 窗口，托盘后台运行 + 首次自设密码）
setlocal
REM 内核已共享：manifests/、web/dist、core/、api/ 等都在**仓库根**，
REM desktop 独有的入口（launcher.py / tray_icon.py）在 windows/ 下，
REM 故工作目录切到仓库根，入口按 windows\launcher.py 指定。
cd /d "%~dp0.."

REM 优先用项目内 Python（若有 venv），否则用 PATH 中的 python
set "PY=python"
if exist venv\Scripts\python.exe set "PY=venv\Scripts\python.exe"

REM 检查 web/dist 是否存在（前端 UI，没有则提示）
if not exist web\dist\index.html (
    echo [WARN] web\dist\index.html 不存在，打包出的 exe 将仅提供 API（无 UI）
    echo [INFO] 需 UI 时先：cd web ^&^& npm install ^&^& npm run build ^&^& cd ..
    echo.
)

REM 检查 pyinstaller
"%PY%" -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [INFO] 未找到 pyinstaller，正在安装...
    "%PY%" -m pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] pyinstaller 安装失败，请手动 pip install pyinstaller
        pause
        exit /b 1
    )
)

REM 检查托盘 + 密码弹窗依赖（pystray / Pillow / pywin32）
"%PY%" -c "import pystray, PIL, win32gui" 2>nul
if errorlevel 1 (
    echo [INFO] 缺少托盘/密码弹窗依赖，正在安装 pystray Pillow pywin32...
    "%PY%" -m pip install pystray Pillow pywin32
    if errorlevel 1 (
        echo [ERROR] 依赖安装失败，请手动 pip install pystray Pillow pywin32
        pause
        exit /b 1
    )
)

echo [ DiceManager Windows ] 开始打包（PyInstaller --onefile --windowed）
echo.

REM 清理旧产物
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist dicemanager.spec del /q dicemanager.spec

"%PY%" -m PyInstaller --noconfirm --onefile --windowed ^
    --name dicemanager ^
    --paths windows ^
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
    windows\launcher.py

if errorlevel 1 (
    echo.
    echo [ERROR] 打包失败，见上方日志
    pause
    exit /b 1
)

echo.
echo [完成] 产物在 dist\dicemanager.exe
dir dist\dicemanager.exe | findstr dicemanager.exe
echo.
echo [用法] 双击 dist\dicemanager.exe 启动（无 CMD 窗口，托盘后台运行）
echo [首次] 第一次启动会弹原生密码设置框，自设管理密码（≥6 位）
echo [托盘] 任务栏右下角骰子图标，右键菜单：打开面板 / 重启 / 退出
echo [状态] 状态目录在 exe 同级的 data 目录（auth.json / instances.json 等）
echo.
pause
endlocal
