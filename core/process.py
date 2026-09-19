"""进程管理：Popen 管道 + 环形缓冲(seq,line) + 日志双限滚动 + 崩溃自动重启（滑动窗口）
ring 存 (seq, line) 对：WS 历史回放与实时 hook 用 seq 去重，消除「快照后 hook 前」丢行窗口。"""
import itertools
import os, shutil, signal, subprocess, threading, time
from collections import deque
from datetime import datetime
from pathlib import Path

_POSIX = os.name == "posix"

LOG_RETENTION_DAYS = 7
LOG_MAX_BYTES = 50 * 1024 * 1024
RING_MAX = 2000
MAX_RESTARTS = 5
RESTART_WINDOW = 300           # 滑动窗口：300s 内重启 ≤5 次（与 README 口径一致）
RESTART_COOLDOWN = 300
ROTATE_CHECK_INTERVAL = 5.0    # 滚动检查节流（秒），避免每行日志都 stat

class ManagedProcess:
    def __init__(self, inst_id: str, log_dir: Path):
        self.id = inst_id
        self.log_path = Path(log_dir) / f"{inst_id}.log"
        self.ring: deque = deque(maxlen=RING_MAX)     # (seq, line) 对，seq 单调递增
        self._seq = itertools.count()
        self._tail_listeners: list = []
        self._proc: subprocess.Popen | None = None
        self._t: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop_flag = threading.Event()
        self._last_cmd: list | None = None
        self._last_cwd: str | None = None
        self._log_fp = None                       # 持久句柄：避免每行 open/close
        self._last_rotate_check = 0.0
        self._restart_times: deque = deque(maxlen=MAX_RESTARTS)
        self.restarts = 0
        self.started_at: float | None = None
        self._load_log_copy()

    def _load_log_copy(self):
        """重启不丢历史：优先加载 .log.1 副本到环形缓冲。"""
        copy = self.log_path.with_suffix(".log.1")
        if copy.exists():
            for line in copy.read_text("utf-8", errors="replace").splitlines()[-RING_MAX:]:
                self.ring.append((next(self._seq), line))

    def start(self, cmd: list[str], cwd: str, env: dict | None = None):
        with self._lock:
            if self._proc and self._proc.poll() is None:
                raise RuntimeError("进程已在运行")
            self._last_cmd, self._last_cwd = cmd, cwd
            self._proc = subprocess.Popen(
                cmd, cwd=cwd, env={**os.environ, **(env or {})},
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, start_new_session=_POSIX)   # Windows 本地开发/测试可运行
            self.started_at = time.time()
            self._stop_flag.clear()
            self._t = threading.Thread(target=self._tail, daemon=True)
            self._t.start()

    def _tail(self):
        """tail 线程：行回调 + 日志写入 + 滑动窗口自动重启。

        重启必须 return：start() 会再起一个新的 tail 线程接管新进程，
        旧线程若继续循环会与新线程同抢一条 stdout，线程数每次崩溃翻倍、
        restarts 计数 +2，导致 5 次/5 分钟熔断被提前误触发。"""
        backoff = 1
        while not self._stop_flag.is_set():
            p = self._proc
            if not p or p.stdout is None: break
            for line in p.stdout:
                line = line.rstrip("\n")
                seq = next(self._seq)
                self.ring.append((seq, line))
                self._append_log(line)
                for cb in list(self._tail_listeners):
                    try: cb(seq, line)
                    except Exception: pass
            rc = p.wait()
            if self._stop_flag.is_set(): break
            now = time.time()
            if now - (self.started_at or now) > RESTART_WINDOW:
                self._restart_times.clear()       # 稳定运行过 → 重启窗口清零
            self._restart_times.append(now)
            self.restarts += 1
            if (len(self._restart_times) >= MAX_RESTARTS
                    and now - self._restart_times[0] <= RESTART_WINDOW):
                self._append_log(
                    f"[manager] {RESTART_WINDOW}s 内重启达 {MAX_RESTARTS} 次，停止自动重启")
                break
            time.sleep(min(backoff, RESTART_COOLDOWN)); backoff *= 2
            self._append_log(f"[manager] 进程退出 rc={rc}，第 {self.restarts} 次重启")
            self.start(self._last_cmd, self._last_cwd)
            return                        # 新 tail 线程已由 start() 接管，旧线程必须退出

    def _append_log(self, line: str):
        with self._lock:
            now = time.time()
            if now - self._last_rotate_check >= ROTATE_CHECK_INTERVAL:
                self._last_rotate_check = now
                if self.log_path.exists():
                    if self.log_path.stat().st_size > LOG_MAX_BYTES:
                        self._rotate()
                    else:
                        mt = datetime.fromtimestamp(self.log_path.stat().st_mtime)
                        if (datetime.now() - mt).days >= LOG_RETENTION_DAYS:
                            self._rotate()
            if self._log_fp is None:
                self._log_fp = open(self.log_path, "a", encoding="utf-8")
            self._log_fp.write(line + "\n")
            self._log_fp.flush()

    def _rotate(self):
        """调用方需已持有 _lock。"""
        if self._log_fp:
            self._log_fp.close(); self._log_fp = None
        shutil.copy2(self.log_path, self.log_path.with_suffix(".log.1"))
        self.log_path.unlink()

    def stop(self):
        self._stop_flag.set()
        if self._proc and self._proc.poll() is None:
            if _POSIX:                    # killpg 仅 POSIX；Windows 本地测试走 terminate
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            else:
                self._proc.terminate()
            try: self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if _POSIX:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                else:
                    self._proc.kill()
        with self._lock:
            if self._log_fp:
                self._log_fp.close(); self._log_fp = None

    def probe(self) -> dict:
        return {"alive": self.is_alive(), "restarts": self.restarts,
                "pid": self._proc.pid if self._proc else None,
                "uptime": time.time() - self.started_at if self.started_at else 0}

    def is_alive(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    def on_line(self, cb):
        """cb 签名 cb(seq, line)：seq 与 ring 中一致，供回放/实时去重。"""
        self._tail_listeners.append(cb)
    def off_line(self, cb):
        if cb in self._tail_listeners: self._tail_listeners.remove(cb)

    @staticmethod
    def cleanup_old_copies(log_dir: Path):
        for f in Path(log_dir).glob("*.log.1"):
            if (datetime.now() - datetime.fromtimestamp(f.stat().st_mtime)).days > LOG_RETENTION_DAYS:
                f.unlink()

class ProcessManager:
    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir); self.log_dir.mkdir(parents=True, exist_ok=True)
        self._procs: dict[str, ManagedProcess] = {}
        self._lock = threading.Lock()
        ManagedProcess.cleanup_old_copies(self.log_dir)

    def get(self, inst_id: str) -> ManagedProcess:
        with self._lock:
            if inst_id not in self._procs:
                self._procs[inst_id] = ManagedProcess(inst_id, self.log_dir)
            return self._procs[inst_id]

    def is_alive(self, inst_id: str) -> bool:
        return self.get(inst_id).is_alive()

    def launch(self, inst_id: str, cmd: list[str], cwd: str, env: dict | None = None):
        """REST start/restart 与向导 Step5 的统一入口。"""
        self.get(inst_id).start(cmd, cwd, env)

    def stop(self, inst_id: str):
        self.get(inst_id).stop()
