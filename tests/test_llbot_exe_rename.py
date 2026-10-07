"""llbot 可执行文件改名：清单名优先 + 候选兜底，两代包都能跑。

## 背景（2026-10-08 真机验证发现）

llbot v8.3.0 起上游把二进制从 `llbot` 改名为 `LuckyLillia`
（与资产名 `LLBot-CLI-*` → `LuckyLillia-CLI-*` 同一波改名），
但 `bin/llbot/` 下的配置与 node 运行时目录名**没变**。

实测两代包（均为 ~2.0MB 的 ELF，是同一个东西）：

|  | 顶层可执行文件 | bin/llbot/ |
|---|---|---|
| 旧包（≤v8.2） | `llbot` | default_config.json / llbot.js / node … |
| v8.3.0 | `LuckyLillia` | 同上（路径未变） |

## 为什么不能直接把清单 exe 改成 LuckyLillia
服务器上那个已跑多天的实例 `/opt/llbot/llbot` 是旧包部署的，改了清单名
就**启动不了**。所以必须「清单登记名优先 + 候选兜底」。

## 钉住的行为
- 旧实例（有 llbot）→ 仍用 llbot 启动
- 新包（只有 LuckyLillia）→ 用 LuckyLillia 启动，且校验通过
- 两个都在 → 取优先级更高的（清单名）
- 都没有 → 报**清单登记名**（llbot），不是二选一
- exe 改名不该让部署失败；配置/运行时路径缺失仍要报
- prepare_start 要给两个候选都补执行位（只补一个会 Permission denied）
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

tmp = tempfile.mkdtemp(prefix="dm_llbot_exe_")
os.environ.setdefault("DM_STATE_DIR", tmp)
os.environ.setdefault("DM_LOG_DIR", os.path.join(tmp, "logs"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.llbot import LLBotAdapter  # noqa: E402

MANIFEST = {
    "name": "llbot", "exe": "llbot",
    "config_path": "bin/llbot/default_config.json",
    "required_files": ["llbot", "bin/llbot/default_config.json"],
}


def _ad():
    return LLBotAdapter(MANIFEST)


def _inst(d: Path, qq=None):
    return SimpleNamespace(dir=str(d), qq=qq, allocated_ports={},
                           actual_port=None, first_run_done=True)


def _make_old_pkg(d: Path):
    """旧包形态：exe 叫 llbot。（可重复调用：新旧包共存的用例要调两次）"""
    (d / "bin" / "llbot").mkdir(parents=True, exist_ok=True)
    (d / "bin" / "llbot" / "default_config.json").write_text("{}")
    (d / "llbot").write_text("ELF-old")


def _make_new_pkg(d: Path):
    """v8.3.0 形态：exe 改名 LuckyLillia，bin/llbot/ 路径不变。"""
    (d / "bin" / "llbot").mkdir(parents=True, exist_ok=True)
    (d / "bin" / "llbot" / "default_config.json").write_text("{}")
    (d / "LuckyLillia").write_text("ELF-new")


# ---------- 旧包（已部署实例）不受影响 ----------

def test_old_package_still_starts_with_llbot(tmp_path):
    _make_old_pkg(tmp_path)
    ad = _ad()
    inst = _inst(tmp_path)
    assert ad.verify_required(inst) == [], "旧包不该报缺件"
    # 用 Path 比较而非 endswith：Windows 上分隔符是 "\"，字符串断言会永远为 False
    assert Path(ad.build_start_cmd(inst)[0]).name == "llbot"
    assert ad._resolve_exe(inst) == "llbot"


# ---------- 新包（v8.3.0）----------

def test_new_package_passes_verify(tmp_path):
    """回归：v8.3.0 包下载解压后校验必须通过（此前报「缺 llbot」卡住部署）。"""
    _make_new_pkg(tmp_path)
    ad = _ad()
    inst = _inst(tmp_path)
    assert ad.verify_required(inst) == [], "exe 改名不该被判缺件"


def test_new_package_starts_with_lucky_lillia(tmp_path):
    _make_new_pkg(tmp_path)
    ad = _ad()
    inst = _inst(tmp_path, qq="12345")
    cmd = ad.build_start_cmd(inst)
    assert Path(cmd[0]).name == "LuckyLillia", f"应启动改名后的二进制，实际 {cmd[0]}"
    assert cmd[1] == "--qq=12345", "QQ 参数不能丢"


def test_prepare_start_chmods_both_candidates(tmp_path):
    """两个候选都要补执行位：只补一个 → 启动 Permission denied。

    跳过 Windows：那里 chmod 没有 +x 语义（st_mode 恒为 0o100666），
    该断言在 Windows 上不成立。真跑在 Linux 服务器上。
    """
    if os.name == "nt":
        pytest.skip("执行位是 POSIX 语义，Windows 上无法验证")
    _make_new_pkg(tmp_path)
    for f in ("LuckyLillia", "bin/llbot/node"):
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        p.chmod(0o644)                       # 模拟 zip 解压丢执行位
    (tmp_path / "bin" / "llbot" / "default_config.json").write_text("{}")
    ad = _ad()
    inst = _inst(tmp_path)
    ad.prepare_start(inst, runner=None)     # 端口写回失败不影响本用例
    for f in ("LuckyLillia", "bin/llbot/node"):
        assert os.stat(tmp_path / f).st_mode & 0o111, f"{f} 缺执行位"


# ---------- 优先级与报错口径 ----------

def test_manifest_name_wins_when_both_present(tmp_path):
    """两个都在时取清单登记名（保证升级路径确定、可预测）。"""
    _make_old_pkg(tmp_path)
    _make_new_pkg(tmp_path)
    ad = _ad()
    assert ad._resolve_exe(_inst(tmp_path)) == "llbot"


def test_missing_exe_reports_manifest_name_not_choices(tmp_path):
    """都没有时，报清单登记名（用户预期的那个），不是「llbot 或 LuckyLillia」。"""
    (tmp_path / "bin" / "llbot").mkdir(parents=True)
    (tmp_path / "bin" / "llbot" / "default_config.json").write_text("{}")
    ad = _ad()
    missing = ad.verify_required(_inst(tmp_path))
    assert missing == ["llbot"], f"应报清单名，实际 {missing}"


def test_missing_runtime_still_reported_even_if_exe_renamed(tmp_path):
    """exe 改名可容忍，但 bin/llbot/default_config.json 缺失仍要报
    （否则「部署成功但启动时找不到配置」，比缺 exe 更难查）。"""
    (tmp_path / "LuckyLillia").write_text("ELF")
    ad = _ad()
    missing = ad.verify_required(_inst(tmp_path))
    assert "bin/llbot/default_config.json" in missing


def test_source_tarball_rejected(tmp_path):
    """误传源码包（既无 exe 也无配置）必须判缺件。"""
    (tmp_path / "README.md").write_text("source")
    (tmp_path / "src").mkdir()
    ad = _ad()
    missing = ad.verify_required(_inst(tmp_path))
    assert "llbot" in missing and "bin/llbot/default_config.json" in missing
