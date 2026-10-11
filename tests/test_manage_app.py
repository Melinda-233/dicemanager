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
import json
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
    instance_config_save,
    instance_manage,
    instance_plugin_install,
    instance_plugin_op,
    instance_plugin_search,
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


# ---------- 基础配置（无 WebUI 程序） ----------

def test_manage_includes_config_schema_for_nonebot2():
    """无 WebUI 的 nonebot2 要在「管理应用」里带上基础配置 schema。

    超级管理员（SUPERUSERS）/ 指令前缀（COMMAND_START）是 nonebot2 的标准 .env
    配置，面板渲染表单的字段来源就在这里；漏了等于没做这次功能。
    """
    d = Path(tempfile.mkdtemp(prefix="dm_nb_cfg_"))
    _mk("m-nbcfg", "nonebot2", webui=None)
    ctx.registry.update("m-nbcfg", dir=str(d))
    r = instance_manage("m-nbcfg", None)
    cfg = r["config"]
    assert cfg is not None, "nonebot2 应暴露基础配置"
    keys = {s["key"] for s in cfg["schema"]}
    assert keys == {"SUPERUSERS", "COMMAND_START"}
    assert cfg["restart_required"] is True


def test_manage_config_is_none_for_webui_program():
    """自带 WebUI 的程序（NapCat）不应在面板里重复管配置，config 应为 None。

    否则等于把海豹/LLBot 等在各自 WebUI 里已能改的配置又做一遍，且字段会冲突。
    """
    _mk("m-napcat-cfg", "napcat")
    r = instance_manage("m-napcat-cfg", None)
    assert r["config"] is None


def _env_value(env_text: str, key: str):
    """取出 .env 里某键的原文值（供 json 回读用）。"""
    return next(l.split("=", 1)[1] for l in env_text.splitlines()
                if l.startswith(key + "="))


def test_config_save_writes_env_for_nonebot2():
    """保存端点把超级管理员/指令前缀写回 .env。

    ⚠️ 必须落成 **JSON 数组**（nonebot 的列表写法），不是逗号分隔：pydantic-settings
    对 set[str] 字段只做 json.loads，逗号串会让实例下次启动直接 SettingsError
    （2026-10-12 在服务器 nonebot 2.5.0 实测）。故这里用 json.loads 回读——
    只比对字符串的话，格式退化成逗号也能蒙混过关。
    """
    d = Path(tempfile.mkdtemp(prefix="dm_nb_save_"))
    _mk("m-nbsave", "nonebot2", webui=None)
    ctx.registry.update("m-nbsave", dir=str(d))
    r = _run(instance_config_save("m-nbsave",
            {"values": {"SUPERUSERS": "123456, 789012 ", "COMMAND_START": "., /"}}))
    assert r["ok"] is True
    env = (d / ".env").read_text("utf-8")
    assert 'SUPERUSERS=["123456","789012"]' in env
    assert 'COMMAND_START=[".","/"]' in env
    # 落盘值必须真是 nonebot 能解析的列表（json 合法即可，不依赖 nonebot 在场）
    assert json.loads(_env_value(env, "SUPERUSERS")) == ["123456", "789012"]
    assert json.loads(_env_value(env, "COMMAND_START")) == [".", "/"]


def test_config_read_shows_plain_values_and_defaults():
    """.env 里的列表写法要显示成纯文本；键缺失时给 nonebot 默认值。

    界面原样回显 .env 值的话会显示成 ["2278442823"]（看着像坏了，其实只是没解析）；
    COMMAND_START 未配置时 nonebot 实际用的是默认 "/"，显示空白等于骗人。
    """
    d = Path(tempfile.mkdtemp(prefix="dm_nb_read_"))
    _mk("m-nbread", "nonebot2", webui=None)
    ctx.registry.update("m-nbread", dir=str(d))
    (d / ".env").write_text('SUPERUSERS=["2278442823"]\n', "utf-8")
    vals = instance_manage("m-nbread", None)["config"]["values"]
    assert vals["SUPERUSERS"] == "2278442823"     # 不带括号引号
    assert vals["COMMAND_START"] == "/"           # 缺失 → nonebot 默认


