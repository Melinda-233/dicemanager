r"""解释器发现：管理器自带的 / 系统上的 Python 在哪（供 pip_project 类程序建venv）

为什么需要这一层
----------------
`pip_project` 类程序（NoneBot2，现在还有未来的 AstrBot）要在**每个实例目录里
建独立 venv**，前提是有一个真解释器可用。这件事在两侧的处境完全不同：

- **Linux（server）**：面板跑在 venv 或系统 python 上，`sys.executable` 就是
  真解释器，直接拿来 `python -m venv` 即可。没有额外问题。
- **Windows（desktop）**：面板是 PyInstaller `--onefile` 打出的单 exe，
  用户机**不装Python**（"双击即用"是既定定位）。而⚠️ **这个 exe 不能当解释器**：

      实测（2026-10-05，dicemanager.exe 25MB）
        > dicemanager.exe -V            → 启动整个面板（uvicorn + resume），rc=0
        > dicemanager.exe --version     → 同上，rc=0
        > dicemanager.exe -c "print(1)" → 同上，rc=0

  它**完全无视 argv**，且退出码是 0。任何"rc==0 就算解释器可用"的探测都会
  被它骗过——于是建出空壳 venv、pip 报一堆看不懂的错。故必须从打包产物里
  单独带一份真解释器。

Windows 侧的解法：embeddable Python
-----------------------------------
官方 embeddable 发行包（python.org 的 `python-X.Y-embed-amd64.zip`）解压后
`python.exe` 就是完整解释器，约 10MB，可直接放进 exe's `runtime/python/`。
用户双击即用，无需自装 Python，也不必破坏"便携单机"的定位。

⚠️ embeddable 版有两个必须处理的特点（不处理就装不了包）：
  1. **没有 pip**：`python._pth` 里 `import site` 被注释掉，`site-packages`
     不在搜索路径上。解法不是打开 `import site`（那会让 `._pth` 整体失效），
     而是在 `python311._pth` 末尾追加一行 `Lib\site-packages` 并自建 `Lib` 目录，
     再把 pip 装进去（见 deploy/prepare_runtime.py）。
  2. **无 `python3.exe` 别名**：清单候选里不要写 `python3`，embeddable 只有 `python.exe`。

本模块只负责**找到**解释器，不负责安装/修补 embeddable 包——那是打包流程的活。
分开是为了让运行时保持只读、可测。
"""
import os
import re
import sys
from pathlib import Path

# 打包产物内的相对路径：exe 同级 runtime/python/
BUNDLED_SUBPATH = ("runtime", "python")
# embeddable 的可执行文件名（无 python3.exe 别名，见模块 docstring）
WIN_PY_EXE = "python.exe"


def _is_frozen() -> bool:
    """是否跑在 PyInstaller 打包产物里。"""
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    """管理器可写根目录（frozen 取 exe 父目录，否则仓库根）。

    与 core/pathutil.project_root 同源；此处单独实现是为了让本模块
    **不依赖 pathutil**，从而在打包脚本等最小环境下也能 import。
    """
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundled_python_dir() -> Path:
    """exe 同级 `runtime/python/` 的绝对路径（不存在也返回该路径本身）。"""
    return project_root().joinpath(*BUNDLED_SUBPATH)


def bundled_interpreters() -> list[str]:
    """管理器自带的解释器候选路径（按优先级）。

    返回的是**存在的**可执行文件路径；不存在时返回空列表。
    Windows 侧带 embeddable Python，Linux 侧不捆绑故恒为空。
    """
    out: list[str] = []
    if os.name != "nt":
        return out
    py = bundled_python_dir() / WIN_PY_EXE
    if py.is_file():
        out.append(str(py))
    return out


# PyInstaller 产物名与本仓同名。列在这里而不是靠"路径在 exe 附近"来猜——
# 后者会把用户恰好放在 dist/ 下的真实 python.exe 也误杀。
_FROZEN_NAMES = {"dicemanager.exe", "dicemanager"}


def is_frozen_like(path: str) -> bool:
    """该路径是否疑似「面板自己的打包产物」，不能当通用解释器。

    两个判据任一命中即返回 True：

    1. **当前进程就是 frozen 且候选正是自身** —— PyInstaller 的 argv 语义
       与解释器不同（见模块 docstring 的实测），是最确凿的信号。
    2. **候选文件名等于面板产物名** —— 保守排除，宁可少认一个解释器，
       也不能把面板自己拉起来当解释器用（那会递归起服务、占8765 端口）。
    """
    name = Path(path).name.lower()
    if name in _FROZEN_NAMES:
        return True
    if _is_frozen():
        try:
            if Path(path).resolve() == Path(sys.executable).resolve():
                return True
        except OSError:
            pass
    return False


_VERSION_RE = re.compile(r"Python\s+\d+\.\d+")
_VERSION_TUPLE_RE = re.compile(r"\(\s*\d+\s*,\s*\d+\s*\)")


