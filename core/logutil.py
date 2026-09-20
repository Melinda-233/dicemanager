"""统一日志：控制台 + 滚动文件（10MB × 3），替换散落的 print()

用法：模块内 `log = logging.getLogger("dicemanager.xxx")`，入口处调一次
setup_logging(log_dir)。注意：管理密码等敏感信息只允许走控制台 handler
（console_only=True），不要落盘到 dicemanager.log。"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False

def setup_logging(log_dir: Path | None = None, level: int = logging.INFO) -> logging.Logger:
    """幂等初始化；log_dir 提供时额外写滚动文件（生产排障用）。"""
    global _CONFIGURED
    root = logging.getLogger("dicemanager")
    if _CONFIGURED:
        return root
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    console = logging.StreamHandler(sys.stdout)      # systemd journal 捕获 stdout
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_dir is not None:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(Path(log_dir) / "dicemanager.log",
                                 maxBytes=10 * 1024 * 1024, backupCount=3,
                                 encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

    _CONFIGURED = True
    return root

def console_only(msg: str) -> None:
    """只输出到控制台（不落盘）：用于管理密码这类敏感横幅。"""
    print(msg, flush=True)