def test_config_save_empty_falls_back_to_default():
    """清空选项不能写出空值：`KEY=` 空串同样会让 nonebot 启动解析失败。

    指令前缀留空回落到默认 "/"，因为空前缀意味着每条普通消息都被当成指令。
    """
    d = Path(tempfile.mkdtemp(prefix="dm_nb_empty_"))
    _mk("m-nbempty", "nonebot2", webui=None)
    ctx.registry.update("m-nbempty", dir=str(d))
    _run(instance_config_save("m-nbempty",
                              {"values": {"SUPERUSERS": "", "COMMAND_START": "  "}}))
    env = (d / ".env").read_text("utf-8")
    assert json.loads(_env_value(env, "SUPERUSERS")) == []
    assert json.loads(_env_value(env, "COMMAND_START")) == ["/"]


def test_config_save_rejects_unsupported_program():
    """不支持面板配置的程序（NapCat）调保存端点应 400，而不是悄悄写错文件。"""
    _mk("m-napcat-save", "napcat")
    with pytest.raises(HTTPException) as e:
        _run(instance_config_save("m-napcat-save", {"values": {}}))
    assert e.value.status_code == 400


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


def test_plugin_install_returns_immediately_and_exposes_progress():
    """装插件是分钟级操作：端点必须**立即返回**，前端才有东西可轮询。

    同步等到 pip 跑完才返回的话，这几分钟里界面只有一个「处理中…」，
    用户分不清是在装还是卡死（2026-10-12 实测：点了安装，服务器上压根没有
    pip 进程，界面却一直挂着「安装中」，就是这么来的）。
    """
    d = Path(tempfile.mkdtemp(prefix="dm_nb_async_"))
    _mk("m-async", "nonebot2", webui=None)
    ctx.registry.update("m-async", dir=str(d))
    r = _run(instance_plugin_install("m-async", {"spec": "nonebot-plugin-demo"}))
    assert r.get("started") is True, "应立即返回，而不是等 pip 跑完"
    # 轮询端点要能读到状态与日志（后台任务此时未必跑完，只验结构）
    op = _run(instance_plugin_op("m-async"))
    assert "busy" in op and "lines" in op, op


def test_plugin_install_rejects_while_op_running():
    """已有操作在跑时再点一次要拒掉，否则两条 pip 同时往 libs/ 里写会互相踩。"""
    d = Path(tempfile.mkdtemp(prefix="dm_nb_dup_"))
    _mk("m-dup", "nonebot2", webui=None)
    ctx.registry.update("m-dup", dir=str(d))
    _run(instance_plugin_install("m-dup", {"spec": "nonebot-plugin-a"}))
    with pytest.raises(HTTPException) as e:
        _run(instance_plugin_install("m-dup", {"spec": "nonebot-plugin-b"}))
    assert e.value.status_code == 409


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


# ---------- 插件搜索 ----------
# ⚠️ 全部用例都 monkeypatch 掉 http_get_json：CI 不一定有外网，而 nonebot 索引
# 有 943 条 / 500KB，真去拉一次会让测试偶发超时。这里验的是「给定索引数据，
# 能不能挑出对的条目」，不是「网络通不通」。

_NB_INDEX = [
    {"module_name": "nonebot_plugin_status", "project_link": "nonebot-plugin-status",
     "name": "服务器状态查看", "desc": "通过戳一戳获取服务器状态",
     "homepage": "https://github.com/nonebot/plugin-status",
     "version": "0.9.0", "is_official": True, "valid": True},
    {"module_name": "nonebot_plugin_wordcloud", "project_link": "nonebot-plugin-wordcloud",
     "name": "词云", "desc": "生成群聊词云", "homepage": "https://ex.com/w",
     "version": "0.6.0", "is_official": False, "valid": True},
    # 下架条目：valid=false 必须排除，否则用户装到一个已被废弃的包还以为能用
    {"module_name": "nonebot_plugin_dead", "project_link": "nonebot-plugin-dead",
     "name": "已废弃", "desc": "词云相关但已下架", "homepage": "", "version": "1.0.0",
     "is_official": False, "valid": False},
    # project_link 被填成 URL 的脏数据：应退回 module_name 推出的包名，
    # 而不是把这个 URL 当包名喂给 pip
    {"module_name": "nonebot_plugin_dirty", "project_link": "https://github.com/x/y",
     "name": "脏数据", "desc": "词云的另一种实现", "homepage": "", "version": "2.0.0",
     "is_official": False, "valid": True},
]


