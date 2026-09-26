"""官方机器人通道（bot_mode=official）回归：清单驱动、免登录端、跳过互联。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.registry import Registry, State  # noqa: E402
from services.wizard import Wizard  # noqa: E402


class _M:
    """够用的 manifest 替身：Wizard 只用到 install_root / login_type。"""
    def __init__(self, **kw):
        self.d = {"install_root": "/tmp/dm-test", "login_type": "external", **kw}
    def get(self, k, d=None):
        return self.d.get(k, d)
    def __getitem__(self, k):
        return self.d[k]


class _Adapter:
    def __init__(self, ok=True):
        self.ok = ok
        self.deployed = False
    def deploy(self, inst):
        self.deployed = True
        return "ok"
    def configure_login(self, inst, cred):
        return {}
    def write_conn_config(self, inst, mode, direction, addr, token):
        raise AssertionError("官方通道不得写入 onebot 互联配置")
    def build_start_cmd(self, inst):
        return ["true"]
    def prepare_start(self, inst, runner):
        return False
    def expose_webui(self, inst):
        return None
    def gen_token(self):
        return "tok"


class _Ports:
    def allocate_many(self, dice, roles, owner=None):
        return {k: 3000 + i for i, k in enumerate(roles)}


class _PM:
    def __init__(self):
        self.started = []
    def get(self, iid):
        p = type("P", (), {})()
        p.is_alive = lambda: False
        p.note = lambda m: None
        p.start = lambda cmd, cwd: self.started.append(iid)
        p.ring = []
        p.run_once = lambda *a, **k: 0
        return p
    def is_alive(self, iid):
        return False


def _mk(tmp_path):
    reg = Registry(tmp_path / "instances.json")
    adapters = {"sealdice": (_M(), lambda m: _Adapter())}
    pm = _PM()
    w = Wizard(reg, adapters, _Ports(), pm, tmp_path)
    return reg, w, pm


def test_create_official_ignores_login_ref(tmp_path):
    reg, w, _ = _mk(tmp_path)
    iid = w.create_instance("sealdice", "standalone", login_ref="llbot-xxx",
                            bot_mode="official")
    inst = reg.get(iid)
    assert inst.bot_mode == "official"
    assert inst.login_ref is None                    # 官方通道不需要协议登录端


def test_next_step_skips_login_for_official(tmp_path):
    reg, w, _ = _mk(tmp_path)
    iid = w.create_instance("sealdice", "standalone", bot_mode="official")
    reg.transition(iid, State.DEPLOYING)
    assert w.next_step(iid) == 2
    reg.transition(iid, State.AWAIT_LOGIN)
    assert w.next_step(iid) == 5                     # 直接进启动，不进登录步

    # 对照组：onebot 通道仍需登录步
    iid2 = w.create_instance("sealdice", "standalone", bot_mode="onebot")
    reg.transition(iid2, State.DEPLOYING)
    reg.transition(iid2, State.AWAIT_LOGIN)
    assert w.next_step(iid2) == 3


def test_step3_and_step4_are_noop_for_official(tmp_path):
    reg, w, _ = _mk(tmp_path)
    iid = w.create_instance("sealdice", "standalone", bot_mode="official")
    reg.transition(iid, State.DEPLOYING)
    reg.transition(iid, State.AWAIT_LOGIN)
    r3 = w.run_step(iid, 3, {})
    assert r3["result"] == "ok" and r3.get("skipped") is True
    assert reg.get(iid).state == State.CONFIGURED.value
    r4 = w.run_step(iid, 4, {})                      # 不写互联配置（否则会留一条连不上的端点）
    assert r4["result"] == "ok" and r4.get("skipped") is True


def test_official_can_start(tmp_path):
    reg, w, pm = _mk(tmp_path)
    iid = w.create_instance("sealdice", "standalone", bot_mode="official")
    reg.transition(iid, State.DEPLOYING)
    reg.transition(iid, State.AWAIT_LOGIN)
    w.run_step(iid, 3, {})
    assert w.run_step(iid, 5, {})["result"] == "ok"
    assert reg.get(iid).state == State.RUNNING.value


def test_manifest_api_exposes_bot_modes():
    """/manifests 是白名单透出：漏登记 bot_modes，前端就拿不到「接入通道」选项。"""
    from api.rest import list_manifests
    m = list_manifests()
    assert "official" in {b["id"] for b in (m["sealdice"].get("bot_modes") or [])}


def test_manifest_declares_bot_modes():
    """清单驱动：海豹必须声明 official 通道，且标记不需要登录端。"""
    import json5
    m = json5.loads((ROOT / "manifests" / "sealdice.json").read_text("utf-8"))
    modes = {b["id"]: b for b in m.get("bot_modes", [])}
    assert "official" in modes and "onebot" in modes
    assert modes["official"]["needs_login"] is False
    assert modes["onebot"]["needs_login"] is True
