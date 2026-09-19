"""互斥锁：进程内 threading + 跨进程文件锁（端口/目录/实例三类临界区）"""
import fcntl
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

class DeploymentConflict(Exception): pass
class PortConflict(Exception): pass

_dir_lock = Lock()
_instance_locks: dict[str, Lock] = {}
_LOCK_DIR = Path("/tmp/dicemanager")

def _file_lock(name: str):
    _LOCK_DIR.mkdir(exist_ok=True)
    f = open(_LOCK_DIR / f"{name}.lock", "a+")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f

@contextmanager
def port_allocation_lock():
    f = _file_lock("ports")
    try: yield
    finally: fcntl.flock(f, fcntl.LOCK_UN); f.close()

@contextmanager
def instance_lock(inst_id: str):
    with _dir_lock:
        lk = _instance_locks.setdefault(inst_id, Lock())
    with lk:
        f = _file_lock(f"inst-{inst_id}")
        try: yield
        finally: fcntl.flock(f, fcntl.LOCK_UN); f.close()

@contextmanager
def program_dir_lock(program: str):
    """目录部署互斥：同名程序并发部署只允许一个进行。"""
    f = _file_lock(f"dir-{program}")
    try: yield
    finally: fcntl.flock(f, fcntl.LOCK_UN); f.close()