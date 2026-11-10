"""实例详情页「QQ 官方机器人」卡片的两个 REST 端点。

背景：向导里的官方通道只在**建实例时**走一次，之后想换 AppSecret 没有入口。
这里补的是「已建实例也能改」的那条路，因此要点都是**不破坏既有语义**：
- 只回 AppID，任何响应里都不出现 AppSecret；
- 程序不支持（海豹走自己 WebUI / 纯登录端）时明确报 400，而不是假装成功；
- 写配置失败或凭据不全时**不落半份配置**，也不改实例的通道标记；
- 写成功后把实例切到 official 并解除旧的 OneBot 关联（否则总览页还会画一条
  指向已不用登录端的连线）。
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import json5  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.dicenext import DiceNextAdapter  # noqa: E402
from api import rest  # noqa: E402


def _manifest() -> dict:
    return json5.loads((ROOT / "manifests" / "dicenext.json").read_text("utf-8"))


class _Reg:
    """只实现端点用到的两个方法：归属校验与写回。"""

    def __init__(self, rec):
        self.rec = rec
        self.updates: list[dict] = []

    def owned_by(self, iid, user, is_admin=False):
        if iid != self.rec.id:
            raise KeyError(iid)
        return self.rec

    def get(self, iid):
        return self.rec

    def update(self, iid, **kw):
        self.updates.append(kw)
        vars(self.rec).update(kw)


def _inst(tmp_path, **over):
    rec = SimpleNamespace(id="dicenext-i1", dice="dicenext", dir=str(tmp_path),
                          bot_mode="onebot", links=[{"login_ref": "llbot-1"}],
                          conn_addr="127.0.0.1:6700", conn_token="t", conn_direction="forward")
    for k, v in over.items():
        setattr(rec, k, v)
    return rec


def _patch(monkeypatch, inst, cls=DiceNextAdapter, manifest=None):
    reg = _Reg(inst)
    monkeypatch.setattr(rest, "ctx", SimpleNamespace(
        adapters={inst.dice: (manifest or _manifest(), cls)}, registry=reg))
    return reg


class _NoOfficial:
    """没有任何官方通道能力的程序（纯登录端那种）。"""
    def __init__(self, m):
        self.m = m


class _WebUiOnly:
    """海豹式：官方凭据在它自己的 WebUI 里填，面板只给指路。"""
    def __init__(self, m):
        self.m = m

    def official_mode(self):
        return {"id": "official", "needs_login": False,
                "guide_url": "https://docs.sealdice.com/deploy/platform-qq-official.html"}


class _Refusing:
    """装了官方通道但上游拒绝写入（例如端口被占、目录只读）。"""
    def __init__(self, m):
        self.m = m

    def official_mode(self):
        return {"id": "official", "credentials": [{"key": "app_id"}, {"key": "app_secret"}]}

    def official_status(self, instance):
        return {"configured": False, "app_id": None, "path": ""}

    def configure_official(self, instance, credentials):
        return {"ok": False, "message": "磁盘只读，写不进去"}


# ---------- GET ----------

def test_status_unsupported_program(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    _patch(monkeypatch, inst, cls=_NoOfficial)
    r = rest.instance_official("dicenext-i1")
    assert r == {"ok": True, "supported": False, "bot_mode": "onebot"}


def test_status_before_configured(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    _patch(monkeypatch, inst)
    r = rest.instance_official("dicenext-i1")
    assert r["supported"] is True and r["requires_credentials"] is True
    assert r["status"]["configured"] is False
    assert [c["key"] for c in r["credentials"]] == ["app_id", "app_secret"]
    assert r["guide_url"].startswith("https://")


def test_status_after_configured_never_returns_secret(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    _patch(monkeypatch, inst)
    DiceNextAdapter(_manifest()).configure_official(inst, {"app_id": "102", "app_secret": "s3cr3t-x"})
    r = rest.instance_official("dicenext-i1")
    assert r["status"]["configured"] is True and r["status"]["app_id"] == "102"
    assert "s3cr3t-x" not in json.dumps(r, ensure_ascii=False)


def test_status_tolerates_broken_config(monkeypatch, tmp_path):
    """配置文件被手动改坏时，卡片应显示「未配置」而不是整页 500。"""
    inst = _inst(tmp_path)
    _patch(monkeypatch, inst)
    cfg = tmp_path / "config" / "adapters.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{ 这不是 json", "utf-8")
    r = rest.instance_official("dicenext-i1")
    assert r["supported"] is True and r["status"]["configured"] is False


# ---------- POST ----------

def test_save_writes_config_and_switches_channel(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    reg = _patch(monkeypatch, inst)
    r = asyncio.run(rest.instance_official_save(
        "dicenext-i1", rest.OfficialCredsReq(app_id=" 102 ", app_secret=" sec ")))
    assert r["ok"] is True and r["saved"] is True and r["app_id"] == "102"
    assert r["restart_required"] is True and r["detached_links"] == 1
    cfg = json.loads((tmp_path / "config" / "adapters.json").read_text("utf-8"))
    official = [e for e in cfg["adapters"] if e.get("type") == "qq_official"]
    assert len(official) == 1
    assert official[0]["app_id"] == "102" and official[0]["app_secret"] == "sec"
    # 通道标记与旧关联一起切：否则总览页仍画一条指向已不用登录端的连线
    assert reg.updates[0]["bot_mode"] == "official"
    assert reg.updates[0]["login_ref"] is None and reg.updates[0]["links"] == []
    assert reg.updates[0]["conn_token"] is None


def test_save_rejects_missing_secret_without_writing(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    reg = _patch(monkeypatch, inst)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(rest.instance_official_save(
            "dicenext-i1", rest.OfficialCredsReq(app_id="102", app_secret="   ")))
    assert ei.value.status_code == 400 and "AppSecret" in ei.value.detail
    assert not (tmp_path / "config" / "adapters.json").exists()
    assert reg.updates == []


def test_save_rejects_program_without_panel_credentials(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    _patch(monkeypatch, inst, cls=_WebUiOnly)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(rest.instance_official_save(
            "dicenext-i1", rest.OfficialCredsReq(app_id="102", app_secret="sec")))
    assert ei.value.status_code == 400 and "WebUI" in ei.value.detail


def test_save_rejects_unknown_program(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    _patch(monkeypatch, inst, cls=_NoOfficial)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(rest.instance_official_save(
            "dicenext-i1", rest.OfficialCredsReq(app_id="102", app_secret="sec")))
    assert ei.value.status_code == 400


def test_save_surfaces_adapter_refusal(monkeypatch, tmp_path):
    """适配器返回 ok=False 时原样把它的中文原因给前端，不要吞成 500。"""
    inst = _inst(tmp_path)
    reg = _patch(monkeypatch, inst, cls=_Refusing)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(rest.instance_official_save(
            "dicenext-i1", rest.OfficialCredsReq(app_id="102", app_secret="sec")))
    assert ei.value.status_code == 400 and "磁盘只读" in ei.value.detail
    assert reg.updates == []


def test_save_404_on_cross_user_instance(monkeypatch, tmp_path):
    inst = _inst(tmp_path)
    _patch(monkeypatch, inst)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(rest.instance_official_save(
            "ghost", rest.OfficialCredsReq(app_id="102", app_secret="sec")))
    assert ei.value.status_code == 404
