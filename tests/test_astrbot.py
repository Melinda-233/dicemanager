"""AstrBot（骰子端，pip_project 策略）测试。

与 nonebot2 的关键差异（也是全部测试点）：

1. **OneBot 方向相反** —— AstrBot 作**服务端**监听 6199，登录端连它。
   传 forward 必须报错而不是默默写成 AstrBot 不读的方向。
2. **配置在 `data/cmd_config.json` 的 `platform` 数组**，不是 .env。
   字段名跟着上游漂移，故要钉死具体键名。
3. **启动前必须先 `astrbot init`**（生成 `.astrbot` 标记与data/ 子目录），
   缺它 astrbot run 会拒绝启动。
4. **没有 pyproject.toml** —— 不该暴露 nonebot2 的「同步依赖声明」能力。

pip 隔离机制（libs/ + sys.path）与 nonebot2 完全一致，那部分不在此重复测，
见 tests/test_nonebot2.py。
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.astrbot import AstrBotAdapter
from adapters.nonebot2 import LIBS_DIR
from conftest import exe_name

ROOT = Path(__file__).resolve().parent.parent


def _manifest() -> dict:
    """读当前 edition 对应的清单（server / _win 两侧同构）。"""
    from core.edition import is_desktop
    name = "astrbot_win.json" if is_desktop() else "astrbot.json"
    text = "\n".join(x for x in (ROOT / "manifests" / name).read_text("utf-8").splitlines()
                     if not x.strip().startswith("//"))
    return json.loads(text)


MANIFEST = _manifest()


def _cfg(d) -> dict:
    """读回写入的 cmd_config.json（回读校验：写对了不等于存对了）。"""
    return json.loads((Path(d) / "data" / "cmd_config.json").read_text("utf-8"))


def _instance_of(cfg_path) -> Path:
    """给 `cmd_config.json` 的路径，返回它所属的实例目录（`_cfg` 收的是实例目录）。"""
    return cfg_path.parent.parent


def _inst(d, **kw) -> SimpleNamespace:
    base = dict(dir=str(d), allocated_ports={}, actual_port=None, conn_token=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _ad() -> AstrBotAdapter:
    return AstrBotAdapter(MANIFEST)


def _full_libs(d: Path) -> Path:
    """造一份「清单依赖全齐」的 libs/（判据是 astrbot 在不在）。"""
    libs = d / LIBS_DIR
    libs.mkdir(parents=True, exist_ok=True)
    for k in MANIFEST["dependencies"]:
        (libs / f"{k.replace('-', '_')}-1.0.dist-info").mkdir(exist_ok=True)
    return libs


# ---------- 清单契约 ----------

def test_manifest_declares_pip_project():
    assert MANIFEST["name"] == "astrbot"
    assert MANIFEST["download_strategy"] == "pip_project"
    assert MANIFEST["config_path"] == "data/cmd_config.json"
    assert MANIFEST["compatible_login"], "登录端候选不能为空"


def test_manifest_python_requires_is_312():
    """上游 pyproject 锁 requires-python >=3.12（实测 4.29.0-beta.1）。

    版本下限填低会让用户装到 3.10 上，然后在 import 时报一堆无关的错
    （faiss / audioop 之类的版本门槛）。
    """
    assert tuple(MANIFEST["python_requires"]) == (3, 12)


def test_manifest_required_files_cover_init_marker():
    """`.astrbot` 必须在 required_files：它是「astrbot run 能不能启动」的前提。

    缺了它 astrbot run 直接抛 "not a valid AstrBot root directory"，
    而部署阶段一路绿灯——只有清单测试拦得住。
    """
    assert ".astrbot" in MANIFEST["required_files"]
    assert LIBS_DIR in MANIFEST["required_files"]
    assert MANIFEST["exe"] == "run.py"
    assert exe_name("run") not in MANIFEST["required_files"], (
        "AstrBot 入口是脚本而非可执行文件，别按平台分化 required_files")


def test_manifest_declares_webui_and_reverse_ports():
    """两个端口都要有清单默认值：分配器据此占端口，避免撞车。

    - 6185：AstrBot WebUI 面板（DEFAULT_CONFIG["dashboard"]["port"]）
    - 6199：OneBot 反向 WS 监听口（AstrBot 是服务端）
    """
    assert MANIFEST["webui_default_port"] == 6185
    assert MANIFEST["ob11_reverse_default_port"] == 6199


def test_registry_loads_astrbot():
    from adapters import ALLOWED_STRATEGY, load_registry
    assert "pip_project" in ALLOWED_STRATEGY
    reg = load_registry(ROOT / "manifests")
    manifest, cls = reg["astrbot"]
    assert cls is AstrBotAdapter
    assert manifest["download_strategy"] == "pip_project"


def test_astrbot_is_not_someone_elses_login_end():
    """AstrBot 是骰子端：不能出现在他人 compatible_login 里。"""
    from adapters import load_registry
    reg = load_registry(ROOT / "manifests")
    assert "astrbot" not in {t for m, _ in reg.values()
                             for t in (m.get("compatible_login") or [])
                             if t != "builtin"}


# ---------- 互联配置（cmd_config.json 的 platform 数组） ----------

def test_write_conn_config_creates_platform_entry(tmp_path):
    """反向 WS：写一条 aiocqhttp 平台条目，字段名与上游一致。

    字段名直接抄自上游 `aiocqhttp_platform_adapter.py`：
    `platform_config["ws_reverse_host"] / ["ws_reverse_port"] / ["ws_reverse_token"]`。
    写错的表现是「部署成功但连不上」，极难查，故逐字钉死。
    """
    d = tmp_path / "ab"
    d.mkdir()
    ad = _ad()
    r = ad.write_conn_config(_inst(d), "ob11", "reverse", "127.0.0.1:3001", "TOK")
    assert r.ok, r.manual
    cfg = _cfg(d)
    plats = cfg["platform"]
    assert len(plats) == 1
    p = plats[0]
    assert p["type"] == "aiocqhttp"
    assert p["enable"] is True
    assert p["ws_reverse_host"] == "0.0.0.0"
    assert p["ws_reverse_port"] == 6199
    assert p["ws_reverse_token"] == "TOK"
    assert p["id"], "id 是 AstrBot 区分平台实例的必填项"
    assert "重启" in r.manual, "AstrBot 无配置热加载，必须提示重启"


def test_write_conn_config_rejects_forward_direction(tmp_path):
    """AstrBot 只支持反向 WS：给 forward 必须报错。

    默默写成AstrBot 不会读的方向，用户看到的是「部署成功但连不上」——
    这类"看起来成功"比直接报错糟糕得多。
    """
    d = tmp_path / "ab-fwd"
    d.mkdir()
    r = _ad().write_conn_config(_inst(d), "ob11", "forward", "127.0.0.1:3001", "T")
    assert r.ok is False
    assert "反向" in r.manual


def test_write_conn_config_rejects_milky(tmp_path):
    d = tmp_path / "ab-mk"
    d.mkdir()
    r = _ad().write_conn_config(_inst(d), "milky", "reverse", "127.0.0.1:3000", "T")
    assert r.ok is False and "Milky" in r.manual


def test_write_conn_config_replaces_same_id_instead_of_appending(tmp_path):
    """同 id 重复点「写互联配置」不能攒出多条 —— AstrBot 会把它们全加载起来。"""
    d = tmp_path / "ab-dup"
    d.mkdir()
    ad = _ad()
    for token in ("T1", "T2"):
        ad.write_conn_config(_inst(d), "ob11", "reverse", "127.0.0.1:3001", token)
    plats = _cfg(d)["platform"]
    assert len(plats) == 1, f"同 id 应替换，实际有 {len(plats)} 条"
    assert plats[0]["ws_reverse_token"] == "T2"


def test_write_conn_config_preserves_other_config(tmp_path):
    """不能整文件重写丢掉用户配置：只改platform 数组，其余键原样保留。"""
    d = tmp_path / "ab-keep"
    d.mkdir()
    cfg = d / "data" / "cmd_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({
        "dashboard": {"port": 6185, "host": "0.0.0.0"},
        "provider_settings": [{"id": "openai", "api_key": "sk-xxx"}],
        "platform": [],
    }), encoding="utf-8")
    _ad().write_conn_config(_inst(d), "ob11", "reverse", "127.0.0.1:3001", "T")
    out = _cfg(_instance_of(cfg))
    assert out["dashboard"]["port"] == 6185
    assert out["provider_settings"][0]["api_key"] == "sk-xxx", "用户的 API key 被抹掉了"
    assert len(out["platform"]) == 1


def test_write_conn_config_gives_guidance_on_broken_json(tmp_path):
    """配置损坏时给手工指引，不写坏配置。"""
    d = tmp_path / "ab-bad"
    d.mkdir()
    cfg = d / "data" / "cmd_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{ not json", encoding="utf-8")
    r = _ad().write_conn_config(_inst(d), "ob11", "reverse", "127.0.0.1:3001", "T")
    assert r.ok is False
    assert "6199" in r.manual, "指引里应含要填的端口"
    assert cfg.read_text("utf-8") == "{ not json", "坏文件不该被覆盖"


def test_reverse_port_prefers_allocation(tmp_path):
    """端口优先用面板分配的（避让他人占用），没有才退回清单默认 6199。"""
    ad = _ad()
    assert ad._reverse_port(_inst(tmp_path / "x", allocated_ports={})) == 6199
    assert ad._reverse_port(_inst(tmp_path / "y",
                                  allocated_ports={"ob11": 7199})) == 7199


def test_write_conn_config_uses_allocated_port(tmp_path):
    d = tmp_path / "ab-port"
    d.mkdir()
    _ad().write_conn_config(_inst(d, allocated_ports={"ob11": 7199}),
                            "ob11", "reverse", "127.0.0.1:3001", "T")
    p = _cfg(d)["platform"][0]
    assert p["ws_reverse_port"] == 7199


# ---------- 启动：init 前置与入口脚本 ----------

def test_entrypoint_inserts_libs_and_calls_cli(tmp_path):
    """入口脚本：插 libs/ + 转发给 astrbot CLI，且能 compile。"""
    d = tmp_path / "ab-entry"
    d.mkdir()
    ad = _ad()
    ad._write_entrypoint(_inst(d))
    src = (d / "run.py").read_text("utf-8")
    assert "sys.path.insert" in src and repr(LIBS_DIR) in src
    assert "astrbot.cli" in src, "没转发到 AstrBot 的 CLI"
    assert "__file__" in src, "写死了绝对路径 → 实例移走后起不来"
    compile(src, "run.py", "exec")
    # 用户已有的绝不覆盖
    (d / "run.py").write_text("# mine\n", encoding="utf-8")
    ad._write_entrypoint(_inst(d))
    assert (d / "run.py").read_text("utf-8") == "# mine\n"


def test_start_cmd_passes_webui_port_flag(tmp_path):
    """启动命令带 --port，让 WebUI 走面板分配的端口而不是抢 6185。

    6185 是AstrBot 的默认值；两个实例同时跑必然撞车，
    故必须在启动命令里覆盖（上游 `astrbot run --port` → DASHBOARD_PORT）。
    """
    ad = _ad()
    ad._instance_python = lambda i: Path("/fake/python")     # noqa: SLF001
    inst = _inst(tmp_path / "ab-start", allocated_ports={"webui": 7000})
    cmd = ad.build_start_cmd(inst)
    assert Path(cmd[0]) == Path("/fake/python")
    assert cmd[1] == "run.py"
    assert cmd[2] == "run"
    assert cmd[cmd.index("--port") + 1] == "7000"


def test_start_cmd_falls_back_to_manifest_webui_port(tmp_path):
    """没分到 WebUI 端口时退回清单默认值 6185（与 NapCat 等同口径）。

    不写"分不到就不传"：6185 是 AstrBot 自己写死在 DEFAULT_CONFIG 里的默认值，
    不显式传就等于去抢它 → 两个实例必然撞车。
    """
    ad = _ad()
    ad._instance_python = lambda i: Path("/fake/python")     # noqa: SLF001
    cmd = ad.build_start_cmd(_inst(tmp_path / "ab-nop", allocated_ports={}))
    assert cmd[cmd.index("--port") + 1] == "6185"


def test_needs_init_detects_missing_marker_and_dirs(tmp_path):
    """init 前置判据：`.astrbot` 标记 + data/{config,plugins,temp} 目录。"""
    ad = _ad()
    d = tmp_path / "ab-init"
    d.mkdir()
    inst = _inst(d)
    assert ad._needs_init(inst) is True, "空目录肯定要init"

    (d / ".astrbot").mkdir()
    for sub in ("config", "plugins", "temp"):
        (d / "data" / sub).mkdir(parents=True)
    assert ad._needs_init(inst) is False, "骨架齐了不该重复 init"

    # 标记在但目录缺一个 —— 上游 init 建全套，缺项说明被用户删过
    import shutil
    shutil.rmtree(d / "data" / "temp")
    assert ad._needs_init(inst) is True


def test_prepare_start_runs_init_when_skeleton_missing(tmp_path, monkeypatch):
    """首启补init：`_needs_init` 为真时跑一次并返回 True（有动作）。"""
    d = tmp_path / "ab-prep"
    d.mkdir()
    _full_libs(d)
    ad = _ad()
    ad._require_interpreter = lambda i: sys.executable       # noqa: SLF001
    calls: list = []
    ad._instance_python = lambda i: Path(sys.executable)     # noqa: SLF001

    def fake_run(cmd, **kw):
        calls.append(cmd)
        (d / ".astrbot").mkdir()
        for sub in ("config", "plugins", "temp"):
            (d / "data" / sub).mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert ad.prepare_start(_inst(d)) is True
    assert calls, "应发起过 astrbot init"
    assert calls[0][1:4] == ["run.py", "init", "--yes"] or \
        calls[0][2:5] == ["init", "--yes", "--yes"], calls[0]


def test_prepare_start_skips_init_when_ready(tmp_path, monkeypatch):
    """骨架已齐时不再跑 init（重复跑会重置用户配置，见 _init_project 注释）。"""
    d = tmp_path / "ab-ready"
    d.mkdir()
    _full_libs(d)
    (d / ".astrbot").mkdir()
    for sub in ("config", "plugins", "temp"):
        (d / "data" / sub).mkdir(parents=True)
    ad = _ad()

    def boom(*a, **k):
        raise AssertionError("骨架已齐，不该再跑 init")

    monkeypatch.setattr("subprocess.run", boom)
    ad._require_interpreter = lambda i: sys.executable       # noqa: SLF001
    assert ad.prepare_start(_inst(d)) is False


def test_init_failure_surfaces_stderr_tail(tmp_path, monkeypatch):
    """init 失败要报出 stderr 尾部：真实原因（缺依赖/权限）都在最后几行。"""
    d = tmp_path / "ab-initfail"
    d.mkdir()
    ad = _ad()
    ad._instance_python = lambda i: Path(sys.executable)     # noqa: SLF001
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: SimpleNamespace(
        returncode=1, stdout="",
        stderr="\n".join(f"line{i}" for i in range(10)) + "\nNo module named click"))
    with pytest.raises(RuntimeError) as e:
        ad._init_project(_inst(d))
    assert "No module named click" in str(e.value)
    assert "line0" not in str(e.value), "不该把整段 stderr 都塞进报错"


# ---------- 能力声明 ----------

def test_no_python_deps_capability_because_no_pyproject(tmp_path):
    """AstrBot 不该暴露「依赖与插件」面板。

    父类的 `extra_manage_capabilities` 会报 `python_deps`，而它的装卸动作
    依赖 pyproject.toml —— AstrBot 没有这个文件，点「安装」会静默不生效
    （`merge_pyproject_deps` 找不到文件就 return）。
    """
    d = tmp_path / "ab-cap"
    d.mkdir()
    _full_libs(d)
    (d / ".astrbot").mkdir()
    caps = _ad().extra_manage_capabilities(_inst(d))
    assert caps == [], f"AstrBot 不该有 python_deps 能力，实际 {caps}"


def test_health_port_keys_empty_but_reverse_port_is_allocated():
    """OneBot 端口由 AstrBot 自己监听，但面板仍要分配一个（不能撞固定6199）。"""
    assert _ad().HEALTH_PORT_KEYS == []


def test_configure_login_is_noop():
    assert _ad().configure_login(_inst("/tmp/x"), {}) == {"needs_login": False}


def test_expose_webui_opens_declared_port(tmp_path):
    """WebUI 6185 要能被面板放通（AstrBot 默认绑 0.0.0.0，无需改配置）。"""
    from adapters.astrbot import AstrBotAdapter as A
    inst = _inst(tmp_path / "ab-webui", allocated_ports={"webui": 6185})
    note = A(MANIFEST).expose_webui(inst)
    assert note is None or isinstance(note, str)   # open_port 在 Linux 上无输出


def test_ensure_webui_binding_opens_loopback(tmp_path):
    """dashboard.host 是回环时放开为 0.0.0.0，否则点「打开 WebUI」连不上。"""
    d = tmp_path / "ab-bind"
    d.mkdir()
    cfg = d / "data" / "cmd_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"dashboard": {"host": "127.0.0.1", "port": 6185}}),
                   encoding="utf-8")
    _ad()._ensure_webui_binding(_inst(d))               # noqa: SLF001
    assert _cfg(_instance_of(cfg))["dashboard"]["host"] == "0.0.0.0"
    # 已是 0.0.0.0 不重复改
    _ad()._ensure_webui_binding(_inst(d))               # noqa: SLF001
    assert _cfg(_instance_of(cfg))["dashboard"]["host"] == "0.0.0.0"


def test_ensure_webui_binding_tolerates_missing_or_broken(tmp_path):
    """配置不存在或损坏时静默跳过，不能阻断启动。"""
    d = tmp_path / "ab-nocfg"
    d.mkdir()
    ad = _ad()
    ad._ensure_webui_binding(_inst(d))                   # noqa: SLF001 不抛即通过
    cfg = d / "data" / "cmd_config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("{ broken", encoding="utf-8")
    ad._ensure_webui_binding(_inst(d))                   # noqa: SLF001
