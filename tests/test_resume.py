"""启动自动恢复回归：面板重启后 RUNNING 实例的拉起边界。

背景（2026-09-26）：`systemctl restart dicemanager` 会杀掉实例子进程，而 registry
仍标 RUNNING，此前无恢复逻辑（面板显示在跑、实际全挂）。恢复口径必须锁死——
「自动拉起」一旦扩大到 AUTO_START 不该管的状态（比如用户主动 stop 的 CONFIGURED），
就会变成「关不掉的实例」。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class _FakeProc:
    def __init__(self, alive: bool = False):
        self._alive = alive
        self.notes: list[str] = []

    def is_alive(self) -> bool:
        return self._alive

    def note(self, msg: str) -> None:
        self.notes.append(msg)


class _FakeRegistry:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def all(self) -> list[dict]:
        return self.rows


class _FakePM:
    def __init__(self, alive: dict[str, bool] | None = None):
        self._procs: dict[str, _FakeProc] = {}
        self._alive = alive or {}

    def get(self, iid: str) -> _FakeProc:
        return self._procs.setdefault(iid, _FakeProc(self._alive.get(iid, False)))


def _resume(rows, pm=None, **kw):
    from services.resume import resume_running_instances

    reg = _FakeRegistry(rows)
    calls: list[str] = []

    def start_fn(iid: str):
        calls.append(iid)
        if kw.get("fail") == iid:
            raise RuntimeError("模拟启动失败")
        return (None, False)

    opts = {k: v for k, v in kw.items() if k in ("delay", "stagger")}
    out = resume_running_instances(reg, pm or _FakePM(), start_fn, **opts)
    return out, calls, reg


def test_resumes_dead_running_instance():
    """RUNNING 且进程已死 → 自动拉起，并在实例日志里留痕。"""
    pm = _FakePM()
    out, calls, _ = _resume([{"id": "llbot-aaa", "state": "RUNNING"}], pm,
                            delay=0, stagger=0)
    assert out == {"llbot-aaa": "started"}
    assert calls == ["llbot-aaa"]
    assert any("自动恢复" in n for n in pm.get("llbot-aaa").notes)


def test_skips_states_that_user_did_not_leave_running():
    """CONFIGURED（用户主动 stop）、AWAIT_LOGIN（待扫码）、ERROR 都不得被自动拉起。"""
    rows = [{"id": "s1", "state": "CONFIGURED"},
            {"id": "s2", "state": "AWAIT_LOGIN"},
            {"id": "s3", "state": "ERROR"},
            {"id": "s4", "state": "UNDEPLOYED"}]
    out, calls, _ = _resume(rows, delay=0, stagger=0)
    assert out == {}
    assert calls == []


def test_skips_alive_process():
    """进程还活着就不重复拉（start_instance 本身也会返回 already_running）。"""
    pm = _FakePM({"llbot-aaa": True})
    out, calls, _ = _resume([{"id": "llbot-aaa", "state": "RUNNING"}], pm,
                            delay=0, stagger=0)
    assert out == {"llbot-aaa": "alive"}
    assert calls == []


def test_one_failure_does_not_block_others():
    """某个实例失败（目录被删/exe 缺失）不影响其余实例恢复。"""
    rows = [{"id": "bad-1", "state": "RUNNING"}, {"id": "ok-2", "state": "RUNNING"}]
    out, calls, _ = _resume(rows, fail="bad-1", delay=0, stagger=0)
    assert out["bad-1"].startswith("error:")
    assert out["ok-2"] == "started"
    assert calls == ["bad-1", "ok-2"]


def test_failure_is_written_into_instance_log():
    pm = _FakePM()
    out, _, _ = _resume([{"id": "bad-1", "state": "RUNNING"}], pm,
                        fail="bad-1", delay=0, stagger=0)
    assert out["bad-1"].startswith("error:")
    assert any("失败" in n for n in pm.get("bad-1").notes)


def test_disabled_by_env(monkeypatch):
    """DM_AUTO_RESUME=0 时整体不拉起（排障开关）。"""
    monkeypatch.setenv("DM_AUTO_RESUME", "0")
    out, calls, _ = _resume([{"id": "llbot-aaa", "state": "RUNNING"}], delay=0,
                            stagger=0)
    assert out == {}
    assert calls == []
