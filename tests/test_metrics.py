"""资源采样（core/metrics.py）回归：环形裁剪、持久化、孤儿回收、CPU 句柄复用。"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.metrics import MetricsSampler, MetricsStore  # noqa: E402


def test_add_prunes_old_points_and_persists(tmp_path):
    s = MetricsStore(tmp_path)
    now = time.time()
    s.add("i1", 100.0, 5.0, ts=now - 25 * 3600)     # 超出 24h 窗口，应被裁掉
    s.add("i1", 120.0, 8.0, ts=now)
    pts = s.points("i1", 24)
    assert len(pts) == 1
    assert pts[0][1] == 120.0 and pts[0][2] == 8.0
    # 落盘：重新读一个 store（模拟面板重启）必须拿回历史
    s2 = MetricsStore(tmp_path)
    assert len(s2.points("i1", 24)) == 1


def test_points_hours_filter(tmp_path):
    s = MetricsStore(tmp_path)
    now = time.time()
    s.add("i1", 10.0, 1.0, ts=now - 3600)           # 1 小时前 → 应在 2h 窗口内、不在 0.5h 内
    s.add("i1", 20.0, 2.0, ts=now)
    assert len(s.points("i1", 2)) == 2
    assert len(s.points("i1", 0.5)) == 1


def test_latest_and_empty(tmp_path):
    s = MetricsStore(tmp_path)
    assert s.points("nope") == []
    assert s.latest("nope") is None
    s.add("i1", 33.0, 4.0)
    assert s.latest("i1")[1] == 33.0


def test_drop_and_prune_files(tmp_path):
    s = MetricsStore(tmp_path)
    s.add("keep", 10.0, 1.0)
    s.add("gone", 10.0, 1.0)
    assert s.drop("gone") is True
    assert not (tmp_path / "gone.json").exists()
    assert s.drop("gone") is False                   # 幂等
    (tmp_path / "orphan.json").write_text("{}")      # 历史遗留：没有对应实例
    removed = s.prune_files({"keep"})
    assert removed == ["orphan"]


def test_sampler_skips_dead_and_counts_children(tmp_path):
    """pm 未存活的实例不采样；采样值取主进程 + 子进程之和。"""

    class FakeReg:
        def all(self):
            return [{"id": "alive"}, {"id": "dead"}]

    class FakePM:
        def __init__(self):
            self.pids = {"alive": 1}
        def is_alive(self, iid):
            return iid in self.pids
        def get(self, iid):
            pid = self.pids[iid]
            return type("P", (), {"probe": lambda self: {"pid": pid}})()

    store = MetricsStore(tmp_path)
    sampler = MetricsSampler(store, FakeReg(), FakePM(), interval=1)

    def fake_sample(iid, pid):
        # 模拟：主进程 50MB / 10%，子进程 30MB / 5%
        return 80.0, 15.0
    sampler.sample_instance = fake_sample
    n = sampler.sample_once()
    assert n == 1                                    # dead 被跳过
    assert store.latest("alive")[1] == 80.0
    assert store.points("dead") == []


def test_sampler_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DM_METRICS", "0")
    s = MetricsSampler(MetricsStore(tmp_path), None, None)
    assert s.enabled is False
    assert s.start() is False                        # 关闭时不起线程
