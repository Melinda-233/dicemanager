"""Dice-Next × QQ 官方机器人通道：清单声明、配置写入、向导接线、凭据自检。

背景（上游 main 分支已核实）：
- Dice-Next 3.x 自带 qq_official 适配器（server/src/adapter/qq_official_adapter.h），
  凭据来自 config/adapters.json 的 {type:"qq_official", app_id, app_secret, ...}
  （main.cpp:3113-3122），启动时以该文件为准覆盖内部适配器表。
- 所以面板的职责只有两件：把凭据写对、让用户能在启动前知道凭据是不是好的。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import json5  # noqa: E402

from adapters.dicenext import DiceNextAdapter  # noqa: E402
from core import qqofficial  # noqa: E402
from core.atomicio import read_json_any  # noqa: E402
from core.registry import Registry, State  # noqa: E402
from services.wizard import Wizard  # noqa: E402


def _manifest(**over):
    m = json5.loads((ROOT / "manifests" / "dicenext.json").read_text("utf-8"))
    m.update(over)
    return m


class _Inst:
    """够用的实例替身：适配器只读 dir。"""
    def __init__(self, d):
        self.id = "dicenext-test"
        self.dir = str(d)


# ---------- 清单 ----------

def test_manifest_declares_official_with_credentials():
    m = _manifest()
    modes = {b["id"]: b for b in m["bot_modes"]}
    assert modes["official"]["needs_login"] is False
    keys = [c["key"] for c in modes["official"]["credentials"]]
    assert keys == ["app_id", "app_secret"]
    assert modes["official"]["credentials"][1]["secret"] is True   # secret 才能用密码框
    assert modes["onebot"]["needs_login"] is True


def test_win_manifest_matches_server_bot_modes():
    """桌面版与服务器版清单分文件维护：官方通道漏一边，桌面版就静默少一条通道。"""
    win = json5.loads((ROOT / "manifests" / "dicenext_win.json").read_text("utf-8"))
    srv = json5.loads((ROOT / "manifests" / "dicenext.json").read_text("utf-8"))
    assert win["bot_modes"] == srv["bot_modes"]


def test_sealdice_official_has_no_credentials():
    """海豹的官方通道由它自己 WebUI 收凭据：清单不该声明 credentials，
    否则向导会逼用户把 AppSecret 交给面板（行为回归）。"""
    m = json5.loads((ROOT / "manifests" / "sealdice.json").read_text("utf-8"))
    modes = {b["id"]: b for b in m["bot_modes"]}
    assert not modes["official"].get("credentials")


def test_requires_official_credentials_follows_manifest():
    ad = DiceNextAdapter(_manifest())
    assert ad.requires_official_credentials() is True
    # 把声明去掉 → 适配器必须立刻退回「程序自己收凭据」的形态
    m = _manifest()
    m["bot_modes"] = [b for b in m["bot_modes"] if b["id"] != "official"]
    assert DiceNextAdapter(m).requires_official_credentials() is False


# ---------- 写配置 ----------

def test_configure_official_writes_entry_and_keeps_onebot(tmp_path):
    d = tmp_path / "dicenext"
    (d / "config").mkdir(parents=True)
    path = d / "config" / "adapters.json"
    path.write_text(json.dumps({"adapters": [
        {"name": "link-a", "type": "onebot_v11", "enabled": True},
        {"name": "qq_official", "type": "qq_official", "app_id": "old", "app_secret": "old"},
    ]}), "utf-8")

    ad = DiceNextAdapter(_manifest())
    r = ad.configure_official(_Inst(d), {"app_id": "102", "app_secret": "sec"})
    assert r["ok"] is True and r["app_id"] == "102"
    assert "sec" not in json.dumps(r, ensure_ascii=False)     # secret 不外传

    entries = read_json_any(path)["adapters"]
    types = [e["type"] for e in entries]
    assert types.count("qq_official") == 1                    # 旧的官方条目被替换
    assert "onebot_v11" in types                              # 登录端条目保留
    off = next(e for e in entries if e["type"] == "qq_official")
    assert off["app_id"] == "102" and off["app_secret"] == "sec"
    assert off["enabled"] is True                             # 上游只注册 enabled 的
    assert off["receive_mode"] == "gateway"                   # 面板部署给不出公网 HTTPS


def test_configure_official_rejects_missing_secret(tmp_path):
    d = tmp_path / "dicenext"
    d.mkdir()
    r = DiceNextAdapter(_manifest()).configure_official(_Inst(d), {"app_id": "102"})
    assert r["ok"] is False and "AppSecret" in r["message"]
    assert not (d / "config" / "adapters.json").exists()       # 缺件不该写半份配置


def test_official_status_reads_back_app_id_only(tmp_path):
    d = tmp_path / "dicenext"
    (d / "config").mkdir(parents=True)
    (d / "config" / "adapters.json").write_text(json.dumps({"adapters": [
        {"name": "qq_official", "type": "qq_official",
         "app_id": "102", "app_secret": "sec"}]}), "utf-8")
    st = DiceNextAdapter(_manifest()).official_status(_Inst(d))
    assert st["configured"] is True and st["app_id"] == "102"
    assert "sec" not in json.dumps(st)


# ---------- 向导接线 ----------

class _Ports:
    def allocate_many(self, dice, roles, owner=None):
        return {k: 3000 + i for i, k in enumerate(roles)}


class _PM:
    def get(self, iid):
        p = type("P", (), {})()
        p.is_alive = lambda: False
        p.note = lambda m: None
        p.start = lambda cmd, cwd: None
        p.ring = []
        return p

    def is_alive(self, iid):
        return False


def _wizard(tmp_path):
    m = _manifest(install_root=str(tmp_path))
    reg = Registry(tmp_path / "instances.json")
    w = Wizard(reg, {"dicenext": (m, DiceNextAdapter)}, _Ports(), _PM(), tmp_path)
    iid = w.create_instance("dicenext", "standalone", bot_mode="official")
    d = Path(reg.get(iid).dir)
    d.mkdir(parents=True, exist_ok=True)
    reg.transition(iid, State.DEPLOYING)
    reg.transition(iid, State.AWAIT_LOGIN)
    return reg, w, iid, d


def test_wizard_step3_writes_official_config(tmp_path):
    reg, w, iid, d = _wizard(tmp_path)
    r = w.run_step(iid, 3, {"credentials": {"app_id": "102", "app_secret": "sec"}})
    assert r["result"] == "ok" and r["saved"] is True and r["app_id"] == "102"
    assert reg.get(iid).state == State.CONFIGURED.value
    off = next(e for e in read_json_any(d / "config" / "adapters.json")["adapters"]
               if e["type"] == "qq_official")
    assert off["app_secret"] == "sec"


def test_wizard_step3_without_credentials_is_an_error_on_fresh_instance(tmp_path):
    """Dice-Next 缺凭据时不能「假装成功」——否则用户点完继续，实例起来只是连不上。"""
    reg, w, iid, _d = _wizard(tmp_path)
    r = w.run_step(iid, 3, {})
    assert r["result"] == "error" and "AppID" in r["message"]
    assert reg.get(iid).state == State.AWAIT_LOGIN.value       # 状态不许被推进


def test_wizard_step3_resume_keeps_saved_config(tmp_path):
    reg, w, iid, d = _wizard(tmp_path)
    w.run_step(iid, 3, {"credentials": {"app_id": "102", "app_secret": "sec"}})
    # 重进向导时前端会再发一次 step3 但 payload 为空：必须沿用已存配置而不是报错
    r = w.run_step(iid, 3, {})
    assert r["result"] == "ok" and r["saved"] is False and r["app_id"] == "102"
    assert reg.get(iid).state == State.CONFIGURED.value


def test_wizard_step4_official_stays_skipped(tmp_path):
    reg, w, iid, _d = _wizard(tmp_path)
    w.run_step(iid, 3, {"credentials": {"app_id": "102", "app_secret": "sec"}})
    r = w.run_step(iid, 4, {})
    assert r["result"] == "ok" and r["skipped"] is True


# ---------- 凭据自检（官方鉴权接口）----------

def test_verify_ok(monkeypatch):
    monkeypatch.setattr(qqofficial, "_post_json",
                        lambda url, body, timeout: {"access_token": "t", "expires_in": "7200"})
    r = qqofficial.verify_official_credentials("102", "sec")
    assert r["ok"] is True and r["expires_in"] == 7200
    assert "120" in r["message"]                               # 7200s → 120 分钟


def test_verify_bad_secret_code(monkeypatch):
    monkeypatch.setattr(qqofficial, "_post_json",
                        lambda url, body, timeout: {"code": 100016, "message": "invalid"})
    r = qqofficial.verify_official_credentials("102", "wrong")
    assert r["ok"] is False and r["code"] == 100016
    assert "不匹配" in r["message"]


def test_verify_network_down_mentions_ip_whitelist(monkeypatch):
    def boom(url, body, timeout):
        raise TimeoutError("timed out")
    monkeypatch.setattr(qqofficial, "_post_json", boom)
    r = qqofficial.verify_official_credentials("102", "sec")
    assert r["ok"] is False and "白名单" in r["message"]


def test_verify_requires_both_fields():
    r = qqofficial.verify_official_credentials("102", "")
    assert r["ok"] is False and "AppSecret" in r["message"]


def test_rest_endpoint_wraps_verifier(monkeypatch):
    from api.rest import OfficialVerifyReq, verify_official_credentials_api
    monkeypatch.setattr(qqofficial, "_post_json",
                        lambda url, body, timeout: {"access_token": "t", "expires_in": "7200"})
    r = verify_official_credentials_api(OfficialVerifyReq(app_id="102", app_secret="sec"))
    assert r["ok"] is True
    assert set(r) == {"ok", "code", "expires_in", "message"}   # token 本身不回到前端
