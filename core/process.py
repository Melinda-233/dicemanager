"""进程管理：Popen 管道 + 环形缓冲(seq,line) + 日志双限滚动 + 崩溃自动重启（滑动窗口）
ring 存 (seq, line) 对：WS 历史回放与实时 hook 用 seq 去重，消除「快照后 hook 前」丢行窗口。"""
import itertools
import os
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import TextIO

_POSIX = os.name == "posix"

LOG_RETENTION_DAYS = 7
LOG_MAX_BYTES = 50 * 1024 * 1024
RING_MAX = 2000
MAX_RESTARTS = 5
RESTART_WINDOW = 300           # 滑动窗口：300s 内重启 ≤5 次（与 README 口径一致）
RESTART_COOLDOWN = 300
ROTATE_CHECK_INTERVAL = 5.0    # 滚动检查节流（秒），避免每行日志都 stat

# 面板自身日志文件（<log_dir>/dicemanager.log）不属于任何实例，日志回收时必须跳过
PANEL_LOG_STEM = "dicemanager"

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
        self._last_cmd: list[str] | None = None
        self._last_cwd: str | None = None
        self._log_fp: TextIO | None = None        # 持久句柄：避免每行 open/close
        self._last_rotate_check = 0.0
        self._restart_times: deque = deque(maxlen=MAX_RESTARTS)
        self.restarts = 0
        self.crash_looped = False                 # 熔断触发：停止自动重启（总览可见告警）
        self._busy_once = False                   # run_once 执行中（与 start() 互斥的硬保证）
        self.started_at: float | None = None
        self._load_log_copy()

    def _load_log_copy(self):
        """重启不丢历史：优先加载 .log.1 副本到环形缓冲（流式读，避免大文件整载内存）。"""
        copy = self.log_path.with_suffix(".log.1")
        if copy.exists():
            try:
                with open(copy, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:                # deque(maxlen) 自动只留最近 RING_MAX 行
                        self.ring.append((next(self._seq), line.rstrip("\n")))
            except OSError:
                pass

    def start(self, cmd: list[str], cwd: str, env: dict | None = None):
        with self._lock:
            if self._busy_once:
                raise RuntimeError("一次性命令正在执行，请稍后再试")
            if self._proc and self._proc.poll() is None:
                raise RuntimeError("进程已在运行")
            try:
                proc = subprocess.Popen(
                    cmd, cwd=cwd, env={**os.environ, **(env or {})},
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, start_new_session=_POSIX)   # Windows 本地开发/测试可运行
            except OSError as e:                           # exe 缺失/权限等：友好报错而非 500
                raise RuntimeError(f"启动失败：{e}") from e
            self._proc = proc
            self._last_cmd, self._last_cwd = cmd, cwd
            self.started_at = time.time()
            self.crash_looped = False               # 人工/向导重新启动 = 脱离熔断态
            self._stop_flag.clear()
            self._t = threading.Thread(target=self._tail, daemon=True)
            self._t.start()

    def run_once(self, cmd: list[str], cwd: str, timeout: float = 600,
                 env: dict | None = None, label: str = "") -> int:
        """一次性命令（自更新、写配置等）：输出同样进 ring 与日志，但不作为常驻进程。

        与 start() 互斥（常驻进程在跑就不执行），且不触发自动重启逻辑——
        --update 这类命令退出码非 0 是正常现象，不该被熔断机制当成崩溃。
        """
        with self._lock:
            if self._proc and self._proc.poll() is None:
                raise RuntimeError("常驻进程正在运行")
            if self._busy_once:                    # 与并发 run_once / start() 的互斥硬保证
                raise RuntimeError("一次性命令正在执行")
            self._busy_once = True
        try:
            self._append_log(f"[manager] 执行一次性命令: {label or ' '.join(cmd)}")
            p = None
            try:
                p = subprocess.Popen(cmd, cwd=cwd, env={**os.environ, **(env or {})},
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, start_new_session=_POSIX)
                assert p.stdout is not None          # 上面指定了 stdout=PIPE，此处收窄供 mypy
                for line in p.stdout:
                    line = line.rstrip("\n")
                    with self._lock:
                        seq = next(self._seq)
                        self.ring.append((seq, line))
                    self._append_log(line)
                    for cb in list(self._tail_listeners):
                        try: cb(seq, line)
                        except Exception: pass
                rc = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                if p: p.kill()
                rc = -1
                self._append_log(f"[manager] 命令超时（{timeout}s）已终止: {label or cmd}")
            self._append_log(f"[manager] 命令结束 rc={rc}")
            return rc
        finally:
            with self._lock:
                self._busy_once = False

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
                self.crash_looped = True          # 熔断：总览节点可见告警，重启后自动解除
                self._append_log(
                    f"[manager] {RESTART_WINDOW}s 内重启达 {MAX_RESTARTS} 次，停止自动重启")
                break
            time.sleep(min(backoff, RESTART_COOLDOWN)); backoff *= 2
            self._append_log(f"[manager] 进程退出 rc={rc}，第 {self.restarts} 次重启")
            # 不变量：_tail 只在 start() 之后运行，二者必已被赋值
            assert self._last_cmd is not None and self._last_cwd is not None
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
        """调用方需已持有 _lock。os.replace 同盘原子改名，替代 copy+unlink（防中途崩溃丢日志）。"""
        if self._log_fp:
            self._log_fp.close(); self._log_fp = None
        os.replace(self.log_path, self.log_path.with_suffix(".log.1"))

    def note(self, msg: str):
        """管理器事件进实例日志与 ring（与进程输出同流，排障可查）。"""
        line = f"[manager] {msg}"
        with self._lock:
            seq = next(self._seq)
            self.ring.append((seq, line))
        self._append_log(line)
        for cb in list(self._tail_listeners):
            try: cb(seq, line)
            except Exception: pass

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
        alive = self.is_alive()
        mem_mb = None
        if alive and self._proc:
            try:
                import psutil
                mem_mb = round(psutil.Process(self._proc.pid).memory_info().rss / 1048576, 1)
            except Exception:
                pass                                # 进程恰好退出 / psutil 异常：不致命
        return {"alive": alive, "restarts": self.restarts,
                "pid": self._proc.pid if self._proc else None,
                "uptime": time.time() - self.started_at if self.started_at else 0,
                "mem_mb": mem_mb, "crash_looped": self.crash_looped}

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

    def run_once(self, inst_id: str, cmd: list[str], cwd: str, **kw) -> int:
        """一次性命令（自更新等），输出进同一条日志流。"""
        return self.get(inst_id).run_once(cmd, cwd, **kw)

    def stop(self, inst_id: str):
        self.get(inst_id).stop()

    def drop(self, inst_id: str):
        """实例删除后释放其 ManagedProcess（含已加载的日志 ring），不再留空壳对象。"""
        with self._lock:
            self._procs.pop(inst_id, None)

    def remove_logs(self, inst_id: str) -> list[str]:
        """删除实例的日志文件（含轮转副本 .log.1），返回被删文件名。

        实例删除侧调用：旧实现只从注册表除名，日志文件永久残留在 log_dir，
        反复增删实例会累积一堆查不到归属的孤儿日志。
        """
        with self._lock:
            proc = self._procs.get(inst_id)
        if proc is not None:                    # 先停 detached tail 线程并关闭句柄，再删文件
            try:
                proc.stop()
            except Exception:
                pass
        self.drop(inst_id)
        removed = []
        for suffix in (".log", ".log.1"):
            p = self.log_dir / f"{inst_id}{suffix}"
            try:
                if p.exists():
                    p.unlink()
                    removed.append(p.name)
            except OSError:                     # 权限/占用：记过即过，不阻断删除流程
                continue
        return removed

    def sweep_orphan_logs(self, keep_ids) -> list[str]:
        """清理不属于任何实例的孤儿日志（历史遗留：实例已删但日志还在）。

        keep_ids 为存活实例 id 集合；面板自身日志 dicemanager.log 恒不清理。
        返回值便于 REST/测试断言。
        """
        keep = set(keep_ids or ())
        removed = []
        for f in list(self.log_dir.glob("*.log")) + list(self.log_dir.glob("*.log.1")):
            stem = f.name[:-len(".log.1")] if f.name.endswith(".log.1") else f.name[:-len(".log")]
            if stem in keep or stem == PANEL_LOG_STEM:
                continue
            try:
                f.unlink()
                removed.append(f.name)
            except OSError:
                continue
        return removed
