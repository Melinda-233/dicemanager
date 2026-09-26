"""Lagrange.Milky（登录端，Milky 协议）测试。

锁定几条从上游源码核实、但容易被改坏的事实：
- 上游 nightly 仅发布 OneBot 二进制 → 必须走 manual 离线上传，否则部署 404；
- Milky 对外服务在 appsettings.json 的 Milky.HttpServer（Host/Port）+ Milky.AccessToken，
  不是 OneBot 的 Implementations；
- 必须填 Lagrange.Protocol.Signer.Token（V2 Sign API）才能启动，否则 Signer 报错；
- 二维码是 stdout 字符画 + 磁盘 qr-*.png，日志里没有 data URL（基类正则抓不到）；
- write_conn_config 写的是 Milky.HttpServer（0.0.0.0:port）+ AccessToken，便于骰子端
  以 Milky 基址 http://<IP>:port 连接（HTTP API + /event WS），与 OneBot 的 ws:// 不同。
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ["DM_STATE_DIR"] = tempfile.mkdtemp(prefix="dm_lmilky_test_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json5

from adapters.lagrange_milky import LagrangeMilkyAdapter
from conftest import put_package

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json5.loads((ROOT / "manifests" / "lagrange_milky.json").read_text(encoding="utf-8"))


def _inst(d) -> SimpleNamespace:
    return SimpleNamespace(dir=str(d), allocated_ports={}, actual_port=None)


# ---------- 清单契约 ----------
def test_manifest_is_login_end_milky():
    assert MANIFEST["name"] == "lagrange_milky"
    assert MANIFEST["arch"] == "standalone"
    assert MANIFEST["login_type"] == "qrcode"
    assert MANIFEST["compatible_login"] == ["builtin"]
    assert MANIFEST["exe"] == "Lagrange.Milky"
    assert MANIFEST["protocol"] == "milky"
    assert MANIFEST["required_files"] == ["Lagrange.Milky"]
    assert MANIFEST["download_strategy"] == "manual"


def test_manifest_requires_signer_token_prereq():
    """区别于 Lagrange.OneBot：Milky 必须 Signer Token 才起得来，要在前置条件里讲清。"""
    assert "prerequisite" in MANIFEST
    assert "Signer.Token" in MANIFEST["prerequisite"]


def test_registry_loads_lagrange_milky():
    from adapters import load_registry
    reg = load_registry(ROOT / "manifests")
    manifest, cls = reg["lagrange_milky"]
    assert cls is LagrangeMilkyAdapter
    assert manifest["exe"] == "Lagrange.Milky"


def test_dice_ends_accept_lagrange_milky_as_login():
    for name in ("sealdice",):
        m = json5.loads((ROOT / "manifests" / f"{name}.json").read_text(encoding="utf-8"))
        assert "lagrange_milky" in m["compatible_login"], name


# ---------- 部署：预置 Milky 配置 ----------
def test_deploy_precreates_appsettings(tmp_path):
    """部署必须预置 appsettings.json（缺则首次启动行为不确定，且 Signer 缺失会报错）。"""
    d = tmp_path / "lagrange_milky-1"
    d.mkdir()
    (d / "Lagrange.Milky").write_text("stub")
    ad = LagrangeMilkyAdapter(MANIFEST)
    assert ad.deploy(_inst(d)) == "ok"
    cfg = json.loads((d / "appsettings.json").read_text("utf-8"))
    assert cfg["Milky"]["HttpServer"]["Host"] == "127.0.0.1"
    assert cfg["Milky"]["HttpServer"]["Port"] == 3000
    assert cfg["Milky"]["AccessToken"] is None
    assert cfg["Milky"]["Api"]["Http"]["Enabled"] is True
    assert cfg["Milky"]["Event"]["WebSocket"]["Enabled"] is True


def test_deploy_from_offline_package(tmp_path):
    """manual 策略：离线上传包 → 必备文件校验 → 预置配置 → 启动命令。"""
    import io
    import zipfile

    from core import packages as pkgstore
    pkgstore.remove_archive("lagrange_milky")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Lagrange.Milky", "stub")
    put_package("lagrange_milky", buf.getvalue(), source="upload")

    ad = LagrangeMilkyAdapter(MANIFEST)
    inst = _inst(tmp_path / "lagrange_milky-1")
    assert ad.deploy(inst) == "ok"
    assert ad.build_start_cmd(inst) == [str(tmp_path / "lagrange_milky-1" / "Lagrange.Milky")]
    assert (Path(inst.dir) / "appsettings.json").exists()
    pkgstore.remove_archive("lagrange_milky")


def test_prepare_start_fixes_exec_bit(tmp_path):
    d = tmp_path / "lagrange_milky-2"
    d.mkdir()
    exe = d / "Lagrange.Milky"
    exe.write_text("stub")
    exe.chmod(0o644)
    ad = LagrangeMilkyAdapter(MANIFEST)
    inst = _inst(d)
    assert ad.prepare_start(inst) is False
    assert (d / "appsettings.json").exists()
    if os.name != "nt":
        assert exe.stat().st_mode & 0o111


def test_prepare_start_keeps_user_config(tmp_path):
    d = tmp_path / "lagrange_milky-3"
    d.mkdir()
    (d / "Lagrange.Milky").write_text("stub")
    (d / "appsettings.json").write_text(
        json.dumps({"Milky": {"HttpServer": {"Host": "10.0.0.9", "Port": 9999}}}))
    ad = LagrangeMilkyAdapter(MANIFEST)
    ad.prepare_start(_inst(d))
    cfg = json.loads((d / "appsettings.json").read_text("utf-8"))
    # 只补缺的键，用户已改的 Milky 段不动
    assert cfg["Milky"]["HttpServer"]["Host"] == "10.0.0.9"
    assert cfg["Milky"]["HttpServer"]["Port"] == 9999
    assert "AccessToken" in cfg["Milky"]


# ---------- Signer Token 注入 ----------
def test_configure_login_injects_signer_token(tmp_path):
    d = tmp_path / "lm-sig"
    d.mkdir()
    (d / "Lagrange.Milky").write_text("stub")
    ad = LagrangeMilkyAdapter(MANIFEST)
    ad.deploy(_inst(d))
    r = ad.configure_login(_inst(d), {"signer_token": "V2_SIGN_TOK"})
    assert r.get("ok") is True
    cfg = json.loads((d / "appsettings.json").read_text("utf-8"))
    assert cfg["Lagrange"]["Protocol"]["Signer"]["Token"] == "V2_SIGN_TOK"


# ---------- 互联配置（写 Milky 服务端）----------
def test_write_conn_config_forward_milky(tmp_path):
    d = tmp_path / "lm-f"
    d.mkdir()
    ad = LagrangeMilkyAdapter(MANIFEST)
    inst = _inst(d)
    r = ad.write_conn_config(inst, "milky", "forward", "127.0.0.1:3000", "TOK123")
    assert r.ok and r.path.endswith("appsettings.json")
    cfg = json.loads((d / "appsettings.json").read_text("utf-8"))["Milky"]
    assert cfg["HttpServer"]["Host"] == "0.0.0.0"
    assert cfg["HttpServer"]["Port"] == 3000
    assert cfg["AccessToken"] == "TOK123"
    assert "http://" in r.manual          # 提示骰子端用 Milky 基址连接


def test_get_conn_token_for_dice_end_inheritance(tmp_path):
    d = tmp_path / "lm-t"
    d.mkdir()
    ad = LagrangeMilkyAdapter(MANIFEST)
    inst = _inst(d)
    assert ad.get_conn_token(inst) is None
    ad.write_conn_config(inst, "milky", "forward", "127.0.0.1:3000", "SHARED")
    assert ad.get_conn_token(inst) == "SHARED"


def test_health_check_probes_milky_port():
    ad = LagrangeMilkyAdapter(MANIFEST)
    inst = type("I", (), {"allocated_ports": {}})()
    assert ad.health_check(inst) == {"alive": False, "conn": "none"}
    inst = type("I", (), {"allocated_ports": {"milky": 1}})()
    # 端口 1 几乎不可能在监听 → down（alive 默认 False）
    assert ad.health_check(inst, is_alive=True) == {"alive": True, "conn": "down"}


# ---------- 二维码与账号 ----------
def test_qrcode_comes_from_png(tmp_path):
    d = tmp_path / "lm-q"
    d.mkdir()
    (d / "qr-0.png").write_bytes(b"\x89PNG-stub")
    ad = LagrangeMilkyAdapter(MANIFEST)
    inst = _inst(d)
    out = ad.extract_qrcode("█▀▄ " * 8, inst)
    assert out and out["base64"].startswith("data:image/png;base64,")
    assert out["url"] is None
    assert ad.extract_qrcode("█▀▄ " * 8, None) is None


def test_detect_account_from_keystore(tmp_path):
    d = tmp_path / "lm-a"
    d.mkdir()
    (d / "keystore.json").write_text('{"Uin": 10001, "Session": {}}')
    ad = LagrangeMilkyAdapter(MANIFEST)
    assert ad.detect_account(_inst(d)) == "10001"
    assert ad.account_from_logs([(1, "info: Bot Uin: 10002")]) == "10002"
