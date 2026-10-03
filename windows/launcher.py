"""DiceManager Windows 版启动入口（PyInstaller 打包用，--windowed 无 CMD 窗口）。

打包后启动流程：
1. **stdout/stderr 兜底**：--windowed 模式下 sys.stdout/sys.stderr 为 None，
   任何 print()/StreamHandler(sys.stdout) 会崩溃。用 io.StringIO 替代，避免崩溃。
2. frozen 模式把 _MEIPASS 加入 sys.path + 设为 cwd（让 manifests/web/dist 相对路径可解析）
3. 启动 uvicorn 在子线程（daemon=True，主线程退出时自动结束）
   - 子线程不能装 signal handler，用 Server + install_signal_handlers=no-op
4. 主线程跑 tray_icon.run_tray() 阻塞（系统托盘消息循环）
5. 用户从托盘"退出" → os._exit(0) → 整个进程结束（含 uvicorn 子线程）

**首次启动密码设置**：不再由 launcher 弹原生密码框（早期方案在精简 Windows 环境下
不一定弹得出）。改为 WebUI 流程：auth.json 不存在时 api/auth.py 的 Auth() 进入
「未初始化」态，前端访问任意 API（含 /api/needs-setup）探测后切到「设置管理密码」
界面，用户输入两次新密码 → POST /api/setup → 写入 auth.json → 自动登录。
launcher 直接启动 uvicorn，不阻断服务，用户在浏览器里完成首次设置。
"""
import io
import os
import sys
import threading
import time
from pathlib import Path


# ---- 0. 启动诊断（必须在任何可能崩溃的代码之前）----
def _diag(msg: str) -> None:
    """写诊断日志到 <state_dir>/diag.log，定位 frozen 模式崩溃点。

    注意：本函数在 import 链最早期就被调用，必须自包含、不依赖任何后续模块。
    """
    try:
        from core.pathutil import default_state_dir
        state_dir = Path(os.environ.get("DM_STATE_DIR",
                         str(default_state_dir())))
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_dir / "diag.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass

# frozen 模式下 sys.stdout/stderr 的实际值（写日志看）
_diag(f"launcher start: frozen={getattr(sys, 'frozen', False)} "
      f"stdout={sys.stdout!r} stderr={sys.stderr!r} "
      f"executable={sys.executable}")


# ---- 1. stdout/stderr 兜底（必须在任何 print/import 之前）----
# PyInstaller --windowed 模式下 sys.stdout / sys.stderr 可能是 None，
# 会让 logutil.StreamHandler(sys.stdout) 和 print() 崩溃。
# 用 StringIO 替代：输出被丢弃，但 logging 文件 handler 仍正常工作。
if sys.stdout is None:
    sys.stdout = io.StringIO()
    _diag("stdout was None → replaced with StringIO")
if sys.stderr is None:
    sys.stderr = io.StringIO()
    _diag("stderr was None → replaced with StringIO")


# ---- 2. 模块解析路径修正 ----
if getattr(sys, "frozen", False):
    _base = Path(sys._MEIPASS)
    if str(_base) not in sys.path:
        sys.path.insert(0, str(_base))
    try:
        os.chdir(_base)
    except OSError:
        pass
else:
    # 开发模式：共享内核（core/ api/ adapters/ services/）在仓库根，不在 windows/ 下。
    # 内核只有一份，windows/ 仅保留 desktop 独有的入口与清单，故必须把仓库根
    # 加进 sys.path，否则 `from core...` / `import api` 解析不到。
    _repo = Path(__file__).resolve().parent.parent
    if str(_repo) not in sys.path:
        sys.path.insert(0, str(_repo))
    try:
        os.chdir(_repo)
    except OSError:
        pass


# ---- 3. 启动主流程（全程 try 包裹，崩溃写 crash.log）----
def _bootstrap() -> None:
    """全程 try：import api.app + tray_icon + 启动 uvicorn 子线程 + 跑托盘。

    --windowed 模式双击崩溃无任何提示，所有异常写 crash.log 便于排查。
    """
    try:
        _diag("_bootstrap enter")
        import uvicorn
        _diag("after import uvicorn")
        from api.app import app
        _diag("after from api.app import app")
        import tray_icon
        _diag("after import tray_icon")

        def _run_uvicorn() -> None:
            """子线程跑 uvicorn.Server（install_signal_handlers 改 no-op）。"""
            config = uvicorn.Config(app, host="127.0.0.1", port=8765,
                                    log_level="info")
            server = uvicorn.Server(config)
            server.install_signal_handlers = lambda: None
            server.run()

        _diag("before uvicorn thread start")
        # 子线程跑 uvicorn（daemon=True → 主进程退出时自动结束）
        t = threading.Thread(target=_run_uvicorn, name="uvicorn-server",
                            daemon=True)
        t.start()
        _diag("after uvicorn thread start, before tray run")

        # 主线程跑托盘（阻塞，直到用户点"退出"）
        # 关键兜底：托盘启动失败（frozen 模式下 pystray 可能因 GUI 子系统不可用而抛异常）
        # 时不能让主线程退出，否则 daemon=True 的 uvicorn 子线程会被一起杀死。
        # 兜底用 sleep 死循环保活，让 API 至少还能访问；用户可用 taskkill 退出。
        try:
            tray_icon.run_tray()
            _diag("tray_icon.run_tray returned normally (should not happen)")
        except BaseException as e:
            _diag(f"tray crashed: {type(e).__name__}: {e}; "
                  f"uvicorn 仍在子线程跑，主线程进入 sleep 保活")
            _crash_log(e)
            # 不 re-raise：保活让 API 继续服务，用户可从 taskkill 或托盘仍可点击（如果还能点）退出
            import time as _t
            while True:
                _t.sleep(3600)
    except BaseException as e:
        _diag(f"_bootstrap crashed: {type(e).__name__}: {e}")
        _crash_log(e)
        raise


def _crash_log(exc: BaseException) -> None:
    """--windowed 模式下双击崩溃无任何提示，把异常写入 crash.log 便于排查。"""
    try:
        from core.pathutil import default_state_dir
        log_path = Path(os.environ.get("DM_STATE_DIR",
                                      str(default_state_dir()))) / "crash.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        import traceback
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"DiceManager launcher crashed at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"sys.executable: {sys.executable}\n")
            f.write(f"frozen: {getattr(sys, 'frozen', False)}\n")
            f.write(f"stdout is None: {sys.stdout is None}\n")
            f.write(f"_MEIPASS: {getattr(sys, '_MEIPASS', 'N/A')}\n")
            f.write(f"exc type: {type(exc).__name__}\n")
            f.write(f"exc msg: {exc}\n")
            f.write("\n--- traceback ---\n")
            traceback.print_exc(file=f)
    except Exception:
        pass  # 崩溃日志写入失败也不影响退出


if __name__ == "__main__":
    _bootstrap()
