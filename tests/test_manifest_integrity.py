"""manifest 完整性断言：把「新增程序漏字段」这类静默退化钉死在测试里。

背景（2026-09 线上故障）：napcat / dicenext 的 required_files 长期是空数组 →
本地缓存包优先于在线下载，误传的 GitHub 源码包也被判「部署成功」，直到启动才炸在
「找不到可执行文件」上。空值等于放弃部署后的完整性校验，这里统一兜住。

另：compatible_login 是向导配对候选的唯一来源（Wizard.vue 纯清单驱动），
引用了不存在的程序名在前端表现为「候选列表少一项」，很难排查。
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(p: Path) -> dict:
    """清单带 // 注释（JSON5 风格）：去掉注释行再交给 json 解析。"""
    raw = "\n".join(l for l in p.read_text(encoding="utf-8").splitlines()
                    if not l.strip().startswith("//"))
    return json.loads(raw)


ALL = {p.stem: _load(p) for p in sorted((ROOT / "manifests").glob("*.json"))}


def test_manifests_load_with_core_fields():
    assert ALL, "manifests 目录为空"
    for name, m in ALL.items():
        assert m["name"] == name, f"{name}.json 的 name 与文件名不一致"
        assert m.get("exe"), f"{name}: 缺 exe（启动命令靠它构建）"
        assert m.get("arch") in ("standalone", "allinone"), f"{name}: arch 非法"


def test_every_manifest_can_reject_wrong_package():
    """required_files 必须非空：空值 = 部署后不做完整性校验。"""
    for name, m in ALL.items():
        assert m.get("required_files"), (
            f"{name}: required_files 为空，误传的源码包会被判部署成功")


def test_compatible_login_targets_exist():
    """骰子端声明的兼容登录端必须真实存在（builtin = 自带登录，跳过）。"""
    for name, m in ALL.items():
        for t in m.get("compatible_login") or []:
            if t == "builtin":
                continue
            assert t in ALL, f"{name}: compatible_login 引用了不存在的程序 {t}"


def test_role_split_is_consistent():
    """口径与 Wizard.vue 一致：登录端 = 在任意骰子端 compatible_login 里出现过的程序。"""
    login_ends = {t for m in ALL.values() for t in (m.get("compatible_login") or [])
                  if t != "builtin"}
    dice_ends = set(ALL) - login_ends
    assert dice_ends and login_ends, "骰子端与登录端都必须非空"
    for d in dice_ends:
        if ALL[d]["arch"] == "standalone":
            assert ALL[d].get("compatible_login"), f"{d}: 骰子端未声明 compatible_login"


def test_napcat_and_dicenext_guard_their_executable():
    """本次修复的定点回归：这两个包的必备文件就是各自的启动体。

    NapCat.sh 只存在于 CI 构建产物里（仓库源码没有），DiceNext 是发行包里的二进制，
    都能把误传的源码包挡在 deploy 阶段。
    """
    assert "NapCat.sh" in ALL["napcat"]["required_files"]
    assert "DiceNext" in ALL["dicenext"]["required_files"]
