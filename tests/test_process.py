"""回归测试：自动重启不产生重复 tail 线程（code-review-2026-09-19 P1-1）

旧实现 _tail() 调 self.start() 重启后未 return，新线程与旧线程同抢一条 stdout：
线程数每次崩溃翻倍、restarts 每次 +2，5 次/5 分钟熔断被提前误触发。
用真实子进程（快速退出）驱动完整重启链路验证。pytest 仅依赖标准库之外零包。
"""
import subprocess
import sys
import threading
import time

import pytest

from core.process import ManagedProcess

CRASH_CMD = None  # 惰性赋值：见下方 fixture


@pytest.fixture()
def crash_proc(tmp_path):
    import sys
    cmd = [sys.executable, "-c", "print('boot'); import sys; sys.exit(1)"]
    mp = ManagedProcess("t-selfexit", tmp_path)
    yield mp, cmd
    mp._stop_flag.set()                 # 测试收尾：不再自动重启
    with mp._lock:
        if mp._log_fp:
            mp._log_fp.close(); mp._log_fp = None


def _tail_threads(mp) -> int:
    return sum(1 for t in threading.enumerate()
               if getattr(t, "_target", None) == mp._tail)


def test_auto_restart_keeps_single_tail_thread(crash_proc):
    mp, cmd = crash_proc
    mp.start(cmd, str(mp.log_path.parent))
    deadline = time.time() + 20
    while mp.restarts < 3 and time.time() < deadline:   # 退避 1s/2s/4s，3 次约 7s+
        time.sleep(0.2)
    assert mp.restarts >= 3, "自动重启未发生"
    assert _tail_threads(mp) == 1, "重启后残留多个 tail 线程（P1-1 回归）"


def test_restarts_counter_counts_once_per_crash(crash_proc):
    mp, cmd = crash_proc
    mp.start(cmd, str(mp.log_path.parent))
    deadline = time.time() + 20
    while mp.restarts < 3 and time.time() < deadline:
        time.sleep(0.2)
    assert mp.restarts == 3, "restarts 每次崩溃被多计（P1-1 副作用）"


def test_ring_stores_seq_line_pairs(crash_proc):
    mp, cmd = crash_proc
    mp.start(cmd, str(mp.log_path.parent))
    deadline = time.time() + 5
    while not mp.ring and time.time() < deadline:
        time.sleep(0.1)
    assert mp.ring, "ring 未收到任何行"
    s, line = mp.ring[0]
    assert isinstance(s, int) and isinstance(line, str), "ring 元素必须是 (seq, line) 对"


def test_hook_receives_seq_matching_ring(crash_proc):
    mp, cmd = crash_proc
    got: list = []
    mp.on_line(lambda s, line: got.append((s, line)))
    mp.start(cmd, str(mp.log_path.parent))
    deadline = time.time() + 5
    while not got and time.time() < deadline:
        time.sleep(0.1)
    assert got
    ring_map = dict(mp.ring)
    for s, line in got:
        assert ring_map.get(s) == line, "hook 收到的 (seq, line) 必须与 ring 一致"


# ---------- re-adopt（端口→pid 接管）回归 ----------

def _spawn_sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_readopt_takes_over_external_process(tmp_path):
    """re_adopt 接管一个外部进程后，is_alive / probe 都指向真实 pid，stop 能正确杀掉它。"""
    ext = _spawn_sleeper()
    try:
        mp = ManagedProcess("t-adopt", tmp_path)
        assert mp.re_adopt(ext.pid) is True
        assert mp.is_alive() is True
        assert mp.probe()["pid"] == ext.pid
        mp.stop()
        for _ in range(50):
            if ext.poll() is not None:
                break
            time.sleep(0.1)
        assert mp.is_alive() is False
    finally:
        if ext.poll() is None:
            ext.kill()
        ext.wait(timeout=5)


def test_readopt_rejects_dead_pid(tmp_path):
    """对不存在的 pid 接管应当失败，不污染状态。"""
    mp = ManagedProcess("t-adopt-dead", tmp_path)
    assert mp.re_adopt(999999) is False
    assert mp.is_alive() is False
    assert mp._adopted_pid is None


def test_is_alive_self_heals_via_resolver(monkeypatch, tmp_path):
    """句柄失效但端口仍被外部进程占用时，is_alive 应自动按端口找回并接管。"""
    ext = _spawn_sleeper()
    try:
        captured = {}

        def resolver(iid):
            captured["iid"] = iid
            return {54321}

        mp = ManagedProcess("t-heal", tmp_path, port_resolver=resolver)
        monkeypatch.setattr("core.process.find_pid_on_port",
                            lambda port: ext.pid if port == 54321 else None)
        assert mp.is_alive() is True
        assert mp._adopted_pid == ext.pid
        assert captured.get("iid") == "t-heal"
    finally:
        mp.stop()
        if ext.poll() is None:
            ext.kill()
        ext.wait(timeout=5)


def test_no_self_heal_without_resolver(tmp_path):
    """无 port_resolver 时，句柄失效即判死（不误触发端口扫描/接管）。"""
    mp = ManagedProcess("t-noheal", tmp_path)
    assert mp.is_alive() is False
