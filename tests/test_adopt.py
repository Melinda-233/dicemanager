"""端口→pid 接管工具单测 + 集成测试。

覆盖：find_pid_on_port（psutil 分支 + /proc 回退）、pid_alive、kill_process_tree
（向上找 launcher 根、向下杀整棵树）。kill 类测试用真实子进程驱动，避免 mock 掩盖真实信号行为。
"""
import os
import socket
import subprocess
import sys
import time

import pytest

import core.adopt as adopt
from types import SimpleNamespace

pytestmark = pytest.mark.skipif(
    adopt.psutil is None, reason="psutil 未安装，端口→pid 工具不可用")


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.listen(1)
    return s, port


def test_find_pid_on_port_psutil(monkeypatch):
    """psutil 分支：net_connections 命中端口即返回其 pid。"""
    srv, port = _free_port()
    fake = SimpleNamespace(status=adopt.psutil.CONN_LISTEN,
                           laddr=SimpleNamespace(port=port), pid=12345)
    monkeypatch.setattr(adopt.psutil, "net_connections",
                        lambda kind=None: [fake])
    assert adopt.find_pid_on_port(port) == 12345
    assert adopt.find_pid_on_port(port + 1) is None
    srv.close()


@pytest.mark.skipif(os.name != "posix", reason="/proc/net/tcp 仅 Linux 可用")
def test_find_pid_on_port_procfs_fallback(monkeypatch):
    """psutil 拿不到 pid 时回退 /proc 解析，可由监听进程的 pid 反查自身。"""
    srv, port = _free_port()
    monkeypatch.setattr(adopt, "_conn_listen_pid", lambda port: None)
    pid = adopt.find_pid_on_port(port)
    assert pid == os.getpid(), "procfs 回退应定位到真正监听的测试进程"
    srv.close()


def test_find_pid_on_port_invalid():
    assert adopt.find_pid_on_port(0) is None
    assert adopt.find_pid_on_port(-1) is None


def test_pid_alive():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert adopt.pid_alive(p.pid)
    p.kill()
    for _ in range(50):
        if not adopt.pid_alive(p.pid):
            break
        time.sleep(0.1)
    assert not adopt.pid_alive(p.pid)


def test_kill_process_tree_kills_parent_and_child():
    """launcher(父) + worker(子) 结构：从子 pid 触发树杀，父也应被一并干掉。"""
    parent = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess,sys,time\n"
         "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
         "sys.stdout.write(str(c.pid)+chr(10)); sys.stdout.flush()\n"
         "time.sleep(30)\n"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    child_pid = int(parent.stdout.readline().strip())
    assert adopt.pid_alive(child_pid)
    adopt.kill_process_tree(child_pid)
    for _ in range(100):
        if not adopt.pid_alive(child_pid):
            break
        time.sleep(0.1)
    assert not adopt.pid_alive(child_pid)
    # 父（launcher）经由树杀一并退出，否则 worker 会被重新拉起
    for _ in range(100):
        if not adopt.pid_alive(parent.pid):
            break
        time.sleep(0.1)
    assert not adopt.pid_alive(parent.pid)
    try:
        parent.wait(timeout=5)
    except Exception:
        pass
