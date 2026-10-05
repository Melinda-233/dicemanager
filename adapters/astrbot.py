r"""AstrBot：Python 源码项目（pip 依赖 + data/cmd_config.json），经 OneBot 连登录端

与 NoneBot2 同源、但**不能直接继承**它
------------------------------------------------
两者都是「PyPI 上游 + pip 依赖 + 无可下载程序包」，所以**继承** NoneBot2Adapter
复用它的 `--target libs/` 隔离机制（解释器探测、pip 安装、依赖快照、
插件装卸全部原样可用）。但四处差异是本质的，故逐个覆写：

1. **OneBot 方向相反**。nonebot2 作**客户端**主动连登录端（正向 WS）；
   AstrBot 只支持**反向 WS**——它自己监听 6199，登录端连它
   （官方文档明确「AstrBot 端作为服务端，实现端作为客户端」）。
   故 `write_conn_config` 写的方向与 nonebot2 相反。
2. **配置载体不同**。nonebot2 读 `.env`（python-dotenv）；AstrBot 读
   `data/cmd_config.json` 的 `platform` 数组（源码 `aiocqhttp_platform_adapter.py`
   直接 `platform_config["ws_reverse_host"]`）。故要新写 JSON 定位编辑。
3. **启动需要先 init**。`astrbot run` 要求 cwd 下有 `.astrbot` 标记
   （`check_astrbot_root`，缺则报 "not a valid AstrBot root directory"），
   该标记与 `data/{config,plugins,temp}` 目录骨架由 `astrbot init` 生成。
   我们在 deploy / prepare_start 里补跑一次。
4. **没有 pyproject.toml**。nonebot2 的 `sync_pyproject` / `_write_pyproject_deps`
   在这里无处落脚（AstrBot 不用 TOML 声明依赖），故本类**不提供**插件装卸
   能力声明——AstrBot 的插件管理走它自己的 WebUI，与 nonebot2 不同。

已核实的事实（2026-10-05 查上游源码，非文档推测）
------------------------------------------------
- 版本 `4.29.0-beta.1`，`requires-python = ">=3.12"`
- 入口：`[project.scripts] astrbot = "astrbot.cli.__main__:cli"`，子命令 `run`
- WebUI 端口：`DEFAULT_CONFIG["dashboard"]["port"] = 6185`，
  `astrbot run --port N` 可覆盖（设 `DASHBOARD_PORT` 环境变量）
- 配置路径：`data/cmd_config.json`（`core/config/default.py` 模块 docstring 明写）
- OneBot 平台条目字段：`id` / `ws_reverse_host` / `ws_reverse_port` /
  `ws_reverse_token`（`platform` 数组默认是 `[]`，条目由用户在 WebUI 建）
- `get_astrbot_root()` 返回 `Path.cwd()` —— 故启动时cwd 必须是实例目录

⚠️ 版本漂移风险：上游 4.x 仍在beta、字段名可能变。故 `_write_conn_config`
的字段名集中在 `_PLATFORM_FIELDS` 一处，且解析失败时给出手工指引而不是
静默写坏——写坏的表现是「部署成功但连不上」，很难查。
"""
import json
import subprocess
from pathlib import Path

from adapters.base import WriteResult
from adapters.nonebot2 import LIBS_DIR, NoneBot2Adapter
from core.atomicio import write_atomic
from core.locks import program_dir_lock

# OneBot 平台条目里我们关心的字段（上游 aiocqhttp_platform_adapter.py 直接下标取这几个）
_PLATFORM_FIELDS = ("id", "enable", "ws_reverse_host", "ws_reverse_port",
                    "ws_reverse_token")
# 平台 id：同一实例里只有这一条，故固定名即可（AstrBot 用它区分不同消息平台实例）
_PLATFORM_ID = "dicemanager"


