"""Yogurt（登录端，Milky 协议）测试。

锁定几条从上游 README / config.json 示例核实、但容易被改坏的事实：
- 配置 config.json（扁平结构）；对外 Milky 服务在 httpConfig.host/port/accessToken，
  不是 OneBot 的 Implementations，也不是 Lagrange 的 Milky 段；
- 不需要签名服务：signApiUrl 留空（底层走 PMHQ）；强依赖 PMHQ 常驻
  （默认 ws://localhost:13000/ws），这是与 Lagrange.Milky 最大的架构区别；
- 原生形态为 Docker 镜像，原生包需自行构建后离线上传 → manual 策略；
- 二维码：原生模式落盘形式上游文档未明确，extract_qrcode 做尽力而为
  （data: URL / http 链接 / 常见图片文件名）。
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ["DM_STATE_DIR"] = tempfile.mkdtemp(prefix="dm_yogurt_test_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json5

from adapters.yogurt import YogurtAdapter
from conftest import put_package

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json5.loads((ROOT / "manifests" / "yogurt.json").read_text(encoding="utf-8"))


def _inst(d) -> SimpleNamespace:
    return SimpleNamespace(dir=str(d), allocated_ports={}, actual_port=None)


# ---------- 清单契约 ----------
def test_manifest_is_login_end_milky():
    assert MANIFEST["name"] == "yogurt"
    assert MANIFEST["arch"] == "standalone"
    assert MANIFEST["login_type"] == "qrcode"
    assert MANIFEST["compatible_login"] == ["builtin"]
    assert MANIFEST["exe"] == "yogurt"
    assert MANIFEST["protocol"] == "milky"
    assert MANIFEST["required_files"] == ["yogurt"]
    assert MANIFEST["download_strategy"] == "manual"


def test_manifest_requires_pmhq_prereq():
    """区别于 Lagrange.Milky：Yogurt 强依赖 PMHQ，前置条件里讲清。"""
    assert "prerequisite" in MANIFEST
    assert "PMHQ" in MANIFEST["prerequisite"]


def test_registry_loads_yogurt():
    from adapters import load_registry
    reg = load_registry(ROOT / "manifests")
    manifest, cls = reg["yogurt"]
    assert cls is YogurtAdapter
    assert manifest["exe"] == "yogurt"


def test_dice_ends_accept_yogurt_as_login():
    for name in ("sealdice",):
        m = json5.loads((ROOT / "manifests" / f"{name}.json").read_text(encoding="utf-8"))
        assert "yogurt" in m["compatible_login"], name


# ---------- 部署：预置 config.json ----------
def test_deploy_precreates_config(tmp_path):
    d = tmp_path / "yogurt-1"
    d.mkdir()
    (d / "yogurt").write_text("stub")
    ad = YogurtAdapter(MANIFEST)
    assert ad.deploy(_inst(d)) == "ok"
    cfg = json.loads((d / "config.json").read_text("utf-8"))
    assert cfg["httpConfig"]["host"] == "127.0.0.1"
    assert cfg["httpConfig"]["port"] == 30001
    assert cfg["httpConfig"]["accessToken"] == ""
    assert cfg["signApiUrl"] == ""                 # 不需要签名服务
    assert cfg["pmhqUrl"] == "ws://localhost:13000/ws"


def test_deploy_from_offline_package(tmp_path):
    """manual 策略：离线上传包 → 必备文件校验 → 预置配置 → 启动命令。"""
    import io
    import zipfile

    from core import packages as pkgstore
    pkgstore.remove_archive("yogurt")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("yogurt", "stub")
    put_package("yogurt", buf.getvalue(), source="upload")

    ad = YogurtAdapter(MANIFEST)
    inst = _inst(tmp_path / "yogurt-1")
    assert ad.deploy(inst) == "ok"
    assert ad.build_start_cmd(inst) == [str(tmp_path / "yogurt-1" / "yogurt")]
    assert (Path(inst.dir) / "config.json").exists()
    pkgstore.remove_archive("yogurt")


def test_prepare_start_keeps_user_config(tmp_path):
    d = tmp_path / "yogurt-3"
    d.mkdir()
    (d / "yogurt").write_text("stub")
    (d / "config.json").write_text(
        json.dumps({"httpConfig": {"host": "10.0.0.9", "port": 7777, "accessToken": "X"}}))
    ad = YogurtAdapter(MANIFEST)
    ad.prepare_start(_inst(d))
    cfg = json.loads((d / "config.json").read_text("utf-8"))
    assert cfg["httpConfig"]["host"] == "10.0.0.9"
    assert cfg["httpConfig"]["port"] == 7777
    assert cfg["httpConfig"]["accessToken"] == "X"
    assert "pmhqUrl" in cfg


# ---------- 互联配置（写 Milky 服务端）----------
def test_write_conn_config_forward_milky(tmp_path):
    d = tmp_path / "yg-f"
    d.mkdir()
    ad = YogurtAdapter(MANIFEST)
    inst = _inst(d)
    r = ad.write_conn_config(inst, "milky", "forward", "127.0.0.1:3000", "TOK123")
    assert r.ok and r.path.endswith("config.json")
    cfg = json.loads((d / "config.json").read_text("utf-8"))["httpConfig"]
    assert cfg["host"] == "0.0.0.0"
    assert cfg["port"] == 3000
    assert cfg["accessToken"] == "TOK123"
    assert "http://" in r.manual


def test_get_conn_token_for_dice_end_inheritance(tmp_path):
    d = tmp_path / "yg-t"
    d.mkdir()
    ad = YogurtAdapter(MANIFEST)
    inst = _inst(d)
    assert ad.get_conn_token(inst) is None
    ad.write_conn_config(inst, "milky", "forward", "127.0.0.1:3000", "SHARED")
    assert ad.get_conn_token(inst) == "SHARED"


def test_health_check_probes_milky_port():
    ad = YogurtAdapter(MANIFEST)
    inst = type("I", (), {"allocated_ports": {}})()
    assert ad.health_check(inst) == {"alive": False, "conn": "none"}
    inst = type("I", (), {"allocated_ports": {"milky": 1}})()
    assert ad.health_check(inst, is_alive=True) == {"alive": True, "conn": "down"}


# ---------- 二维码与账号 ----------
def test_qrcode_from_data_url_line(tmp_path):
    d = tmp_path / "yg-q"
    d.mkdir()
    ad = YogurtAdapter(MANIFEST)
    line = "scan this: data:image/png;base64,iVBORw0KGgoAAAANS=="
    out = ad.extract_qrcode(line, _inst(d))
    assert out and out["base64"] == line.split(": ", 1)[1]
    assert out["url"] is None


def test_qrcode_from_disk_image(tmp_path):
    d = tmp_path / "yg-q2"
    d.mkdir()
    (d / "qrcode.png").write_bytes(b"\x89PNG-stub")
    ad = YogurtAdapter(MANIFEST)
    out = ad.extract_qrcode("no-qr-here", _inst(d))
    assert out and out["base64"].startswith("data:image/png;base64,")


def test_detect_account_from_quick_login_uin(tmp_path):
    d = tmp_path / "yg-a"
    d.mkdir()
    (d / "config.json").write_text(json.dumps({"quickLoginUin": 10001}))
    ad = YogurtAdapter(MANIFEST)
    assert ad.detect_account(_inst(d)) == "10001"
    # 缺该字段时返回 None（而不是瞎猜日志里的数字）
    (d / "config.json").write_text(json.dumps({"quickLoginUin": None}))
    assert ad.detect_account(_inst(d)) is None
