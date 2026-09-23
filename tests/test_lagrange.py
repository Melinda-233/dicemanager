"""Lagrange.OneBot（登录端）测试。

锁定几条从上游源码核实、但容易被改坏的事实：
- release 只有 nightly tag（无 latest）→ 必须走 /releases/tags/nightly，否则 404；
- 缺 appsettings.json 时程序会 Console.ReadKey 等按键 → 部署必须预置配置；
- 二维码是 stdout 字符画 + 磁盘 qr-*.png，日志里没有 data URL（基类正则抓不到）；
- ForwardWebSocket 没有 Suffix 字段，ReverseWebSocket 才有。
"""
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
from types import SimpleNamespace

os.environ["DM_STATE_DIR"] = tempfile.mkdtemp(prefix="dm_lagrange_test_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json5

from adapters.base import BaseAdapter, WriteResult
from adapters.lagrange import LagrangeAdapter

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json5.loads((ROOT / "manifests" / "lagrange.json").read_text(encoding="utf-8"))


def _inst(d) -> SimpleNamespace:
    return SimpleNamespace(dir=str(d), allocated_ports={}, actual_port=None)


# ---------- 清单契约 ----------
def test_manifest_is_login_end_with_qrcode():
    assert MANIFEST["name"] == "lagrange"
    assert MANIFEST["arch"] == "standalone"
    assert MANIFEST["login_type"] == "qrcode"
    assert MANIFEST["compatible_login"] == ["builtin"]
    assert MANIFEST["exe"] == "Lagrange.OneBot"
    assert MANIFEST["required_files"] == ["Lagrange.OneBot"]


def test_manifest_uses_nightly_tag_and_tar_gz():
    """上游无 latest release，只有 nightly；资产是自带运行时的 tar.gz。"""
    assert MANIFEST["download_strategy"] == "resolve_latest_via_api"
    assert MANIFEST["release_tag"] == "nightly"
    assert "linux-x64" in MANIFEST["asset_name_pattern"]


def test_no_linuxqq_prerequisite():
    """协议自建（纯 C#），不像 NapCat 需要 linuxqq-deb。"""
    assert "prerequisite" not in MANIFEST


def test_registry_loads_lagrange():
    from adapters import load_registry
    reg = load_registry(ROOT / "manifests")
    manifest, cls = reg["lagrange"]
    assert cls is LagrangeAdapter
    assert manifest["exe"] == "Lagrange.OneBot"


def test_dice_ends_accept_lagrange_as_login():
    """登录端要在骰子端的兼容矩阵里登记，否则向导选不到。"""
    for name in ("sealdice", "shiki", "dicenext"):
        m = json5.loads((ROOT / "manifests" / f"{name}.json").read_text(encoding="utf-8"))
        assert "lagrange" in m["compatible_login"], name


# ---------- 下载解析 ----------
def test_release_tag_is_used_in_api_url(monkeypatch):
    """release_tag 必须改写 API 端点：/releases/latest 对 Lagrange 是 404。"""
    seen = {}

    class Resp:
        def read(self):
            return json.dumps({"assets": [
                {"name": "Lagrange.OneBot_win-x64_net9.0_SelfContained.zip",
                 "browser_download_url": "https://x/win.zip"},
                {"name": "Lagrange.OneBot_linux-x64_net9.0_SelfContained.tar.gz",
                 "browser_download_url": "https://x/linux-x64.tar.gz"},
            ]}).encode()

    def fake(req, *a, **k):
        seen["url"] = req.full_url
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    url = LagrangeAdapter(MANIFEST)._resolve_download()
    assert "/releases/tags/nightly" in seen["url"]
    assert url == "https://x/linux-x64.tar.gz"      # 正则选中 linux-x64，不是 win


# ---------- 部署：必须预置配置 ----------
def test_deploy_precreates_appsettings(tmp_path):
    """缺 appsettings.json 时程序会 Console.ReadKey 等按键 → 无头部署卡死。"""
    d = tmp_path / "lagrange-1"
    d.mkdir()
    (d / "Lagrange.OneBot").write_text("stub")
    ad = LagrangeAdapter(MANIFEST)
    assert ad.deploy(_inst(d)) == "ok"
    cfg = json.loads((d / "appsettings.json").read_text("utf-8"))
    assert cfg["Account"]["Protocol"] == "Linux"
    assert cfg["QrCode"]["ConsoleCompatibilityMode"] is False
    assert cfg["Implementations"] == []


def test_deploy_from_official_tar_gz(tmp_path):
    """首次安装路径：tar.gz 解压 → 必备文件校验 → 预置配置 → 启动命令。"""
    import io
    import tarfile

    from core import packages as pkgstore
    pkgstore.remove_archive("lagrange")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"stub"
        info = tarfile.TarInfo("Lagrange.OneBot")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    pkgstore.save_archive("lagrange", buf.getvalue(), source="upload")

    ad = LagrangeAdapter(MANIFEST)
    inst = _inst(tmp_path / "lagrange-1")
    assert ad.deploy(inst) == "ok"
    assert ad.build_start_cmd(inst) == [str(tmp_path / "lagrange-1" / "Lagrange.OneBot")]
    assert (Path(inst.dir) / "appsettings.json").exists()
    pkgstore.remove_archive("lagrange")


def test_prepare_start_fixes_exec_bit(tmp_path):
    d = tmp_path / "lagrange-2"
    d.mkdir()
    exe = d / "Lagrange.OneBot"
    exe.write_text("stub")
    exe.chmod(0o644)
    ad = LagrangeAdapter(MANIFEST)
    inst = _inst(d)
    assert ad.prepare_start(inst) is False          # 幂等，不是首启一次性动作
    assert (d / "appsettings.json").exists()
    if os.name != "nt":                       # Windows 没有执行位，chmod 无意义
        assert exe.stat().st_mode & 0o111


def test_prepare_start_keeps_user_config(tmp_path):
    """用户已改过的配置不能被默认骨架覆盖。"""
    d = tmp_path / "lagrange-3"
    d.mkdir()
    (d / "Lagrange.OneBot").write_text("stub")
    (d / "appsettings.json").write_text(
        json.dumps({"Account": {"Uin": 12345, "Protocol": "Windows"}}))
    ad = LagrangeAdapter(MANIFEST)
    ad.prepare_start(_inst(d))
    cfg = json.loads((d / "appsettings.json").read_text("utf-8"))
    assert cfg["Account"]["Uin"] == 12345 and cfg["Account"]["Protocol"] == "Windows"
    assert cfg["Implementations"] == []             # 只补缺的键


# ---------- 互联配置 ----------
def test_forward_ws_has_no_suffix(tmp_path):
    d = tmp_path / "lg-f"
    d.mkdir()
    ad = LagrangeAdapter(MANIFEST)
    inst = _inst(d)
    r = ad.write_conn_config(inst, "ob11", "forward", "127.0.0.1:3001", "TOK123")
    assert r.ok and r.path.endswith("appsettings.json")
    impl = json.loads((d / "appsettings.json").read_text("utf-8"))["Implementations"][0]
    assert impl["Type"] == "ForwardWebSocket"
    assert impl["Port"] == 3001 and impl["AccessToken"] == "TOK123"
    assert "Suffix" not in impl                     # schema: 正向没有该字段
    assert "TOK123" in r.manual


def test_reverse_ws_has_suffix(tmp_path):
    d = tmp_path / "lg-r"
    d.mkdir()
    ad = LagrangeAdapter(MANIFEST)
    r = ad.write_conn_config(_inst(d), "ob11", "reverse", "ws://10.0.0.5:6700/", "TOK9")
    assert r.ok
    impl = json.loads((d / "appsettings.json").read_text("utf-8"))["Implementations"][0]
    assert impl["Type"] == "ReverseWebSocket"
    assert impl["Host"] == "10.0.0.5" and impl["Port"] == 6700
    assert impl["Suffix"] == "/onebot/v11/ws"
    assert impl["AccessToken"] == "TOK9"


def test_rewrite_replaces_same_type_entry(tmp_path):
    """同类型只保留一条，反复写入不堆积；用户自建的其它类型不动。"""
    d = tmp_path / "lg-d"
    d.mkdir()
    ad = LagrangeAdapter(MANIFEST)
    inst = _inst(d)
    ad.write_conn_config(inst, "ob11", "forward", "127.0.0.1:3001", "OLD")
    ad.write_conn_config(inst, "ob11", "forward", "127.0.0.1:3002", "NEW")
    ad.write_conn_config(inst, "ob11", "reverse", "127.0.0.1:6700", "NEW")
    impls = json.loads((d / "appsettings.json").read_text("utf-8"))["Implementations"]
    assert len(impls) == 2
    fwd = [i for i in impls if i["Type"] == "ForwardWebSocket"]
    assert len(fwd) == 1 and fwd[0]["Port"] == 3002 and fwd[0]["AccessToken"] == "NEW"


def test_get_conn_token_for_dice_end_inheritance(tmp_path):
    """骰子端经 login_ref 继承同一个 token。"""
    d = tmp_path / "lg-t"
    d.mkdir()
    ad = LagrangeAdapter(MANIFEST)
    inst = _inst(d)
    assert ad.get_conn_token(inst) is None
    ad.write_conn_config(inst, "ob11", "forward", "127.0.0.1:3001", "SHARED")
    assert ad.get_conn_token(inst) == "SHARED"


# ---------- 二维码与账号 ----------
def test_qrcode_comes_from_png_not_log(tmp_path):
    d = tmp_path / "lg-q"
    d.mkdir()
    (d / "qr-0.png").write_bytes(b"\x89PNG-stub")
    ad = LagrangeAdapter(MANIFEST)
    inst = _inst(d)
    out = ad.extract_qrcode("█▀▄ " * 8, inst)
    assert out and out["base64"].startswith("data:image/png;base64,")
    assert out["url"] is None
    # 兼容模式（ConsoleCompatibilityMode=true）是 ASCII 字符画
    assert ad.extract_qrcode(".^@ " * 8, inst) is not None
    # 普通日志行不命中；基类那套 data-URL 正则对字符画也无效
    assert ad.extract_qrcode("info: Lagrange.OneBot started", inst) is None

    class _BaseOnly(BaseAdapter):            # 只继承基类的 data-URL 提取器
        def build_start_cmd(self, instance): return []

        def configure_login(self, instance, credentials): return {}

        def write_conn_config(self, instance, mode, direction, addr, token):
            return WriteResult()

    assert _BaseOnly(MANIFEST).extract_qrcode("█▀▄ " * 8, inst) is None


def test_qrcode_missing_png_returns_none(tmp_path):
    d = tmp_path / "lg-n"
    d.mkdir()
    ad = LagrangeAdapter(MANIFEST)
    assert ad.extract_qrcode("█▀▄ " * 8, _inst(d)) is None
    assert ad.extract_qrcode("█▀▄ " * 8, None) is None


def test_detect_account_and_logs(tmp_path):
    d = tmp_path / "lg-a"
    d.mkdir()
    (d / "keystore.json").write_text('{"Uin": 10001, "Session": {}}')
    ad = LagrangeAdapter(MANIFEST)
    assert ad.detect_account(_inst(d)) == "10001"
    assert ad.account_from_logs([(1, "info: Bot Uin: 10002")]) == "10002"
    assert ad.account_from_logs([(1, "Version: nightly")]) is None
