"""NoneBot2（骰子端，pip_project 策略）测试。

四条主线，各自对应一类容易悄悄坏掉的行为：

1. **清单契约**——策略/依赖/必备文件钉死。`dependencies` 少一项不会在部署时报错，
   而是在实例第一次启动时才炸（2026-10-05 实测：DRIVER=~fastapi 缺 fastapi →
   `ImportError: Please install FastAPI first`），部署阶段完全无感，故必须测。
2. **deploy 幂等判据**——判据是"依赖装上没有"（扫 `libs/*.dist-info`），不是
   "目录在不在"。用目录存在性当判据会让断点续跑的用户看到"目录冲突"弹窗，
   而真实原因（依赖没装完）无处可查；反过来把空 `libs/` 判成已装好，则实例
   一起就ModuleNotFoundError。
3. **sys.path 隔离**——依赖装在 `<实例>/libs`（`pip install --target`），不在解释器
   的 site-packages，`bot.py` 必须 `sys.path.insert` 才引得到。这条断掉了，
   部署全绿但实例一启动就死。
4. **文本级配置编辑**——.env 与 pyproject.toml 都靠文本定位改，因为项目没有 TOML 写库
   （tomllib 只读），整文件重写会清掉用户的 [tool.nonebot] 配置与手写注释。
   这类代码的 bug 不在语法而在"改完之后语义对不对"，故每条路径都用 tomllib /
   read_dotenv 回读校验。

不测真跑 pip：装全套依赖要 60s+，且依赖网络。相关步骤一律 monkeypatch，
只验调用顺序与参数拼装（`_pip_install` 的命令行里的 `--target`）。真跑一遍的
端到端验证在 .workbuddy/e2e_libs_check.py（手动执行，不进CI）。
"""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomllib

from adapters.nonebot2 import (
    LIBS_DIR,
    PIP_TIMEOUT,
    NoneBot2Adapter,
    _dep_name,
    bump_pyproject_versions,
    merge_pyproject_deps,
)
from conftest import exe_name
from core.atomicio import atomic_write_dotenv, read_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _manifest() -> dict:
    """读当前 edition 对应的清单（server / _win 两侧同构，差异只在路径与解释器候选）。"""
    from core.edition import is_desktop
    name = "nonebot2_win.json" if is_desktop() else "nonebot2.json"
    text = "\n".join(x for x in
                     (ROOT / "manifests" / name).read_text("utf-8").splitlines()
                     if not x.strip().startswith("//"))
    return json.loads(text)


MANIFEST = _manifest()


