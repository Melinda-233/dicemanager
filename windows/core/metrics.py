"""实例资源时序采样：内存 / CPU，默认 60s 一点、保留 24h，供总览绘制曲线。

设计取舍
- 只采「进程活着」的实例；进程退出后曲线保留（不再追加点），便于事后看崩溃前的走势。
- cpu_percent 必须复用同一个 psutil.Process 对象：它是「距上次调用」的增量值，
  每次新建对象得到的会是「自进程启动以来的均值」，曲线会失真 → 按 pid 缓存句柄。
- 落盘按实例一个 JSON（<state>/metrics/<id>.json），采样即写：24h × 60s ≈ 1440 点
  ≈ 30KB，每分钟一次全量重写完全可接受，换来的是面板重启不丢历史。
- 子进程（llbot 会拉起 node/pmhq）计入总量，否则曲线只反映父进程、严重低估。
"""
import json
import threading
import time
from collections import deque
from pathlib import Path

DEFAULT_INTERVAL = 60.0          # 采样间隔（秒）
DEFAULT_KEEP_HOURS = 24.0        # 保留窗口
OFF = {"0", "false", "no", "off"}


class MetricsStore:
    """环形落盘：内存 deque + JSON 文件，读写都在锁内（采样线程与 REST 并发）。"""

    def __init__(self, dir_, keep_hours: float = DEFAULT_KEEP_HOURS):
        self.dir = Path(dir_)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.keep_sec = keep_hours * 3600
        self._lock = threading.Lock()
        self._buf: dict[str, deque] = {}
        self._loaded: set[str] = set()

    def _file(self, iid: str) -> Path:
        return self.dir / f"{iid}.json"

    def _load(self, iid: str) -> deque:
        if iid in self._loaded:
            return self._buf.setdefault(iid, deque())
        d: deque = deque()
        f = self._file(iid)
        if f.exists():
            try:
                data = json.loads(f.read_text("utf-8"))
                for p in data.get("points", []):
                    if len(p) >= 3:
                        d.append((float(p[0]), float(p[1]), float(p[2])))
            except Exception:                       # 坏文件不致命：从头开始采
                d = deque()
        self._buf[iid] = d
        self._loaded.add(iid)
        return d

    def _persist(self, iid: str, d: deque) -> None:
        from core.atomicio import write_atomic
        payload = {"interval": DEFAULT_INTERVAL,
                   "points": [[round(t, 1), m, c] for t, m, c in d]}
        write_atomic(self._file(iid),
                     json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def add(self, iid: str, mem_mb: float, cpu: float, ts: float | None = None) -> None:
        with self._lock:
            d = self._load(iid)
            now = ts if ts is not None else time.time()
            d.append((round(now, 1), round(mem_mb, 1), round(cpu, 1)))
            cutoff = now - self.keep_sec
            while d and d[0][0] < cutoff:
                d.popleft()
            self._persist(iid, d)

    def points(self, iid: str, hours: float | None = None) -> list[list[float]]:
        with self._lock:
            d = self._load(iid)
            if not d:
                return []
            cutoff = time.time() - ((hours or self.keep_sec / 3600) * 3600)
            return [[t, m, c] for t, m, c in d if t >= cutoff]

    def latest(self, iid: str) -> list[float] | None:
        pts = self.points(iid)
        return pts[-1] if pts else None

    def drop(self, iid: str) -> bool:
        """实例删除：同步丢掉它的曲线文件（与实例日志同源的回收口径）。"""
        with self._lock:
            self._buf.pop(iid, None); self._loaded.discard(iid)
        f = self._file(iid)
        if f.exists():
            f.unlink(); return True
        return False

    def prune_files(self, keep_ids) -> list[str]:
        """启动时回收已删除实例的孤儿曲线文件。"""
        keep = set(keep_ids)
        removed = []
        for f in self.dir.glob("*.json"):
            if f.stem not in keep:
                f.unlink(); removed.append(f.stem)
        return removed


class MetricsSampler:
    """后台采样线程：遍历注册表里进程存活的实例，采内存与 CPU。"""

    def __init__(self, store: MetricsStore, registry, pm,
                 interval: float | None = None, enabled=None):
        self.store = store
        self.reg = registry
        self.pm = pm
        self.interval = interval if interval is not None else _env_float(
            "DM_METRICS_INTERVAL", DEFAULT_INTERVAL)
        self.enabled = _enabled() if enabled is None else enabled
        self._ps: dict[str, object] = {}            # iid -> psutil.Process（含 pid 校验）
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def _handle(self, iid: str, pid: int):
        """取（或重建）该实例的 psutil 句柄；pid 变化（进程重启）必须换新句柄。"""
        import psutil
        h = self._ps.get(iid)
        if isinstance(h, psutil.Process):
            try:
                if h.pid == pid and h.is_running():
                    return h
            except Exception:
                pass
        new = psutil.Process(pid)
        self._ps[iid] = new
        return new

    def sample_instance(self, iid: str, pid: int) -> tuple[float, float] | None:
        """返回 (mem_mb, cpu%)；进程刚好退出返回 None。子进程计入总量。"""
        try:
            h = self._handle(iid, pid)
            mem = h.memory_info().rss
            cpu = h.cpu_percent(None)               # 首次调用返回 0.0（基线），可接受
            for c in h.children(recursive=True):
                try:
                    mem += c.memory_info().rss
                    cpu += c.cpu_percent(None)
                except Exception:
                    continue
            return round(mem / 1048576, 1), round(cpu, 1)
        except Exception:
            self._ps.pop(iid, None)
            return None

    def sample_once(self) -> int:
        n = 0
        for rec in self.reg.all():
            iid = rec["id"]
            try:
                if not self.pm.is_alive(iid):
                    continue
                pid = self.pm.get(iid).probe().get("pid")
                if not pid:
                    continue
                got = self.sample_instance(iid, pid)
                if got is None:
                    continue
                self.store.add(iid, got[0], got[1])
                n += 1
            except Exception:                       # 单实例失败不影响其余
                continue
        return n

    def loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.sample_once()
            except Exception:
                continue

    def start(self) -> bool:
        if not self.enabled or self._t is not None:
            return False
        self._t = threading.Thread(target=self.loop, name="metrics-sampler",
                                   daemon=True)
        self._t.start()
        return True

    def stop(self) -> None:
        self._stop.set()


def _env_float(name: str, default: float) -> float:
    try:
        return float(__import__("os").environ.get(name, "") or default)
    except ValueError:
        return default


def _enabled() -> bool:
    import os
    return os.environ.get("DM_METRICS", "1").strip().lower() not in OFF
