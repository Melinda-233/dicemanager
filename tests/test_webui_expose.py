"""登录/启动同时开放 WebUI：绑定修正（回环 → 0.0.0.0）+ ufw 放行钩子。"""
import json
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import adapters.base
import pytest

from adapters.base import BaseAdapter  # noqa: F401  (确认钩子挂在基类)
from adapters.llbot import LLBotAdapter
from adapters.napcat import NapCatAdapter
from adapters.snowluma import SnowLumaAdapter
from core.firewall import open_port
from core.registry import Instance


def inst(tmp_path, program, allocated):
    return Instance(id=f"{program}-1", dice=program, arch="standalone",
                    dir=str(tmp_path), port=0, allocated_ports=dict(allocated))


# ---------- core.firewall：ufw 尽力而为 ----------
def _patch_ufw(monkeypatch, active, rc=0):
    calls = []

    def run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[-1] == "status":
            out = "Status: active\n" if active else "Status: inactive\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

    monkeypatch.setattr("shutil.which", lambda name: "/usr/sbin/ufw" if name == "ufw" else None)
    monkeypatch.setattr("core.firewall.subprocess.run", run)
    return calls


def test_open_port_without_ufw_is_silent(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert open_port(6099) is None
    assert open_port(None) is None          # 无 WebUI 端口的程序（Lagrange 等）


def test_open_port_skips_when_ufw_inactive(monkeypatch):
    calls = _patch_ufw(monkeypatch, active=False)
    assert open_port(6099) is None
    assert len(calls) == 1                  # 未启用：不添加死规则


def test_open_port_allows_when_ufw_active(monkeypatch):
    calls = _patch_ufw(monkeypatch, active=True)
    note = open_port(6099)
    assert note and "6099" in note and "安全组" in note
    assert calls[-1] == ["/usr/sbin/ufw", "allow", "6099/tcp"]


def test_open_port_ufw_failure_returns_none(monkeypatch):
    _patch_ufw(monkeypatch, active=True, rc=4)      # 非 root 等失败场景
    assert open_port(6099) is None


# ---------- NapCat：webui.json 绑定修正 ----------
NAP = NapCatAdapter({"name": "napcat", "exe": "NapCat.sh"})

def test_napcat_presets_webui_json_when_missing(tmp_path):
    NAP._ensure_webui_binding(inst(tmp_path, "napcat", {"webui": 6099}))
    d = json.loads((tmp_path / "config" / "webui.json").read_text("utf-8"))
    assert d["host"] == "0.0.0.0" and d["port"] == 6099 and d["loginRate"] == 3
    assert "token" not in d                 # 令牌留给 NapCat 生成并从日志回读


def test_napcat_flips_loopback_host_only(tmp_path):
    f = tmp_path / "config" / "webui.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"host": "127.0.0.1", "port": 6100, "token": "keep-me"}))
    NAP._ensure_webui_binding(inst(tmp_path, "napcat", {"webui": 6099}))
    d = json.loads(f.read_text("utf-8"))
    assert d["host"] == "0.0.0.0" and d["token"] == "keep-me"
    assert d["port"] == 6100                # 端口是 NapCat 自管（占用自增），不代写


def test_napcat_keeps_custom_host(tmp_path):
    f = tmp_path / "config" / "webui.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"host": "192.168.1.5", "port": 6099}))
    NAP._ensure_webui_binding(inst(tmp_path, "napcat", {"webui": 6099}))
    assert json.loads(f.read_text("utf-8"))["host"] == "192.168.1.5"


# ---------- LLBot：default_config.json 补 host ----------
LLB = LLBotAdapter({"name": "llbot", "exe": "llbot",
                    "config_path": "default_config.json"})

def test_llbot_webui_host_defaults_open(tmp_path):
    inst_ = inst(tmp_path, "llbot", {"webui": 3080, "ob11": 3001})
    LLB._apply_allocated_ports(inst_)       # 配置文件缺失：整体新建
    d = json.loads((tmp_path / "default_config.json").read_text("utf-8"))
    assert d["webui"]["host"] == "0.0.0.0" and d["webui"]["port"] == 3080


def test_llbot_webui_host_loopback_flipped(tmp_path):
    f = tmp_path / "default_config.json"
    f.write_text(json.dumps({"webui": {"enable": True, "host": "127.0.0.1", "port": 3080}}))
    LLB._apply_allocated_ports(inst(tmp_path, "llbot", {"webui": 3081}))
    d = json.loads(f.read_text("utf-8"))
    assert d["webui"]["host"] == "0.0.0.0" and d["webui"]["port"] == 3081


def test_llbot_webui_host_custom_kept(tmp_path):
    f = tmp_path / "default_config.json"
    f.write_text(json.dumps({"webui": {"enable": True, "host": "10.0.0.2", "port": 3080}}))
    LLB._apply_allocated_ports(inst(tmp_path, "llbot", {"webui": 3081}))
    assert json.loads(f.read_text("utf-8"))["webui"]["host"] == "10.0.0.2"


def test_llbot_patches_per_uin_config(tmp_path):
    """登录后 LLBot 以 data/config_{qq}.json 为准（覆盖 default_config），
    端口写回必须同时覆盖两份，否则外网监听会被打回回环。"""
    f = tmp_path / "default_config.json"
    f.write_text(json.dumps({"webui": {"enable": True, "host": "0.0.0.0", "port": 3080}}))
    i = Instance(id="llbot-1", dice="llbot", arch="standalone", dir=str(tmp_path),
                 port=3080, allocated_ports={"webui": 3081}, qq="975809162")
    LLB._apply_allocated_ports(i)
    per = tmp_path / "bin" / "llbot" / "data" / "config_975809162.json"
    d = json.loads(per.read_text("utf-8"))          # 缺失时由引导配置播种
    assert d["webui"]["host"] == "0.0.0.0" and d["webui"]["port"] == 3081


def test_llbot_flips_loopback_in_existing_per_uin_config(tmp_path):
    f = tmp_path / "default_config.json"
    f.write_text(json.dumps({"webui": {"enable": True, "host": "0.0.0.0", "port": 3080}}))
    per = tmp_path / "bin" / "llbot" / "data" / "config_975809162.json"
    per.parent.mkdir(parents=True)
    per.write_text(json.dumps({"webui": {"enable": True, "host": "127.0.0.1", "port": 3080}}))
    i = Instance(id="llbot-1", dice="llbot", arch="standalone", dir=str(tmp_path),
                 port=3080, allocated_ports={"webui": 3080}, qq="975809162")
    LLB._apply_allocated_ports(i)
    d = json.loads(per.read_text("utf-8"))
    assert d["webui"]["host"] == "0.0.0.0"          # 已存在的按账号配置被就地修正


def test_llbot_no_per_uin_seed_without_qq(tmp_path):
    """qq 未知（未登录过）时不应凭空造出按账号配置。"""
    f = tmp_path / "default_config.json"
    f.write_text(json.dumps({"webui": {"enable": True, "port": 3080}}))
    LLB._apply_allocated_ports(inst(tmp_path, "llbot", {"webui": 3081}))
    assert not (tmp_path / "bin" / "llbot" / "data").exists()


# ---------- SnowLuma：config/runtime.json 绑定修正 ----------
SNOW = SnowLumaAdapter({"name": "snowluma", "webui_default_port": 5099})

def test_snowluma_presets_runtime_json(tmp_path):
    SNOW._ensure_webui_binding(inst(tmp_path, "snowluma", {"webui": 5099}))
    d = json.loads((tmp_path / "config" / "runtime.json").read_text("utf-8"))
    assert d["webuiHost"] == "0.0.0.0" and d["webuiPort"] == 5099


def test_snowluma_uses_allocated_port_when_preset(tmp_path):
    SNOW._ensure_webui_binding(inst(tmp_path, "snowluma", {"webui": 5101}))
    d = json.loads((tmp_path / "config" / "runtime.json").read_text("utf-8"))
    assert d["webuiPort"] == 5101


def test_snowluma_flips_loopback_keeps_rest(tmp_path):
    f = tmp_path / "config" / "runtime.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"webuiHost": "127.0.0.1", "webuiPort": 5150,
                             "logRetainDays": 7}))
    SNOW._ensure_webui_binding(inst(tmp_path, "snowluma", {"webui": 5099}))
    d = json.loads(f.read_text("utf-8"))
    assert d["webuiHost"] == "0.0.0.0" and d["webuiPort"] == 5150   # 端口不代写
    assert d["logRetainDays"] == 7


