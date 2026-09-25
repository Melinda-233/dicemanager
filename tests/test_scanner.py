"""扫描器回归：目录分类口径（owned / orphan / external）与 skip。

背景（2026-09-25）：/opt/sealdice 残留目录无任何实例/墓碑记录——删除链路旧实现
rmtree(ignore_errors=True) 静默吞错后记录已墓碑，失败即成无主孤儿。扫描器是
这类残留的发现与清理入口，分类口径必须锁死：external（无关软件）绝不能被判成
orphan 而拿到删除按钮。

进程侧同理（2026-09-25 二次收紧）：/opt/alist/alist曾被标成「游离进程」并拿到
结束按钮——external 软件的进程必须 killable=False，只展示不可操作。"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.scanner import (  # noqa: E402
    check_deletable,
    classify_dir,
    match_program_dir,
    scan_dirs,
    scan_procs,
)


def test_classify_dir_kinds(tmp_path):
    owned = {str(tmp_path / "llbot"): {"id": "llbot-x", "dice": "llbot"}}
    assert classify_dir(tmp_path / "llbot", owned, ["llbot", "sealdice"]) == "owned"
    assert classify_dir(tmp_path / "sealdice", owned, ["llbot", "sealdice"]) == "orphan"
    assert classify_dir(tmp_path / "sealdice-3", owned, ["llbot", "sealdice"]) == "orphan"
    assert classify_dir(tmp_path / "sealdicefoo", owned, ["llbot", "sealdice"]) == "external"
    assert classify_dir(tmp_path / "alist", owned, ["llbot", "sealdice"]) == "external"
    assert classify_dir(tmp_path / "llbot-1-backup", owned, ["llbot"]) == "external"


def test_scan_dirs_annotates_and_skips(tmp_path):
    (tmp_path / "llbot").mkdir()
    (tmp_path / "sealdice").mkdir()
    (tmp_path / "alist").mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    owned = {str(tmp_path / "llbot"): {"id": "i1", "dice": "llbot", "state": "RUNNING"}}
    rows = scan_dirs([str(tmp_path)], owned, ["llbot", "sealdice"], skip=[str(other)])
    by_path = {r["path"]: r for r in rows}
    assert "elsewhere" not in by_path                    # skip 生效（管理器自身目录）
    assert by_path[str(tmp_path / "llbot")]["kind"] == "owned"
    assert by_path[str(tmp_path / "llbot")]["id"] == "i1"
    assert by_path[str(tmp_path / "sealdice")]["kind"] == "orphan"
    assert by_path[str(tmp_path / "alist")]["kind"] == "external"
    assert all("mtime" in r for r in rows)


def test_check_deletable_guards(tmp_path):
    """str/Path 混比曾把受管实例目录放行删除（2026-09-25 /opt/llbot 事故），口径锁死。"""
    names = ["llbot", "sealdice"]
    roots = [str(tmp_path)]
    inst_dir = tmp_path / "llbot"
    inst_dir.mkdir()
    protected = [str(inst_dir)]

    # 受管目录及其子目录必须拒绝
    try:
        check_deletable(str(inst_dir), roots, protected, names)
        raise AssertionError("受管目录被放行删除")
    except ValueError:
        pass
    (inst_dir / "bin").mkdir()
    try:
        check_deletable(str(inst_dir / "bin"), roots, protected, names)
        raise AssertionError("受管目录的子目录被放行删除")
    except ValueError:
        pass

    # 真正的 orphan 放行
    orphan = tmp_path / "sealdice"
    orphan.mkdir()
    check_deletable(str(orphan), roots, protected, names)   # 不抛即通过

    # 无关目录拒绝
    other = tmp_path / "alist"
    other.mkdir()
    try:
        check_deletable(str(other), roots, protected, names)
        raise AssertionError("无关目录被放行删除")
    except ValueError:
        pass

    # 安装根自身 / 根外路径拒绝
    try:
        check_deletable(str(tmp_path), roots, protected, names)
        raise AssertionError("安装根本身被放行删除")
    except ValueError:
        pass


def test_match_program_dir(tmp_path):
    """进程可结束口径：exe/cwd 须位于安装根内、名字匹配已知程序的目录下。"""
    root = tmp_path.as_posix()            # 纯字符串判定，Windows 下也用 / 拼接
    names = ["llbot", "sealdice"]
    roots = [root]
    assert match_program_dir(f"{root}/llbot/bin/llbot", roots, names)       # 子目录层级也命中
    assert match_program_dir(f"{root}/llbot", roots, names)                 # 恰好等于目录
    assert match_program_dir(f"{root}/sealdice-2/Dice", roots, names)       # -N 序号后缀
    assert not match_program_dir(f"{root}/alist/alist", roots, names)       # external
    assert not match_program_dir(f"{root}/llbot-1-backup/x", roots, names)  # 非法后缀不命中
    assert not match_program_dir("/home/llbot/llbot", roots, names)         # 根外一律 False
    assert not match_program_dir("", roots, names)


class _FakeProc:
    """scan_procs 同时读 p.pid 与 p.info，两者都要有（真 psutil.Process 如此）。"""

    def __init__(self, info):
        self.pid = info["pid"]
        self.info = info


def test_scan_procs_killable(tmp_path, monkeypatch):
    """owned → killable=False；orphan 目录下的进程 → killable=True；
    external 目录（alist）下的进程 → 只展示、killable=False。"""
    root = tmp_path.as_posix()
    names = ["llbot", "sealdice"]
    owned = {f"{root}/llbot": {"id": "i1", "dice": "llbot", "state": "RUNNING"}}
    procs = [
        _FakeProc({"pid": 1, "exe": f"{root}/llbot/dice", "cwd": f"{root}/llbot",
                   "cmdline": ["dice"]}),                                  # owned
        _FakeProc({"pid": 2, "exe": f"{root}/sealdice-2/Dice", "cwd": f"{root}/sealdice-2",
                   "cmdline": ["Dice"]}),                                  # orphan → 可结束
        _FakeProc({"pid": 3, "exe": f"{root}/alist/alist", "cwd": f"{root}/alist",
                   "cmdline": ["alist", "server"]}),                       # external → 不可结束
    ]
    try:
        import psutil
        monkeypatch.setattr(psutil, "process_iter", lambda attrs: procs)
    except ImportError:                     # 本机无 psutil 时注入假模块
        fake = types.ModuleType("psutil")
        fake.process_iter = lambda attrs: procs
        fake.Error = Exception
        monkeypatch.setitem(sys.modules, "psutil", fake)
    rows = {r["pid"]: r for r in scan_procs([root], owned, names)}
    assert rows[1]["owner"] == "i1" and rows[1]["killable"] is False
    assert rows[2]["owner"] is None and rows[2]["killable"] is True
    assert rows[3]["owner"] is None and rows[3]["killable"] is False