@pytest.fixture
def _search_cache_clean():
    """索引缓存是**模块级** dict，不清会串到别的用例上（那条用例可能正在
    monkeypatch 另一个返回值，却读到了这里缓存的数据）。"""
    from adapters import base
    saved = dict(base.SEARCH_CACHE)
    base.SEARCH_CACHE.clear()
    yield
    base.SEARCH_CACHE.clear()
    base.SEARCH_CACHE.update(saved)


def test_search_local_works_without_network(monkeypatch, _search_cache_clean):
    """断网也要能搜到本地推荐目录：那几条是人工逐个核过的，是最稳的一档结果。

    这条是「远端不可降级」的底线——没有它，无外网的机器上搜索直接变空白。
    """
    monkeypatch.setattr("adapters.nonebot2.http_get_json", lambda *a, **k: None)
    got = _ad().search_plugins(None, "alconna", 10)
    assert [p["spec"] for p in got] == ["nonebot-plugin-alconna"]
    assert got[0]["source"] == "local"


def test_search_short_query_returns_nothing(monkeypatch, _search_cache_clean):
    """1 个字符不搜：全量索引里几乎每个包都能被单字符命中，结果会淹掉想要的那个。"""
    monkeypatch.setattr("adapters.nonebot2.http_get_json", lambda *a, **k: _NB_INDEX)
    assert _ad().search_plugins(None, "a", 10) == []


def test_search_remote_failure_degrades_to_local(monkeypatch, _search_cache_clean):
    """远端不可达要降级，不能报错：索引站挂了本地推荐目录依然是有用的结果，
    抛异常的话用户看到的是整个插件面板红字报错。"""
    monkeypatch.setattr("adapters.nonebot2.http_get_json", lambda *a, **k: None)
    got = _ad().search_plugins(None, "定时任务", 10)
    assert got and got[0]["spec"] == "nonebot-plugin-apscheduler"
    assert got[0]["source"] == "local"


def test_search_remote_filters_invalid_and_dirty_project_link(monkeypatch, _search_cache_clean):
    """索引里的两类脏数据：下架条目（valid=false）与 project_link 填成 URL 的条目。

    前者不排会诱导用户装废弃包；后者不处理等于把一个 URL 当 pip 包名喂进去。
    """
    monkeypatch.setattr("adapters.nonebot2.http_get_json", lambda *a, **k: _NB_INDEX)
    got = _ad().search_plugins(None, "词云", 10)
    specs = [p["spec"] for p in got]
    assert "nonebot-plugin-wordcloud" in specs
    assert "nonebot-plugin-dead" not in specs, "valid=false 的下架插件不能进结果"
    assert "nonebot-plugin-dirty" in specs, "project_link 是 URL 时应退回 module_name"
    assert not any(p["spec"].startswith("http") for p in got), specs


def test_search_dedups_local_over_remote(monkeypatch, _search_cache_clean):
    """同一 spec 只留一条，本地那条排前面：重复条目会让用户纠结装哪个，
    而本地那条带人工写的中文说明，信息量更大。"""
    dup = [{"module_name": "nonebot_plugin_orm", "project_link": "nonebot-plugin-orm",
            "name": "ORM", "desc": "ORM 支持", "homepage": "", "version": "0.1.0",
            "is_official": False, "valid": True}]
    monkeypatch.setattr("adapters.nonebot2.http_get_json", lambda *a, **k: dup)
    got = _ad().search_plugins(None, "orm", 10)
    assert [p["spec"] for p in got].count("nonebot-plugin-orm") == 1, got
    assert got[0]["source"] == "local"


