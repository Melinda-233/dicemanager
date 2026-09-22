"""SnowLuma(登录端) / Dice!Next(骰子) 适配器回归测试。

覆盖：
- 程序包缓存支持 tar 系（.tar.gz 魔数识别 + 解压归一化顶层目录）
- 下载策略 resolve_latest_via_api 的 asset_name_pattern 正则选资
- SnowLuma.write_conn_config 只给指引（自身是 OneBot 服务端，不写配置）
- SnowLuma 从 onebot.json 回读账号 / accessToken（供骰子端继承）
- Dice!Next.write_conn_config 写入 config/adapters.json（forward_ws / reverse_ws / 按名查重）
"""
import io
import json
import os
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

os.environ["DM_STATE_DIR"] = tempfile.mkdtemp(prefix="dm_sn_dn_test_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import urllib.request
from adapters.snowluma import SnowLumaAdapter
from adapters.dicenext import DiceNextAdapter
from core import packages as pkgstore


# ---------- 构造工具 ----------
def _tar_gz_bytes(top_dir: str, files: dict) -> bytes:
    """files: 相对顶层目录的文件名 -> 内容。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            ti = tarfile.TarInfo(f"{top_dir}/{name}")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


SNOWLUMA_MANIFEST = {
    "name": "snowluma", "exe": "launcher.sh",
    "required_files": ["launcher.sh"],
}
DICENEXT_MANIFEST = {
    "name": "dicenext", "exe": "DiceNext",
    "config_path": "config/adapters.json", "required_files": [],
}


# ---------- tar 系：魔数 / 缓存 / 解压归一化 ----------
def test_ext_for_magic_gzip_is_targz():
    assert pkgstore._ext_for_magic(b"\x1f\x8b\x08anything") == ".tar.gz"
    assert pkgstore._ext_for_magic(b"PK\x03\x04rest") == ".zip"
    assert pkgstore._ext_for_magic(b"unsupported") is None


def test_tar_gz_roundtrip_and_find():
    blob = _tar_gz_bytes("SnowLuma-linux-x64", {"onebot.json": "{}"})
    info = pkgstore.save_archive("snowluma", blob, source="upload")
    assert info["exists"] and info["source"] == "upload"
    # find_archive 必须能识别 .tar.gz（而非只认 .zip）
    assert pkgstore.find_archive("snowluma").name == "snowluma.tar.gz"
    assert pkgstore.remove_archive("snowluma")
    assert pkgstore.find_archive("snowluma") is None


def test_deploy_tar_gz_normalizes_top_level(tmp_path, monkeypatch):
    """压缩包带唯一顶层目录时，解压后内容应上移到实例目录根（launcher.sh 直接可寻址）。"""
    blob = _tar_gz_bytes("SnowLuma-linux-x64", {
        "launcher.sh": "#!/bin/sh\necho hi",
        "onebot.json": "{}",
    })
    pkgstore.save_archive("snowluma", blob, source="upload")
    # 有本地包，禁止联网
    def _boom(*a, **k):
        raise AssertionError("本地有包时不应下载")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    ad = SnowLumaAdapter(SNOWLUMA_MANIFEST)
    inst = SimpleNamespace(dir=str(tmp_path / "sl"), allocated_ports={}, actual_port=None)
    assert ad.deploy(inst) == "ok"
    assert (tmp_path / "sl" / "launcher.sh").exists()
    assert not (tmp_path / "sl" / "SnowLuma-linux-x64").exists()   # 顶层目录已归一化
    # build_start_cmd 指向归一化后的 launcher.sh
    assert ad.build_start_cmd(inst) == [str(tmp_path / "sl" / "launcher.sh")]


# ---------- 下载策略：asset_name_pattern 选资 ----------
def test_resolve_download_picks_pattern_asset(monkeypatch):
    """release 资产里混有多平台包时，asset_name_pattern 应精确命中 linux-x64 完整包。"""
    assets = [
        {"name": "SnowLuma-v1.14.19-macos-arm64.tar.gz", "browser_download_url": "http://x/macos"},
        {"name": "SnowLuma-v1.14.19-linux-x64.tar.gz", "browser_download_url": "http://x/linux64"},
        {"name": "SnowLuma-v1.14.19-linux-arm64.tar.gz", "browser_download_url": "http://x/linuxarm"},
    ]
    payload = json.dumps({"assets": assets}).encode("utf-8")

    class Resp:
        def read(self, n=-1):
            return payload if n == -1 else payload[:n]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, *a, **k):
        # 校验确实请求了 latest release API
        assert "releases/latest" in req.full_url
        return Resp()
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    m = {**SNOWLUMA_MANIFEST,
         "download_strategy": "resolve_latest_via_api",
         "release_page": "https://github.com/SnowLuma/SnowLuma/releases",
         "asset_name_pattern": r"SnowLuma-.*-linux-x64\.tar\.gz"}
    ad = SnowLumaAdapter(m)
    assert ad._resolve_download() == "http://x/linux64"


# ---------- SnowLuma.write_conn_config / 回读 ----------
def test_snowluma_write_conn_config_is_guidance_only():
    ad = SnowLumaAdapter(SNOWLUMA_MANIFEST)
    inst = SimpleNamespace(dir="/tmp/x", allocated_ports={}, actual_port=None)
    r = ad.write_conn_config(inst, "ob11", "forward", "127.0.0.1:3001", "TOK")
    assert r.ok is True
    assert r.path is None                       # SnowLuma 自身不写配置文件
    assert "OneBot v11 服务端" in r.manual


def test_snowluma_configure_login_guides_to_webui():
    """SnowLuma 在自身 WebUI 登录：向导应停在登录页展示指引（needs_login=True，不开 WS）。"""
    ad = SnowLumaAdapter(SNOWLUMA_MANIFEST)
    inst = SimpleNamespace(dir="/tmp/x", allocated_ports={}, actual_port=None)
    r = ad.configure_login(inst, {})
    assert r.get("needs_login") is True
    assert "5099" in r["manual"] and "WebUI" in r["manual"]


def test_snowluma_detect_account_and_token(tmp_path):
    onebot = {
        "accounts": [{"uin": "10001", "qq": "10001"}],
        "networks": {"wsServers": [
            {"account": "10001", "accessToken": "SHH-TOKEN-123",
             "url": "ws://127.0.0.1:3001/"}]},
    }
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "onebot.json").write_text(
        json.dumps(onebot), encoding="utf-8")
    ad = SnowLumaAdapter(SNOWLUMA_MANIFEST)
    inst = SimpleNamespace(dir=str(tmp_path), allocated_ports={}, actual_port=None)
    assert ad.detect_account(inst) == "10001"
    assert ad.get_conn_token(inst) == "SHH-TOKEN-123"


# ---------- Dice!Next.write_conn_config -> config/adapters.json ----------
def test_dicenext_write_conn_forward_ws(tmp_path):
    ad = DiceNextAdapter(DICENEXT_MANIFEST)
    inst = SimpleNamespace(dir=str(tmp_path), allocated_ports={}, actual_port=None)
    r = ad.write_conn_config(inst, "ob11", "forward", "127.0.0.1:3001", "TOK")
    assert r.ok and r.path.replace("\\", "/").endswith("config/adapters.json")
    cfg = json.loads((tmp_path / "config" / "adapters.json").read_text("utf-8"))
    assert len(cfg["adapters"]) == 1
    e = cfg["adapters"][0]
    assert e["name"] == "dicemanager" and e["type"] == "onebot_v11"
    assert e["connection_mode"] == "forward_ws"
    assert e["endpoint"] == "ws://127.0.0.1:3001/"
    assert e["access_token"] == "TOK" and e["enabled"] is True


def test_dicenext_write_conn_reverse_ws(tmp_path):
    ad = DiceNextAdapter(DICENEXT_MANIFEST)
    inst = SimpleNamespace(dir=str(tmp_path), allocated_ports={}, actual_port=None)
    r = ad.write_conn_config(inst, "ob11", "reverse", "127.0.0.1:6700", "")
    assert r.ok
    e = json.loads((tmp_path / "config" / "adapters.json").read_text("utf-8"))["adapters"][0]
    assert e["connection_mode"] == "reverse_ws"
    assert e["endpoint"] == "6700"              # 反向：endpoint 为端口号
    assert e["access_token"] == ""


def test_dicenext_write_conn_dedup_by_name(tmp_path):
    ad = DiceNextAdapter(DICENEXT_MANIFEST)
    inst = SimpleNamespace(dir=str(tmp_path), allocated_ports={}, actual_port=None)
    ad.write_conn_config(inst, "ob11", "forward", "127.0.0.1:3001", "A")
    ad.write_conn_config(inst, "ob11", "reverse", "127.0.0.1:6700", "B")  # 改反向应覆盖同 name
    cfg = json.loads((tmp_path / "config" / "adapters.json").read_text("utf-8"))
    assert len(cfg["adapters"]) == 1            # 按 name 查重，不重复添加
    assert cfg["adapters"][0]["connection_mode"] == "reverse_ws"
    assert cfg["adapters"][0]["access_token"] == "B"


def test_dicenext_build_start_cmd_glob_fallback(tmp_path):
    """二进制名与清单 exe 不符时，按 dicenext* 前缀 glob 兜底。"""
    exe = tmp_path / "DiceNext-1.2.3"
    exe.write_text("ELF")
    ad = DiceNextAdapter(DICENEXT_MANIFEST)
    inst = SimpleNamespace(dir=str(tmp_path), allocated_ports={}, actual_port=None)
    assert ad.build_start_cmd(inst) == [str(exe)]