def _inst(d, **kw) -> SimpleNamespace:
    base = dict(dir=str(d), allocated_ports={}, actual_port=None, conn_token=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _ad() -> NoneBot2Adapter:
    return NoneBot2Adapter(MANIFEST)


# ---------- 清单契约 ----------

def test_manifest_declares_pip_project():
    """pip_project 必须同时具备骨架来源与解释器候选，缺任一项都无法自动部署。"""
    assert MANIFEST["name"] == "nonebot2"
    assert MANIFEST["arch"] == "standalone"
    assert MANIFEST["login_type"] == "external"
    assert MANIFEST["download_strategy"] == "pip_project"
    assert MANIFEST["config_path"] == ".env"
    assert "pyproject.toml" in MANIFEST["skeleton"]["files"]
    assert MANIFEST["python_candidates"]
    # ⚠️ 清单**不该**再填 repo/ref（回归，2026-10-06 服务器部署卡住）：
    # `nonebot/nonebot2-template` 这个仓库根本不存在（GitHub 返404），
    # 而 NoneBot2 官方没有独立的模板仓库（模板在 nb-cli 内部且是 Jinja2）。
    # 适配器改为自生成骨架，故不需要 repo —— 留着会让人误以为它有效。
    assert "repo" not in MANIFEST["skeleton"], \
        "别再填 skeleton.repo：那个仓库不存在，会让部署必然失败"
    assert "ref" not in MANIFEST["skeleton"]


def test_manifest_lists_fastapi_stack_explicitly():
    """回归：DRIVER=~fastapi+~websockets 的必需项必须显式声明。

    nonebot2 本体不含 fastapi/uvicorn/websockets，少装任一项的报错发生在**实例首次
    启动**（ImportError: Please install FastAPI first），而 deploy 阶段一路绿灯——
    用户看到的是「部署成功，一启动就死」。这类 bug 只有清单测试拦得住。
    """
    deps = {k.lower(): v for k, v in MANIFEST["dependencies"].items()}
    for pkg in ("nonebot2", "fastapi", "uvicorn", "websockets",
                "nonebot-adapter-onebot", "onedice"):
        assert pkg in deps, f"清单缺依赖 {pkg}（DRIVER=~fastapi+~websockets 必需）"
    # 刻意不用 nonebot2[fastapi]：extras 键进 pyproject.toml 会被 nb run 二次解析
    assert "[" not in "".join(MANIFEST["dependencies"])


def test_manifest_required_files_cover_libs():
    """libs 必须在 required_files 里：它是"依赖真的装上了"的标志（deploy 幂等判据）。"""
    assert MANIFEST["required_files"] == ["bot.py", "pyproject.toml", "libs"]
    assert LIBS_DIR == "libs", "依赖目录名与清单不一致 → verify_required 永远判缺失"


def test_manifest_declares_python_requires():
    """版本下限必须钉死：探测通过不等于版本够，不够时要给可操作报错。"""
    assert tuple(MANIFEST["python_requires"]) == (3, 10)
    # embeddable 里没有 python3.exe 这个别名，列进去只是白费一次探测；
    # Linux 版则相反（系统一定有 python3），故分edition 断言
    from core.edition import is_desktop
    if is_desktop():
        assert "python3" not in MANIFEST["python_candidates"]


def test_manifest_exe_is_entry_script():
    assert MANIFEST["exe"] == "bot.py"
    assert exe_name("bot") not in MANIFEST["required_files"], (
        "nonebot2 入口是脚本而非可执行文件，别按平台分化 required_files")


def test_registry_loads_nonebot2():
    from adapters import ALLOWED_STRATEGY, load_registry
    assert "pip_project" in ALLOWED_STRATEGY
    reg = load_registry(ROOT / "manifests")
    manifest, cls = reg["nonebot2"]
    assert cls is NoneBot2Adapter
    assert manifest["download_strategy"] == "pip_project"


def test_dice_ends_list_nonebot2_as_compatible_login():
    """nonebot2 是登录端候选：向导配对靠清单里的 compatible_login 反查。"""
    from adapters import load_registry
    reg = load_registry(ROOT / "manifests")
    assert "nonebot2" not in {t for m, _ in reg.values()
                              for t in (m.get("compatible_login") or [])
                              if t != "builtin"}, (
        "若 nonebot2 被列入他人 compatible_login，它就成了登录端而非骰子端")


# ---------- 升级通道：pip_project 不得掉进「未知下载策略" ----------

def test_latest_tag_returns_none_for_pip_project():
    """pip_project 无上游 release tag；走 _resolve_release 会抛「未知下载策略」。

    该异常在 upgrade-check 端点里会变成 502，在 upgrade 端点里会变成
    「已停机 + 已备份之后才 500」——所以必须在 latest_tag 这一层就拦住。
    """
    assert _ad().latest_tag() is None


def test_resolve_release_rejects_pip_project_with_readable_error():
    """误用 _resolve_release 时报人话，而不是掉到末尾的「未知下载策略」。"""
    with pytest.raises(RuntimeError) as e:
        _ad()._resolve_release()
    assert "pip install -U" in str(e.value)


def test_upgrade_requires_deployed_instance(tmp_path):
    """未部署（libs 为空）就升级 → 明确报错，不去动不存在的环境。"""
    with pytest.raises(RuntimeError) as e:
        _ad().upgrade(_inst(tmp_path / "nb-1"))
    assert "尚未完成部署" in str(e.value)


def test_upgrade_rejects_empty_libs_dir(tmp_path):
    """回归：`libs/` 目录在但里面是空的 → 仍算「未部署」，不许进入升级流程。

    目录存在性当判据是最容易犯的错：断点续跑 / 用户手放骨架 / pip 半途失败都会
    留下一个空的 `libs/`，而面板上实例目录明明是存在的。
    """
    d = tmp_path / "nb-empty-libs"
    (d / LIBS_DIR).mkdir(parents=True)
    with pytest.raises(RuntimeError) as e:
        _ad().upgrade(_inst(d))
    assert "尚未完成部署" in str(e.value)


def _fake_libs(d: Path, versions: dict[str, str]) -> Path:
    """造一个装着 dist-info 的 libs/（替代真跑 pip）。"""
    libs = d / LIBS_DIR
    libs.mkdir(parents=True, exist_ok=True)
    for name, ver in versions.items():
        (libs / f"{name}-{ver}.dist-info").mkdir(exist_ok=True)
    return libs


def _full_libs(d: Path, **over) -> Path:
    """造一份「清单声明的依赖全齐」的 libs/。

    目录名按 pip 实际产出的形态写（连字符归一前的下划线形态），与真实安装一致。
    ⚠️ `over` 里指定的版本会**先清掉同名旧目录**再写——模拟 pip 升级的真实行为
    （pip 升级是替换 dist-info，不是并存）。并存会让扫描结果依赖 glob 顺序，
    在 Linux 与 Windows 上得出相反的版本号（2026-10-05 CI 踩过）。
    """
    for stem in over:
        for old in (d / LIBS_DIR).glob(f"{stem.replace('-', '_')}-*.dist-info"):
            import shutil as _sh
            _sh.rmtree(old, ignore_errors=True)
    vers = {k.lower().replace("-", "_"): "1.0" for k in MANIFEST["dependencies"]}
    vers.update({k.replace("-", "_"): v for k, v in over.items()})
    return _fake_libs(d, vers)


def test_installed_versions_reads_dist_info_not_pip_list(tmp_path):
    """已装版本必须读 libs/*.dist-info，不能用 `pip list`。

    `pip list` 列的是**解释器自己** site-packages 里的包，而依赖在 `libs/`，
    两者毫无关系——用了 pip list 会永远读到空/无关的清单，基线与 bump 全错。
    """
    d = tmp_path / "nb-ver"
    d.mkdir()
    _fake_libs(d, {"nonebot2": "2.7.1", "nonebot_adapter_onebot": "2.6.0",
                    "uvicorn": "0.30.1"})
    got = _ad()._installed_versions(_inst(d))
    assert got["nonebot2"] == "2.7.1"
    # 下划线/连字符归一：pip 装出来的目录名用下划线，清单里写连字符
    assert got["nonebot-adapter-onebot"] == "2.6.0"
    assert got["uvicorn"] == "0.30.1"


def test_installed_versions_pick_highest_on_duplicate_dist_info(tmp_path):
    """回归（2026-10-05 CI 在Linux 上炸的）：同名前缀的 dist-info 并存时取**版本号大**的。

    原本是「后扫的覆盖先扫的」，而 `Path.glob` 的返回顺序在 Linux 与 Windows 上
    不同 —— 同一份目录在 Windows 本地扫到 2.7.1（绿），CI 的Linux 扫到 2.4.3（红）。
    残留重复来自 pip 升级中断或手工拷贝，概率低但会让「基线算错、误判成没升级」，
    所以扫描结果必须是确定的。

    `sorted()` 反向构造即模拟「旧的排后面」——若实现退化成覆盖式，
    这条用例在任何平台都会失败。
    """
    d = tmp_path / "nb-dup"
    d.mkdir()
    for v in ("2.7.1", "2.4.3"):
        (d / LIBS_DIR / f"nonebot2-{v}.dist-info").mkdir(parents=True)
    got = _ad()._installed_versions(_inst(d))
    assert got["nonebot2"] == "2.7.1", "并存时必须取版本号大的那个"


def test_deps_installed_requires_all_declared_packages(tmp_path):
    """判据是「清单声明的**全部**都在」，不是「libs 非空」、也不是「任意一项在」。

    用any 的话，删掉 nonebot2 只剩 fastapi/uvicorn 也会被判装好——
    而 nonebot2 恰恰是本体，缺了它实例一起来就 ModuleNotFoundError
    （2026-10-05 端到端实测：真装 28 个包后删掉 nonebot2 的 dist-info，
     any 判据仍然返回 True）。
    """
    d = tmp_path / "nb-deps"
    d.mkdir()
    inst = _inst(d)
    ad = _ad()

    assert ad._deps_installed(inst) is False, "libs 都不该算装好"

    _fake_libs(d, {"uvicorn": "0.30.1"})                 # 只装了个传递依赖
    assert ad._deps_installed(inst) is False, "只有传递依赖不算装好"

    _fake_libs(d, {"nonebot2": "2.7.1", "fastapi": "0.110.0"})
    assert ad._deps_installed(inst) is False, "只齐两项（缺 nonebot-adapter-onebot）不算"
    assert "nonebot-adapter-onebot" in ad._deps_missing(inst)

    _full_libs(d, nonebot2="2.7.1")
    assert ad._deps_installed(inst) is True
    assert ad._deps_missing(inst) == []


def test_upgrade_publishes_pip_baseline(tmp_path, monkeypatch):
    """升级后基线（依赖快照）要落到 DEPLOY_VERSION，供 wizard 写进 registry.version。

    否则实例 version 永远是 None，升级通道与「已装依赖版本」无从比对——
    base.deploy 里有这一步，覆写了 deploy() 的适配器必须自己做。
    """
    from adapters.base import DEPLOY_VERSION

    d = tmp_path / "nb-up"
    d.mkdir()
    (d / "bot.py").write_text("x", encoding="utf-8")
    (d / "pyproject.toml").write_text(
        '[project]\ndependencies = ["nonebot2>=2.4.0"]\n', encoding="utf-8")
    _full_libs(d, nonebot2="2.4.3")                     # 升级前的旧版本

    calls: list = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        # 升级后把版本换成新的（模拟 pip 真的动了 libs/）
        if "install" in cmd:
            _full_libs(d, nonebot2="2.7.1")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tag = _ad().upgrade(_inst(d, id="up1"))
    DEPLOY_VERSION.pop("up1", None)

    assert tag and tag.startswith("pip:")
    # 基线必须反映**升级后**的版本，而非旧的
    assert _ad()._installed_versions(_inst(d))["nonebot2"] == "2.7.1"

    inst_cmds = [c for c in calls if "install" in c]
    assert inst_cmds, "未发起 pip install"
    cmd = inst_cmds[0]
    assert "--upgrade" in cmd
    # --target 必须指向本实例的 libs/，否则隔离形同虚设
    assert cmd[cmd.index("--target") + 1] == str(d / LIBS_DIR)
    for k in MANIFEST["dependencies"]:
        assert any(_dep_name(t) == k for t in cmd
                   if not t.startswith(("-", "http")) and t != "install"), \
            f"依赖 {k} 未纳入升级"
    # 已装版本抬为新的下限，否则 nb run 会按旧下限把包降回去
    pp = (d / "pyproject.toml").read_text("utf-8")
    assert "nonebot2>=2.7.1" in pp
    assert tomllib.loads(pp)["project"]["dependencies"] == ["nonebot2>=2.7.1"]


# ---------- deploy：幂等判据 ----------

def _ready_dir(d: Path, *, with_deps: bool = True) -> Path:
    """造一个「目录已存在」的实例目录；with_deps 决定依赖是否算装好。"""
    d.mkdir(parents=True, exist_ok=True)
    (d / "bot.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, str(Path(__file__).resolve().parent / {LIBS_DIR!r}))\n"
        "import nonebot\nnonebot.init()\nnonebot.run()\n", encoding="utf-8")
    (d / "pyproject.toml").write_text(
        '[project]\nname = "nb"\ndependencies = ["nonebot2>=2.4.0"]\n\n'
        '[tool.nonebot]\nplugin_dirs = ["plugins"]\n', encoding="utf-8")
    if with_deps:
        _full_libs(d, nonebot2="2.7.1")
    return d


def test_deploy_is_idempotent_when_deps_present(tmp_path, monkeypatch):
    """依赖已装 + 必备文件齐 → 幂等返回 ok，不重装。"""
    d = _ready_dir(tmp_path / "nb-ok")
    calls: list = []
    ad = _ad()
    monkeypatch.setattr(ad, "_pip_install", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(ad, "_fetch_skeleton", lambda i: calls.append("skeleton"))
    assert ad.deploy(_inst(d)) == "ok"
    assert calls == [], f"已装好的实例不该再动：{calls}"


def test_deploy_resumes_when_dir_exists_without_deps(tmp_path, monkeypatch):
    """回归：目录存在但依赖没装（中断残留 / 用户手放骨架）→ 必须继续装，**不能报 conflict**。

    base.deploy 的判据是「目录存在」，对 nonebot2 是错的：会把「依赖没装完」显示成
    「目录冲突」，用户既看不到也查不到真实原因。
    """
    d = _ready_dir(tmp_path / "nb-resume", with_deps=False)
    order: list = []
    ad = _ad()
    monkeypatch.setattr(ad, "_fetch_skeleton", lambda i: order.append("skeleton"))
    monkeypatch.setattr(ad, "_require_interpreter", lambda i: sys.executable)
    monkeypatch.setattr(ad, "_ensure_libs", lambda i: order.append("libs")
                        or _full_libs(Path(i.dir), nonebot2="2.7.1"))
    monkeypatch.setattr(ad, "_pip_install", lambda i, *t, **k: order.append("pip"))
    monkeypatch.setattr(ad, "_freeze_baseline", lambda i: None)

    assert ad.deploy(_inst(d)) == "ok"
    # 顺序：先取骨架 → 再建 libs → 最后装依赖
    assert order[:3] == ["skeleton", "libs", "pip"], order


def test_deploy_checks_interpreter_before_installing(tmp_path, monkeypatch):
    """找不到解释器要在装依赖**之前**失败。

    反过来的话用户会等 3 分钟 pip 才发现环境不对，且 libs 里留一堆装到一半的包。
    """
    d = _ready_dir(tmp_path / "nb-nointerp", with_deps=False)
    order: list = []
    ad = _ad()
    monkeypatch.setattr(ad, "_fetch_skeleton", lambda i: None)
    monkeypatch.setattr(ad, "_require_interpreter",
                        lambda i: order.append("interp") or sys.executable)
    monkeypatch.setattr(ad, "_ensure_libs",
                        lambda i: _full_libs(Path(i.dir), nonebot2="2.7.1"))
    monkeypatch.setattr(ad, "_pip_install", lambda i, *t, **k: order.append("pip"))
    monkeypatch.setattr(ad, "_freeze_baseline", lambda i: None)
    ad.deploy(_inst(d))
    assert order.index("interp") < order.index("pip"), order


def test_deploy_reports_missing_required_files(tmp_path, monkeypatch):
    """必备文件缺失必须报错，不得静默 ok（沿用 base.deploy 的口径）。"""
    d = tmp_path / "nb-bad"
    d.mkdir()
    ad = _ad()
    monkeypatch.setattr(ad, "_fetch_skeleton", lambda i: None)
    monkeypatch.setattr(ad, "_require_interpreter", lambda i: sys.executable)
    monkeypatch.setattr(ad, "_pip_install", lambda i, *t, **k: None)
    with pytest.raises(RuntimeError) as e:
        ad.deploy(_inst(d))
    assert "缺失必备文件" in str(e.value)


def test_verify_required_flags_empty_libs_dir(tmp_path):
    """空 libs/ 必须算缺失：`.exists()` 对空目录返回 True。

    不覆写 verify_required 的话，pip 装到一半失败留下的空目录会被判成「装好了」，
    deploy 误判幂等返回 ok，实例一启动就 ModuleNotFoundError。
    """
    d = tmp_path / "nb-emptycheck"
    d.mkdir()
    (d / "bot.py").write_text("# e\n", encoding="utf-8")
    (d / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (d / LIBS_DIR).mkdir()
    assert LIBS_DIR in _ad().verify_required(_inst(d))

    _full_libs(d)
    assert not _ad().verify_required(_inst(d))


def test_deploy_writes_entrypoint_and_keeps_user_bot(tmp_path):
    """入口脚本：骨架没有就生成最小 bot.py；用户已有的绝不覆盖。"""
    d = tmp_path / "nb-entry"
    d.mkdir()
    (d / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    ad = _ad()
    ad._write_entrypoint(_inst(d))
    assert "nonebot.init()" in (d / "bot.py").read_text("utf-8")
    (d / "bot.py").write_text("# mine\n", encoding="utf-8")
    ad._write_entrypoint(_inst(d))
    assert (d / "bot.py").read_text("utf-8") == "# mine\n"


def test_entrypoint_inserts_libs_onto_sys_path(tmp_path):
    """回归（承重墙）：生成的 bot.py 必须自己把 libs/ 插进 sys.path。

    依赖装在 `pip install --target` 的目录里，不在解释器的 site-packages，
    不插路径就是 `import nonebot` ModuleNotFoundError。而改成靠 PYTHONPATH
    也不行——embeddable 的 `._pth` 存在时无视环境变量（2026-10-05 实测），
    所以只有改 sys.path 这一条路。
    """
    d = tmp_path / "nb-syspath"
    d.mkdir()
    ad = _ad()
    ad._write_entrypoint(_inst(d))
    src = (d / "bot.py").read_text("utf-8")

    assert "sys.path.insert" in src, "入口没改 sys.path"
    assert f"{LIBS_DIR!r}" in src, "插入路径没指向 libs"
    # 路径必须现算（Path(__file__).parent），不能写死绝对路径：
    # 实例目录被备份还原 / 换盘 / 改名后仍要能起来
    assert "__file__" in src, "写死了绝对路径 → 实例移走后起不来"
    assert str(d) not in src
    assert ad.entrypoint_has_deps_path(_inst(d)) is True
    compile(src, "bot.py", "exec")           # 拼接引号错了这里就炸


def test_entrypoint_has_deps_path_detects_broken_entry(tmp_path):
    """用户手写的 bot.py 若没引 libs，要能识别出来（否则只有启动失败才发现）。"""
    d = tmp_path / "nb-badentry"
    d.mkdir()
    ad = _ad()
    inst = _inst(d)
    assert ad.entrypoint_has_deps_path(inst) is False, "根本没有 bot.py"
    (d / "bot.py").write_text("import nonebot\n", encoding="utf-8")
    assert ad.entrypoint_has_deps_path(inst) is False


def test_prepare_start_installs_deps_once(tmp_path, monkeypatch):
    """首启补装：依赖缺失时装并写入口（返回 True=有动作），已齐则跳过（False）。"""
    d = _ready_dir(tmp_path / "nb-prep", with_deps=False)
    ad = _ad()
    monkeypatch.setattr(ad, "_require_interpreter", lambda i: sys.executable)
    installs: list = []

    def fake_install(i, *t, **k):
        installs.append(t)
        _full_libs(Path(i.dir), nonebot2="2.7.1")

    monkeypatch.setattr(ad, "_pip_install", fake_install)

    assert ad.prepare_start(_inst(d)) is True
    assert installs, "补装时应同时装依赖"
    assert ad.prepare_start(_inst(d)) is False
    assert len(installs) == 1, "依赖已齐，不该重复装"


def test_prepare_start_heals_partially_installed_libs(tmp_path, monkeypatch):
    """回归：libs 在但缺关键包（用户手删 / pip 半途失败）→ 应识别并补装。

    只判「libs 目录在不在」的话，这种残缺状态永远补不上——
    面板上实例显示正常、实际一启动就死。
    """
    d = _ready_dir(tmp_path / "nb-partial", with_deps=True)
    import shutil
    shutil.rmtree(d / LIBS_DIR / "nonebot2-2.7.1.dist-info")
    ad = _ad()
    monkeypatch.setattr(ad, "_require_interpreter", lambda i: sys.executable)
    installs: list = []
    monkeypatch.setattr(ad, "_pip_install",
                        lambda i, *t, **k: installs.append(t)
                        or _full_libs(Path(i.dir), nonebot2="2.7.1"))
    assert ad.prepare_start(_inst(d)) is True
    assert installs
    assert ad.prepare_start(_inst(d)) is False


def test_pip_install_uses_target_and_clears_pythonpath(tmp_path, monkeypatch):
    """pip 命令行必须是 `--target <libs>`，且清掉 PYTHONHOME/PYTHONPATH。

    - `--target`：依赖落实例目录，多实例互不污染，删实例即彻底清理
    - 清环境变量：embeddable 的 `._pth` 无视 PYTHONPATH，留着只会误导排错
    """
    d = _ready_dir(tmp_path / "nb-pipcmd", with_deps=False)
    ad = _ad()
    monkeypatch.setattr(ad, "_instance_python", lambda i: Path(sys.executable))
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw.get("env") or {}
        # 造出依赖元数据（真实 pip 会做），否则新加的产物检查会误判失败
        for n in MANIFEST["dependencies"]:
            meta = d / LIBS_DIR / f"{n}-9.9.9.dist-info"
            meta.mkdir(parents=True, exist_ok=True)
            (meta / "METADATA").write_text(
                f"Name: {n}\nVersion: 9.9.9\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("PYTHONPATH", "/should/be/ignored")
    monkeypatch.setenv("PYTHONHOME", "/should/be/ignored")
    ad._pip_install(_inst(d), "nonebot2>=2.4.0")

    cmd = seen["cmd"]
    assert cmd[:3] == [sys.executable, "-m", "pip"]
    assert cmd[cmd.index("--target") + 1] == str(d / LIBS_DIR)
    assert cmd[-1] == "nonebot2>=2.4.0"
    assert "PYTHONPATH" not in seen["env"]
    assert "PYTHONHOME" not in seen["env"]


def test_pip_install_raises_with_tail_on_failure(tmp_path, monkeypatch):
    """pip 失败要报出 stderr 尾部：报错信息全在最后几行，只报 rc 等于没报。"""
    d = _ready_dir(tmp_path / "nb-pipfail", with_deps=False)
    ad = _ad()
    monkeypatch.setattr(ad, "_instance_python", lambda i: Path(sys.executable))
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: SimpleNamespace(
            returncode=1, stdout="", stderr="\n".join(
                f"line{i}" for i in range(20)) + "\nNo matching distribution"))
    with pytest.raises(RuntimeError) as e:
        ad._pip_install(_inst(d), "nonebot2")
    msg = str(e.value)
    assert "rc=1" in msg
    assert "No matching distribution" in msg, "报错尾部（真正的失败原因）被截掉了"
    assert "line0" not in msg, "不该把整段 stderr 都塞进报错"


def test_pip_install_noop_without_targets(tmp_path):
    """没目标就不该跑 pip（否则空依赖时会白等一趟网络）。"""
    ad = _ad()

    def boom(*a, **k):
        raise AssertionError("空目标不该发起 pip")

    ad._pip = boom                                   # noqa: SLF001
    ad._pip_install(_inst(tmp_path / "nb-noop"))      # 不抛即通过


def test_build_start_cmd_uses_instance_python(tmp_path):
    """启动命令的解释器必须与装依赖时同一个，否则二进制包（cryptography）对不上。"""
    ad = _ad()
    inst = _inst(tmp_path / "nb-start")
    ad._instance_python = lambda i: Path("/fake/python")    # noqa: SLF001
    cmd = ad.build_start_cmd(inst)
    assert cmd[1] == "bot.py"
    # str(Path(...)) 在 Windows 上会补出反斜杠，故按Path 比较而非按字符串
    assert Path(cmd[0]) == Path("/fake/python")


def test_instance_python_matches_find_interpreter(tmp_path):
    """回归：装依赖的解释器与启动的解释器必须同源。

    早前 `_instance_python` 自己写了一遍候选顺序（只有 bundled + sys.executable），
    漏掉 DM_PYTHON 与清单候选——用户设了 DM_PYTHON 就会装到系统 Python、
    却用内置解释器启动，二进制包在 import 时报找不到 DLL。
    """
    ad = _ad()
    inst = _inst(tmp_path / "nb-same")
    ad._find_interpreter = lambda i: sys.executable       # noqa: SLF001
    assert str(ad._instance_python(inst)) == str(Path(sys.executable))


def test_instance_python_cache_avoids_repeated_probing(tmp_path):
    """探测结果应记忆：prepare_start → pip_install → build_start_cmd 会连问三次，
    每次要跑两次子进程。缓存键是实例目录，故不同实例互不干扰。"""
    from adapters.nonebot2 import _INTERP_CACHE

    ad = _ad()
    calls: list = []

    def fake_find(i):
        calls.append(str(i.dir))
        return sys.executable

    ad._find_interpreter = fake_find                  # noqa: SLF001
    a, b = _inst(tmp_path / "n1"), _inst(tmp_path / "n2")
    ad._instance_python(a)
    ad._instance_python(a)
    ad._instance_python(b)
    assert calls == [str(a.dir), str(b.dir)], calls
    _INTERP_CACHE.clear()


def test_health_check_treats_process_as_connected():
    """客户端形态：不监听互联端口，进程存活即视为已连接（HEALTH_PORT_KEYS 为空）。"""
    ad = _ad()
    assert ad.HEALTH_PORT_KEYS == []
    assert ad.health_check(_inst("/tmp/x"), is_alive=True) == {"alive": True, "conn": "ok"}
    assert ad.health_check(_inst("/tmp/x"), is_alive=False)["conn"] == "down"


def test_configure_login_is_noop():
    """不登录 QQ：OneBot 由登录端承载，扫码是登录端的事。"""
    assert _ad().configure_login(_inst("/tmp/x"), {}) == {"needs_login": False}


def test_expose_webui_is_none():
    """无自带 WebUI：面板不代为放通 .env 里的应用端口。"""
    assert _ad().expose_webui(_inst("/tmp/x")) is None


# ---------- 互联配置（.env） ----------

def test_write_conn_config_forward_sets_ws_urls(tmp_path):
    """正向 WS：本端主动连登录端 → OB11_WS_URLS，且必须带 DRIVER。"""
    d = tmp_path / "nb-fwd"
    d.mkdir()
    ad = _ad()
    r = ad.write_conn_config(_inst(d), "ob11", "forward", "127.0.0.1:3001", "TOK123")
    assert r.ok and r.path.endswith(".env")
    env = read_dotenv(d / ".env")
    assert env["DRIVER"] == "~fastapi+~websockets", "缺 DRIVER 则 OneBot 收发不工作"
    assert env["OB11_WS_URLS"] == json.dumps(["ws://127.0.0.1:3001/"])
    assert env["OB11_TOKEN"] == "TOK123"
    assert "OB11_REVERSE_WS_URLS" not in env
    assert "重启" in r.manual, "nonebot2 无配置热加载，必须提示重启"


def test_write_conn_config_reverse_uses_reverse_key(tmp_path):
    """反向 WS：登录端连本端 → OB11_REVERSE_WS_URLS，监听 0.0.0.0。"""
    d = tmp_path / "nb-rev"
    d.mkdir()
    ad = _ad()
    ad.write_conn_config(_inst(d), "ob11", "reverse", "127.0.0.1:6700", "T2")
    env = read_dotenv(d / ".env")
    assert env["OB11_REVERSE_WS_URLS"] == json.dumps(["ws://0.0.0.0:6700/"])
    assert env["OB11_TOKEN"] == "T2"
    assert "OB11_WS_URLS" not in env


def test_write_conn_config_uses_allocated_port(tmp_path):
    d = tmp_path / "nb-port"
    d.mkdir()
    ad = _ad()
    ad.write_conn_config(_inst(d, allocated_ports={"bot": 8123}), "ob11",
                         "forward", "127.0.0.1:3001", "T")
    assert read_dotenv(d / ".env")["PORT"] == "8123"


def test_write_conn_config_rejects_milky(tmp_path):
    """Milky 需另装适配器（nonebot-adapter-milky），清单只声明 OneBot。"""
    r = _ad().write_conn_config(_inst(tmp_path / "nb-mk"), "milky",
                                "forward", "127.0.0.1:3000", "T")
    assert r.ok is False and "Milky" in r.manual


def test_dotenv_preserves_comments_and_is_idempotent(tmp_path):
    """回归：写 .env 不能毁掉用户手写的注释与分组；重复写同一份不产生差异。"""
    p = tmp_path / ".env"
    p.write_text(
        "# OneBot 配置\n"
        "LOG_LEVEL=INFO\n"
        "export SUPERUSERS=123456\n"
        "\n"
        "DRIVER=~none\n",
        encoding="utf-8")

    def put(c):
        return {**c, "DRIVER": "~fastapi+~websockets", "OB11_TOKEN": "TOK"}

    atomic_write_dotenv(p, put)
    text = p.read_text("utf-8")
    assert text.startswith("# OneBot 配置\n"), "注释被抹掉了"
    assert "LOG_LEVEL=INFO" in text
    assert "export SUPERUSERS=123456" in text, "export 写法被改写"
    assert read_dotenv(p)["DRIVER"] == "~fastapi+~websockets"
    assert read_dotenv(p)["SUPERUSERS"] == "123456"

    before = text
    atomic_write_dotenv(p, put)
    assert p.read_text("utf-8") == before, "重复写入不幂等（会逐次追加或重排）"


def test_dotenv_new_file_is_written_with_trailing_newline(tmp_path):
    p = tmp_path / ".env"
    out = atomic_write_dotenv(p, lambda c: {**c, "OB11_TOKEN": "T"})
    assert out["OB11_TOKEN"] == "T"
    assert p.read_text("utf-8").endswith("\n")


# ---------- pyproject.toml 文本级合并 ----------

_TOML_MULTI = """[project]
name = "nb"
version = "0.1.0"
dependencies = [
    "nonebot2>=2.4.0",
    "nonebot-adapter-onebot>=2.4.0",
]

[tool.nonebot]
plugin_dirs = ["plugins"]
builtins = ["nonebot.adapters.onebot.v11"]
"""

_TOML_SINGLE = ('[project]\nname = "nb"\n'
                'dependencies = ["nonebot2>=2.4.0"]\n\n[tool.nonebot]\n'
                'plugin_dirs = ["plugins"]\nbuiltins = ["nonebot.adapters.onebot.v11"]\n')


@pytest.mark.parametrize("text", [_TOML_MULTI, _TOML_SINGLE])
def test_merge_adds_deps_and_keeps_tool_section(text):
    """合并后必须仍是合法 TOML，且 [tool.nonebot] 逐字保留。

    这是文本级编辑最大的风险点：整文件重写会把用户的 nonebot 配置清掉，
    而 nonebot2 启动依赖 plugin_dirs/builtins。
    """
    out = merge_pyproject_deps(text, {"onedice": ""})
    parsed = tomllib.loads(out)
    assert "onedice" in parsed["project"]["dependencies"]
    assert parsed["tool"]["nonebot"]["plugin_dirs"] == ["plugins"]
    assert parsed["tool"]["nonebot"]["builtins"] == [
        "nonebot.adapters.onebot.v11"]


def test_merge_is_idempotent_and_never_downgrades():
    """已有同名包 → 原样保留（不覆盖用户钉的版本），重复调用不改文件。"""
    out = merge_pyproject_deps(_TOML_MULTI, {"nonebot2": ""})
    assert out == _TOML_MULTI, "merge 把 nonebot2 冲掉了"
    once = merge_pyproject_deps(_TOML_MULTI, {"onedice": ""})
    assert merge_pyproject_deps(once, {"onedice": ""}) == once


def test_merge_quotes_every_value():
    """回写必须补引号：裸串 nonebot2>=2.4.0 会让 tomllib 直接解析失败。"""
    out = merge_pyproject_deps(_TOML_MULTI, {"fastapi": ">=0.110"})
    tomllib.loads(out)                       # 不抛即合法
    assert '"fastapi>=0.110",' in out


def test_merge_rejects_missing_sections():
    for bad in ('[build-system]\nrequires = ["x"]\n',      # 没有 [project]
                '[project]\nname = "nb"\n'):                 # 没有 dependencies
        with pytest.raises(ValueError):
            merge_pyproject_deps(bad, {"onedice": ""})


def test_merge_handles_trailing_comment_after_array():
    """闭合 ] 后带行尾注释时，重建不得把注释吞进数组或丢掉。"""
    text = ('[project]\n'
            'dependencies = [ # 显式依赖\n'
            '    "nonebot2>=2.4.0",\n'
            '] # 保持以上为运行所需\n')
    out = merge_pyproject_deps(text, {"onedice": ""})
    assert "onedice" in tomllib.loads(out)["project"]["dependencies"]
    assert "显式依赖" in out and "保持以上为运行所需" in out


# ---------- 版本下限抬高（升级通道） ----------

def test_bump_replaces_only_listed_versions():
    """bump 只改版本、不新增条目；deps 里不在列表中的包被忽略。"""
    out = bump_pyproject_versions(_TOML_MULTI, {"nonebot2": ">=2.7.1"})
    deps = tomllib.loads(out)["project"]["dependencies"]
    assert "nonebot2>=2.7.1" in deps
    assert "nonebot2>=2.4.0" not in deps
    assert len(deps) == 2, f"bump 不该增删条目：{deps}"
    assert tomllib.loads(out)["tool"]["nonebot"]["plugin_dirs"] == ["plugins"]


def test_bump_is_noop_when_versions_match():
    assert bump_pyproject_versions(_TOML_MULTI, {"nonebot2": ">=2.4.0"}) == _TOML_MULTI
    assert bump_pyproject_versions(_TOML_MULTI, {"不存在": ">=1"}) == _TOML_MULTI


# ---------- 骨架自生成（回归：2026-10-06 服务器部署卡住） ----------

def test_skeleton_generated_without_network(tmp_path, monkeypatch):
    """⚠️ 骨架生成**不得有任何网络请求**（回归用例）。

    起因：清单原来写 `skeleton.repo = nonebot/nonebot2-template`，但
    **该仓库不存在**（GitHub 返 404）。服务器上的表现是「部署卡住」——
    面板反复轮询部署进度、最后报「下载骨架文件 pyproject.toml 失败：
    The read operation timed out」，用户只看到一直转圈。
    实际等了近 30 秒才拿到那个 404。

    故改为自生成骨架。这里把 `urlopen` 打成炸弹：一旦有人重新引入网络
    下载，测试会立刻炸掉，而不是等到用户在服务器上再踩一次。
    """
    d = tmp_path / "nb-skel"
    d.mkdir()

    def bomb(*a, **kw):
        raise AssertionError("骨架生成不该访问网络！")

    monkeypatch.setattr("urllib.request.urlopen", bomb)
    monkeypatch.setattr("urllib.request.Request", bomb, raising=False)
    _ad()._fetch_skeleton(_inst(d))                     # noqa: SLF001
    pj = d / "pyproject.toml"
    assert pj.is_file(), "骨架没生成"
    assert pj.stat().st_size > 0


def test_generated_pyproject_is_valid_and_has_nonebot_section(tmp_path):
    """生成的 pyproject.toml 必须是合法 TOML，且含 `[tool.nonebot]` 段。

    `[tool.nonebot]` 是承重墙：`nb run` 依 `plugin_dirs` 找插件目录，
    缺了插件加载不到（表现为「启动了但没有任何命令」）。
    """
    import tomllib
    d = tmp_path / "nb-pj"
    d.mkdir()
    _ad()._fetch_skeleton(_inst(d))                     # noqa: SLF001
    data = tomllib.loads((d / "pyproject.toml").read_text("utf-8"))
    assert data["project"]["name"]
    assert data["project"]["requires-python"]
    assert "dependencies" in data["project"], \
        "要有 dependencies 段 —— _write_pyproject_deps 要往里写"
    assert data["tool"]["nonebot"]["plugin_dirs"] == ["plugins"]


def test_existing_pyproject_not_overwritten(tmp_path):
    """用户手工放的 pyproject.toml **不能被覆盖**（断点续跑也靠这条）。"""
    d = tmp_path / "nb-keep"
    d.mkdir()
    mine = "# 我自己写的\n"
    (d / "pyproject.toml").write_text(mine, encoding="utf-8")
    _ad()._fetch_skeleton(_inst(d))                     # noqa: SLF001
    assert (d / "pyproject.toml").read_text("utf-8") == mine


def test_unknown_skeleton_file_fails_loudly(tmp_path):
    """清单声明了适配器不会生成的文件 → 明确报错，不静默跳过。

    静默跳过会得到一个「部署成功但起不来」的实例，最难排查。
    """
    d = tmp_path / "nb-unknown"
    d.mkdir()
    ad = _ad()
    m = dict(MANIFEST)
    m["skeleton"] = {"files": ["poetry.lock"]}
    from adapters.nonebot2 import NoneBot2Adapter
    bad = NoneBot2Adapter(m)
    with pytest.raises(RuntimeError) as e:
        bad._fetch_skeleton(_inst(d))                  # noqa: SLF001
    assert "poetry.lock" in str(e.value)
    assert "离线程序包" in str(e.value), "要告诉用户可行的替代方案"


def test_bump_works_on_single_line_array():
    out = bump_pyproject_versions(_TOML_SINGLE, {"nonebot2": ">=2.7.1"})
    assert tomllib.loads(out)["project"]["dependencies"] == ["nonebot2>=2.7.1"]


def test_dep_name_normalizes_extras_and_case():
    assert _dep_name("D>=1.0") == "d"
    assert _dep_name("nonebot2[fastapi]") == "nonebot2"
    assert _dep_name("Pkg~=1.0") == "pkg"
    assert _dep_name("plain") == "plain"


def test_pip_timeout_is_generous():
    """nonebot2 本体实测45s；超时给慢网留余量，但必须有上限（不能无限等）。"""
    assert 300 <= PIP_TIMEOUT <= 1800


# ---------- pip 调用加固（回归：2026-10-07 审计） ----------

def test_pip_install_closes_stdin(tmp_path, monkeypatch):
    """pip 在依赖冲突时会问「要不要 --force」—— 不关 stdin 会挂到超时。"""
    d = _ready_dir(tmp_path / "nb-pipstdin")
    ad = _ad()
    monkeypatch.setattr(ad, "_instance_python", lambda i: Path(sys.executable))
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        for n in MANIFEST["dependencies"]:
            meta = d / LIBS_DIR / f"{n}-9.9.9.dist-info"
            meta.mkdir(parents=True, exist_ok=True)
            (meta / "METADATA").write_text(
                f"Name: {n}\nVersion: 9.9.9\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ad._pip_install(_inst(d), "nonebot2>=2.4.0")
    assert seen.get("stdin") == subprocess.DEVNULL, \
        "没关 stdin → 依赖冲突时挂在 pip 的提问上"


def test_pip_install_detects_zero_exit_but_missing_deps(tmp_path, monkeypatch):
    """⚠️ pip 退出码 0不代表依赖真的装上了（rc=0 但包名拼错 / 平台不匹配）。

    只信退出码会得到「部署成功但一启动就 ModuleNotFoundError」的实例。
    """
    d = _ready_dir(tmp_path / "nb-piprc0", with_deps=False)
    ad = _ad()
    monkeypatch.setattr(ad, "_instance_python", lambda i: Path(sys.executable))
    # 返回 0 但什么都不装
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: SimpleNamespace(
        returncode=0, stdout="Successfully installed nothing", stderr=""))
    with pytest.raises(RuntimeError) as e:
        ad._pip_install(_inst(d), "nonebot2>=2.4.0")
    msg = str(e.value)
    assert "退出码是 0" in msg, "要解释为什么退出码没能反映失败"
    assert "nonebot2" in msg, "要点出缺哪个依赖"
