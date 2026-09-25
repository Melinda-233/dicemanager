"""pytest 全局配置：把仓库根加入 sys.path（所有测试共用的导入前提）。

背景（CI 实测踩坑）：测试模块普遍直接 `from core...` / `import api`，这要求仓库根
在 sys.path 里。裸跑 `pytest tests/` 时 pytest 只会把 tests/ 目录加进 sys.path，
仓库根不在其中；而本地习惯的 `python -m pytest` 会隐式把当前工作目录加进去，
所以「本地全绿、CI 报 ModuleNotFoundError」——差的是调用方式，不是代码。

放在仓库根的 conftest.py 由 pytest 自动加载（且 prepend 导入模式会把它所在目录
加入 sys.path），因此无论怎么调用都成立。这里再显式插一次，避免依赖导入模式的
实现细节。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
