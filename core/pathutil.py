"""项目路径工具：路径常量统一引用，避免散落 os.environ.get。

平台策略（server / desktop 分化的 C5 点）：

- **POSIX（server，Linux 服务器部署）**：沿用 FHS 固定位置
  state=/var/lib/dicemanager、log=/var/log/dicemanager、lock=/tmp/dicemanager；
  install_root 不覆盖（manifest 自带 /opt 等部署位）。
- **Windows（desktop，单机本地）**：全部落在项目根 data/ 下，
  开发期 `<项目根>/data` 可见，打包后落在 exe 同级（用户可备份/迁移）。

DM_STATE_DIR / DM_LOG_DIR / DM_LOCK_DIR 仍可覆盖（测试与特殊部署用）。
"""  # noqa: D400
import os
import re
import sys
from pathlib import Path

from core.edition import is_desktop

_VAR_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")

# 统一以 edition 判定（而非 os.name）：DM_EDITION 显式指定时，路径策略必须跟着一起变，
# 否则在 Windows 上模拟 server 会出现「按 server 鉴权、按 desktop 存盘」的串味结果。
_DESKTOP = is_desktop()


def expand_windows_vars(path: str) -> str:
    """展开 %VAR% 形式的环境变量占位符（保留兼容旧测试与外部调用方）。

    未定义的变量保留原占位符（便于排查配置错误，而非静默变成空字符串）。

    历史：Windows 版早期把数据存在 %LOCALAPPDATA%\\dicemanager，manifest 的
    install_root 也用该占位符，但该占位符从未被真正展开（latent bug）。
    现 Windows 侧统一改为项目内 data/ 与 package/，此函数仅保留兼容。
    """
    def _sub(m):
        name = m.group(1)
        val = os.environ.get(name)
        return val if val is not None else m.group(0)
    return _VAR_RE.sub(_sub, path)


def project_root() -> Path:
    """项目根目录。

    - 开发模式：pathutil.py 在 core/ 下，其父目录的父目录即项目根。
    - frozen 模式（PyInstaller --onefile --windowed）：sys.executable 即
      dicemanager.exe，把 exe 同级目录视为项目根，便于用户找到 data/。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_state_dir() -> Path:
    """数据目录。POSIX 保持 /var/lib/dicemanager（不可改，服务器部署位）；
    Windows 为项目根/data。"""
    if _DESKTOP:
        return project_root() / "data"
    return Path("/var/lib/dicemanager")


def default_log_dir() -> Path:
    """日志目录。POSIX 保持 /var/log/dicemanager；Windows 为 data/logs。"""
    if _DESKTOP:
        return default_state_dir() / "logs"
    return Path("/var/log/dicemanager")


def default_lock_dir() -> Path:
    """锁目录。POSIX 保持 /tmp/dicemanager；Windows 为 data/locks。"""
    if _DESKTOP:
        return default_state_dir() / "locks"
    return Path("/tmp/dicemanager")


def default_install_root() -> Path | None:
    """骰子程序安装根。

    Windows（desktop）：项目根/package，登录端与应用端实例统一装在
    package/<dice>/，与 exe 同级可见。

    POSIX（server）：返回 None 表示**不覆盖** manifest 自带 install_root
    （Linux 部署位由清单给出，无占位符问题）。
    """
    if _DESKTOP:
        return project_root() / "package"
    return None
