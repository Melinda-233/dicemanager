"""core 层 import 链验证脚本（阶段1验收用，验证后可删）"""
import sys
import os
from pathlib import Path

# 把仓库根加入 sys.path（与 _verify_stage2_win / _verify_stage3_win 同口径）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 确认在 Windows 平台
print("platform:", os.name)
assert os.name == "nt", "此验证脚本预期在 Windows 运行"

from core import pathutil, locks

# pathutil 展开 %LOCALAPPDATA%（保留兼容旧测）
expanded = pathutil.expand_windows_vars(r"%LOCALAPPDATA%\dicemanager\programs")
print("expand_windows_vars('%LOCALAPPDATA%\\dicemanager\\programs') =", expanded)
assert "%LOCALAPPDATA%" not in expanded, "占位符未展开"
assert "dicemanager" in expanded, "展开后路径不含 dicemanager"

# pathutil 默认路径：项目内 data 目录
_root = Path(__file__).resolve().parent.parent
expected_data = _root / "data"
print("default_state_dir =", pathutil.default_state_dir())
print("default_lock_dir =", pathutil.default_lock_dir())
print("default_install_root =", pathutil.default_install_root())
assert str(pathutil.default_state_dir()) == str(expected_data.resolve()), \
    f"default_state_dir 不在 {expected_data}（实际: {pathutil.default_state_dir()}）"
assert str(pathutil.default_install_root()) == str((_root / "package").resolve()), \
    f"default_install_root 不在 {_root}/package（实际: {pathutil.default_install_root()}）"

# locks 依赖 pathutil：_LOCK_DIR 应在项目 data/locks 下
print("locks._LOCK_DIR =", locks._LOCK_DIR)
assert str(locks._LOCK_DIR) != "/tmp/dicemanager", "_LOCK_DIR 仍是 Linux 默认值"
assert str(locks._LOCK_DIR) == str((expected_data / "locks").resolve()), \
    f"_LOCK_DIR 不在项目 data/locks 下（实际: {locks._LOCK_DIR}）"

# 平台分支：Windows 应 msvcrt 可用、fcntl 为 None
print("locks.msvcrt is not None =", locks.msvcrt is not None)
print("locks.fcntl is None =", locks.fcntl is None)
assert locks.msvcrt is not None, "Windows 应 import msvcrt"
assert locks.fcntl is None, "Windows 不应 import fcntl"

# process 平台标志
from core import process
print("process._POSIX =", process._POSIX)
print("process._WIN =", process._WIN)
assert process._POSIX is False, "Windows 上 _POSIX 应为 False"
assert process._WIN is True, "Windows 上 _WIN 应为 True"
assert hasattr(process, "_terminate_tree"), "缺少 _terminate_tree 辅助函数"
assert hasattr(process, "_popen_kwargs"), "缺少 _popen_kwargs 辅助函数"

# scanner 用 Path.is_relative_to（Python 3.9+）
from core import scanner
assert hasattr(scanner, "_under"), "scanner 缺少 _under"

# adopt 平台判断
from core import adopt
assert adopt._conn_listen_pid_procfs(8080) is None, "Windows 上 /proc 回退应直接返回 None"

print()
print("=== core 层阶段1 验证全部通过 ===")
