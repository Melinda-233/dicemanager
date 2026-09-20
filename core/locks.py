"""互斥锁：进程内 threading + 跨进程文件锁（端口/目录/实例三类临界区）

可重入约定：
  REST 层（DELETE/OP）与 services 层（向导）都会先取 instance_lock，再调用
  registry.transition/update/remove，而这些方法内部也会取同一把 instance_lock。
  若用不可重入的 Lock + 每次新建 fd 的 flock，就会出现「自己等自己」的永久死锁
  （同一进程内两个 fd 对同一文件 flock 也会互相阻塞）。因此这里统一改成
  RLock + 引用计数的文件锁，保证同一线程/同一进程内可安全嵌套。
"""
import os
import threading
from contextlib import contextmanager
from pathlib import Path

if os.name == "posix":
    import fcntl
else:
    fcntl = None                    # Windows 本地开发/测试：退化为进程内锁
    import msvcrt

class DeploymentConflict(Exception): pass
class PortConflict(Exception): pass

_dir_lock = threading.Lock()
_instance_locks: dict[str, threading.RLock] = {}
_file_guard = threading.Lock()
_file_locks: dict[str, list] = {}          # name -> [refcount, fileobj]
_LOCK_DIR = Path(os.environ.get("DM_LOCK_DIR", "/tmp/dicemanager"))

def _acquire_file(name: str) -> list:
    """同一进程内对同一锁名只锁一次，重复进入仅增加引用计数。"""
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    with _file_guard:
        entry = _file_locks.get(name)
        if entry is None:
            f = open(_LOCK_DIR / f"{name}.lock", "a+")
            if fcntl is not None:
                fcntl.flock(f, fcntl.LOCK_EX)
            else:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)   # 锁 1 字节即可
            entry = [0, f]
            _file_locks[name] = entry
        entry[0] += 1
        return entry

def _release_file(name: str) -> None:
    with _file_guard:
        entry = _file_locks.get(name)
        if not entry:
            return
        entry[0] -= 1
        if entry[0] > 0:
            return
        try:
            if fcntl is not None:
                fcntl.flock(entry[1], fcntl.LOCK_UN)
            else:
                entry[1].seek(0)
                msvcrt.locking(entry[1].fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            entry[1].close()
        _file_locks.pop(name, None)

@contextmanager
def _file_lock(name: str):
    _acquire_file(name)
    try:
        yield
    finally:
        _release_file(name)

@contextmanager
def port_allocation_lock():
    with _file_lock("ports"):
        yield

@contextmanager
def instance_lock(inst_id: str):
    with _dir_lock:
        lk = _instance_locks.setdefault(inst_id, threading.RLock())
    with lk, _file_lock(f"inst-{inst_id}"):
        yield

@contextmanager
def program_dir_lock(program: str):
    """目录部署互斥：同名程序并发部署只允许一个进行。"""
    with _file_lock(f"dir-{program}"):
        yield
