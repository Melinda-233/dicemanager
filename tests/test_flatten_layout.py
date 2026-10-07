"""压缩包顶层深度归一化：让必备文件永远落在实例根目录（exe 路径 = dir/<exe>）。

## 为什么要有这个

部署完成后一律按 `instance.dir / manifest.exe` 找可执行文件，但上游包的顶层
深度并不统一，三种形态都真实存在（均实测过）：

| 形态 | 包内结构 | 举例 |
|---|---|---|
| ① 根平铺 | `NapCat.sh`、`config/`… | 多数单文件程序 |
| ② 单一顶层目录 | `SnowLuma-linux-x64/launcher.sh` | 带版本号的构建产物 |
| ③ 顶层目录 + 深嵌套 | `Lagrange.OneBot/bin/Release/net9.0/linux-x64/publish/Lagrange.OneBot` | .NET publish 产物 |

旧实现只处理 ②：把「唯一顶层目录」的内容上移一层。遇到 ③ 时它会把 `bin/`
连同 publish 一起上移，`Lagrange.OneBot` 仍落在 `bin/Release/...` 下 →
`verify_required` 报「部署后缺失必备文件」→ **实例永久卡在 DEPLOYING**
（服务器上真实发生过一次，排查耗时远大于写这个函数）。

所以改成以清单 `required_files` 为准绳定位：找到它在哪一层，就把那一层上移。

## 钉住的行为

- 三种形态都要让必备文件落在根目录
- 找不到时不猜、不乱动（交给 verify_required 报明确缺失清单）
- 不覆盖根目录已有的同名文件（用户手工放的东西不能被解压覆盖）
- 搬完不留下空的目录链
- 单层深度限制（防御异常深的包）
- 无 required_files 的程序完全不受影响
"""
import io
import os
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

