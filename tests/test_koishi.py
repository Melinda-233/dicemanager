"""Koishi（骰子端，npm_project 策略）测试。

与 nonebot2/astrbot 的关键差异（也是全部测试点）：

1. **语言栈是 Node 不是 Python** —— 所以不能继承 NoneBot2Adapter，
   解释器探测/依赖目录/配置格式/启动命令全都不同。
2. **脚手架必须带 `-y`**：create-koishi 用 prompts 交互，面板无 TTY，
   不带会挂死。⚠️ 且 `-y` 会连依赖安装一起跳过，故装依赖是我们自己的步骤。
3. **OneBot 方向是反向 WS**（与 AstrBot 同）：`protocol: ws-reverse`。
   这个取值是 Koishi 特有的，写成 `ws` + `websocket:` 段是**实现端**的写法，
   装上去连不上。
4. **默认模板不含 OneBot 适配器**（给的是 qq/discord/telegram），
   清单必须显式声明。
5. **selfId 是 required** —— 留空插件加载不了，故要在提示里告知用户去填。

依赖安装真跑 npm（要几分钟 + 网络），故全部 monkeypatch，只验
调用顺序与参数拼装；这与 nonebot2/astrbot 的测试口径一致。
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.koishi import KoishiAdapter, _merge_onebot_yaml
from conftest import exe_name

ROOT = Path(__file__).resolve().parent.parent


def _manifest() -> dict:
    """读当前 edition 对应的清单（server / _win 两侧同构）。"""
    from core.edition import is_desktop
    name = "koishi_win.json" if is_desktop() else "koishi.json"
    text = "\n".join(x for x in (ROOT / "manifests" / name).read_text("utf-8").splitlines()
                     if not x.strip().startswith("//"))
    return json.loads(text)


MANIFEST = _manifest()
NODE = sys.executable      # 冒充 node：测试只关心「用哪个解释器」，不真跑


def _inst(d, **kw) -> SimpleNamespace:
    base = dict(dir=str(d), allocated_ports={}, actual_port=None, conn_token=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _ad() -> KoishiAdapter:
    return KoishiAdapter(MANIFEST)


def _ready_dir(d: Path, deps: set[str] | None = None) -> Path:
    """造一个「已部署好」的实例目录：package.json + node_modules + koishi.yml + CLI。"""
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.json").write_text(json.dumps({
        "name": "koishi-app", "private": True,
        "dependencies": {"koishi": "^4.18.0",
                         "@koishijs/plugin-adapter-onebot": "^6.0.0"},
    }), encoding="utf-8")
    (d / "koishi.yml").write_text("plugins:\n  group:server:\n    server:\n"
                                  "      port: 5140\n", encoding="utf-8")
    for name in (deps if deps is not None
                 else set(MANIFEST["dependencies"])):
        (d / "node_modules" / name).mkdir(parents=True, exist_ok=True)
    (d / "node_modules" / ".bin").mkdir(parents=True, exist_ok=True)
    (d / "node_modules" / ".bin" / "koishi.cmd").write_text("@echo off\n")
    (d / "node_modules" / ".bin" / "koishi").write_text("#!/bin/sh\n")
    return d


# ---------- 清单契约 ----------

def test_manifest_declares_npm_project():
    assert MANIFEST["name"] == "koishi"
    assert MANIFEST["download_strategy"] == "npm_project"
    assert MANIFEST["config_path"] == "koishi.yml"
    assert MANIFEST["node_candidates"], "node 候选不能为空，否则永远探测不到"


def test_manifest_requires_onebot_adapter_explicitly():
    """⚠️ 回归（2026-10-05 核实 npm registry）：默认模板**不含** OneBot 适配器。

    模板 @koishijs/boilerplate 给的是 qq/discord/telegram/kook/satori 等 39 个包，
    其中没有 @koishijs/plugin-adapter-onebot。漏声明就没有 OneBot 通道，
    而部署阶段一路绿灯——只有清单测试拦得住。
    """
    assert "@koishijs/plugin-adapter-onebot" in MANIFEST["dependencies"]
    assert "koishi" in MANIFEST["dependencies"], \
        "onebot 适配器 peerDeps 要 koishi ^4.14.6，必须一起钉住"


def test_manifest_declares_node_18_floor():
    """Node ≥18（官方推荐 LTS；20+ 更稳）。填低会让用户装到 16 而 npm install 失败。"""
    assert tuple(MANIFEST["node_requires"][0]) == (18, 0)


def test_manifest_required_files_cover_node_modules():
    """node_modules 必须在校验里：缺它实例一起来就 "Cannot find module 'koishi'"。"""
    assert "node_modules" in MANIFEST["required_files"]
    assert "package.json" in MANIFEST["required_files"]
    assert MANIFEST["exe"] == "package.json"
    assert exe_name("koishi") not in MANIFEST["required_files"], (
        "Koishi 入口是 CLI 脚本而非可执行文件，别按平台分化 required_files")


def test_manifest_scaffold_points_at_npm_registry():
    """上游 GitHub release 零资产（核实过），故只能走 npm registry 的脚手架。

    ⚠️ `scaffold` 存的是**短名** `koishi`：`npm create` 自己会补 `create-` 前缀，
    清单里写完整名 `create-koishi` 会变成 `create-create-koishi` → npm 404。
    （这个坑是端到端真跑抓出来的。）
    """
    sk = MANIFEST["skeleton"]
    assert sk["scaffold"] == "koishi", "应存短名，让 npm create 补前缀"
    assert sk["template"] == "@koishijs/boilerplate"
    assert sk["registry"].startswith("https://registry.")


def test_manifest_node_modules_not_in_backup():
    """node_modules 不进备份：几百 MB 且可重装，打进包里会传不动。"""
    assert "node_modules" not in MANIFEST["save_keep_dir"]
    assert "node_modules" not in MANIFEST["data_paths"]


def test_registry_loads_koishi():
    from adapters import ALLOWED_STRATEGY, load_registry
    assert "npm_project" in ALLOWED_STRATEGY
    reg = load_registry(ROOT / "manifests")
    manifest, cls = reg["koishi"]
    assert cls is KoishiAdapter
    assert manifest["download_strategy"] == "npm_project"


def test_koishi_is_not_someone_elses_login_end():
    from adapters import load_registry
    reg = load_registry(ROOT / "manifests")
    assert "koishi" not in {t for m, _ in reg.values()
                            for t in (m.get("compatible_login") or [])
                            if t != "builtin"}


# ---------- 依赖完整性判据 ----------

def test_deps_installed_requires_all_declared(tmp_path):
    """判据是「清单声明的**全部**在 node_modules 里」，不是「目录非空」。

    用 any 的话，只装了 koishi 本体、没装 onebot 适配器也会被判装好，
    而实例启动后连不上登录端——最难排查的一类故障。
    """
    d = tmp_path / "kb-deps"
    d.mkdir()
    ad = _ad()
    inst = _inst(d)
    assert ad._deps_installed(inst) is False, "连 node_modules 都没有"

    _ready_dir(d, deps={"koishi"})
    assert ad._deps_installed(inst) is False, "只装本体不算装好（缺 onebot 适配器）"
    assert "@koishijs/plugin-adapter-onebot" in ad._deps_missing(inst)

    _ready_dir(d)                # 全齐
    assert ad._deps_installed(inst) is True
    assert ad._deps_missing(inst) == []


def test_verify_required_flags_empty_node_modules(tmp_path):
    """空 node_modules 必须报错——基类只查 exists()，空目录会被判成「装好了」。

    npm 装到一半失败会留下空的 node_modules/，基类的 .exists() 对空目录
    返回 True，于是 deploy 误判幂等返回 ok，用户到启动时才见
    "Cannot find module"。这是 Koishi 最容易踩的假成功。
    """
    d = tmp_path / "kb-empty"
    d.mkdir()
    (d / "package.json").write_text("{}", encoding="utf-8")
    (d / "koishi.yml").write_text("plugins: {}\n", encoding="utf-8")
    (d / "node_modules").mkdir()               # 空的
    missing = _ad().verify_required(_inst(d))
    assert any("node_modules" in m for m in missing), missing


# ---------- 启动 ----------

def test_build_start_cmd_uses_node_and_cli(tmp_path):
    """启动命令 = [<node>, <node_modules/.bin/koishi[.cmd]>, "start"]。

    走 CLI 而非 `npm start`：后者多一层 node+npm 进程，npm 会吞信号 →
    停止实例时留孤儿。Windows 上必须用 .cmd。
    """
    d = _ready_dir(tmp_path / "kb-start")
    ad = _ad()
    ad._require_node = lambda i: NODE                           # noqa: SLF001
    cmd = ad.build_start_cmd(_inst(d))
    assert Path(cmd[0]) == Path(NODE)
    assert cmd[1].endswith(("koishi", "koishi.cmd"))
    assert cmd[2] == "start"


def test_build_start_cmd_errors_when_cli_missing(tmp_path):
    """node_modules 装坏（无 CLI）时要给可操作报错，而不是启动时才崩。"""
    d = tmp_path / "kb-nocli"
    d.mkdir()
    ad = _ad()
    ad._require_node = lambda i: NODE                           # noqa: SLF001
    with pytest.raises(RuntimeError) as e:
        ad.build_start_cmd(_inst(d))
    assert "node_modules" in str(e.value)
    assert "重新部署" in str(e.value)


def test_prepare_start_reinstalls_when_deps_missing(tmp_path, monkeypatch):
    """面板重启后 resume 也走 prepare_start：缺依赖要补装一次。"""
    d = tmp_path / "kb-prep"
    d.mkdir()
    ad = _ad()
    ad._require_node = lambda i: NODE                           # noqa: SLF001
    calls: list = []
    ad._npm = lambda node: "npm"                                # noqa: SLF001
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: (
        calls.append(cmd) or SimpleNamespace(returncode=0, stdout="", stderr="")))
    assert ad.prepare_start(_inst(d)) is True
    assert calls and "install" in calls[0], calls


def test_prepare_start_skips_when_ready(tmp_path):
    d = _ready_dir(tmp_path / "kb-ready")
    ad = _ad()
    ad._require_node = lambda i: NODE                           # noqa: SLF001
    assert ad.prepare_start(_inst(d)) is False, "依赖齐了不该重装"


# ---------- 脚手架 ----------

def test_scaffold_passes_yes_flag_and_template(tmp_path, monkeypatch):
    """⚠️ 必须带 `-y`：create-koishi 用 prompts 交互，面板无 TTY 会挂死。"""
    d = tmp_path / "kb-scaf"
    d.mkdir()
    ad = _ad()
    ad._require_node = lambda i: NODE                           # noqa: SLF001
    ad._npm = lambda node: "npm"                                # noqa: SLF001
    calls: list = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        (d / "package.json").write_text("{}", encoding="utf-8")  # 模拟脚手架产出
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    ad._scaffold(_inst(d))
    assert calls, "应发起过脚手架"
    cmd = calls[0]
    assert "@koishijs/boilerplate" in " ".join(cmd)
    # ⚠️ 必须用 `npm create`（按原样解析包名），不能用 `npm init`
    #（会给 `create-koishi` 再补一层 `create-` 前缀 → create-create-koishi → 404）
    assert cmd[1] == "create", f"应用 npm create 而非 npm init：{cmd}"
    assert "create-create-koishi" not in " ".join(cmd), "包名前缀重复了"
    assert cmd[2].startswith("koishi@"), cmd[2]
    # ⚠️ 位置参数必须是 `.`（服务器实测：给实例名会多建一层同名子目录）
    assert "." in cmd[3:], f"位置参数应为 `.`（=当前目录），实际 {cmd[3:]}"
    assert not any(a == "koishi-app" for a in cmd), \
        "位置参数给了实例名 → 产物落在 <实例>/koishi-app/ 而不是实例目录"


def test_scaffold_closes_stdin_to_avoid_hanging(tmp_path, monkeypatch):
    """⚠️ **stdin 必须接 DEVNULL** —— 这才是挂死的真正原因（服务器实测）。

    create-koishi 最后一问（`? Install and start it now?`）**不受 --yes 影响**
    （npm 把自己那个 --yes 吃掉了，不转发给脚手架脚本）。
    实测对比：stdin 开着 → 永久挂住直到超时；stdin 接 DEVNULL → 1.7 秒返回，
    拿到的模板完全正确。我们本来就要自己装依赖，它问不问无所谓。
    """
    d = tmp_path / "kb-stdin"
    d.mkdir()
    ad = _ad()
    ad._require_node = lambda i: NODE                           # noqa: SLF001
    ad._npm = lambda node: "npm"                                # noqa: SLF001
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        (d / "package.json").write_text("{}", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    ad._scaffold(_inst(d))                                      # noqa: SLF001
    import subprocess
    assert seen.get("stdin") == subprocess.DEVNULL, \
        "没关 stdin → 挂在脚手架的交互提问上（实测永久挂住）"


def test_scaffold_is_idempotent(tmp_path, monkeypatch):
    """已有 package.json 就不重跑——重跑会清空用户的 node_modules。"""
    d = _ready_dir(tmp_path / "kb-idem")
    ad = _ad()

    def boom(*a, **k):
        raise AssertionError("已装好，不该重跑脚手架")

    monkeypatch.setattr("subprocess.run", boom)
    ad._scaffold(_inst(d))                                      # 不抛即通过


def test_scaffold_failure_surfaces_output(tmp_path, monkeypatch):
    """脚手架失败要报出尾部输出（真实原因都在最后几行）。"""
    d = tmp_path / "kb-scaffail"
    d.mkdir()
    ad = _ad()
    ad._require_node = lambda i: NODE                           # noqa: SLF001
    ad._npm = lambda node: "npm"                                # noqa: SLF001
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: SimpleNamespace(
        returncode=1, stdout="", stderr="\n".join(f"line{i}" for i in range(8))
        + "\nE404 registry not found"))
    with pytest.raises(RuntimeError) as e:
        ad._scaffold(_inst(d))
    assert "E404 registry not found" in str(e.value)
    assert "line0" not in str(e.value)


def test_npm_install_timeout_gives_actionable_error(tmp_path, monkeypatch):
    """npm install 超时要给可操作建议，不能抛裸的 TimeoutExpired。

    TimeoutExpired 里只有"command timed out after 1800 seconds"，
    用户看不出该做什么（换镜像源？重试？）。实测过卡死场景，故必须有这句。
    """
    import subprocess
    d = tmp_path / "kb-timeout"
    d.mkdir()
    ad = _ad()
    ad._require_node = lambda i: NODE                           # noqa: SLF001
    ad._npm = lambda node: "npm"                                # noqa: SLF001

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1800)

    monkeypatch.setattr("subprocess.run", boom)
    with pytest.raises(RuntimeError) as e:
        ad._ensure_deps(_inst(d))                              # noqa: SLF001
    msg = str(e.value)
    assert "超时" in msg
    assert "npmmirror" in msg, "超时提示要含换镜像源的可操作建议"


def test_npm_install_passes_retry_and_timeout_flags(tmp_path, monkeypatch):
    """npm 的 fetch 超时/重试参数要给足。

    默认 fetch-timeout 无上限，网络卡住时整个部署会挂死到面板超时
    （本项目 E2E 实测：单个包卡住 → 18 分钟无进展）。
    """
    d = tmp_path / "kb-flags"
    d.mkdir()
    ad = _ad()
    ad._require_node = lambda i: NODE                           # noqa: SLF001
    ad._npm = lambda node: "npm"                                # noqa: SLF001
    calls: list = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: (
        calls.append(cmd) or SimpleNamespace(returncode=0, stdout="", stderr="")))
    ad._ensure_deps(_inst(d))                                  # noqa: SLF001
    assert calls, "应发起过 npm install"
    cmd = " ".join(calls[0])
    assert "--fetch-retries=4" in cmd
    assert "--fetch-timeout=300000" in cmd, "缺单请求超时上限 → 卡死会挂住整体"


# ---------- package.json 依赖合并 ----------

def test_merge_deps_writes_missing_only(tmp_path):
    """清单声明项要写进 package.json，否则换机/重装后依赖就没了。"""
    d = tmp_path / "kb-pkg"
    d.mkdir()
    (d / "package.json").write_text(json.dumps(
        {"name": "x", "dependencies": {"koishi": "^4.18.0"}}), encoding="utf-8")
    ad = _ad()
    ad._merge_deps_to_pkgjson(_inst(d))                          # noqa: SLF001
    deps = json.loads((d / "package.json").read_text("utf-8"))["dependencies"]
    assert "@koishijs/plugin-adapter-onebot" in deps
    assert deps["koishi"] == "^4.18.0", "不该覆盖模板自己钉的版本"


def test_merge_deps_tolerates_broken_json(tmp_path):
    """配置损坏时静默跳过，不能阻断部署。"""
    d = tmp_path / "kb-badpkg"
    d.mkdir()
    (d / "package.json").write_text("{ broken", encoding="utf-8")
    _ad()._merge_deps_to_pkgjson(_inst(d))                       # noqa: SLF001
    assert (d / "package.json").read_text("utf-8") == "{ broken"


# ---------- 互联配置（koishi.yml 的 plugins 段） ----------

def test_write_conn_config_uses_ws_reverse(tmp_path):
    """反向 WS 的 protocol 取值是 **`ws-reverse`**（Koishi 特有）。

    ⚠️ 不是 `ws` + 单独的 `websocket:` 段——那是 NapCat 等「实现端」的写法。
    写错的表征是「部署成功但连不上」，日志里只看得到连接超时。
    字段名逐字取自 @satorijs/adapter-onebot@6.0.2 的 Config schema。
    """
    d = _ready_dir(tmp_path / "kb-conn")
    r = _ad().write_conn_config(_inst(d, allocated_ports={"ob11": 7199}),
                                "ob11", "reverse", "127.0.0.1:3001", "TOK")
    assert r.ok, r.manual
    y = (d / "koishi.yml").read_text("utf-8")
    assert "adapter-onebot:" in y
    assert "protocol: ws-reverse" in y, y
    assert "port: 7199" in y
    assert "path: /onebot" in y
    assert "TOK" in y
    # 模板原有的配置不能被抹掉
    assert "group:server" in y
    assert "5140" in y
    assert "selfId" in y, "selfId 是 required，漏了插件加载不了"
    assert "重启" in r.manual


def test_write_conn_config_rejects_forward(tmp_path):
    """Koishi 只支持反向 WS，给 forward 必须报错而不是默默写坏。"""
    d = _ready_dir(tmp_path / "kb-fwd")
    r = _ad().write_conn_config(_inst(d), "ob11", "forward", "127.0.0.1:3001", "T")
    assert r.ok is False
    assert "反向" in r.manual


def test_write_conn_config_rejects_milky(tmp_path):
    d = _ready_dir(tmp_path / "kb-mk")
    r = _ad().write_conn_config(_inst(d), "milky", "reverse", "127.0.0.1:3000", "T")
    assert r.ok is False and "Milky" in r.manual


def test_write_conn_config_is_idempotent(tmp_path):
    """重复写不追加：YAML 同名键后者覆盖前者，追加会攒出多份、表现为「改了没生效」。"""
    d = _ready_dir(tmp_path / "kb-idem2")
    ad = _ad()
    for token in ("T1", "T2"):
        ad.write_conn_config(_inst(d), "ob11", "reverse", "127.0.0.1:3001", token)
    y = (d / "koishi.yml").read_text("utf-8")
    assert y.count("adapter-onebot:") == 1, y
    assert "T2" in y and "T1" not in y


def test_write_conn_config_creates_file_when_absent(tmp_path):
    """配置不存在时创建（Koishi 首启会自己生成，但我们要能提前写好）。"""
    d = tmp_path / "kb-nocfg"
    d.mkdir()
    r = _ad().write_conn_config(_inst(d), "ob11", "reverse", "127.0.0.1:3001", "T")
    assert r.ok, r.manual
    assert (d / "koishi.yml").is_file()


def test_yaml_merge_preserves_other_plugins(tmp_path):
    """只动目标插件段，其余插件与注释原样保留。"""
    src = ("plugins:\n"
           "  group:server:\n"
           "    server:\n"
           "      port: 5140\n"
           "  ~adapter-discord: {}\n"
           "  commands: {}\n")
    out = tmp_path / "koishi.yml"
    out.write_text(src, encoding="utf-8")
    _merge_onebot_yaml(out, 7199, "TOK", "/onebot")
    y = out.read_text("utf-8")
    assert "~adapter-discord" in y
    assert "commands" in y
    assert "port: 5140" in y
    assert "adapter-onebot" in y and "port: 7199" in y


def test_yaml_merge_appends_when_no_plugins_section(tmp_path):
    """没有 plugins: 段时整体追加。"""
    out = tmp_path / "koishi.yml"
    out.write_text("# 我的配置\nfoo: bar\n", encoding="utf-8")
    _merge_onebot_yaml(out, 7199, "", "/onebot")
    y = out.read_text("utf-8")
    assert "# 我的配置" in y
    assert "foo: bar" in y
    assert "plugins:" in y
    assert "adapter-onebot" in y
    assert "token: ''" in y, "无 token 时也要写出来（覆盖旧值）"


def test_yaml_merge_preserves_sibling_plugin_groups(tmp_path):
    """回归（2026-10-05 真YAML 解析验证抓到的）：替换不能吃掉同级的其它段。

    Koishi 的插件都缩进两空格，而 `group:xxx:` 这些**分组键也是两空格**
    （与插件名同级）。早期实现把段边界判成「下一个行首无缩进的键」，找不到就
    替换到文件末 —— 结果把 `group:adapter` 整段（含 ~adapter-discord、
    ~adapter-qq、database-sqlite）全吃掉了。表现为「面板写完配置后别的插件
    凭空消失」，且因为 YAML 仍合法，**不会报错**，只能靠对比发现。

    正确判据：边界是**与目标键同缩进**的下一个键。
    """
    src = (
        "plugins:\n"
        "  group:server:\n"
        "    server:\n"
        "      port: 5140\n"
        "  group:adapter:\n"
        "    ~adapter-discord: {}\n"
        "    ~adapter-qq: {}\n"
        "    database-sqlite:\n"
        "      path: data/koishi.db\n"
        "  adapter-onebot:\n"
        "    protocol: ws-reverse\n"
        "    path: /onebot\n"
        "    port: 1111\n"
        "    token: |\n"
        "      OLD\n"
    )
    out = tmp_path / "koishi.yml"
    out.write_text(src, encoding="utf-8")
    _merge_onebot_yaml(out, 2222, "NEW", "/onebot")
    y = out.read_text("utf-8")
    assert "group:adapter:" in y, "group:adapter 整段被吃掉了"
    assert "~adapter-discord" in y
    assert "~adapter-qq" in y
    assert "data/koishi.db" in y
    assert "group:server" in y
    assert y.count("adapter-onebot:") == 1
    assert "port: 2222" in y and "port: 1111" not in y
    assert "NEW" in y and "OLD" not in y


def test_yaml_merge_replaces_old_section_in_place(tmp_path):
    """重复写入是**替换**整段，不会留下上一次的残留键。"""
    out = tmp_path / "koishi.yml"
    out.write_text("plugins:\n", encoding="utf-8")
    _merge_onebot_yaml(out, 1111, "OLD", "/onebot")
    _merge_onebot_yaml(out, 2222, "NEW", "/onebot")
    y = out.read_text("utf-8")
    assert y.count("adapter-onebot:") == 1
    assert "port: 2222" in y and "port: 1111" not in y
    assert "NEW" in y and "OLD" not in y


def test_manage_capability_exposes_python_deps(tmp_path):
    """Koishi 要暴露「依赖与插件」面板 —— kind 与 nonebot2 一致，前端同一套渲染。"""
    d = _ready_dir(tmp_path / "kb-cap")
    caps = _ad().extra_manage_capabilities(_inst(d))
    assert [c["kind"] for c in caps] == ["python_deps"]
    assert caps[0].get("deployed") is True


def test_manage_capability_marks_undeployed(tmp_path):
    d = tmp_path / "kb-cap2"
    d.mkdir()
    caps = _ad().extra_manage_capabilities(_inst(d))
    assert caps[0].get("requires_deployed") is True
    assert "部署" in caps[0].get("disabled_reason", "")


# ---------- 升级通道 ----------

def test_upgrade_runs_npm_update_and_raises_floor(tmp_path, monkeypatch):
    """升级 = `npm update`（尊重 package.json 的范围），不是 `--latest`。

    `--latest` 会无视版本范围直接跳最新，可能引入破坏性变更；
    `npm update` 是「在声明的兼容范围内取最新」，与 nonebot2 的
    `pip install -U` 口径一致。

    升完必须把已装版本抬进package.json，否则下次 npm install 会降回去。
    """
    from adapters.base import DEPLOY_VERSION
    d = _ready_dir(tmp_path / "kb-up")
    ad = _ad()
    ad._require_node = lambda i: NODE# noqa: SLF001
    ad._npm = lambda node: "npm"                                  # noqa: SLF001
    calls: list = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: (
        calls.append(cmd) or SimpleNamespace(returncode=0, stdout="", stderr="")))
    # 模拟 upgrade 后实装版本变了
    (d / "node_modules" / "koishi" / "package.json").write_text(
        json.dumps({"name": "koishi", "version": "4.19.1"}), encoding="utf-8")

    inst = _inst(d, id="kb-up")
    try:
        tag = ad.upgrade(inst)
    finally:
        DEPLOY_VERSION.pop("kb-up", None)
    assert calls, "应发起过 npm"
    assert "update" in calls[0]
    assert "--latest" not in " ".join(calls[0]), \
        "npm update --latest 会无视版本范围，引入破坏性变更"
    assert tag and tag.startswith("npm:")
    deps = json.loads((d / "package.json").read_text("utf-8"))["dependencies"]
    assert deps["koishi"] == "^4.19.1", \
        f"下限没抬到实装版本（实际 {deps['koishi']}）→ 下次 install 会降回去"


def test_upgrade_refuses_when_not_deployed(tmp_path):
    d = tmp_path / "kb-up2"
    d.mkdir()
    with pytest.raises(RuntimeError) as e:
        _ad().upgrade(_inst(d))
    assert "重新部署" in str(e.value)


def test_raise_floor_preserves_range_type():
    """抬下限要保留原有的范围类型，否则把 `~1.2` 变成 `^1.2` 会放宽兼容范围。"""
    from adapters.koishi import _raise_floor
    assert _raise_floor("^1.2.3", "1.4.0") == "^1.4.0"
    assert _raise_floor("~1.2.3", "1.2.9") == "~1.2.9"
    assert _raise_floor(">=1.0.0", "1.4.0") == ">=1.4.0"
    assert _raise_floor("1.2.3", "1.4.0") == "^1.4.0", "精确版本升不动，要改范围"
    assert _raise_floor("*", "1.4.0") == "*", "无约束不动"
    assert _raise_floor("^4.18.0", "4.19.0-beta.1") == "^4.19.0-beta.1", \
        "预发布版本原样用：那才是实际跑着的版本"


def test_npm_name_keeps_scoped_package_intact():
    """scoped 包名里的 `@` 不是版本分隔符 —— 切错会把包名截断。"""
    from adapters.koishi import _npm_name, _spec_version
    assert _npm_name("@koishijs/plugin-adapter-onebot") == \
        "@koishijs/plugin-adapter-onebot"
    assert _npm_name("@koishijs/plugin-adapter-onebot@^6.0.0") == \
        "@koishijs/plugin-adapter-onebot"
    assert _spec_version("@koishijs/plugin-adapter-onebot@^6.0.0") == "^6.0.0"
    assert _npm_name("koishi-plugin-schedule@5.0.1") == "koishi-plugin-schedule"
    assert _spec_version("koishi-plugin-schedule@5.0.1") == "5.0.1"
    assert _spec_version("koishi") == ""


def test_is_koishi_plugin_distinguishes_core_deps():
    """核心依赖（koishi 本体）不算插件，否则用户能把它卸载掉→ 实例起不来。"""
    from adapters.koishi import _is_koishi_plugin
    assert _is_koishi_plugin("koishi-plugin-schedule")
    assert _is_koishi_plugin("@koishijs/plugin-adapter-onebot")
    assert not _is_koishi_plugin("koishi")
    assert not _is_koishi_plugin("@satorijs/adapter-onebot")
    assert not _is_koishi_plugin("typescript")


# ---------- 插件装卸 ----------

def test_install_package_syncs_pkgjson(tmp_path, monkeypatch):
    """装插件必须同时写 package.json —— 只 npm install 的话下次 install 会丢。"""
    d = _ready_dir(tmp_path / "kb-ins")
    ad = _ad()
    ad._require_node = lambda i: NODE# noqa: SLF001
    ad._npm = lambda node: "npm"                                  # noqa: SLF001
    calls: list = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        name = [a for a in cmd if a.startswith("koishi-plugin-")]
        if name:
            (d / "node_modules" / name[0]).mkdir(parents=True, exist_ok=True)
            (d / "node_modules" / name[0] / "package.json").write_text(
                json.dumps({"name": name[0], "version": "5.0.1"}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    ver = ad.install_package(_inst(d, id="kb-i"), "koishi-plugin-schedule@^5.0.0")
    assert ver == "5.0.1"
    deps = json.loads((d / "package.json").read_text("utf-8"))["dependencies"]
    assert "koishi-plugin-schedule" in deps, "装完没写进 package.json"
    assert "install" in calls[0] and "koishi-plugin-schedule" in calls[0]
    assert "koishi-plugin-schedule" in ad.list_plugins(_inst(d))["packages"][0]["name"] \
        or any(p["name"] == "koishi-plugin-schedule"
               for p in ad.list_plugins(_inst(d))["packages"])


def test_install_package_rejects_bad_spec(tmp_path):
    d = _ready_dir(tmp_path / "kb-ins2")
    ad = _ad()
    for spec in ("   ", "a b"):
        with pytest.raises(ValueError) as e:
            ad.install_package(_inst(d), spec)
        assert "包名" in str(e.value)


def test_uninstall_removes_from_pkgjson(tmp_path, monkeypatch):
    """卸载要同时删package.json 里的声明，否则下次 install 会装回来。"""
    d = _ready_dir(tmp_path / "kb-un")
    (d / "package.json").write_text(json.dumps({
        "name": "kb",
        "dependencies": {"koishi": "^4.18.0",
                         "koishi-plugin-schedule": "^5.0.0"},
    }), encoding="utf-8")
    (d / "node_modules" / "koishi-plugin-schedule").mkdir(parents=True)
    (d / "node_modules" / "koishi-plugin-schedule" / "package.json").write_text(
        json.dumps({"name": "koishi-plugin-schedule", "version": "5.0.1"}),
        encoding="utf-8")
    ad = _ad()
    ad._require_node = lambda i: NODE# noqa: SLF001
    ad._npm = lambda node: "npm"                                  # noqa: SLF001
    calls: list = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: (
        calls.append(cmd) or SimpleNamespace(returncode=0, stdout="", stderr="")))
    ad.uninstall_package(_inst(d, id="kb-un"), "koishi-plugin-schedule")
    assert "remove" in calls[0]
    deps = json.loads((d / "package.json").read_text("utf-8"))["dependencies"]
    assert "koishi-plugin-schedule" not in deps, "声明没删 → 下次 install 会装回来"
    assert "koishi" in deps, "不该动别的依赖"


def test_uninstall_rejects_not_declared(tmp_path):
    d = _ready_dir(tmp_path / "kb-un2")
    with pytest.raises(ValueError) as e:
        _ad().uninstall_package(_inst(d), "koishi-plugin-nope")
    assert "不在 package.json" in str(e.value)


def test_sync_pyproject_pulls_in_installed(tmp_path):
    """用户手工 `npm install --no-save` 装过插件 → sync 补写进 package.json。"""
    d = _ready_dir(tmp_path / "kb-sync")
    (d / "node_modules" / "koishi-plugin-schedule").mkdir(parents=True)
    (d / "node_modules" / "koishi-plugin-schedule" / "package.json").write_text(
        json.dumps({"name": "koishi-plugin-schedule", "version": "5.0.1"}),
        encoding="utf-8")
    # 已装版本读的是 node_modules/<pkg>/package.json，故每个包都要有
    for n in ("koishi", "@koishijs/plugin-adapter-onebot"):
        (d / "node_modules" / n / "package.json").write_text(
            json.dumps({"name": n, "version": "1.0.0"}), encoding="utf-8")
    r = _ad().sync_pyproject(_inst(d))
    assert r.endswith("package.json")
    deps = json.loads((d / "package.json").read_text("utf-8"))["dependencies"]
    assert deps["koishi-plugin-schedule"] == ">=5.0.1"


def test_list_plugins_separates_core_from_plugins(tmp_path):
    """核心依赖与插件要分开列：混在一起用户会以为能卸载 koishi 本体。"""
    d = _ready_dir(tmp_path / "kb-ls")
    (d / "package.json").write_text(json.dumps({
        "name": "kb",
        "dependencies": {"koishi": "^4.18.0",
                         "@koishijs/plugin-adapter-onebot": "^6.0.0",
                         "koishi-plugin-schedule": "^5.0.0"},
    }), encoding="utf-8")
    for n in ("koishi", "@koishijs/plugin-adapter-onebot",
              "koishi-plugin-schedule"):
        (d / "node_modules" / n).mkdir(parents=True, exist_ok=True)
        (d / "node_modules" / n / "package.json").write_text(
            json.dumps({"name": n, "version": "1.0.0"}), encoding="utf-8")
    (d / "plugins" / "my_local").mkdir(parents=True)
    (d / "plugins" / "__pycache__").mkdir()

    data = _ad().list_plugins(_inst(d))
    names = [p["name"] for p in data["packages"]]
    assert names == ["@koishijs/plugin-adapter-onebot", "koishi-plugin-schedule"]
    assert [p["name"] for p in data["core_deps"]] == ["koishi"]
    assert data["core_deps"][0].get("core") is True
    assert [p["name"] for p in data["dir_plugins"]] == ["my_local"]
    off = [p["name"] for p in data["packages"] if p.get("official")]
    assert off == ["@koishijs/plugin-adapter-onebot"], "官方包应可识别"


def test_version_tag_is_stable_and_changes_with_deps(tmp_path):
    """基线标签：依赖快照不变则标签不变；变了就变（供「已是最新」判定）。"""
    d = _ready_dir(tmp_path / "kb-tag")
    ad = _ad()
    t1 = ad._version_tag(_inst(d))                             # noqa: SLF001
    t2 = ad._version_tag(_inst(d))                             # noqa: SLF001
    assert t1 == t2 and t1.startswith("npm:")
    (d / "package.json").write_text(json.dumps({
        "name": "kb", "dependencies": {"koishi": "^4.19.0"}}), encoding="utf-8")
    assert ad._version_tag(_inst(d)) != t1# noqa: SLF001


def test_npm_failure_gives_actionable_error(tmp_path, monkeypatch):
    """npm 非零退出要带尾部输出，且提示镜像源（用户能据此行动）。"""
    d = _ready_dir(tmp_path / "kb-npmfail")
    ad = _ad()
    ad._require_node = lambda i: NODE# noqa: SLF001
    ad._npm = lambda node: "npm"                                  # noqa: SLF001
    monkeypatch.setattr("subprocess.run", lambda cmd, **kw: SimpleNamespace(
        returncode=1, stdout="", stderr="\n".join(f"l{i}" for i in range(6))
        + "\nERESOLVE conflicting peer dependency"))
    with pytest.raises(RuntimeError) as e:
        ad._run_npm(_inst(d), ["install", "x"], stage="安装")  # noqa: SLF001
    assert "ERESOLVE" in str(e.value)
    assert "l0" in str(e.value), "尾部输出要保留（真实原因就在最后几行）"
    assert len(str(e.value)) < 400, "报错要短，整段 stderr 塞进去反而看不清重点"


def test_registry_prefers_env_over_manifest(tmp_path, monkeypatch):
    """回归（2026-10-06 服务器 E2E 抓到）：用户设的镜像源必须优先于清单。

    实测：服务器上 `npm create` 走清单硬编码的官方源 **600 秒超时**，
    而用户早就设了 `npm_config_registry=npmmirror`。清单的 registry
    只能是兜底。
    """
    ad = _ad()
    inst = _inst(tmp_path / "kb-reg")
    # 没有任何用户意图→ 用清单兜底
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.delenv("DM_NPM_REGISTRY", raising=False)
    assert ad._registry(inst) == MANIFEST["skeleton"]["registry"]  # noqa: SLF001
    # npm_config_registry 优先
    monkeypatch.setenv("npm_config_registry", "https://registry.npmmirror.com")
    assert ad._registry(inst) == "https://registry.npmmirror.com"  # noqa: SLF001
    # DM_NPM_REGISTRY 次之（面板自己的旋钮）
    monkeypatch.delenv("npm_config_registry", raising=False)
    monkeypatch.setenv("DM_NPM_REGISTRY", "https://r.example.com")
    assert ad._registry(inst) == "https://r.example.com"  # noqa: SLF001
    # 两个都设时 npm_config_registry 赢（它更接近用户的直接意图）
    monkeypatch.setenv("npm_config_registry", "https://a.example.com")
    assert ad._registry(inst) == "https://a.example.com"  # noqa: SLF001


def test_clean_env_keeps_registry_drops_prefix(tmp_path, monkeypatch):
    """**不能**剔掉 `npm_config_registry`（用户的镜像源），要剔 prefix/globalconfig。"""
    ad = _ad()
    monkeypatch.setenv("npm_config_registry", "https://registry.npmmirror.com")
    monkeypatch.setenv("npm_config_prefix", "/home/me/.npm-global")
    monkeypatch.setenv("NODE_OPTIONS", "--inspect")
    env = ad._clean_env()                                # noqa: SLF001
    # ⚠️ Windows 上 os.environ 把键规范化成大写，故断言必须大小写不敏感
    lower = {k.lower(): v for k, v in env.items()}
    assert lower.get("npm_config_registry") == "https://registry.npmmirror.com", \
        "剔了它 = 无视用户设的镜像源（实测会卡到超时）"
    assert "npm_config_prefix" not in lower, \
        "prefix 会改变全局安装位置，不该让面板行为随「谁在跑」变化"
    assert "node_options" not in lower, "用户全局调试参数会打进我们的子进程"


# ---------- 能力声明 ----------

def test_health_port_keys_empty():
    """OneBot 端口在登录端侧（Koishi 是服务端），面板探不到，故置空。"""
    assert _ad().HEALTH_PORT_KEYS == []


def test_configure_login_is_noop():
    assert _ad().configure_login(_inst("/tmp/x"), {}) == {"needs_login": False}


def test_baseline_is_published_on_deploy(tmp_path, monkeypatch):
    """覆写 deploy() 就丢了基类末尾的 DEPLOY_VERSION 发布，必须自己做。

    不做的话 wizard 取不到基线，实例 version 永远是 None。
    """
    from adapters.base import DEPLOY_VERSION
    d = tmp_path / "kb-base"
    d.mkdir()
    ad = _ad()
    ad._require_node = lambda i: NODE                           # noqa: SLF001
    ad._npm = lambda node: "npm"                                # noqa: SLF001
    ad._scaffold = lambda i: (d / "package.json").write_text(   # noqa: SLF001
        json.dumps({"dependencies": {"koishi": "^4.18.0"}}), encoding="utf-8")

    def fake_run(cmd, **kw):
        for name in MANIFEST["dependencies"]:
            (d / "node_modules" / name).mkdir(parents=True, exist_ok=True)
        (d / "koishi.yml").write_text("plugins: {}\n", encoding="utf-8")
        (d / "node_modules" / ".bin").mkdir(parents=True, exist_ok=True)
        (d / "node_modules" / ".bin" / "koishi.cmd").write_text("@echo off\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    inst = _inst(d, id="kb-1")
    try:
        assert ad.deploy(inst) == "ok"
        assert DEPLOY_VERSION.get("kb-1", "").startswith("npm:"), \
            "基线未发布 → wizard 取不到版本"
    finally:
        DEPLOY_VERSION.pop("kb-1", None)


def test_deploy_reports_conflict_when_already_installed(tmp_path):
    """依赖已齐时返回 conflict，不重跑 npm——重装会丢掉用户装的额外插件状态。"""
    d = _ready_dir(tmp_path / "kb-conf")
    ad = _ad()
    assert ad.deploy(_inst(d, id="kb-2")) == "conflict"