def test_search_ranks_package_name_matches_first(monkeypatch, _search_cache_clean):
    """「包名以关键词开头」的结果要排在只写命中的前面。

    943 条里大量包的 desc 会顺带提到热门关键词，不排序的话用户搜 "alconna"
    拿到的是一堆「用了 alconna 的插件」，真正想要的那个在几十行开外。

    本地目录（OFFICIAL_PLUGINS）里恰好也有一条 alconna —— 它会按设计排在最前，
    盖掉远端排序。这里把 _search_local 打成空，专测远端的相关性排序逻辑。
    """
    data = [
        {"module_name": "nonebot_plugin_x", "project_link": "nonebot-plugin-x",
         "name": "某插件", "desc": "内部用到了 alconna", "homepage": "",
         "version": "1.0.0", "is_official": False, "valid": True},
        {"module_name": "nonebot_plugin_alchelper", "project_link": "nonebot-plugin-alchelper",
         "name": "Alconna 帮助工具", "desc": "生成帮助菜单", "homepage": "",
         "version": "1.0.0", "is_official": False, "valid": True},
    ]
    monkeypatch.setattr("adapters.nonebot2.http_get_json", lambda *a, **k: data)
    monkeypatch.setattr(NoneBot2Adapter, "_search_local", lambda self, q: [])
    got = _ad().search_plugins(None, "alconna", 10)
    assert got[0]["spec"] == "nonebot-plugin-alchelper", got


def test_manage_declares_search_capability_by_adapter(tmp_path):
    """能不能搜由适配器是否实现 search_plugins 决定，不由程序名决定。

    断言这一条是防着有人图省事在后端写 `if dice == "nonebot2"` —— 那么新加
    一个支持搜索的程序时，忘了改这行就永远不显示搜索框，而且不报错。
    """
    _mk("m-caps", "nonebot2", webui=None)
    ctx.registry.update("m-caps", dir=str(tmp_path))
    assert instance_manage("m-caps")["search"] is True
    _mk("m-caps2", "napcat", webui=3080)
    assert instance_manage("m-caps2")["search"] is False


def test_search_endpoint_returns_candidates(monkeypatch, tmp_path, _search_cache_clean):
    monkeypatch.setattr("adapters.nonebot2.http_get_json", lambda *a, **k: _NB_INDEX)
    _mk("m-search", "nonebot2", webui=None)
    ctx.registry.update("m-search", dir=str(tmp_path))
    r = _run(instance_plugin_search("m-search", "词云", 10))
    assert r["ok"] is True and r["q"] == "词云"
    specs = [p["spec"] for p in r["results"]]
    assert "nonebot-plugin-wordcloud" in specs
    # 每条都必须带 spec —— 前端拿它去安装，缺了就是「点了没反应」
    assert all(p.get("spec") for p in r["results"])


def test_search_endpoint_rejects_short_query(tmp_path):
    """短词在端点层就 400：走到底层会变成对全量索引的一次无意义扫描。

    先建实例，否则 _own 会先回 404，断言就落到 404 上了（那是在测别的东西）。
    """
    from fastapi import HTTPException as _HE
    _mk("m-search", "nonebot2", webui=None)
    ctx.registry.update("m-search", dir=str(tmp_path))
    try:
        _run(instance_plugin_search("m-search", "x", 10))
    except _HE as e:
        assert e.status_code == 400
    else:
        raise AssertionError("短搜索词应被拒绝")


def test_search_endpoint_rejects_unsupported_program(tmp_path):
    """NapCat 这类没有插件体系的程序：给它一个搜索框本身就是 bug。"""
    from fastapi import HTTPException as _HE
    _mk("m-nosearch", "napcat", webui=3080)
    ctx.registry.update("m-nosearch", dir=str(tmp_path))
    try:
        _run(instance_plugin_search("m-nosearch", "nonebot", 10))
    except _HE as e:
        assert e.status_code == 400
    else:
        raise AssertionError("不支持搜索的程序应被拒绝")


def test_search_endpoint_is_404_for_other_users_instance(monkeypatch, tmp_path, _search_cache_clean):
    """搜索也要过归属校验：不校验的话，凭一个实例 ID 就能探到别人实例的存在。"""
    from fastapi import HTTPException as _HE

    from api.auth import CurrentUser
    monkeypatch.setattr("adapters.nonebot2.http_get_json", lambda *a, **k: _NB_INDEX)
    _mk("m-own-search", "nonebot2", webui=None)
    ctx.registry.update("m-own-search", dir=str(tmp_path), owner="alice")
    intruder = CurrentUser(username="bob", role="user")
    try:
        _run(instance_plugin_search("m-own-search", "词云", 10, user=intruder))
    except _HE as e:
        assert e.status_code == 404, e.status_code
    else:
        raise AssertionError("他人实例必须 404（与不存在同码，不可区分）")
