"""修补项回归测试：NapCat 日志回读/配置写入、LLBot 端口写入、token 生成与继承。"""
import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from adapters.base import BaseAdapter, WriteResult
from adapters.napcat import NapCatAdapter
from adapters.llbot import LLBotAdapter
from core.registry import Registry, Instance


def ring(lines):
    """构造 (seq, line) 形式的日志环形缓冲。"""
    return list(enumerate(lines))


# ---------- NapCat：日志回读 ----------
NAP = NapCatAdapter({"name": "napcat", "exe": "NapCat.sh"})

def test_napcat_panel_url_re():
    line = ("[2026-09-20 08:00:00.000] [info] [NapCat] [WebUi] WebUi User Panel Url: "
            "http://127.0.0.1:6099/webui?token=abc123-XYZ_~")
    r = ring(["boot...", line])
    assert NAP.get_actual_port(r) == 6099
    assert NAP.get_webui_token(r) == "abc123-XYZ_~"

def test_napcat_legacy_token_line():
    line = "[WebUi] Login Token is deadbeef99"
    r = ring([line])
    assert NAP.get_webui_token(r) == "deadbeef99"
    assert NAP.get_actual_port(r) is None

def test_napcat_detect_account_from_config(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "onebot11_123456789.json").write_text("{}", encoding="utf-8")
    inst = Instance(id="x", dice="napcat", arch="standalone", dir=str(tmp_path), port=0)
    assert NAP.detect_account(inst) == "123456789"

# ---------- NapCat：互联配置写入 ----------
def test_napcat_write_forward(tmp_path):
    """正向 = websocketServers（NapCat 监听），无 QQ 号写默认配置文件。"""
    inst = Instance(id="napcat-1", dice="napcat", arch="standalone",
                    dir=str(tmp_path), port=6099)
    wr = NAP.write_conn_config(inst, "ob11", "forward", "127.0.0.1:3001", "tok1")
    assert wr.ok
    f = tmp_path / "config" / "onebot11.json"
    assert f.exists()
    net = json.loads(f.read_text("utf-8"))["network"]
    sv = [e for e in net["websocketServers"] if e["name"] == "dicemanager"]
    assert len(sv) == 1 and sv[0]["port"] == 3001 and sv[0]["token"] == "tok1"

def test_napcat_write_reverse_idempotent(tmp_path):
    """反向 = websocketClients；重复写入同名条目应替换而非累加。"""
    inst = Instance(id="napcat-1", dice="napcat", arch="standalone",
                    dir=str(tmp_path), port=6099, qq="10001")
    for tok in ("t1", "t2"):
        wr = NAP.write_conn_config(inst, "ob11", "reverse", "127.0.0.1:3001", tok)
        assert wr.ok
    f = tmp_path / "config" / "onebot11_10001.json"
    net = json.loads(f.read_text("utf-8"))["network"]
    cl = [e for e in net["websocketClients"] if e["name"] == "dicemanager"]
    assert len(cl) == 1 and cl[0]["token"] == "t2"
    assert cl[0]["url"].startswith("ws://127.0.0.1:3001")

# ---------- LLBot：端口写入与互联配置 ----------
LLB = LLBotAdapter({"name": "llbot", "exe": "LLBot-CLI",
                    "config_path": "default_config.json", "post_start_action": "--update"})

def test_llbot_apply_allocated_ports(tmp_path):
    cfg = tmp_path / "default_config.json"
    cfg.write_text(json.dumps({"webui": {"enable": True, "host": "0.0.0.0", "port": 3080},
                               "ob11": {"enable": True, "connect": [
                                   {"type": "ws", "enable": True, "host": "0.0.0.0",
                                    "port": 3001, "token": ""}]}}), encoding="utf-8")
    inst = Instance(id="llbot-1", dice="llbot", arch="standalone", dir=str(tmp_path),
                    port=3080, allocated_ports={"webui": 3081, "ob11": 3005})
    LLB._apply_allocated_ports(inst)                       # 幂等：连跑两次
    LLB._apply_allocated_ports(inst)
    d = json.loads(cfg.read_text("utf-8"))
    assert d["webui"]["port"] == 3081
    ws = [e for e in d["ob11"]["connect"] if e["type"] == "ws"]
    assert len(ws) == 1 and ws[0]["port"] == 3005 and d["ob11"]["enable"] is True

def test_llbot_write_conn_config_creates_entry(tmp_path):
    inst = Instance(id="llbot-1", dice="llbot", arch="standalone", dir=str(tmp_path),
                    port=3080, allocated_ports={"webui": 3080})
    LLB._apply_allocated_ports(inst)
    wr = LLB.write_conn_config(inst, "ob11", "forward", "127.0.0.1:3001", "tok")
    assert wr.ok
    d = json.loads((tmp_path / "default_config.json").read_text("utf-8"))
    ws = [e for e in d["ob11"]["connect"] if e.get("name") == "dicemanager"]
    assert len(ws) == 1 and ws[0]["port"] == 3001 and ws[0]["token"] == "tok"

def test_llbot_prepare_start_first_run(tmp_path):
    """首启执行 --update 且只执行一次；runner 缺失时不阻断。"""
    calls = []
    inst = Instance(id="llbot-1", dice="llbot", arch="standalone", dir=str(tmp_path),
                    port=3080)
    assert LLB.prepare_start(inst, lambda cmd, cwd, label: calls.append(cmd)) is True
    assert len(calls) == 1 and calls[0][-1] == "--update"
    inst.first_run_done = True
    assert LLB.prepare_start(inst, lambda cmd, cwd, label: calls.append(cmd)) is False
    assert len(calls) == 1

# ---------- 基础设施 ----------
def test_gen_token():
    t = BaseAdapter.gen_token()
    assert t and len(t) >= 16 and BaseAdapter.gen_token() != t

def test_registry_new_fields_roundtrip(tmp_path):
    reg = Registry(tmp_path / "instances.json")
    reg.create("i1", dice="napcat", arch="standalone", dir_="/tmp/x", port=6099)
    reg.update("i1", conn_token="tk", conn_addr="127.0.0.1:3001",
               conn_direction="forward", first_run_done=True, webui_token="wtok")
    got = reg.get("i1")
    assert (got.conn_token, got.conn_addr, got.conn_direction,
            got.first_run_done, got.webui_token) == ("tk", "127.0.0.1:3001",
                                                     "forward", True, "wtok")

def test_write_result_defaults():
    wr = WriteResult()
    assert wr.ok and wr.manual is None and wr.path is None
