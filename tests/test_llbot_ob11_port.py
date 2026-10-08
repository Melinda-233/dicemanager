# -*- coding: utf-8 -*-
"""LLBot 的 ob11 端口写入哪一条 connect 条目。

真实故障（2026-10-08）：两个 llbot 实例时，第二个实例的应用端
（nonebot2）收不到消息，而面板诊断显示「登录端 ob11 端口未监听」。

根因：LLBot 的 ob11 模板里有**两条 `type: "ws"`**：
    一条 host=127.0.0.1 —— 客户端（LLBot 主动去连别人）
    一条 host=0.0.0.0   —— 服务端（等别人连它）
应用端要连的是**服务端**那条。早先的实现取「第一条 type==ws」，
于是把**客户端**那条改成了 ob11 分配端口 ——
客户端端口撞上另一个 llbot 的服务端端口，真正的服务端条目却没人连。
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DM_STATE_DIR", tempfile.mkdtemp())

from adapters.llbot import LLBotAdapter  # noqa: E402


def _ad() -> LLBotAdapter:
    return LLBotAdapter({"name": "llbot", "exe": "llbot",
                         "config_path": "bin/llbot/default_config.json",
                         "required_files": ["llbot",
                                            "bin/llbot/default_config.json"]})


# LLBot 发行包里 default_config.json 的 ob11 段形状（两条 ws）
_TEMPLATE_OB11 = {
    "enable": True,
    "connect": [
        {"type": "ws", "enable": False, "host": "127.0.0.1", "port": 3000,
         "heartInterval": 60000, "token": "", "reportSelfMessage": False,
         "reportOfflineMessage": False, "messagePostFormat": "array"},
        {"type": "ws-reverse", "enable": False, "host": None, "port": None,
         "heartInterval": 60000, "token": "", "reportSelfMessage": False,
         "reportOfflineMessage": False, "messagePostFormat": "array"},
        {"type": "http", "enable": False, "host": "127.0.0.1", "port": 3000,
         "heartInterval": 60000, "token": "", "reportSelfMessage": False,
         "reportOfflineMessage": False, "messagePostFormat": "array"},
        {"type": "http-post", "enable": False, "host": None, "port": None,
         "heartInterval": 60000, "token": "", "reportSelfMessage": False,
         "reportOfflineMessage": False, "messagePostFormat": "array"},
        {"type": "ws", "enable": False, "host": "0.0.0.0", "port": 3001,
         "heartInterval": 60000, "token": "", "reportSelfMessage": False,
         "reportOfflineMessage": False, "messagePostFormat": "array"},
    ],
}


def _write_cfg(tmp_path: Path) -> Path:
    """铺一份与发行包同形状的 default_config.json（路径按 manifest 的 config_path）。"""
    d = tmp_path / "bin" / "llbot"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "default_config.json"
    p.write_text(json.dumps({"ob11": json.loads(json.dumps(_TEMPLATE_OB11)),
                             "milky": {"http": {"port": 3002}},
                             "satori": {"port": 5600}},
                    ensure_ascii=False), encoding="utf-8")
    return p


def _instance(tmp_path: Path, ports: dict | None = None):
    alloc = {"webui": 3080}
    alloc.update(ports or {})
    return SimpleNamespace(id="llbot-x", dice="llbot", qq="10001",
                           dir=str(tmp_path), allocated_ports=alloc)


def _cfg_path(tmp_path: Path) -> Path:
    return tmp_path / "bin" / "llbot" / "default_config.json"


def _entries(tmp_path: Path) -> list:
    return json.loads(_cfg_path(tmp_path).read_text(encoding="utf-8"))["ob11"]["connect"]


def test_writes_port_to_server_entry_not_client(tmp_path):
    """ob11 端口必须写进 host=0.0.0.0 的**服务端**条目。"""
    _write_cfg(tmp_path)
    ad = _ad()
    ad._apply_allocated_ports(_instance(tmp_path, {"ob11": 3009}))

    ws = [e for e in _entries(tmp_path) if e.get("type") == "ws"]
    client = next(e for e in ws if str(e.get("host")) == "127.0.0.1")
    server = next(e for e in ws if str(e.get("host")) == "0.0.0.0")
    assert server["port"] == 3009 and server["enable"] is True, "服务端条目应拿到 ob11 端口"
    assert client["port"] == 3000, "客户端条目**不该**被改（它连的是别的实例）"


def test_two_instances_do_not_collide(tmp_path):
    """两个 llbot 实例各写各的服务端端口，互不干扰（回归原始故障）。"""
    a = tmp_path / "a"; b = tmp_path / "b"
    a.mkdir(); b.mkdir()
    ad = _ad()
    ad._apply_allocated_ports(_instance(a, {"ob11": 3001}))
    ad._apply_allocated_ports(_instance(b, {"ob11": 3002}))

    def server_port(d: Path) -> int:
        entries = json.loads(_cfg_path(d).read_text(encoding="utf-8"))["ob11"]["connect"]
        return next(e["port"] for e in entries
                    if e.get("type") == "ws" and str(e.get("host")) == "0.0.0.0")

    assert server_port(a) == 3001
    assert server_port(b) == 3002, "第二个实例的服务端端口应独立，不能被客户端条目抢走"


def test_creates_server_entry_when_only_ws_is_client(tmp_path):
    """只有一条 ws 且它是客户端时，必须新建服务端条目。"""
    p = _write_cfg(tmp_path)
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg["ob11"]["connect"] = [
        {"type": "ws", "enable": False, "host": "0.0.0.0", "port": 3001,
         "heartInterval": 60000, "token": "", "reportSelfMessage": False,
         "reportOfflineMessage": False, "messagePostFormat": "array"},
    ]
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    ad = _ad()
    ad._apply_allocated_ports(_instance(tmp_path, {"ob11": 3005}))
    got = json.loads(p.read_text(encoding="utf-8"))["ob11"]["connect"]
    server = [e for e in got if str(e.get("host")) == "0.0.0.0"]
    assert server and server[0]["port"] == 3005 and server[0]["enable"] is True, \
        "没有服务端条目时应新建一条"


def test_appends_when_no_ws_entry(tmp_path):
    """没有 ws 条目时新增一条服务端条目（不是客户端）。"""
    p = _write_cfg(tmp_path)
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg["ob11"]["connect"] = [e for e in cfg["ob11"]["connect"] if e.get("type") != "ws"]
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    ad = _ad()
    ad._apply_allocated_ports(_instance(tmp_path, {"ob11": 3007}))
    got = json.loads(p.read_text(encoding="utf-8"))["ob11"]["connect"]
    added = [e for e in got if e.get("type") == "ws"]
    assert len(added) == 1 and added[0]["port"] == 3007
    assert str(added[0].get("host")) == "0.0.0.0", "新增的应是服务端条目"


def test_creates_server_entry_when_logged_in_config_is_client_only(tmp_path):
    """登录后的 config_{qq}.json 只剩客户端条目时，必须**新建**服务端条目。

    真实故障第二层（2026-10-08）：LLBot 登录成功后会重写
    `data/config_{qq}.json`，把 ob11.connect 收敛成**只有客户端**一条。
    此时若只「退而改客户端那条」，分配到的 ob11 端口就变成
    「LLBot 主动去连的那个目标」，而应用端要连的服务端从未启动。
    现象：default_config.json 里两条都对、进程也在跑，但该端口无人监听。
    """
    cfg_dir = tmp_path / "bin" / "llbot" / "data"
    cfg_dir.mkdir(parents=True)
    # 按账号配置：只剩客户端一条（LLBot 登录后的真实形状）
    per_uin = cfg_dir / "config_10001.json"
    per_uin.write_text(json.dumps({"ob11": {"enable": True, "connect": [
        {"type": "ws", "enable": True, "host": "127.0.0.1", "port": 3002,
         "heartInterval": 60000, "token": "", "reportSelfMessage": False,
         "reportOfflineMessage": False, "messagePostFormat": "array"},
    ]}}, ensure_ascii=False), encoding="utf-8")
    _write_cfg(tmp_path)          # 引导配置照旧

    ad = _ad()
    ad._apply_allocated_ports(_instance(tmp_path, {"ob11": 3002}))

    got = json.loads(per_uin.read_text(encoding="utf-8"))["ob11"]["connect"]
    ws = [e for e in got if e.get("type") == "ws"]
    server = [e for e in ws if str(e.get("host")) == "0.0.0.0"]
    assert server, "必须新建服务端条目，否则应用端无从连接"
    assert server[0]["port"] == 3002 and server[0]["enable"] is True
