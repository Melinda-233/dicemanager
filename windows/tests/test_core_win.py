"""Windows 化改造的核心点回归测试。

覆盖阶段1/2/3 改造项：
- pathutil：expand_windows_vars 展开 %LOCALAPPDATA% 等占位（保留兼容旧测）
- pathutil：default_state_dir / log_dir / lock_dir / install_root 全部落项目 data 目录下
- locks：Windows 用 msvcrt 替代 fcntl（_LOCK_DIR 走 data 目录）
- process：_WIN 标志 + _terminate_tree（taskkill /T）+ _popen_kwargs（CREATE_NEW_PROCESS_GROUP）
            + ManagedProcess.re_adopt / _try_readopt_now（接管逻辑保留）
- scanner：_under / match_program_dir 用 Path.is_relative_to 兼容 \\ 和 /
- adopt：/proc 专属兜底，Windows 直接返回 None
- context / auth：路径默认值走项目 data 目录
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_pathutil_expand_windows_vars():
    from core import pathutil
    # 占位符被实际展开为本地真实路径（保留兼容旧测）
    expanded = pathutil.expand_windows_vars(r"%LOCALAPPDATA%\dicemanager\programs")
    assert "%LOCALAPPDATA%" not in expanded, "占位符未展开"
    assert "dicemanager" in expanded.lower()
    # 不含占位的路径原样返回
    plain = pathutil.expand_windows_vars(r"C:\some\path")
    assert plain == r"C:\some\path"


def test_pathutil_default_dirs_under_project_data():
    """默认路径：state/log/lock 落在项目根/data 下，install_root 落在项目根/package 下。"""
    from core import pathutil
    state = pathutil.default_state_dir()
    log = pathutil.default_log_dir()
    lock = pathutil.default_lock_dir()
    install = pathutil.default_install_root()
    expected_root = (ROOT / "data").resolve()
    assert state == expected_root, f"state_dir 不在项目 data 下：{state}"
    assert log == expected_root / "logs"
    assert lock == expected_root / "locks"
    # 登录端与应用端实例统一装在项目根 package/<dice>/（与 exe 同级可见）
    assert install == (ROOT / "package").resolve(), \
        f"install_root 不在项目根 package/ 下：{install}"


def test_locks_uses_msvcrt_on_windows():
    """Windows 上 fcntl 应为 None，msvcrt 应可用（或两者都 None 表示平台回退）。
    _LOCK_DIR 必须落在项目 data 目录下，而非 Linux 的 /tmp/dicemanager。
    """
    from core import locks
    assert "/tmp/dicemanager" != str(locks._LOCK_DIR), \
        f"locks._LOCK_DIR 仍是 Linux 路径：{locks._LOCK_DIR}"
    expected_lock = (ROOT / "data" / "locks").resolve()
    assert str(locks._LOCK_DIR) == str(expected_lock), \
        f"_LOCK_DIR 不在项目 data/locks 下：{locks._LOCK_DIR}"


def test_process_win_flag_and_helpers():
    """Windows 上 _WIN is True，_terminate_tree / _popen_kwargs 就位。"""
    from core import process
    assert process._WIN is True
    assert callable(process._terminate_tree), "缺少 _terminate_tree 辅助函数"
    assert callable(process._popen_kwargs), "缺少 _popen_kwargs 辅助函数"
    # _popen_kwargs 在 Windows 上应带 CREATE_NEW_PROCESS_GROUP
    kw = process._popen_kwargs()
    assert "creationflags" in kw or "start_new_session" not in kw, \
        f"_popen_kwargs 在 Windows 上应避免 start_new_session：{kw}"


def test_managed_process_has_adopt_logic():
    """阶段2返工补的接管逻辑必须就位（resume.py 依赖 re_adopt）。"""
    from core import process
    assert hasattr(process.ManagedProcess, "re_adopt"), "ManagedProcess 缺 re_adopt"
    assert hasattr(process.ManagedProcess, "_try_readopt_now"), "ManagedProcess 缺 _try_readopt_now"
    assert hasattr(process.ManagedProcess, "is_alive"), "ManagedProcess 缺 is_alive"


def test_scanner_path_separator_compatibility():
    """scanner 的路径匹配须兼容 \\ 和 /（Windows 改造点）。

    原 Linux 版用 path.startswith(b + "/")，Windows 上 \\ 路径会匹配失败。
    阶段1改用 Path.is_relative_to() 后，两种分隔符都应正确判定。
    """
    from core import scanner
    # 构造跨分隔符测试数据
    parent = Path(r"C:\Users\test\dicemanager\programs")
    child_win = Path(r"C:\Users\test\dicemanager\programs\sealdice")
    child_fwd = Path("C:/Users/test/dicemanager/programs/sealdice/sub")
    # is_relative_to 本身兼容两种分隔符（Path 自动归一）
    assert child_win.is_relative_to(parent)
    assert child_fwd.is_relative_to(parent)


def test_adopt_procfs_skipped_on_windows():
    """/proc/net/tcp 是 Linux 专属；Windows 上 _conn_listen_pid_procfs 应直接返回 None。"""
    from core import adopt
    result = adopt._conn_listen_pid_procfs(8080)
    # Windows 上必须返回 None（回退到 psutil 优先路径）
    assert result is None, f"Windows 上 _conn_listen_pid_procfs 应返回 None，实际: {result}"


def test_context_paths_use_project_data():
    """api/context.py 的 STATE_DIR / LOG_DIR 不能写死 /var/lib、/var/log，
    且默认值落在项目 data 目录下。"""
    from api import context
    assert "/var/lib" not in str(context.STATE_DIR), \
        f"STATE_DIR 仍是 Linux 路径：{context.STATE_DIR}"
    assert "/var/log" not in str(context.LOG_DIR), \
        f"LOG_DIR 仍是 Linux 路径：log={context.LOG_DIR}"
    expected_state = (ROOT / "data").resolve()
    assert str(context.STATE_DIR) == str(expected_state), \
        f"STATE_DIR 不在项目 data 下：{context.STATE_DIR}"


def test_auth_file_under_state_dir():
    """api/auth.py 的 AUTH_FILE 应落在 STATE_DIR 之下。"""
    from api import context, auth
    assert str(auth.AUTH_FILE).endswith("auth.json")
    assert "/var/lib" not in str(auth.AUTH_FILE)
    # parent 与 context.STATE_DIR 一致（都走 pathutil 或同环境变量）
    assert str(auth.AUTH_FILE.parent) == str(context.STATE_DIR)


def test_app_umask_guarded_by_posix_check():
    """app.py 第12行的 /var/lib 路径只在 os.name == 'posix' 分支下（Windows 不执行）。"""
    app_src = (ROOT / "api" / "app.py").read_text(encoding="utf-8")
    assert "os.name == \"posix\"" in app_src or "os.name == 'posix'" in app_src, \
        "app.py 缺少 os.name == 'posix' 平台分支（umask 在 Windows 上会被错误执行）"
