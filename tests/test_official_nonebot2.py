"""NoneBot2 的 QQ 官方机器人通道（面板侧写入）。

覆盖三件事：
1. 清单声明（server/win 两份都在、凭据字段正确）；
2. `configure_official()` 写 .env 的契约（QQ_BOTS / DRIVER / 清掉 OneBot 项 / 权限）；
3. 官方适配器的安装方式**不能**用 `pip install --target --upgrade`
   （2026-11-10 服务器事故：会把 libs/nonebot 整个替换、删掉 onebot 适配器）。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json5  # noqa: E402

from adapters.nonebot2 import NoneBot2Adapter  # noqa: E402
from core.atomicio import read_dotenv  # noqa: E402
from core.registry import Registry, State  # noqa: E402
from services.wizard import Wizard  # noqa: E402


def _manifest(**over):
    m = json5.loads((ROOT / "manifests" / "nonebot2.json").read_text("utf-8"))
    m.update(over)
    return m


class _Inst:
    def __init__(self, d):
        self.id = "nonebot2-test"
        self.dir = str(d)
        self.allocated_ports = {}


def _adapter(**over) -> NoneBot2Adapter:
    return NoneBot2Adapter(_manifest(**over))


@pytest.fixture()
def inst(tmp_path):
    d = tmp_path / "nonebot2"
    (d / "libs" / "nonebot" / "adapters").mkdir(parents=True)
    # 预置适配器目录：让 configure_official 认为适配器已装，整条用例不联网
    (d / "libs" / "nonebot" / "adapters" / "qq").mkdir()
    (d / "plugins").mkdir()
    (d / ".env").write_text(
        "DRIVER=~fastapi+~websockets\n"
        "HOST=127.0.0.1\n"
        "PORT=8080\n"
        'ONEBOT_V11_WS_URLS=["ws://127.0.0.1:3002/"]\n'
        "ONEBOT_V11_ACCESS_TOKEN=tok\n"
        'SUPERUSERS=["10001"]\n',
        "utf-8",
    )
    return _Inst(d)


# ---------- 清单 ----------

def test_manifest_declares_official_with_credentials():
    modes = {b["id"]: b for b in _manifest()["bot_modes"]}
    assert modes["onebot"]["needs_login"] is True
    assert modes["official"]["needs_login"] is False
    assert [c["key"] for c in modes["official"]["credentials"]] == ["app_id", "app_secret"]
    assert modes["official"]["credentials"][1]["secret"] is True
    assert modes["official"]["credential_hint"]
    assert modes["official"]["guide_url"].startswith("https://")


def test_win_manifest_matches_server_bot_modes():
    win = json5.loads((ROOT / "manifests" / "nonebot2_win.json").read_text("utf-8"))
    srv = json5.loads((ROOT / "manifests" / "nonebot2.json").read_text("utf-8"))
    assert win["bot_modes"] == srv["bot_modes"]


def test_requires_official_credentials_follows_manifest():
    assert _adapter().requires_official_credentials() is True
    m = _manifest()
    m["bot_modes"] = [{"id": "onebot", "label": "OneBot", "needs_login": True}]
    assert NoneBot2Adapter(m).requires_official_credentials() is False


# ---------- 写 .env ----------

def test_configure_official_writes_env(inst):
    r = _adapter().configure_official(inst, {"app_id": "102", "app_secret": "sec-xyz"})
    assert r["ok"] is True and r["app_id"] == "102"
    assert r["installed_adapter"] is False                   # 预置了目录 → 不重装
    assert "sec-xyz" not in json.dumps(r, ensure_ascii=False)  # Secret 不外传

    env = read_dotenv(Path(inst.dir) / ".env")
    bots = json.loads(env["QQ_BOTS"])
    assert bots[0]["id"] == "102" and bots[0]["secret"] == "sec-xyz"
    assert bots[0]["use_websocket"] is True
    assert bots[0]["intent"]["c2c_group_at_messages"] is True
    assert not any(bots[0]["intent"][k] for k in bots[0]["intent"]
                   if k != "c2c_group_at_messages")
    # 官方通道下 OneBot 的连接项必须清掉，否则实例会去连不存在的登录端
    assert "ONEBOT_V11_WS_URLS" not in env
    assert "ONEBOT_V11_ACCESS_TOKEN" not in env
    # DRIVER 必须补 HTTPClient，否则 QQ 适配器 setup 直接抛
    assert "aiohttp" in env["DRIVER"]
    # 用户自己的配置不能被顺手清掉
    assert "10001" in env["SUPERUSERS"]


def test_configure_official_creates_boot_plugin(inst):
    _adapter().configure_official(inst, {"app_id": "102", "app_secret": "s"})
    boot = Path(inst.dir) / "plugins" / "qq_official_boot.py"
    assert boot.is_file()
    text = boot.read_text("utf-8")
    assert "register_adapter(QQAdapter)" in text
    assert "nonebot.adapters.qq" in text


def test_configure_official_boot_plugin_is_idempotent(inst):
    ad = _adapter()
    ad.configure_official(inst, {"app_id": "102", "app_secret": "s"})
    first = (Path(inst.dir) / "plugins" / "qq_official_boot.py").read_bytes()
    ad.configure_official(inst, {"app_id": "102", "app_secret": "s2"})
    assert (Path(inst.dir) / "plugins" / "qq_official_boot.py").read_bytes() == first


def test_configure_official_replaces_existing_bot_entry(inst):
    ad = _adapter()
    ad.configure_official(inst, {"app_id": "old", "app_secret": "oldsec"})
    r = ad.configure_official(inst, {"app_id": "new", "app_secret": "newsec"})
    assert r["ok"] is True
    bots = json.loads(read_dotenv(Path(inst.dir) / ".env")["QQ_BOTS"])
    assert len(bots) == 1 and bots[0]["id"] == "new"


def test_configure_official_rejects_missing_secret(tmp_path):
    d = tmp_path / "nb"
    d.mkdir()
    r = _adapter().configure_official(_Inst(d), {"app_id": "102", "app_secret": ""})
    assert r["ok"] is False and "AppSecret" in r["message"]
    assert not (d / ".env").exists()          # 半份配置不落盘


def test_driver_gains_http_client_without_duplicating():
    ad = _adapter()
    assert ad._driver_with_http_client("~fastapi+~websockets") == "~fastapi+~websockets+~aiohttp"
    # 已经是 httpx 的驱动器不必再加
    assert ad._driver_with_http_client("~fastapi+~httpx") == "~fastapi+~httpx"
    assert "~aiohttp" in ad._driver_with_http_client(None)
    assert "~aiohttp" in ad._driver_with_http_client("")


# ---------- 回读 ----------

def test_official_status_reads_back_app_id_only(inst):
    ad = _adapter()
    assert ad.official_status(inst)["configured"] is False
    ad.configure_official(inst, {"app_id": "102", "app_secret": "sec-xyz"})
    st = ad.official_status(inst)
    assert st["configured"] is True and st["app_id"] == "102"
    assert "sec-xyz" not in json.dumps(st, ensure_ascii=False)


def test_official_status_tolerates_broken_json(tmp_path):
    d = tmp_path / "nb"
    d.mkdir()
    (d / ".env").write_text("QQ_BOTS=[not json\n", "utf-8")
    st = _adapter().official_status(_Inst(d))
    assert st["configured"] is False and st["app_id"] == ""


# ---------- 适配器安装方式（事故回归） ----------

def test_ensure_qq_adapter_skips_when_already_present(inst, monkeypatch):
    import subprocess
    def _boom(*a, **k):
        raise AssertionError("适配器已装时不该再联网装（会触碰已有依赖）")
    monkeypatch.setattr(subprocess, "run", _boom)
    assert _adapter()._ensure_qq_adapter(inst) is False


def test_ensure_qq_adapter_never_uses_pip_install_target(monkeypatch, tmp_path):
    """回归：必须走 pip download + 解包，绝不能用 `pip install --target`。"""
    d = tmp_path / "nb"
    (d / "libs" / "nonebot" / "adapters").mkdir(parents=True)
    seen: list[list[str]] = []

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "boom"

    def _run(cmd, **kw):
        seen.append(list(cmd))
        return _Proc()

    import subprocess
    monkeypatch.setattr(subprocess, "run", _run)
    with pytest.raises(RuntimeError):
        _adapter()._ensure_qq_adapter(_Inst(d))
    assert seen, "应当调用 pip"
    pip_cmd = next(c for c in seen if "pip" in c)
    assert "download" in pip_cmd and "install" not in pip_cmd
    assert "--upgrade" not in pip_cmd and "--target" not in pip_cmd


# ---------- 向导第 3 步（面板真实链路） ----------

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
    w = Wizard(reg, {"nonebot2": (m, NoneBot2Adapter)}, _Ports(), _PM(), tmp_path)
    iid = w.create_instance("nonebot2", "standalone", bot_mode="official")
    d = Path(reg.get(iid).dir)
    d.mkdir(parents=True, exist_ok=True)
    # 预置官方适配器目录，避免用例联网下载（下载逻辑另有专门用例）
    (d / "libs" / "nonebot" / "adapters" / "qq").mkdir(parents=True)
    reg.transition(iid, State.DEPLOYING)
    reg.transition(iid, State.AWAIT_LOGIN)
    return reg, w, iid, d


def test_wizard_step3_writes_env_and_configures(tmp_path):
    reg, w, iid, d = _wizard(tmp_path)
    r = w.run_step(iid, 3, {"credentials": {"app_id": "102", "app_secret": "sec"}})
    assert r["result"] == "ok" and r["saved"] is True and r["app_id"] == "102"
    assert "sec" not in json.dumps(r, ensure_ascii=False)
    assert reg.get(iid).state == State.CONFIGURED.value
    env = read_dotenv(d / ".env")
    assert json.loads(env["QQ_BOTS"])[0]["id"] == "102"
    assert "aiohttp" in env["DRIVER"]
    assert (d / "plugins" / "qq_official_boot.py").is_file()


def test_wizard_step3_missing_credentials_stays_on_login_step(tmp_path):
    """缺凭据要报错并停在 AWAIT_LOGIN：不能假装配置好，否则实例起来只是连不上。"""
    reg, w, iid, _d = _wizard(tmp_path)
    r = w.run_step(iid, 3, {})
    assert r["result"] == "error" and "AppID" in r["message"]
    assert reg.get(iid).state == State.AWAIT_LOGIN.value
