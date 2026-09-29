"""项目内 data 路径工具：路径常量统一引用，避免散落 os.environ.get。

数据保存位置：项目根目录下的 data 子目录。
- 开发模式：pathutil.py 在 core/ 下，上两级即项目根 dicemanager_win，data 落在
  dicemanager_win/data（开发期可见，便于排查）。
- 打包模式（PyInstaller --onefile --windowed）：sys.executable 即 dist 下生成的
  dicemanager.exe，data 落在 exe 同级目录（用户可见、可备份、可迁移）。

manifest 的 install_root 字段不再使用 %LOCALAPPDATA% 占位符（旧版保留兼容），
adapters 加载清单时统一覆盖为 default_install_root()，让骰子程序统一装进项目根 package/。
"""
import os
import re
import sys
from pathlib import Path

_VAR_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


def expand_windows_vars(path: str) -> str:
    """展开 %VAR% 形式的环境变量占位符（保留兼容旧测试与外部调用方）。

    未定义的变量保留原占位符（便于排查配置错误，而非静默变成空字符串）。
    """
    def _sub(m):
        name = m.group(1)
        val = os.environ.get(name)
        return val if val is not None else m.group(0)
    return _VAR_RE.sub(_sub, path)


def project_root() -> Path:
    """项目根目录：

    - 开发模式：pathutil.py 在 core/ 下，其父目录的父目录即项目根 dicemanager_win。
    - frozen 模式（PyInstaller --onefile --windowed）：sys.executable 即
      dist/dicemanager.exe，把 exe 同级目录视为项目根，便于用户找到 data/、备份迁移。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_state_dir() -> Path:
    """数据目录：项目根/data。

    旧版用 %LOCALAPPDATA%\\dicemanager，现统一改为项目内 data 目录，
    开发期可见、打包后位于 exe 同级（用户可备份/迁移）。DM_STATE_DIR 仍可覆盖（仅测试/特殊部署用）。
    """
    return project_root() / "data"


def default_log_dir() -> Path:
    return default_state_dir() / "logs"


def default_lock_dir() -> Path:
    return default_state_dir() / "locks"


def default_install_root() -> Path:
    """骰子程序安装根：项目根/package（登录端与应用端实例统一装在
    package/<dice>/，如 dicemanager_win/package/sealdice），与 exe 同级可见，
    便于用户直接找到程序目录。数据/缓存仍在 data/ 下，整体备份不受影响。"""
    return project_root() / "package"
