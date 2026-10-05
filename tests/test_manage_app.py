"""「管理应用」契约：能力查询（/manage）+ 插件装卸（/plugins/*）。

「管理应用」是 manifest/适配器驱动的补位机制：前端不按程序名硬编码，
而是问后端"这个实例能管什么"。所以测试重点是**能力判定正确**：

- 有 WebUI 的程序（NapCat/LLBot…）必须仍能拿到 webui 入口，
  否则这次改造会把「打开 WebUI」功能**回退掉**——这是最该盯的回归。
- 依赖型程序（NoneBot2）无端口但有 python_deps 入口。
- 未部署的实例要给出 disabled_reason，而不是让用户点进去撞报错。

直接调用端点函数（ctx 是模块级单例，DM_STATE_DIR 指向临时目录），
做法参考 tests/test_link_webui.py。
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

tmp = tempfile.mkdtemp(prefix="dm_manage_")
os.environ["DM_STATE_DIR"] = tmp
os.environ["DM_LOG_DIR"] = os.path.join(tmp, "logs")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from adapters import load_registry  # noqa: E402
from adapters.nonebot2 import NoneBot2Adapter  # noqa: E402
from api.context import ctx  # noqa: E402
from api.rest import (  # noqa: E402
    instance_manage,
    instance_plugin_install,
    instance_plugin_sync,
)

ROOT = Path(__file__).resolve().parent.parent


def _mk(iid, dice, webui=3080):
    """建实例。webui=None 表示该程序无 WebUI（nonebot2 就是这种）。

    真实部署里端口按 manifest 的 `webui_default_port` 分配（services/wizard.py），
    没有该字段就不会分到 webui 端口 —— 所以这里必须能模拟"无 WebUI"，
    否则会误以为 nonebot2 也有 WebUI 入口（第一版测试就栽在这）。
    """
    ap = {"ob11": 3001}
    if webui is not None:
        ap["webui"] = webui
    ctx.registry.create(iid, dice=dice, arch="standalone", dir_=f"/tmp/{iid}",
                        port=3000, allocated_ports=ap)


def _run(coro):
    """跑 async 端点。三个 plugin端点是 async（内部 to_thread 跑 pip，可能几分钟），
    但项目刻意不引 pytest-asyncio 保持依赖精简，故用 asyncio.run 包装。"""
    return asyncio.run(coro)


def _ad(dice="nonebot2") -> NoneBot2Adapter:
    """从 load_registry 取清单（已按当前 edition 分流过 _win），不再自己读文件。"""
    reg = load_registry(ROOT / "manifests")
    return NoneBot2Adapter(reg[dice][0])


@pytest.fixture(autouse=True)
def _clean():
    yield
    for r in ctx.registry.all():
        ctx.registry.remove(r["id"])
    ctx.registry.purge_tombstones(days=0)


# ---------- 能力查询 ----------

def test_manage_keeps_webui_capability_for_webui_programs():
    """回归：改造后有 WebUI 的程序**仍必须**拿到 webui 入口。

    「打开 WebUI」改成「管理应用」是加法不是替换——把这条能力弄丢，
    等于用「管理应用」的名义把 NapCat/LLBot 的扫码入口砍了。
    """
    _mk("m-napcat", "napcat")
    r = instance_manage("m-napcat", None)
    kinds = [c["kind"] for c in r["capabilities"]]
    assert "webui" in kinds
    cap = next(c for c in r["capabilities"] if c["kind"] == "webui")
    assert cap["label"] == "打开 WebUI"
    assert r["data"]["webui"]["port"] == 3080


def test_manage_webui_port_prefers_actual_over_allocated():
    """实际端口优先于分配端口：端口被占时程序常 +1 换端口，用旧的会打不开。"""
    _mk("m-napcat2", "napcat")
    ctx.registry.update("m-napcat2", actual_port=3099)
    r = instance_manage("m-napcat2", None)
    assert r["data"]["webui"]["port"] == 3099


def test_manage_for_nonebot2_has_python_deps_not_webui():
    """NoneBot2 无自带 WebUI，但有依赖/插件可管 → 走 python_deps 入口。"""
    d = Path(tempfile.mkdtemp(prefix="dm_nb_"))
    _mk("m-nb", "nonebot2", webui=None)
    ctx.registry.update("m-nb", dir=str(d))
    r = instance_manage("m-nb", None)
    kinds = [c["kind"] for c in r["capabilities"]]
    assert "webui" not in kinds, "nonebot2 没有 WebUI 端口，不该产出该入口"
    assert "python_deps" in kinds
    assert r["data"]["python_deps"]["libs_path"].endswith("libs")


def test_manage_marks_undeployed_instance_disabled_with_reason():
    """未部署（依赖未装齐）的实例要带 disabled_reason 与requires_deployed。

    否则用户点「管理应用」才撞「实例尚未完成部署」，是明显的体验倒退。
    """
    d = Path(tempfile.mkdtemp(prefix="dm_nb2_"))
    (d / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    _mk("m-nb2", "nonebot2", webui=None)
    ctx.registry.update("m-nb2", dir=str(d))
    cap = instance_manage("m-nb2", None)["capabilities"][0]
    assert cap["kind"] == "python_deps"
    assert cap.get("requires_deployed") is True
    assert "部署" in cap.get("disabled_reason", "")


def _full_libs(d: Path, drop: str = "") -> Path:
    """造一份「清单依赖全齐」的 libs/，可指定少一个（模拟部分完成状态）。"""
    from adapters import load_registry as _lr
    reg = _lr(ROOT / "manifests")
    m = reg["nonebot2"][0]
    libs = d / "libs"
    libs.mkdir(parents=True, exist_ok=True)
    for k in m["dependencies"]:
        if k == drop:
            continue
        (libs / f"{k.replace('-', '_')}-1.0.dist-info").mkdir(exist_ok=True)
    return libs


def test_manage_deployed_instance_has_no_missing():
    """全齐时deployed=True、missing 为空，面板不显示补装入口。"""
    d = Path(tempfile.mkdtemp(prefix="dm_nb3a_"))
    _full_libs(d)
    _mk("m-nb3a", "nonebot2", webui=None)
    ctx.registry.update("m-nb3a", dir=str(d))
    cap = instance_manage("m-nb3a", None)["capabilities"][0]
    assert cap.get("deployed") is True
    assert cap["missing"] == []
    assert "disabled_reason" not in cap, "已部署的实例不该带禁用原因"


def test_manage_partial_install_lists_missing_for_repair():
    """部分完成（少一个）时列出缺哪些，供面板「补装缺失项」用。

    判据是**全部**在才算装好（见 adapters.nonebot2._deps_installed）：
    只少一个包也要报，不能让面板以为一切正常。
    """
    d = Path(tempfile.mkdtemp(prefix="dm_nb3_"))
    _full_libs(d, drop="nonebot2")
    _mk("m-nb3", "nonebot2", webui=None)
    ctx.registry.update("m-nb3", dir=str(d))
    cap = instance_manage("m-nb3", None)["capabilities"][0]
    assert cap.get("deployed") is False, "缺 nonebot2 本体，不算装齐"
    assert "nonebot2" in cap["missing"]


def test_manage_unknown_instance_is_404():
    with pytest.raises(HTTPException) as e:
        instance_manage("no-such-inst", None)
    assert e.value.status_code == 404


# ---------- 插件装卸 ----------

def test_install_rejects_when_instance_running(monkeypatch):
    """运行中装插件要拦下：进程 import 着 libs/ 的 .py，换文件会半新半旧。"""
    _mk("m-run", "nonebot2", webui=None)
    proc = ctx.pm.get("m-run")
    monkey = type("P", (), {"poll": staticmethod(lambda: None),
                            "pid": 999999, "returncode": None,
                            "terminate": lambda self: None,
                            "kill": lambda self: None})()
    proc._proc = monkey                                # 伪装成活着的进程
    try:
        with pytest.raises(HTTPException) as e:
            _run(instance_plugin_install("m-run", {"spec": "nonebot-plugin-alconna"}))
        assert e.value.status_code == 400
        assert "停止" in str(e.value.detail)
    finally:
        proc._proc = None


def test_install_rejects_bad_spec(monkeypatch, tmp_path):
    """空包名 / 带空格的包名要在装之前拒掉，别等pip 跑完才失败。"""
    d = tmp_path / "nb-bad"
    d.mkdir()
    _mk("m-bad", "nonebot2", webui=None)
    ctx.registry.update("m-bad", dir=str(d))
    for spec, hint in (("   ", "包名"), ("a b", "不合法")):
        with pytest.raises(HTTPException) as e:
            _run(instance_plugin_install("m-bad", {"spec": spec}))
        assert e.value.status_code in (400, 500)
        if spec.strip():
            assert hint in str(e.value.detail)


def test_install_writes_pyproject_together(monkeypatch, tmp_path):
    """核心不变量：装插件必须**同时**写 pyproject.toml。

    只 pip 不写的话，`nb run` 依 pyproject 同步依赖时会把插件悄悄移除，
    用户看到的是「装完重启就没了」。这个 bug 静默、单测容易漏，故钉死。
    """
    d = tmp_path / "nb-inst"
    d.mkdir()
    (d / "pyproject.toml").write_text(
        '[project]\nname = "nb"\ndependencies = ["nonebot2>=2.4.0"]\n\n'
        '[tool.nonebot]\nplugin_dirs = ["plugins"]\n', encoding="utf-8")
    _mk("m-inst", "nonebot2", webui=None)
    ctx.registry.update("m-inst", dir=str(d))

    def fake_pip_install(self_, i, *targets, **k):
        """只mock pip 这一层：装完把 dist-info 落到 libs/。

        不能整个mock 掉 install_package——那就把"装完要写 pyproject"这段
        本次要验的逻辑一起mock 掉了，测试就成了自证。
        """
        for t in targets:
            nm = t.split(">=")[0].split("==")[0]
            (d / "libs" / f"{nm.replace('-', '_')}-1.2.3.dist-info").mkdir(
                parents=True, exist_ok=True)

    import adapters.nonebot2 as nb
    monkeypatch.setattr(nb.NoneBot2Adapter, "_pip_install", fake_pip_install)
    monkeypatch.setattr(nb.NoneBot2Adapter, "_require_interpreter",
                        lambda self_, i: sys.executable)
    r = _run(instance_plugin_install("m-inst", {"spec": "nonebot-plugin-alconna"}))
    assert r["ok"] is True
    import tomllib
    pp = tomllib.loads((d / "pyproject.toml").read_text("utf-8"))
    deps = pp["project"]["dependencies"]
    assert "nonebot-plugin-alconna" in deps, "装完没写进 pyproject → nb run 会移除它"
    assert pp["tool"]["nonebot"]["plugin_dirs"] == ["plugins"], "用户配置被破坏"


def test_uninstall_removes_from_pyproject_and_libs(tmp_path, monkeypatch):
    """卸载要同时清 libs/ 与 pyproject，只清一处会留下「幽灵依赖」。"""
    d = tmp_path / "nb-un"
    d.mkdir()
    (d / "pyproject.toml").write_text(
        '[project]\ndependencies = ["nonebot2>=2.4.0", "nonebot-plugin-alconna"]\n',
        encoding="utf-8")
    (d / "libs" / "nonebot_plugin_alconna-1.2.3.dist-info").mkdir(parents=True)
    (d / "libs" / "nonebot_plugin_alconna").mkdir()
    (d / "libs" / "nonebot_plugin_alconna" / "__init__.py").write_text("x")
    _mk("m-un", "nonebot2", webui=None)
    ctx.registry.update("m-un", dir=str(d))

    ad = _ad()
    inst = type("I", (), {"dir": str(d)})()
    ver = ad.uninstall_package(inst, "nonebot-plugin-alconna")
    assert ver == "1.2.3"
    assert not (d / "libs" / "nonebot_plugin_alconna").exists(), "包目录没删掉"
    assert not list((d / "libs").glob("nonebot_plugin_alconna-*.dist-info"))
    import tomllib
    assert tomllib.loads((d / "pyproject.toml").read_text("utf-8")
                         )["project"]["dependencies"] == ["nonebot2>=2.4.0"]


def test_uninstall_rejects_not_installed(tmp_path):
    d = tmp_path / "nb-no"
    d.mkdir()
    (d / "libs").mkdir()
    ad = _ad()
    inst = type("I", (), {"dir": str(d)})()
    with pytest.raises(ValueError) as e:
        ad.uninstall_package(inst, "nonebot-plugin-not-there")
    assert "不在已装依赖里" in str(e.value)


def test_sync_pyproject_pulls_in_libs_contents(tmp_path):
    """用户在实例目录手工 pip install --target 装过插件 → sync 兜底写进 pyproject。"""
    d = tmp_path / "nb-sync"
    d.mkdir()
    (d / "pyproject.toml").write_text(
        '[project]\ndependencies = ["nonebot2>=2.4.0"]\n', encoding="utf-8")
    (d / "libs" / "nonebot_plugin_rsshell-1.0.0.dist-info").mkdir(parents=True)
    _mk("m-sync", "nonebot2", webui=None)
    ctx.registry.update("m-sync", dir=str(d))

    r = _run(instance_plugin_sync("m-sync", None))
    assert r["ok"] is True
    import tomllib
    deps = tomllib.loads((d / "pyproject.toml").read_text("utf-8"))["project"]["dependencies"]
    assert "nonebot-plugin-rsshell>=1.0.0" in deps
    assert any(p["name"] == "nonebot-plugin-rsshell" for p in r["data"]["packages"])


def test_list_plugins_separates_packages_from_dir_plugins(tmp_path):
    """包形态与目录形态插件要分开列：卸载方式完全不同（删 dist-info vs 删目录）。"""
    d = tmp_path / "nb-list"
    d.mkdir()
    (d / "libs" / "nonebot_plugin_x-1.0.0.dist-info").mkdir(parents=True)
    (d / "libs" / "fastapi-0.110.0.dist-info").mkdir()
    (d / "plugins" / "my_local").mkdir(parents=True)
    (d / "plugins" / "__pycache__").mkdir()
    ad = _ad()
    data = ad.list_plugins(type("I", (), {"dir": str(d)})())
    assert [p["name"] for p in data["packages"]] == ["nonebot-plugin-x"]
    assert [p["name"] for p in data["dir_plugins"]] == ["my_local"]
    assert "fastapi" not in [p["name"] for p in data["packages"]], \
        "基础依赖不是插件，混进来会让用户误以为能卸载 nonebot2 本体"
