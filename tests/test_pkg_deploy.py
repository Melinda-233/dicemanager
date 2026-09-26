"""程序包链路测试：本地包部署（零网络）、下载缓存、上传/列表/删除端点。"""
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

os.environ["DM_STATE_DIR"] = tempfile.mkdtemp(prefix="dm_pkg_test_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from adapters.base import BaseAdapter
from conftest import put_package
from core import packages as pkgstore


def _zip_bytes(*names) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in names:
            zf.writestr(n, f"content of {n}")
    return buf.getvalue()


class FakeAdapter(BaseAdapter):
    def build_start_cmd(self, instance): return ["x"]
    def configure_login(self, instance, credentials): return {}
    def write_conn_config(self, *a): return None


MANIFEST = {"name": "fakedice", "required_files": ["a.txt"]}


def test_pkg_roundtrip(tmp_path):
    info = put_package("d1", _zip_bytes("a.txt"), source="upload")
    assert info["exists"] and info["source"] == "upload" and info["size_mb"] >= 0
    assert pkgstore.find_archive("d1") == pkgstore.archive_path("d1")
    assert [p["dice"] for p in pkgstore.list_archives()] == ["d1"]
    assert pkgstore.remove_archive("d1")
    assert not pkgstore.find_archive("d1")
    assert not pkgstore.remove_archive("d1")          # 幂等


def test_pkg_rejects_bad_zip():
    with pytest.raises(ValueError):
        put_package("d2", b"not a zip at all")
    assert not pkgstore.find_archive("d2")            # 坏包不落盘


def test_deploy_uses_local_package_no_network(tmp_path, monkeypatch):
    put_package("fakedice", _zip_bytes("a.txt"), source="upload")
    # 任何联网尝试都直接失败：本地包链路必须零网络
    import urllib.request
    def _boom(*a, **k):
        raise AssertionError("本地有包时不应发起下载")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    ad = FakeAdapter(MANIFEST)
    inst = SimpleNamespace(dir=str(tmp_path / "fd"))
    assert ad.deploy(inst) == "ok"
    assert (tmp_path / "fd" / "a.txt").exists()


def test_deploy_downloads_then_caches(tmp_path, monkeypatch):
    pkgstore.remove_archive("fakedice2")              # 隔离：清掉可能的遗留缓存
    m = {**MANIFEST, "name": "fakedice2"}
    # 无本地包：urlopen 返回内存 zip（替代真实 HTTP）
    import urllib.request
    class Resp:
        def __init__(self, data): self._b = io.BytesIO(data)
        def read(self, n=-1): return self._b.read(n)
        def __enter__(self): return self
        def __exit__(self, *a): return False
    data = _zip_bytes("a.txt")
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: Resp(data))
    monkeypatch.setattr(FakeAdapter, "_resolve_download", lambda self: "http://x/pkg.zip")
    ad = FakeAdapter(m)
    inst = SimpleNamespace(dir=str(tmp_path / "fd2"))
    assert ad.deploy(inst) == "ok"
    assert pkgstore.find_archive("fakedice2").exists()   # 下载包已缓存
    assert pkgstore.info_of("fakedice2")["source"] == "download"
    # 第二个实例部署：直接用缓存，不再联网
    def _boom(*a, **k):
        raise AssertionError("已有缓存包时不应再次下载")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    inst2 = SimpleNamespace(dir=str(tmp_path / "fd3"))
    assert ad.deploy(inst2) == "ok"


def test_meta_updated_at_format():
    put_package("d3", _zip_bytes("a.txt"))
    info = pkgstore.info_of("d3")
    assert "updated_at" in info and ":" in info["updated_at"]
    pkgstore.remove_archive("d3")

# REST 端点（上传/列表/删除）的验证在 tests/smoke_patch.py：
# 全量 pytest 时 api.context 单例已被先导入的 smoke 模块绑定（密码/限速状态），
# 端点测试需要干净的独立进程。
