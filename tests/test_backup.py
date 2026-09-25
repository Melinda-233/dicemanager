"""备份导入核心逻辑：顶层目录剥离 / zip-slip / 覆盖语义 / 格式校验"""
import tarfile
import zipfile
from pathlib import Path

import pytest

from core.backup import restore_into


def _mkzip(path: Path, mapping: dict) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in mapping.items():
            zf.writestr(name, data)


def test_zip_top_prefix_stripped(tmp_path):
    """压缩整个实例目录的打包方式：顶层目录剥离，文件落到实例根。"""
    a = tmp_path / "b.zip"
    _mkzip(a, {"sealdice/data/default/data.db": b"DB1",
               "sealdice/config/a.json": b"{}", "sealdice/": b""})
    out = tmp_path / "inst"
    r = restore_into(a, out)
    assert (out / "data/default/data.db").read_bytes() == b"DB1"
    assert (out / "config/a.json").read_bytes() == b"{}"
    assert not (out / "sealdice").exists()
    assert r == {"format": ".zip", "files": 2}


def test_zip_no_prefix_kept(tmp_path):
    """压缩目录内容的打包方式：按包内结构原样落位。"""
    a = tmp_path / "b.zip"
    _mkzip(a, {"data/default/data.db": b"DB", "README.md": b"hi"})
    out = tmp_path / "inst"
    restore_into(a, out)
    assert (out / "data/default/data.db").read_bytes() == b"DB"
    assert (out / "README.md").read_bytes() == b"hi"


def test_single_file_not_stripped(tmp_path):
    """单文件包：backup.db 不应被误判为顶层目录而剥掉。"""
    a = tmp_path / "b.zip"
    _mkzip(a, {"backup.db": b"X"})
    out = tmp_path / "inst"
    r = restore_into(a, out)
    assert (out / "backup.db").read_bytes() == b"X"
    assert r["files"] == 1


def test_data_folder_backup_placed_under_existing_dir(tmp_path):
    """顶层是 data 的纯数据包：实例中 data/ 已存在 → 原样落位（不误剥）。"""
    out = tmp_path / "inst"
    (out / "data").mkdir(parents=True)
    (out / "data/x.db").write_bytes(b"OLD")
    a = tmp_path / "b.zip"
    _mkzip(a, {"data/x.db": b"V1"})
    restore_into(a, out)
    assert (out / "data/x.db").read_bytes() == b"V1"
    assert not (out / "x.db").exists()


def test_data_only_zip_into_dir_with_existing_data_dir(tmp_path):
    """data/ 已存在但包内文件名是新的（典型数据备份）→ 靠祖先目录命中原样落位。"""
    out = tmp_path / "inst"
    (out / "data").mkdir(parents=True)
    (out / "data/other.db").write_bytes(b"whatever")
    a = tmp_path / "b.zip"
    _mkzip(a, {"data/newfile.db": b"V"})
    restore_into(a, out)
    assert (out / "data/newfile.db").read_bytes() == b"V"
    assert not (out / "newfile.db").exists()


def test_full_dir_backup_stripped_when_root_files_exist(tmp_path):
    """整目录备份导入已部署实例（根级有程序文件）→ 剥离落位成可用实例。"""
    out = tmp_path / "inst"
    (out / "data").mkdir(parents=True)
    (out / "sealdice-core").write_bytes(b"binary")
    a = tmp_path / "b.zip"
    _mkzip(a, {"olddir/data/x.db": b"V", "olddir/config/c.json": b"{}"})
    restore_into(a, out)
    assert (out / "data/x.db").read_bytes() == b"V"
    assert (out / "config/c.json").read_bytes() == b"{}"
    assert not (out / "olddir").exists()


def test_overwrite_existing_keep_others(tmp_path):
    """覆盖语义：包内文件替换实例同名文件，包外文件不动。"""
    out = tmp_path / "inst"
    (out / "data").mkdir(parents=True)
    (out / "data/old.bin").write_bytes(b"OLD")
    (out / "keep.txt").write_bytes(b"keep me")
    a = tmp_path / "b.zip"
    _mkzip(a, {"data/old.bin": b"NEW", "data/new.txt": b"n"})
    restore_into(a, out)
    assert (out / "data/old.bin").read_bytes() == b"NEW"
    assert (out / "data/new.txt").exists()
    assert (out / "keep.txt").read_bytes() == b"keep me"


def test_zip_slip_rejected(tmp_path):
    """穿越条目（.. / 绝对路径）整体拒绝，且不落任何文件到实例外。"""
    a = tmp_path / "b.zip"
    _mkzip(a, {"data/ok.txt": b"1", "../evil.txt": b"evil"})
    out = tmp_path / "inst"
    with pytest.raises(ValueError, match="非法路径"):
        restore_into(a, out)
    assert not (tmp_path / "evil.txt").exists()
    assert not (out / "data").exists()          # 校验在解压前，一个都没落


def test_bad_format_rejected(tmp_path):
    a = tmp_path / "b.bin"
    a.write_bytes(b"this is definitely not an archive payload....")
    with pytest.raises(ValueError, match="不支持"):
        restore_into(a, tmp_path / "inst")


def test_tar_gz_prefix_stripped_and_overwrite(tmp_path):
    src = tmp_path / "seal"
    (src / "data").mkdir(parents=True)
    (src / "data/x.db").write_bytes(b"V1")
    a = tmp_path / "b.tar.gz"
    with tarfile.open(a, "w:gz") as tf:
        tf.add(src, arcname="seal")
    out = tmp_path / "inst"
    (out / "data").mkdir(parents=True)
    (out / "data/x.db").write_bytes(b"OLD")
    r = restore_into(a, out)
    assert (out / "data/x.db").read_bytes() == b"V1"
    assert r["format"] == ".tar.gz" and r["files"] == 1