def looks_like_python(path: str, timeout: int = 30) -> bool:
    """探测某路径是否是**真解释器**（能响应 `-V` 且真能执行代码）。

    为什么不只看退出码：面板 exe 传任何参数都会启动服务并rc=0（实测）。
    故要求版本 banner 文本里含 `Python x.y`（面板打印的是 uvicorn 启动日志，
    两者不可能混淆），并再跑一次 `-c` 求值自检。

    只判定"像"，不保证版本/依赖满足——调用方仍需自己核对版本下限。
    """
    if is_frozen_like(path):
        return False
    import subprocess
    try:
        r = subprocess.run([path, "-V"], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        return False
    if not _VERSION_RE.search(f"{r.stdout or ''}{r.stderr or ''}"):
        return False
    try:
        r2 = subprocess.run(
            [path, "-c", "import sys; print(sys.version_info[:2])"],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return r2.returncode == 0 and bool(_VERSION_TUPLE_RE.search(r2.stdout or ""))


def python_version(path: str, timeout: int = 30) -> tuple[int, int] | None:
    """取解释器的 (major, minor)；不是解释器或探测失败返回 None。

    供清单声明的 `python_requires`（如 nonebot2 的 ≥3.10）做前置校验——
    版本不够时应给用户一句人话，而不是让pip 装到一半才炸。
    """
    import subprocess
    if is_frozen_like(path):
        return None
    try:
        r = subprocess.run(
            [path, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)", r.stdout or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def find_interpreter(candidates=None, env_var: str = "DM_PYTHON",
                     requires=None) -> str | None:
    """按统一优先级找一个真解释器：环境变量 > 自带 > 候选 > sys.executable。

    `requires` 形如 `[(3, 10)]`（清单的 `python_requires`）：给出时按**版本下限**
    过滤，每个候选都要 `python_version(...) >= requires` 才算可用。
    找不到合格的返回 None（调用方负责报「需要 Python ≥X.Y」）。

    ⚠️ 环境变量指定的版本不够时**不再回退**到别的候选：用户显式指定了
    DM_PYTHON 却悄悄换成另一个解释器，比直接报错更难排查。
    """
    ok = _meets(requires, lambda p: python_version(p))
    if override := os.environ.get(env_var, "").strip():
        return override if looks_like_python(override) and ok(override) else None
    for cand in bundled_interpreters():
        if looks_like_python(cand) and ok(cand):
            return cand
    for name in (candidates or []):
        from shutil import which
        found = which(name)
        if found and looks_like_python(found) and ok(found):
            return found
    if sys.executable and not is_frozen_like(sys.executable) \
            and looks_like_python(sys.executable) and ok(sys.executable):
        return sys.executable
    return None


def _meets(requires, getter):
    """按 `requires` 下限过滤候选；requires 为空则恒真。

    `requires` 形如 `[(3, 10)]`；**取各项的最小值**作为门槛——清单写多项
    是表达「支持这些版本」，门槛理应是最低的那个。多项之间是「或」关系。
    """
    if not requires:
        return lambda path: True
    floor = min(tuple(v) for v in requires)
    return lambda path: (v := getter(path)) is not None and tuple(v) >= floor


# ---------- Node.js（npm_project 类程序，如 Koishi）----------

# node 的版本字符串有两种形态，**取决于怎么问**：
#   `node -v`               → "v20.11.1"   （带 v）
#   `node -p process.versions.node` → "20.11.1"（不带 v）
# 正则要同时吃下两种，别只按其中一种写（踩过：只认带 v 的，导致自检永远不过）。
_NODE_VER_RE = re.compile(r"v?(\d+)\.(\d+)")


def looks_like_node(path: str, timeout: int = 30) -> bool:
    """探测某路径是否是**真 node**（能响应 `-v` 且真能执行代码）。

    与 `looks_like_python` 同思路：不能只看退出码——面板 exe 传任何参数都会
    启动服务并 rc=0（见模块 docstring 的实测）。故要求版本 banner 形如
    `v20.11.1`，并再跑一次 `-p` 求值自检。
    """
    if is_frozen_like(path):
        return False
    import subprocess
    try:
        r = subprocess.run([path, "-v"], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0 or not _NODE_VER_RE.search(f"{r.stdout or ''}{r.stderr or ''}"):
        return False
    try:
        r2 = subprocess.run([path, "-p", "process.versions.node"],
                            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return r2.returncode == 0 and bool(_NODE_VER_RE.search(r2.stdout or ""))


def node_version(path: str, timeout: int = 30) -> tuple[int, int] | None:
    """取 node 的 (major, minor)；不是 node 或探测失败返回 None。"""
    import subprocess
    if is_frozen_like(path):
        return None
    try:
        r = subprocess.run([path, "-v"], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    m = _NODE_VER_RE.search(f"{r.stdout or ''}{r.stderr or ''}")
    return (int(m.group(1)), int(m.group(2))) if m else None


def find_node(candidates=None, env_var: str = "DM_NODE", requires=None) -> str | None:
    """找一个真 node：环境变量 > 候选（which）> 面板自身所在目录旁的 node。

    优先级与 `find_interpreter` 对齐。⚠️ 面板是 PyInstaller 单 exe，
    **不捆绑 Node**（Node 运行时 80MB+，与"便携单机"定位冲突），
    故没有 `bundled_node()` 一层；用户需自装 Node.js LTS。
    找不到时由调用方报「需安装 Node.js ≥18」。
    """
    ok = _meets(requires, lambda p: node_version(p))
    if override := os.environ.get(env_var, "").strip():
        return override if looks_like_node(override) and ok(override) else None
    for name in (candidates or []):
        from shutil import which
        found = which(name)
        if found and looks_like_node(found) and ok(found):
            return found
    return None


def node_with_npm(node: str) -> str | None:
    """从 node 可执行文件推出同目录的 npm 入口（跨平台）。

    Windows 上 npm 是 `npm.cmd`（`npm` 是 shell 脚本，Popen 直接跑会失败），
    Linux/macOS 上是 `npm`。都找不到时返回 None——宁可不装，也不要跑错文件。
    """
    d = Path(node).resolve().parent
    for name in (("npm.cmd", "npm") if os.name == "nt" else ("npm", "npm.cmd")):
        p = d / name
        if p.is_file():
            return str(p)
    return None
