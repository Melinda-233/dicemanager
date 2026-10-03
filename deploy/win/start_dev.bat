@echo off
REM DiceManager Windows 版开发启动脚本
REM 双击即用：启动 FastAPI 面板，监听 127.0.0.1:8765
REM 首次启动在 WebUI 设置管理密码（明文从不落盘、从不进控制台）

setlocal
REM 内核已共享到仓库根：core/、api/、manifests/、web/ 都在上一级
cd /d "%~dp0.."

REM 优先用项目内 Python（若有），否则用 PATH 中的 python
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 未找到 python，请先安装 Python 3.10+ 并加入 PATH
    pause
    exit /b 1
)

echo [ DiceManager Windows ] starting on http://127.0.0.1:8765
echo [ 首次启动管理密码见下方输出，关闭本窗口将丢失明文 ]
echo.

python -m api.app

echo.
echo [ DiceManager 已退出 ]
pause
endlocal
