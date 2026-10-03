"""模拟 PyInstaller --windowed 双击环境：sys.stdout/stderr = None。

验证 launcher.py 的 StringIO 兜底 + api.app 的 setup_logging 在无 stdout 时不崩溃。
结果写到 result.txt。
"""
import sys
import os

RESULT = os.path.join(os.path.dirname(__file__), "_windowed_test_result.txt")

def log(msg):
    with open(RESULT, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

# 清空结果
open(RESULT, "w").close()

# 模拟 --windowed 双击：sys.stdout/stderr = None
sys.stdout = None
sys.stderr = None
log(f"step1: stdout={sys.stdout!r} stderr={sys.stderr!r}")

# stdout 兜底
import io
if sys.stdout is None:
    sys.stdout = io.StringIO()
    log("step2: stdout was None → StringIO")
if sys.stderr is None:
    sys.stderr = io.StringIO()
    log("step3: stderr was None → StringIO")

try:
    # import api.app（触发 setup_logging → StreamHandler(sys.stdout)）
    from api.app import app
    log("step4: import api.app OK")

    from core.logutil import console_only
    console_only("[test] console_only with StringIO stdout")
    log("step5: console_only OK")

    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="info")
    log("step6: uvicorn.Config OK")

    import tray_icon
    log("step7: import tray_icon OK")

    log("ALL OK: stdout fix works in --windowed env")
except BaseException as e:
    import traceback
    log(f"FAILED: {type(e).__name__}: {e}")
    log(traceback.format_exc())
    sys.exit(1)