tmp = tempfile.mkdtemp(prefix="dm_flatten_")
os.environ.setdefault("DM_STATE_DIR", tmp)
os.environ.setdefault("DM_LOG_DIR", os.path.join(tmp, "logs"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.base import BaseAdapter  # noqa: E402


class _Ad(BaseAdapter):
    def __init__(self, required):
        self.m = {"name": "fake", "arch": "standalone", "install_root": "/opt",
                  "required_files": required}
        self._last_tag = None

    def verify_required(self, instance):
        return [r for r in self.m.get("required_files", []) if not (Path(instance.dir) / r).exists()]

    def build_start_cmd(self, instance) -> list[str]:
        return [str(Path(instance.dir) / self.m["exe"])]

    def configure_login(self, instance, credentials) -> dict:
        return {"ok": True}

    def write_conn_config(self, instance, mode, direction, *a, **kw) -> dict:
        return {"ok": True}

    def login_modes(self, instance=None):
        return ["qrcode"]


# ---------- 打包辅助 ----------

def make_tar_gz(path: Path, entries: dict[str, bytes]):
    with tarfile.open(path, "w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(data))


def make_zip(path: Path, entries: dict[str, bytes]):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


class _Inst:
    def __init__(self, d: Path):
        self.id = "i-flat"
        self.dice = "fake"
        self.dir = str(d)
        self.state = "CREATED"
        self.allocated_ports = {}
        self.actual_port = None
        self.webui_token = None
        self.warnings = []


def _deploy(archive: Path, required, dest_name="d"):
    d = Path(tmp) / dest_name
    ad = _Ad(required)
    ad._extract(archive, _Inst(d))
    return d, ad


# ---------- 形态 ①：根平铺 ----------

def test_form1_flat_root_untouched():
    """根目录已有全部必备文件 → 不动任何东西（不能把已就绪的部署搅乱）。"""
    arc = Path(tmp) / "f1.tar.gz"
    make_tar_gz(arc, {"NapCat.sh": b"x", "config/a.json": b"{}"})
    d, ad = _deploy(arc, ["NapCat.sh"], "f1")
    assert (d / "NapCat.sh").exists()
    assert ad.verify_required(_Inst(d)) == []


# ---------- 形态 ②：单一顶层目录 ----------

def test_form2_single_top_dir_flattened():
    """单一顶层目录 → 上移一层（旧的 normalize 行为不能退化）。"""
    arc = Path(tmp) / "f2.tar.gz"
    make_tar_gz(arc, {"SnowLuma-linux-x64/launcher.sh": b"x",
                      "SnowLuma-linux-x64/res/config.json": b"{}"})
    d, ad = _deploy(arc, ["launcher.sh"], "f2")
    assert (d / "launcher.sh").exists(), "应上移到实例根"
    assert (d / "res" / "config.json").exists(), "同级资源也要一起上移"
    assert ad.verify_required(_Inst(d)) == []
    assert not (d / "SnowLuma-linux-x64").exists(), "搬空后应清掉顶层壳"


# ---------- 形态 ③：顶层目录 + 深嵌套（.NET publish，真实的 lagrange 形态） ----------

def test_form3_dotnet_publish_nested():
    """Lagrange 的真实包形态：四层深。exe 必须在根，否则实例卡在 DEPLOYING。"""
    arc = Path(tmp) / "f3.tar.gz"
    make_tar_gz(arc, {
        "Lagrange.OneBot/bin/Release/net9.0/linux-x64/publish/Lagrange.OneBot": b"ELF",
        "Lagrange.OneBot/bin/Release/net9.0/linux-x64/publish/appsettings.json": b"{}",
        "Lagrange.OneBot/bin/Release/net9.0/linux-x64/publish/LICENSE": b"MIT",
    })
    d, ad = _deploy(arc, ["Lagrange.OneBot"], "f3")
    assert (d / "Lagrange.OneBot").exists(), (
        "exe 必须在实例根：适配器按 dir/<exe> 启动，留在深层就等于部署失败")
    assert (d / "appsettings.json").exists(), "exe 同级的配置也要上移（否则读到空配置）"
    assert (d / "LICENSE").exists()
    assert ad.verify_required(_Inst(d)) == []


def test_form3_exe_and_siblings_together():
    """publish 目录里 exe 与依赖同级 → 整体上移后仍能互相找到。"""
    arc = Path(tmp) / "f3b.tar.gz"
    make_tar_gz(arc, {
        "App/bin/Release/net9.0/linux-x64/publish/App": b"ELF",
        "App/bin/Release/net9.0/linux-x64/publish/lib.so": b"SO",
    })
    d, _ = _deploy(arc, ["App"], "f3b")
    assert (d / "App").exists() and (d / "lib.so").exists()


def test_form3_zip_also_handled():
    """zip 走另一条解压分支，归一化必须同样生效。"""
    arc = Path(tmp) / "f3.zip"
    make_zip(arc, {
        "Lagrange.OneBot/bin/Release/net9.0/linux-x64/publish/Lagrange.OneBot": b"ELF",
    })
    d, ad = _deploy(arc, ["Lagrange.OneBot"], "f3z")
    assert (d / "Lagrange.OneBot").exists()
    assert ad.verify_required(_Inst(d)) == []


# ---------- 找不到时的行为：不猜、不乱动 ----------

def test_missing_exe_leaves_tree_and_reports():
    """包里没有 exe → 不做任何搬动，让 verify_required 报明确缺失（别在这里瞎猜）。"""
    arc = Path(tmp) / "nofind.tar.gz"
    make_tar_gz(arc, {"docs/readme.md": b"hi", "src/a.cs": b"//"})
    d, ad = _deploy(arc, ["Lagrange.OneBot"], "nofind")
    assert (d / "docs").exists() and (d / "src").exists(), "不该乱搬"
    assert "Lagrange.OneBot" in ad.verify_required(_Inst(d))


def test_no_required_files_is_noop():
    """清单没声明 required_files → 完全不动（老程序行为不变）。"""
    arc = Path(tmp) / "noreq.tar.gz"
    make_tar_gz(arc, {"Top/launcher": b"x"})
    d, _ = _deploy(arc, [], "noreq")
    assert (d / "Top" / "launcher").exists(), "无 required_files 时不该改结构"
    assert (d / "Top").exists()


# ---------- 安全与边界 ----------

def test_does_not_overwrite_existing_root_file():
    """根目录已有同名文件（用户手工放的）→ 不覆盖。"""
    arc = Path(tmp) / "keep.tar.gz"
    make_tar_gz(arc, {"Top/NapCat.sh": b"FROM_ARCHIVE"})
    d = Path(tmp) / "keep"
    d.mkdir(parents=True)
    (d / "NapCat.sh").write_bytes(b"MINE")
    _Ad(["NapCat.sh"])._extract(arc, _Inst(d))
    assert (d / "NapCat.sh").read_bytes() == b"MINE", "不能覆盖根目录已有文件"


def test_empty_dirs_cleaned_up():
    """搬空后不留空目录链（否则实例目录里一堆空壳看着像坏了）。"""
    arc = Path(tmp) / "emptydirs.tar.gz"
    make_tar_gz(arc, {"Top/deep/deeper/launcher.sh": b"x"})
    d, _ = _deploy(arc, ["launcher.sh"], "emptydirs")
    assert (d / "launcher.sh").exists()
    assert not (d / "Top").exists(), "应清掉搬空后的空壳"
    leftovers = [p for p in d.rglob("*") if p.is_dir()]
    assert not leftovers, f"不该有残留空目录: {leftovers}"


def test_preexisting_empty_dir_kept_not_broken():
    """包里带的空目录（非搬空残留）应保留 —— 删它属于超出职责，可能触发程序异常。"""
    arc = Path(tmp) / "keepempty.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        for name in ("Top/launcher.sh", "Top/data/"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE if name.endswith("/") else tarfile.REGTYPE
            info.size = 0 if info.type == tarfile.REGTYPE else 0
            info.mode = 0o755
            tf.addfile(info, io.BytesIO(b"" if info.type == tarfile.REGTYPE else b""))
    d, _ = _deploy(arc, ["launcher.sh"], "keepempty")
    assert (d / "launcher.sh").exists()
    assert (d / "data").is_dir(), "包自带的空目录（如 data/）应保留"


def test_shallowest_match_wins():
    """多个层级都有同名 exe → 取最浅那层（少移动文件，且最可能是发布根）。"""
    arc = Path(tmp) / "shallow.tar.gz"
    make_tar_gz(arc, {
        "Top/launcher.sh": b"REAL",
        "Top/sub/launcher.sh": b"NESTED",
    })
    d, _ = _deploy(arc, ["launcher.sh"], "shallow")
    assert (d / "launcher.sh").read_bytes() == b"REAL"


def test_multiple_required_all_must_match_same_dir():
    """多个必备文件须落在同一层（不同层就说明包结构不符预期 → 不乱搬）。"""
    arc = Path(tmp) / "multi.tar.gz"
    make_tar_gz(arc, {
        "Top/a/launcher.sh": b"x",
        "Top/b/config.json": b"{}",
    })
    d, ad = _deploy(arc, ["launcher.sh", "config.json"], "multi")
    # 分处两层 → 不搬，交给 verify_required 如实报告缺哪个
    assert "launcher.sh" in ad.verify_required(_Inst(d))


def test_path_traversal_still_blocked():
    """归一化不能削弱原有的 tar 穿越防护。

    不刻意断言「抛异常」——不同 Python 版本的 filter 行为略有差异（拒绝对外路径、
    或退回 _safe_tar_members 过滤），真正要钉住的是**结果**：
    恶意条目一个都没落到 target 之外。
    """
    arc = Path(tmp) / "evil.tar.gz"
    with tarfile.open(arc, "w:gz") as tf:
        for name in ("../escaped.sh", "/abs.sh"):
            info = tarfile.TarInfo(name)
            info.size = 2
            tf.addfile(info, io.BytesIO(b"x" * 2))
    d = Path(tmp) / "evil"
    d.mkdir(parents=True)
    try:
        _Ad(["escaped.sh"])._extract(arc, _Inst(d))
    except Exception:                 # noqa: BLE001 — 抛异常是合法结果之一，不作断言
        pass
    assert not (Path(tmp) / "escaped.sh").exists(), "相对路径穿越必须被挡住"
    assert not Path("/abs.sh").exists(), "绝对路径必须被挡住"
