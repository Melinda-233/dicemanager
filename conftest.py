"""pytest 全局配置：把仓库根加入 sys.path（所有测试共用的导入前提）。

背景（CI 实测踩坑）：测试模块普遍直接 `from core...` / `import api`，这要求仓库根
在 sys.path 里。裸跑 `pytest tests/` 时 pytest 只会把 tests/ 目录加进 sys.path，
仓库根不在其中；而本地习惯的 `python -m pytest` 会隐式把当前工作目录加进去，
所以「本地全绿、CI 报 ModuleNotFoundError」——差的是调用方式，不是代码。

放在仓库根的 conftest.py 由 pytest 自动加载（且 prepend 导入模式会把它所在目录
加入 sys.path），因此无论怎么调用都成立。这里再显式插一次，避免依赖导入模式的
实现细节。
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# 测试一律不碰生产路径（本机记录里的第二个坑）：
#
# api/auth.py 有模块级单例 `auth = Auth()`，其 __init__ 会立刻 write_atomic 写
# auth.json。在 server（Linux）语义下目标是 /var/lib/dicemanager，而 CI runner
# 非 root —— 于是**收集阶段**就 PermissionError，整个测试进程起不来。
#
# 本地之所以长期复现不出来：开发时为了绕本机沙箱的批量删除守卫，总是先 export
# DM_STATE_DIR 到临时目录，恰好把这条路径也一起盖住了。
#
# 必须在任何 core / api 模块被导入之前设置（环境变量 + setdefault 保留外部覆盖），
# 放在本文件顶部正为此——conftest 由 pytest 在收集前最先加载。
_TEST_ROOT = Path(tempfile.gettempdir()) / "dicemanager-test"
for _var, _sub in (("DM_STATE_DIR", "state"),
                   ("DM_LOG_DIR", "log"),
                   ("DM_LOCK_DIR", "lock")):
    os.environ.setdefault(_var, str(_TEST_ROOT / _sub))
# ---------------------------------------------------------------------------


def exe_name(base: str) -> str:
    """按当前 edition 返回清单里的可执行文件名：desktop 带 .exe，server 不带。

    Linux 与 Windows 清单同处 manifests/（<dice>.json / <dice>_win.json），
    load_registry 按 edition 只加载一侧，所以断言 exe / required_files 时必须
    跟着走，否则在对方 edition 下必然失败。
    """
    from core.edition import is_desktop
    return f"{base}.exe" if is_desktop() else base


def put_package(dice: str, data: bytes, source: str = "upload") -> dict:
    """测试辅助：把字节流落成本地程序包缓存，等价于旧的 pkgstore.save_archive。

    生产上传走流式落盘 + commit_archive，不存在「整体读入内存」的路径，
    故 save_archive 从 core 移除，仅保留测试侧的等价实现。
    用法：`from conftest import put_package`（ROOT 已在 sys.path 中）。
    """
    from core import packages as pkgstore
    tmp = pkgstore.pkg_dir() / f"{dice}.up.tmp"
    tmp.write_bytes(data)
    try:
        return pkgstore.commit_archive(dice, tmp, source=source)
    finally:
        tmp.unlink(missing_ok=True)
