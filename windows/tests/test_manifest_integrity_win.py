"""manifest 完整性断言（Windows 版）：把「新增程序漏字段」这类静默退化钉死在测试里。

与 Linux 版 test_manifest_integrity.py 的差异：
- 加载 manifests/*_win.json（而非 *.json，避免重复加载 sealdice_win.json 示例）
- napcat 必备文件期望 NapCat.bat（Linux 版是 NapCat.sh）
- dicenext 必备文件期望 DiceNext.exe（Linux 版是 DiceNext）
- platform 字段必须含 win32
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(p: Path) -> dict:
    """清单带 // 注释（JSON5 风格）：去掉注释行再交给 json 解析。"""
    raw = "\n".join(l for l in p.read_text(encoding="utf-8").splitlines()
                    if not l.strip().startswith("//"))
    return json.loads(raw)


# 只加载 *_win.json：避免与 Linux 版 *.json 混淆（如果同目录有两份 sealdice.json
# 与 sealdice_win.json，p.stem 会都叫 sealdice，后者覆盖前者）
ALL = {p.stem.removesuffix("_win"): _load(p)
       for p in sorted((ROOT / "manifests").glob("*_win.json"))}


def test_manifests_load_with_core_fields():
    assert ALL, "manifests/*_win.json 目录为空"
    for name, m in ALL.items():
        assert m["name"] == name, f"{name}_win.json 的 name 与文件名不一致"
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


def test_platform_field_includes_win32():
    """Windows 版 manifest 必须显式声明 platform: ['win32']。"""
    for name, m in ALL.items():
        plat = m.get("platform")
        assert plat and "win32" in plat, f"{name}: platform 字段缺 win32"


def test_exe_has_windows_extension():
    """Windows 版 exe 必须带 .exe / .bat / .cmd 后缀（防止从 Linux 版直接拷贝漏改）。"""
    import re
    ext_re = re.compile(r"\.(exe|bat|cmd)$", re.I)
    for name, m in ALL.items():
        exe = m.get("exe", "")
        assert ext_re.search(exe), \
            f"{name}: exe '{exe}' 缺 Windows 可执行后缀 (.exe/.bat/.cmd)"


def test_install_root_not_linux_path():
    """install_root 字段不应是 Linux 的 /opt（运行时由 adapters 加载清单覆盖为项目根 package/）。

    历史背景：旧版 manifest 写 %LOCALAPPDATA%\\dicemanager\\programs 占位符，但
    services/wizard.py 直接读 manifest["install_root"] 时并未展开占位符——字面路径会变成
    cwd 下带 % 的怪目录（latent bug，已由 load_registry 修复）。当前 install_root 字段
    仍写占位符保留向后兼容，运行时被 default_install_root() 覆盖。
    """
    for name, m in ALL.items():
        root = m.get("install_root", "")
        assert "/opt" not in root, f"{name}: install_root 仍是 Linux 路径 {root}"


def test_napcat_and_dicenext_guard_their_executable():
    """定点回归：NapCat.bat 是 Windows 启动体（误传源码包能挡住）；
    DiceNext.exe 是发行包里的二进制。"""
    assert "NapCat.bat" in ALL["napcat"]["required_files"]
    assert "DiceNext.exe" in ALL["dicenext"]["required_files"]