def test_snowluma_keeps_custom_host(tmp_path):
    f = tmp_path / "config" / "runtime.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"webuiHost": "192.168.0.9", "webuiPort": 5099}))
    SNOW._ensure_webui_binding(inst(tmp_path, "snowluma", {"webui": 5099}))
    assert json.loads(f.read_text("utf-8"))["webuiHost"] == "192.168.0.9"


# ---------- SealDice：UI 绑定由启动命令决定，必须 0.0.0.0 ----------
def test_sealdice_binds_public_address(tmp_path):
    from adapters.sealdice import SealDiceAdapter
    a = SealDiceAdapter({"name": "sealdice", "exe": "sealdice-core"})
    i = Instance(id="sealdice-1", dice="sealdice", arch="standalone",
                 dir=str(tmp_path), port=3211)
    assert a.build_start_cmd(i) == [str(tmp_path / "sealdice-core"),
                                    "--address=0.0.0.0:3211"]


# ---------- 基类钩子：端口来源与 ufw 提示透传 ----------
def test_expose_webui_prefers_allocated_then_manifest(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(adapters.base, "open_port", lambda p: seen.append(p) or "note")
    manifest = {"name": "napcat", "exe": "NapCat.sh", "webui_default_port": 6099}
    a = NapCatAdapter(manifest)
    assert a.expose_webui(inst(tmp_path, "napcat", {"webui": 6098})) == "note"
    assert seen[-1] == 6098                 # 分配端口优先
    a.expose_webui(inst(tmp_path, "napcat", {}))
    assert seen[-1] == 6099                 # 兜底 manifest 默认端口


def test_expose_webui_no_port_no_action(monkeypatch, tmp_path):
    # 无 WebUI 的程序：不允许拿非空端口去放行（open_port(None) 静默返回 None）
    def _no_open(p):
        assert not p, f"不应尝试放行端口: {p}"
        return None
    monkeypatch.setattr(adapters.base, "open_port", _no_open)
    from adapters.lagrange import LagrangeAdapter
    a = LagrangeAdapter({"name": "lagrange", "exe": "Lagrange.OneBot"})
    assert a.expose_webui(inst(tmp_path, "lagrange", {})) is None   # 无 WebUI：无动作