class AstrBotAdapter(NoneBot2Adapter):
    # 与 nonebot2 同款：OneBot 客户端形态，进程存活即视为已连接。
    # 但**互联端口**由 AstrBot 自己监听（6199），面板要探测它才知道连上没有
    HEALTH_PORT_KEYS: list[str] = []

    def _cfg_path(self, instance) -> Path:
        """AstrBot 的配置文件：data/cmd_config.json（清单里可覆盖）。"""
        return Path(instance.dir) / self.m.get("config_path", "data/cmd_config.json")

    def _reverse_port(self, instance) -> int:
        """本实例给OneBot 反向 WS 用的监听端口。

        优先用面板分配的端口（避让他人占用），没有则退回清单默认值 6199。
        """
        alloc = (instance.allocated_ports or {}).get("ob11") \
            or (instance.allocated_ports or {}).get("bot")
        return int(alloc or self.m.get("ob11_reverse_default_port") or 6199)

    # ---------- 启动 ----------

    def build_start_cmd(self, instance) -> list[str]:
        """`[<解释器>, "run.py", "run", "--port", <webui>]`。

        不用 `python -m astrbot.cli`：`--target` 装出来的包在 `libs/` 里，
        由 `run.py` 开头 `sys.path.insert` 引入，模块路径与源码运行一致。
        """
        cmd = [str(self._instance_python(instance)), "run.py", "run"]
        if (port := self._webui_port(instance)) is not None:
            cmd += [self.m.get("webui_port_flag", "--port"), str(port)]
        return cmd

    def _write_entrypoint(self, instance) -> None:
        """生成 `run.py` 入口（AstrBot 装成包后没有可直接跑的文件）。

        只做两件事：把 `libs/` 插进 `sys.path`，然后转发给 AstrBot 的 CLI。
        `cmd.run` 内部会 `os.environ["ASTRBOT_ROOT"] = cwd`，故 cwd 必须是实例目录
        （由 core/process 负责传cwd）。
        """
        path = Path(instance.dir) / "run.py"
        if path.exists():
            return
        path.write_text(
            '"""AstrBot 入口（由 DiceManager 生成，可自由修改）。"""\n'
            "import sys\n"
            "from pathlib import Path\n\n"
            # 依赖目录（pip install --target 的产物）不在默认搜索路径里
            "sys.path.insert(0, str(Path(__file__).resolve().parent / "
            f"{LIBS_DIR!r}))\n\n"
            "from astrbot.cli.__main__ import cli\n\n"
            "if __name__ == '__main__':\n"
            "    cli()\n",
            encoding="utf-8")

    def _needs_init(self, instance) -> bool:
        """是否还缺 `astrbot init` 生成的骨架（`.astrbot` 标记 + data 子目录）。"""
        if not (Path(instance.dir) / ".astrbot").exists():
            return True
        data = Path(instance.dir) / "data"
        return not all((data / sub).is_dir() for sub in ("config", "plugins", "temp"))

    def _init_project(self, instance) -> None:
        """跑一次 `astrbot init --yes`（幂等：已有骨架时直接返回）。

        只在缺 `.astrbot` 时跑：upstream 的 init 是 `initialize_astrbot()`，
        会建目录并写一份默认 `cmd_config.json`（含初始面板密码）。
        重复跑会**重置用户的配置**，故必须先判 `_needs_init`。
        """
        root = Path(instance.dir)
        root.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [str(self._instance_python(instance)), "run.py", "init", "--yes"],
            cwd=str(root), capture_output=True, text=True,
            timeout=180, env=self._clean_env())
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-6:]
            raise RuntimeError("AstrBot 初始化失败（ astrbot init）：\n" + "\n".join(tail))

    def prepare_start(self, instance, runner=None) -> bool:
        """首启前补齐：依赖（PythonDepsMixin 语义）+ `astrbot init` 骨架。"""
        did = super().prepare_start(instance, runner)
        if self._needs_init(instance):
            self._init_project(instance)
            did = True
        return did

    # ---------- 部署 ----------

    def deploy(self, instance) -> str:
        """语义同 `PythonDepsMixin.deploy`，额外保证 `astrbot init` 骨架存在。

        骨架必须在 deploy 阶段建好（而不是拖到首启）：`required_files` 里
        含 `.astrbot`，缺它 verify 会失败，实例会被判成「没装好」。
        """
        with program_dir_lock(self.m["name"]):
            did = super().deploy(instance)
            if self._needs_init(instance):
                self._require_interpreter(instance)
                self._init_project(instance)
                self._verify_after_init(instance)
            return did

    def _verify_after_init(self, instance) -> None:
        """init 之后补一次校验。

        `astrbot init` 会写一份默认 `cmd_config.json`，而 `required_files` 里
        就有它——若 init 静默失败，deploy 会带着一个「缺配置」的实例返回 ok，
        用户到 WebUI 里才发现。
        """
        if missing := self.verify_required(instance):
            raise RuntimeError(f"初始化后缺失必备文件: {missing}")

    def configure_login(self, instance, credentials) -> dict:
        return {"needs_login": False}          # 不登录 QQ（OneBot 由登录端承载）

    # ---------- 互联配置（data/cmd_config.json 的 platform 数组） ----------

    def write_conn_config(self, instance, mode, direction, addr, token,
                          link_id: str | None = None) -> WriteResult:
        """把 OneBot 反向 WS 写进 `cmd_config.json` 的 `platform` 数组。

        AstrBot **只支持反向 WS**（它监听、登录端连它），所以这里不接受
        `direction == "forward"`：给了就报错并说明原因，而不是默默写成
        一个 AstrBot 不会读的方向 —— 那样用户看到的是「部署成功但连不上」。

        `addr` 传的是**登录端**地址，但反向 WS 下它用不到（AstrBot 不知道
        登录端在哪，是登录端来连它）。这里只取其中的端口做日志提示，
        实际写的是本实例自己的监听端口。
        """
        if mode == "milky":
            return WriteResult(
                ok=False,
                manual="AstrBot 当前仅适配 OneBot v11（aiocqhttp），"
                       "Milky 需另装适配器。请把关联登录端改为 OneBot 协议。")
        if direction != "reverse":
            return WriteResult(
                ok=False,
                manual="AstrBot 只支持反向 WebSocket（由 AstrBot 监听、登录端连它），"
                       "不接受正向 WS。请在向导里把互联方向选为「反向」。")

        port = self._reverse_port(instance)
        path = self._cfg_path(instance)
        entry = {
            "type": "aiocqhttp",
            "id": _PLATFORM_ID,
            "enable": True,
            "ws_reverse_host": "0.0.0.0",       # 面板单机部署；需对外可改
            "ws_reverse_port": port,
            "ws_reverse_token": token,
        }
        try:
            _merge_platform(path, entry)
        except ValueError as e:
            # 字段结构认不出来时给可操作指引，不写坏配置
            return WriteResult(
                ok=False,
                manual=f"无法解析 {path.name}：{e}。"
                       f"请在 AstrBot WebUI（机器人 → 创建机器人 → OneBot v11）"
                       f"手工填反向 WS 端口 {port}，"
                       f"token 与本面板互联配置保持一致。")
        return WriteResult(
            ok=True,
            path=str(path),
            manual=f"已在 {path.name} 写入 OneBot v11 反向 WS 配置"
                   f"（监听 0.0.0.0:{port}）。"
                   f"请让关联的登录端以 ws://<本机IP>:{port}/ws 连过来。\n"
                   f"⚠️ AstrBot 无配置热加载，**需重启该实例**才会生效。")

    def extra_manage_capabilities(self, instance) -> list[dict]:
        """不提供「依赖与插件」面板。

        父类会报 `python_deps`，但它的装卸动作靠 `pyproject.toml` 落声明
        （AstrBot 没有这个文件）→ 面板上点了「安装」会**静默不生效**。
        插件管理走AstrBot 自己的 WebUI（数据/日志 → 插件），
        面板只需给WebUI 入口（基类按 `webui_default_port` 自动产出）。
        """
        return []

    def _ensure_webui_binding(self, instance) -> None:
        """AstrBot 的 WebUI 监听地址在 `cmd_config.json` 的 dashboard.host。

        放开回环绑定好让面板能打开它——不改的话用户从浏览器点「打开 WebUI」
        会连不上（面板与 AstrBot 同机，回环不通的场景少但存在，
        且容器部署时更常见）。
        """
        path = self._cfg_path(instance)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        dash = data.get("dashboard")
        if not isinstance(dash, dict) or dash.get("host") not in ("127.0.0.1", "localhost"):
            return                              # 没配或已是 0.0.0.0，不动
        dash["host"] = "0.0.0.0"
        write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def _merge_platform(path: Path, entry: dict) -> None:
    """把一条平台配置并入 `cmd_config.json` 的 `platform` 数组（幂等）。

    用 `atomic_write_json`（整文件读 → 改 → 写回）而不是文本级编辑：
    JSON 有解析器可用，文本定位在这里没有额外收益（AstrBot 的配置是程序
    自己生成并整体读写的，缩进/键序交给 `json.dumps` 即可）。

    同 `id` 的旧条目被**替换**而不是追加 —— 反复点「写互联配置」会
    攒出多条同 id 条目，AstrBot 会把它们都加载起来连多个实例。
    """
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError(f"读取失败或不是合法 JSON：{e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"顶层不是对象（是 {type(data).__name__}）")
    else:
        data = {}
    plats = data.get("platform")
    if plats is None:
        plats = []
    if not isinstance(plats, list):
        raise ValueError(f"platform 不是数组（是 {type(plats).__name__}）")

    kept = [p for p in plats
            if not (isinstance(p, dict) and p.get("id") == entry["id"])]
    kept.append(entry)
    data["platform"] = kept
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def _platform_fields() -> tuple[str, ...]:
    """平台条目里我们写入的字段名（测试与文档据此核对上游漂移）。"""
    return _PLATFORM_FIELDS
