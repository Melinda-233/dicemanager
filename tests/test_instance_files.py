"""实例文件管理：core.instance_files 纯逻辑 + REST 端点（只读浏览 + 受限写）。

安全底线是 realpath 前缀 containment：所有操作都限制在实例目录内，
``../`` 穿越 / 绝对路径 / 软链逃逸一律拒绝（PermissionError）。REST 层复用 ``_own``
做归属校验，本测试直接调端点函数（ctx 是模块级单例，DM_STATE_DIR 指向临时目录），
做法参考 tests/test_manage_app.py。
"""
import base64
import os
import sys
import tempfile
from pathlib import Path

import pytest

tmp = tempfile.mkdtemp(prefix="dm_instfiles_")
os.environ["DM_STATE_DIR"] = tmp
os.environ["DM_LOG_DIR"] = os.path.join(tmp, "logs")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

from api.context import ctx  # noqa: E402
from api.rest import (  # noqa: E402
    FileMkdirReq,
    FileRenameReq,
    FileWriteReq,
    instance_file_delete,
    instance_file_download,
    instance_file_mkdir,
    instance_file_read,
    instance_file_rename,
    instance_file_write,
    instance_files_list,
    router,
)
from core import instance_files as F  # noqa: E402


# ---------------- 纯文件系统逻辑（不依赖网络 / 真实实例 / ctx） ----------------
def _tree():
    d = Path(tempfile.mkdtemp(prefix="dm_ifs_"))
    (d / "a.txt").write_text("hello", encoding="utf-8")
    (d / "sub").mkdir()
    (d / "sub" / "b.txt").write_text("world", encoding="utf-8")
    (d / "bin.bin").write_bytes(b"\x00\x01\x02\xff")
    return d


def test_list_and_read_text():
    d = _tree()
    res = F.list_files(str(d))
    assert res["ok"] and res["path"] == ""
    names = {i["name"] for i in res["items"]}
    assert {"a.txt", "sub", "bin.bin"} <= names
    # 目录排在文件前面、按名称不区分大小写
    r = F.read_file(str(d), "a.txt")
    assert r["is_text"] and r["content"] == "hello"


def test_read_binary_returns_base64():
    d = _tree()
    r = F.read_file(str(d), "bin.bin")
    assert not r["is_text"]
    assert base64.b64decode(r["content"]) == b"\x00\x01\x02\xff"


def test_write_text_and_base64_and_auto_mkdir():
    d = Path(tempfile.mkdtemp(prefix="dm_ifw_"))
    F.write_file(str(d), "x.txt", "data", "text")
    assert (d / "x.txt").read_text(encoding="utf-8") == "data"
    payload = base64.b64encode(b"\xDE\xAD\xBE\xEF").decode()
    F.write_file(str(d), "deep/nested/y.bin", payload, "base64")
    assert (d / "deep" / "nested" / "y.bin").read_bytes() == b"\xDE\xAD\xBE\xEF"


def test_write_atomic_no_temp_leftover():
    d = Path(tempfile.mkdtemp(prefix="dm_ifa_"))
    F.write_file(str(d), "z.txt", "v1", "text")
    assert not list(d.glob(".dm_write_tmp_*"))


def test_mkdir_rename_delete():
    d = Path(tempfile.mkdtemp(prefix="dm_ifm_"))
    F.mkdir(str(d), "folder")
    assert (d / "folder").is_dir()
    (d / "old.txt").write_text("x")
    F.rename_file(str(d), "old.txt", "new.txt")
    assert (d / "new.txt").is_file() and not (d / "old.txt").exists()
    F.delete_file(str(d), "new.txt")
    assert not (d / "new.txt").exists()
    F.delete_file(str(d), "folder")
    assert not (d / "folder").exists()


def test_containment_rejects_traversal():
    d = _tree()
    outside = d.parent / "secret.txt"
    outside.write_text("top", encoding="utf-8")
    with pytest.raises(PermissionError):
        F.read_file(str(d), "../secret.txt")
    with pytest.raises(PermissionError):
        F.read_file(str(d), "/etc/passwd")


@pytest.mark.skipif(sys.platform == "win32", reason="创建软链需特权，Linux 上已覆盖")
def test_containment_rejects_symlink_escape():
    d = _tree()
    outside = Path(tempfile.mkdtemp(prefix="dm_esc_"))
    (outside / "secret.txt").write_text("leak", encoding="utf-8")
    link = d / "escape"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PermissionError):
        F.read_file(str(d), "escape/secret.txt")


def test_delete_root_forbidden():
    d = _tree()
    with pytest.raises(PermissionError):
        F.delete_file(str(d), "")


def test_read_size_limit():
    d = Path(tempfile.mkdtemp(prefix="dm_ifl_"))
    (d / "big.txt").write_text("x" * (F.MAX_READ + 1), encoding="utf-8")
    with pytest.raises(ValueError):
        F.read_file(str(d), "big.txt")


def test_write_size_limit():
    d = Path(tempfile.mkdtemp(prefix="dm_ifw2_"))
    huge = base64.b64encode(b"x" * (F.MAX_WRITE + 1)).decode()
    with pytest.raises(ValueError):
        F.write_file(str(d), "h.bin", huge, "base64")


# ---------------- REST 层（直接调端点函数；归属校验走 _own） ----------------
def _mk(iid):
    real = Path(tempfile.mkdtemp(prefix=f"dm_inst_{iid}_"))
    ctx.registry.create(iid, dice="nonebot2", arch="standalone", dir_=str(real),
                        port=3000, allocated_ports={"ob11": 3001})
    return real


def test_rest_list_read_write_delete_loop():
    real = _mk("ift1")
    res = instance_files_list("ift1", "", user=None)
    assert "entries" in res
    instance_file_write("ift1", FileWriteReq(path="c.txt", content="hi", encoding="text"), user=None)
    r = instance_file_read("ift1", "c.txt", user=None)
    assert r["text"] == "hi"
    dl = instance_file_download("ift1", "c.txt", user=None)
    assert Path(dl.path).name == "c.txt"
    instance_file_delete("ift1", "c.txt", user=None)
    assert not (real / "c.txt").exists()


def test_rest_containment_rejects_traversal():
    _mk("ift2")
    with pytest.raises(HTTPException) as e:
        instance_file_read("ift2", "../etc/passwd", user=None)
    assert e.value.status_code == 403


def test_rest_unknown_instance_404():
    with pytest.raises(HTTPException) as e:
        instance_files_list("does-not-exist", "", user=None)
    assert e.value.status_code == 404


def test_rest_mkdir_and_rename():
    real = _mk("ift3")
    instance_file_mkdir("ift3", FileMkdirReq(path="fd"), user=None)
    assert (real / "fd").is_dir()
    instance_file_write("ift3", FileWriteReq(path="fd/o.txt", content="o"), user=None)
    instance_file_rename("ift3", FileRenameReq(path="fd/o.txt", new_name="n.txt"), user=None)
    assert (real / "fd" / "n.txt").is_file()


def test_file_routes_registered_before_op_wildcard():
    paths = [getattr(r, "path", None) for r in router.routes]
    assert "/api/instances/{inst_id}/files" in paths
    assert "/api/instances/{inst_id}/{op}" in paths
    assert paths.index("/api/instances/{inst_id}/files") < paths.index("/api/instances/{inst_id}/{op}")
