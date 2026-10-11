r"""NoneBot2：Python 源码项目（pip 依赖 + .env 配置），经 OneBot 连登录端

与另外 10 个适配器的根本差异（决定了本文件的全部特殊处理）
----------------------------------------------------------
现有程序都是「上游发行压缩包 → 解压 → 跑 exe」，`base._extract()` 是纯解压语义。
NoneBot2 上游在 PyPI 而非 GitHub Release，没有可下载的可执行程序包；它的形态是
「Python 项目 + 依赖」：

    bot.py                 入口（nonebot.run()）
    pyproject.toml         依赖声明 —— nb run 依此同步依赖
    .env                   pydantic-settings 配置（OneBot 连接写在这里）
    plugins/               插件目录（非包形式的插件）
    libs/                  本实例独立的依赖目录（pip install --target 的产物）

因此 deploy() 覆写为「取骨架 → pip 装依赖到 libs/」（与 OlivaDiceAdapter 的
OPK 组合部署同类先例——契约本就留了口子），而非塞进 _extract()。

四个容易踩的坑（均已在实现中处理，改动前请先读）
------------------------------------------------
1. **依赖隔离靠 `--target` 而非 venv**（2026-10-05 实测后定的，两版统一）。
   原设计是每实例一个 `python -m venv`，在 Windows 版上根本走不通：内置的
   embeddable Python **不带 venv 模块**，而 `pip install venv` 也失败——venv 是
   标准库模块，PyPI 上没有。这是解释器形态决定的，换版本也解决不了。
   改 `pip install --target <实例>/libs`，由 `bot.py` 开头 `sys.path.insert` 引入。
   （也不能改用 PYTHONPATH：embeddable 的 `._pth` 存在时无视环境变量。）

   ⚠️ 副作用：`libs/` 里没有 `.venv/bin` 那套激活机制，也**没有 console_scripts
   入口**。nonebot2 不需要（`python bot.py` 就是全部），但若日后要加带 CLI 的依赖
   （alembic 之类），得改用 `python -m包名` 或自己生成包装脚本。

2. **不能用 `pip list` 查已装版本**：那列的是解释器自己 site-packages 里的包，
   而我们的依赖在 `libs/`，两者毫无关系。故扫 `*.dist-info` 目录名（见
   `_installed_versions`）。

3. **改 .env 必须重启进程**：nonebot2 不像 NapCat/LLBot 有配置热加载，
   pydantic-settings 在启动时读一次。故 write_conn_config 的 manual 必须提示
   "需重启实例"。

4. **装插件不是纯 pip 动作**：只 pip 不写 pyproject.toml 的 dependencies，
   `nb run` 下次同步会把插件**悄悄移除**。装插件必须两步成对
   （见 pip_install 的 callers / 第二阶段插件 UI）。

依赖隔离策略：每实例独立（`<instance.dir>/libs`），不跨实例共享。
最初设计为"按程序+Python 版本共享 venv"，实测后推翻——插件安装是高频操作，共享会让
A 实例装插件影响 B 实例。wheel 层由 pip 默认 cache 全局复用（DM_PIP_CACHE_DIR
可覆盖），已下载的 wheel 不会重复下载。
"""
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from adapters.base import (
    DEPLOY_PROGRESS,
    DEPLOY_VERSION,
    BaseAdapter,
    WriteResult,
    http_get_json,
    plugin_op_log,
    remote_search_disabled,
)
from core.atomicio import atomic_write_dotenv, read_dotenv, write_atomic
from core.locks import program_dir_lock

# 依赖安装目录（放实例目录内，删实例即彻底清理；不进备份——备份只关心 .env 与 plugins/）
LIBS_DIR = "libs"
# pip 安装超时：nonebot2 本体 45s（实测），留足余量给慢网/多依赖
PIP_TIMEOUT = 900
# pip 国内源：直连 pypi.org 在国内常超时，默认走清华镜像；置空串可关闭
DEFAULT_PIP_INDEX = os.environ.get(
    "DM_PIP_INDEX", "https://pypi.tuna.tsinghua.edu.cn/simple").strip()
# 解释器探测结果记忆：{实例目录: 解释器路径}，见 _instance_python 的注释
_INTERP_CACHE: dict[str, Path] = {}


# 自生成的 pyproject.toml 骨架。
#
# **形态参照 nb-cli 的 simple 模板渲染后的结果**
# （`nb_cli/template/project/simple/{{cookiecutter.computed.project_slug}}/pyproject.toml`，
#  2026-10-06 实际读取核对过），但**不能直接抄原模板**：原模板整份是 Jinja2
#  动态生成（`{% set %}` / `{{ cookiecutter... }}` 拼 dependencies 与 adapters 段），
#  要用得先实现一遍 cookiecutter 的变量渲染 —— 脆，且会随上游变动失效。
#
# 保留 `[tool.nonebot]` 两行的原因：`nb run` 依它找插件目录；缺了插件加载不到。
_SKELETON_PYPROJECT = """\
[project]
name = "dicebot"
version = "0.1.0"
description = "NoneBot2 instance managed by DiceManager"
readme = "README.md"
requires-python = ">=3.10, <4.0"
# 空数组占位，实际依赖由 DiceManager 依清单声明写入（见 _write_pyproject_deps）。
# **必须留这个空数组**：merge_pyproject_deps 是文本级编辑，依赖找到
# `dependencies` 这个键；没有它会抛「dependencies 段落无法识别」。
# 但不能在这里写死版本 —— 那会让用户装的插件被这里降回去。
dependencies = []

[project.optional-dependencies]
dev = []

[tool.nonebot]
plugin_dirs = ["plugins"]
builtin_plugins = []

[tool.nonebot.adapters]
"""


# 官方 / 常用插件目录：面板「安装插件」下拉的候选（已装的会被前端过滤掉）。
#
# ⚠️⚠️ **每一条的 spec 与 repo 都对着 PyPI / GitHub 逐个核过（2026-10-12）**：
# 这类错在界面上完全看不出来 —— 下拉里能选、按钮能点，只有 pip 装的时候才失败
# （或更糟：装了个空壳包，退出码还是 0）。改动前请重新核对，别凭印象增删。
#
# 核对结果（建目录时 7 个候选里筛掉 1 个）：
#   nonebot-plugin-saa → PyPI 404（不存在），已排除。
# repo 的实际归属与直觉不同，按 PyPI 的 project_urls 如实登记：
#   plugin-session 在 **noneplugin** 组织（不是 nonebot）、
#   plugin-htmlrender 是社区作者 kexue-z，两者都不在 nonebot 组织下。
# ---------- 基础配置：.env 里两个列表键的读写 ----------
#
# ⚠️ 这两个键在 .env 里是 **nonebot 的列表写法（JSON 数组）**，不是逗号分隔。
# 2026-10-12 在服务器实测（nonebot 2.5.0 + pydantic-settings）：
#     SUPERUSERS=123,456        → SettingsError: error parsing env var "superusers"
#     SUPERUSERS=["123","456"]  → 正常
#     SUPERUSERS=123            → ValidationError: Input should be a valid set
# 即 pydantic-settings 对 set[str] 字段**只做 json.loads**，不做逗号切分。
# 写成逗号串的后果不是「配置没生效」，而是**实例下次启动直接失败**。
#
# 界面显示与输入仍用逗号分隔（更好读），读写入口在 _parse_env_list / _dump_env_list
# 做转换。⚠️ 两端都要转：只转读 → 写回去就是非法格式；只转写 → 界面显示带括号引号。
#
# 值 = 该键未出现在 .env 时的默认值（读自 nonebot/config.py 实测：
#   superusers: set[str] = set()、command_start: set[str] = {"/"}）。
# 键缺失时按默认值显示，否则「明明有默认行为，界面却一片空白」。
_CONFIG_DEFAULTS: dict[str, str] = {"SUPERUSERS": "", "COMMAND_START": "/"}
# nonebot 默认指令前缀：清空时回落用（前缀为空集会让每条普通消息都被当成指令）
DEFAULT_COMMAND_START = "/"


def _parse_env_list(raw: str | None) -> list[str]:
    """.env 的列表值 → list[str]。兼容 JSON 数组 / 逗号分隔 / 裸值三种写法。"""
    s = (raw or "").strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            v = json.loads(s)
        except ValueError:
            v = None
        if isinstance(v, (list, tuple, set)):
            return [str(x).strip() for x in v if str(x).strip()]
        s = s.strip("[]")          # 括号里不是合法 JSON：退化成按逗号切
    return [p.strip().strip("\"'") for p in s.split(",") if p.strip()]


def _dump_env_list(items) -> str:
    """list[str] → nonebot 唯一能解析的 .env 值（JSON 数组）。

    紧凑分隔（无空格）以贴近 nb-cli 模板的写法，也让「读出来再原样写回」
    与用户手写的值字面一致 —— 带空格的 JSON 虽然同样合法，但每次保存都会
    产生一行无意义的文件 diff。
    """
    return json.dumps([str(i).strip() for i in items if str(i).strip()],
                      ensure_ascii=False, separators=(",", ":"))


# ---------- bot.py 里的插件登记 ----------
#
# ⚠️ **装进 libs/ 不等于会被加载**。入口 bot.py 里只有
# `nonebot.load_plugins('plugins')`，那扫的是**目录形态**插件；pip 装到 libs/
# 的 PyPI 插件不显式 `nonebot.load_plugin()` 就永远不会被加载 ——
# 表现为「面板显示已安装，但日志里没有它、功能也没有」（2026-10-12 实测：
# 装了 apscheduler / localstore 却一个都没进日志）。
#
# 登记块写成「列表 + 逐个 try」而不是一行行 load_plugin：
# 1. 增删插件只需改列表，幂等可重入；
# 2. 单个插件装坏不会让整个 bot 起不来（跳过并打到实例日志）；
# 3. 「已被别的插件带进来」不算失败（alconna 依赖 localstore，会先注册它），
#    否则每次启动都刷一条吓人的警告。
_PLUGINS_MARK_BEGIN = "# ---- DiceManager 插件登记（面板装卸时自动维护，勿手改）----"
_PLUGINS_MARK_END = "# ---- /DiceManager 插件登记 ----"


def _plugin_module(name: str) -> str:
    """包名 → 可 import 的模块名（nonebot-plugin-alconna → nonebot_plugin_alconna）。"""
    return name.strip().lower().replace("-", "_")


def _botpy_plugins(instance) -> list[str]:
    """读 bot.py 当前登记的插件模块名；无登记块返回 []。"""
    path = Path(instance.dir) / "bot.py"
    if not path.is_file():
        return []
    text = path.read_text("utf-8", errors="replace")
    m = re.search(re.escape(_PLUGINS_MARK_BEGIN) + r".*?"
                  + re.escape(_PLUGINS_MARK_END), text, re.S)
    if not m:
        return []
    lst = re.search(r"for _p in (\[.*?\])", m.group(0), re.S)
    if not lst:
        return []
    try:
        return [str(x) for x in ast.literal_eval(lst.group(1))]
    except (ValueError, SyntaxError):
        return []


def _botpy_set_plugins(instance, modules: list[str]) -> None:
    """写回登记块（幂等）。bot.py 不存在就不动 —— 入口归用户，我们不生成它。"""
    path = Path(instance.dir) / "bot.py"
    if not path.is_file():
        return
    mods = sorted({m for m in modules if m})
    body = ("for _p in " + repr(mods) + ":\n"
            "    try:\n"
            "        nonebot.load_plugin(_p)\n"
            "    except Exception as _e:\n"
            "        # 已被别的插件带进来 = 已经加载过了，不是失败，别刷警告\n"
            "        if \"already exists\" not in str(_e):\n"
            "            print(\"[DiceManager] 插件加载失败，已跳过：\", _p, _e,"
            " file=sys.stderr)")
    block = _PLUGINS_MARK_BEGIN + "\n" + body + "\n" + _PLUGINS_MARK_END
    text = path.read_text("utf-8", errors="replace")
    if _PLUGINS_MARK_BEGIN in text and _PLUGINS_MARK_END in text:
        new = re.sub(re.escape(_PLUGINS_MARK_BEGIN) + r".*?"
                     + re.escape(_PLUGINS_MARK_END),
                     lambda _m: block, text, flags=re.S)
    else:
        if "nonebot.run()" not in text:
            return
        new = text.replace("nonebot.run()", block + "\n\nnonebot.run()", 1)
    if new != text:
        write_atomic(path, new.encode("utf-8"))


OFFICIAL_PLUGINS: list[dict] = [
    {"name": "Alconna 指令解析", "spec": "nonebot-plugin-alconna",
     "desc": "灵活强大的指令解析：子命令、类型标注、自动补全、模糊匹配",
     "repo": "https://github.com/nonebot/plugin-alconna"},
    {"name": "APScheduler 定时任务", "spec": "nonebot-plugin-apscheduler",
     "desc": "定时任务调度（cron / interval / date 三种触发器）",
     "repo": "https://github.com/nonebot/plugin-apscheduler"},
    {"name": "ORM 数据库支持", "spec": "nonebot-plugin-orm",
     "desc": "SQLAlchemy ORM 支持，供插件持久化存数据",
     "repo": "https://github.com/nonebot/plugin-orm"},
    {"name": "LocalStore 本地存储", "spec": "nonebot-plugin-localstore",
     "desc": "统一的插件数据 / 缓存 / 配置目录，避免各插件自己乱放",
     "repo": "https://github.com/nonebot/plugin-localstore"},
    {"name": "Session 会话信息", "spec": "nonebot-plugin-session",
     "desc": "会话信息提取与统一会话 ID（多平台场景下的会话抽象）",
     "repo": "https://github.com/noneplugin/nonebot-plugin-session"},
    {"name": "HTML 渲染", "spec": "nonebot-plugin-htmlrender",
     "desc": "把 HTML / Markdown / 模板渲染成图片发送",
     "repo": "https://github.com/kexue-z/nonebot-plugin-htmlrender"},
]


class NoneBot2Adapter(BaseAdapter):
    # 骰子端是OneBot **客户端**（主动连登录端），不监听任何互联端口。
    # 与 OlivaDice / ShikiAdapter 同款：进程存活即视为已连接。
    HEALTH_PORT_KEYS: list[str] = []

    # ---------- 解释器与依赖目录 ----------

    def _libs_dir(self, instance) -> Path:
        """本实例的依赖目录：`<instance.dir>/libs`。

        ⚠️ **这里不是 venv，而是 `pip install --target`**（2026-10-05 实测后的
        决定，两版统一）。原设计是每实例一个 `python -m venv`，但那条路在
        Windows 版上**根本走不通**：

        - 内置的 embeddable Python **不带 venv 模块**，且 `pip install venv`
          也失败——venv 是标准库模块，PyPI 上没有。这是解释器形态决定的，
          不是配置问题，换版本也解决不了。
        - embeddable 的 `._pth` 文件存在时，Python **只按它列的路径**找包，
          `PYTHONPATH` 被完全无视 → 靠环境变量隔离同样不可行。

        故改为把依赖装进实例自己的目录，由入口脚本 `sys.path.insert` 引入。
        好处是删实例即彻底清理、依赖可随时重建，且两版同一套机制。
        """
        return Path(instance.dir) / LIBS_DIR

    def _instance_python(self, instance) -> Path:
        """启动 / 装依赖用的解释器，**与 `_find_interpreter` 同一套候选顺序**。

        二者必须一致：cryptography 之类的二进制包是针对特定Python 版本编译的，
        用A 解释器装、用 B 解释器跑会在 import 时报找不到 DLL 之类的怪错。
        早前这里自己写了一遍顺序（只有 bundled + sys.executable），漏掉了
        DM_PYTHON 与清单候选——用户设了 DM_PYTHON 就会装到系统 Python、却用
        内置解释器启动，故改为直接委托 `_find_interpreter`。

        结果做进程内记忆：每次探测要跑两次子进程（-V 与 -c），而
        prepare_start → pip_install → build_start_cmd 会在一次启动里连着问三次。
        探测对象（env、内置文件、清单）运行期不变，缓存是安全的。
        """
        key = str(instance.dir)
        if (hit := _INTERP_CACHE.get(key)) is not None:
            return hit
        found = self._find_interpreter(instance)
        path = Path(found) if found else Path(sys.executable)
        _INTERP_CACHE[key] = path
        return path

    def _find_interpreter(self, instance) -> str | None:
        """定位可用解释器：DM_PYTHON > 捆绑的 embeddable > 清单候选 > 面板自身。

        ⚠️ **`sys.executable` 在 Windows 版上绝不能直接用**（2026-10-05 实测）：
        PyInstaller `--onefile` 打出的 `dicemanager.exe` 会**完全无视 argv**——
        传 `-V`、`--version`、`-c "print(1)"` 给它，一律启动整个面板
        （起 uvicorn、加载实例、尝试 resume），且退出码是 0。
        即"rc==0 就算解释器可用"这个朴素判据会把它**误判成合法解释器**，
        随后装出一堆依赖、启动时却缺这缺那。

        判定逻辑统一在 core/interpreter.py（适配器与打包流程共用一处真相），
        本方法只负责给出候选顺序。
        """
        if override := os.environ.get("DM_PYTHON", "").strip():
            return override if self._interp_ok(override) else None
        for cand in self.bundled_interpreters():
            if self._interp_ok(cand):
                return cand
        for cand in self.m.get("python_candidates") or []:
            found = self._which(cand)
            if found and self._interp_ok(found):
                return found
        # Linux 版面板跑在真实解释器（venv / 系统 python）里，可以拿来装依赖；
        # Windows 版是单 exe，必须在这里就挡住（见 core/interpreter.is_frozen_like）。
        if sys.executable and self._interp_ok(sys.executable):
            return sys.executable
        return None

    def bundled_interpreters(self) -> list[str]:
        """管理器自带的 Python（embeddable）候选路径，按序探测。

        Windows 版把 embeddable Python 放在 exe 同级的 `runtime/python/`，
        这样用户双击即用、无需自装Python。Linux 版不捆绑（系统必有 python3）。
        """
        from core.interpreter import bundled_interpreters
        return bundled_interpreters()

    @staticmethod
    def _interp_ok(path: str) -> bool:
        """确认是真解释器而不只是「退出码为 0」（细节见 core/interpreter）。"""
        from core.interpreter import looks_like_python
        return looks_like_python(path)

    @staticmethod
    def _which(name: str) -> str | None:
        from shutil import which
        return which(name)

    def _require_interpreter(self, instance) -> str:
        """定位解释器并核对版本下限；不满足时给可操作的指引（不是一句「未找到」）。"""
        interp = self._find_interpreter(instance)
        if not interp:
            raise RuntimeError(self._no_interpreter_msg())
        want = tuple(self.m.get("python_requires") or ()) or None
        if want:
            from core.interpreter import python_version
            got = python_version(interp)
            if got is None or got < want:
                raise RuntimeError(
                    f"解释器版本不足：{interp} 为 "
                    f"{'.'.join(map(str, got)) if got else '未知'}，"
                    f"本程序需要 Python ≥ {'.'.join(map(str, want))}。"
                    "请安装所需版本的 Python 并置于 PATH，"
                    "或用环境变量 DM_PYTHON 指向它。")
        return interp

    def _no_interpreter_msg(self) -> str:
        """构造「找不到解释器」的报错：区分 bundled 缺失与系统缺失两种成因。

        两种成因的解法完全不同，混成一句话会让用户反复重试都没用：
        - bundled 缺失 = 打包产物不完整（发版问题，用户自己修不了）
        - 系统缺失   = 用户机没装 Python，或装了但不在 PATH
        """
        need = "≥" + ".".join(map(str, self.m.get("python_requires") or (3, 10)))
        if not self.bundled_interpreters():
            return (
                f"未找到可用的 Python 解释器（本程序需 Python {need}），"
                "且管理器未携带内置解释器。"
                "若这是官方发行版请重新下载完整包；"
                "否则请安装 Python 并确保它在 PATH 中，"
                "或设置环境变量 DM_PYTHON 指向解释器。")
        return (
            f"未找到可用的 Python 解释器（本程序需 Python {need}）。"
            "管理器自带的内置解释器无法启动，"
            "请改用系统 Python（安装后确保在 PATH 中），"
            "或设置环境变量 DM_PYTHON 指向解释器。")

    def _ensure_libs(self, instance) -> Path:
        """确保依赖目录存在并返回它（替代原 `_create_venv`）。

        不需要建任何东西——`pip install --target` 会自己建目录。
        """
        libs = self._libs_dir(instance)
        libs.mkdir(parents=True, exist_ok=True)
        return libs

    def _pip(self, instance) -> list[str]:
        """用**启动该实例的那个解释器**去装依赖。

        必须与启动时同一个解释器：cryptography 之类的二进制包是针对特定
        Python 版本编译的，用 A 解释器装、用 B 解释器跑会在 import 时报
        找不到 DLL 之类的怪错。
        """
        return [str(self._instance_python(instance)), "-m", "pip"]

    def _pip_install(self, instance, *targets: str, timeout: int = PIP_TIMEOUT,
                     upgrade: bool = False) -> None:
        """同步执行 `pip install --target <libs>`，输出汇入实例日志流。

        ⚠️ 用 `--target` 而非裸装：依赖落在 `<实例>/libs` 而不是解释器的
        site-packages，多实例互不污染，删实例即彻底清理。
        """
        if not targets:
            return
        libs = self._ensure_libs(instance)
        cmd = [*self._pip(instance), "install",
               "--disable-pip-version-check", "--target", str(libs)]
        if upgrade:
            cmd.append("--upgrade")
        if DEFAULT_PIP_INDEX:
            cmd += ["--index-url", DEFAULT_PIP_INDEX]
        cmd += list(targets)
        self._progress(instance, "pip", 0, len(targets))
        # ⚠️ 用 Popen 逐行读而不用 subprocess.run(capture_output=True)：
        # 装一个插件常要几分钟（下载 + 编译），全部憋到结束才吐出来的话，
        # 界面只能干转圈，用户分不清「在装」还是「卡死了」。逐行写进 PLUGIN_OP
        # 供前端轮询，pip 的输出本身就是最可信的进度来源。
        # stdin=DEVNULL：pip 在依赖冲突时会问「要不要 --force」（实测 AstrBot /
        # Koishi 的 CLI 都有这毛病）。面板无 TTY，stdin 开着会挂到超时。
        key = getattr(instance, "id", "") or ""
        plugin_op_log(key, "$ " + " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                stdin=subprocess.DEVNULL, bufsize=1,
                                env=self._clean_env())
        assert proc.stdout is not None  # Popen(stdout=PIPE) 必返回流，仅类型收窄用
        out: list[str] = []
        try:
            for ln in proc.stdout:                      # 行缓冲：bufsize=1 + text
                out.append(ln)
                plugin_op_log(key, ln)
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            proc.kill()
            plugin_op_log(key, f"安装超时（>{timeout}s），已中止")
            raise RuntimeError(f"依赖安装超时（>{timeout}s），请检查网络后重试") from e
        text_out = "".join(out)
        # ⚠️ 退出码之外还要验产物：`pip install` 在部分失败情形下仍返回 0
        # （例如只有 wheel 平台不匹配而降级为源码编译成功、或包名拼错被当成
        # 新包名而"成功"装了个空壳）。故检查声明的依赖是否都真的进了 libs/。
        if proc.returncode != 0:
            tail = text_out.strip().splitlines()[-6:]
            raise RuntimeError(
                f"依赖安装失败（rc={proc.returncode}）：\n" + "\n".join(tail))
        missing = self._deps_missing(instance)
        if missing:
            tail = text_out.strip().splitlines()[-6:]
            raise RuntimeError(
                "依赖安装未完成：pip 退出码是 0，但以下依赖没装上——"
                + "、".join(missing) + "。\n命令输出：\n"
                + ("\n".join(tail) or "(空)"))
        self._progress(instance, "pip", len(targets), len(targets))

    def _clean_env(self) -> dict:
        """子进程环境：剔除 PYTHONHOME / PYTHONPATH。

        embeddable 的 `._pth` 会无视 PYTHONPATH，留着只会让排错时误以为
        「路径已经注入了怎么还 import 不到」。剔掉后行为两版一致。
        依赖隔离靠 `run.py` 里的 `sys.path.insert`，不靠环境变量。
        """
        return {k: v for k, v in os.environ.items()
                if k not in ("PYTHONHOME", "PYTHONPATH")}

    def _progress(self, instance, stage: str, done: int, total: int) -> None:
        key = getattr(instance, "id", None)
        if key:
            DEPLOY_PROGRESS[key] = {"stage": stage, "done": done, "total": total}

    # ---------- 骨架 ----------

    def _req_file(self, instance) -> Path:
        return Path(instance.dir) / self.m.get("requirements_file", "requirements.txt")

    def _fetch_skeleton(self, instance) -> None:
        """准备骨架文件：**自生成** pyproject.toml（必要时才联网）。

        ⚠️ **不再从上游仓库拉**（2026-10-06 修正）：此前清单写的是
        `nonebot/nonebot2-template`，但**该仓库根本不存在**（GitHub 返404），
        部署 nonebot2 必然失败。NoneBot2 官方**没有**独立的模板仓库——
        模板在 `nonebot/nb-cli` 内部的 `nb_cli/template/project/simple/`，
        是 **cookiecutter 模板**：目录名带 `{{cookiecutter.computed.project_slug}}`
        占位符、内容是 Jinja2 表达式，**不能直接当骨架用**（要重新实现一遍
        cookiecutter 的变量渲染，脆且会随上游变动而失效）。

        改为自生成的理由：
        1. pyproject.toml 的内容**我们完全知道该长什么样**（见 _SKELETON_PYPROJECT）
        2. 不受上游仓库改名 / 删除 / 改分支影响
        3. 部署阶段不需要为骨架联网，只有 pip 装依赖要网 —— 少一个失败点
        4. 用户手工放进去的 pyproject.toml / 离线程序包**优先**，不覆盖
        """
        files = (self.m.get("skeleton") or {}).get("files") or []
        for rel in files:
            dest = Path(instance.dir) / rel
            if dest.exists():
                continue                # 已存在（用户放的 / 断点续跑）不覆盖
            dest.parent.mkdir(parents=True, exist_ok=True)
            if rel == "pyproject.toml":
                dest.write_text(_SKELETON_PYPROJECT.strip() + "\n",
                                encoding="utf-8")
            else:
                # 清单声明了别的骨架文件但我们不会生成 → 明确报出来，
                # 而不是安静地跳过（跳过会得到一个"部署成功但起不来"的实例）
                raise RuntimeError(
                    f"清单 skeleton.files 声明了 {rel!r}，但适配器不会生成它。"
                    f"当前只自生成 pyproject.toml；请在 WebUI「离线程序包」里"
                    f"上传包含该文件的完整 NoneBot2 项目包。"
                )

    def _write_pyproject_deps(self, instance, deps: dict[str, str]) -> None:
        """把依赖同步进 pyproject.toml 的 project.dependencies。

        nb run 依此同步依赖：只 pip 不写这里，插件会在下次 nb run 时被移除。
        故装插件/装依赖后必须调用本函数。

        用**文本级定位编辑**而非 TOML 反序列化再回写：项目未引入 TOML 写库
        （tomllib 只读），且整文件重写会清掉用户的 [tool.nonebot] 等配置。
        """
        path = Path(instance.dir) / "pyproject.toml"
        if not path.exists() or not deps:
            return
        try:
            text = path.read_text("utf-8")
        except OSError:
            return
        try:
            new = merge_pyproject_deps(text, deps)
        except ValueError as e:
            raise RuntimeError(f"pyproject.toml 的 dependencies 段落无法识别：{e}") from e
        if new != text:
            write_atomic(path, new.encode("utf-8"))

    def _deps_installed(self, instance) -> bool:
        """依赖是否真的装好了——`libs/` 存在**不等于**装好了。

        判据是「至少有清单声明的那几项的 dist-info」，而不是「目录非空」：
        断点续跑 / 用户手放了骨架 / pip 半途失败，都会留下一个空的或残缺的
        `libs/`，而面板上看到的实例目录是存在的，很容易被误判成已部署。

        判空目录会让 deploy 走「继续装」而不是报 conflict，用户看到的是一次
        正常的补装流程；判错成已装好则实例根本起不来。
        """
        installed = self._installed_versions(instance)
        if not installed:
            return False
        declared = self.m.get("dependencies") or {}
        if not declared:
            return True                       # 无声明依赖时，有包即视为装好
        # 必须是「全部声明项都在」，不是「任意一项在」。
        # 用 any 的话，删掉 nonebot2 只剩 fastapi/uvicorn 也会被判装好——
        # 而 nonebot2 恰恰是本体，缺了它实例一起来就ModuleNotFoundError。
        return not self._deps_missing(instance)

    def _deps_missing(self, instance) -> list[str]:
        """清单声明但尚未装上的依赖（供 prepare_start / 报错给用户看）。"""
        installed = self._installed_versions(instance)
        return [k for k in (self.m.get("dependencies") or {}) if k.lower() not in installed]

    def _installed_versions(self, instance) -> dict[str, str]:
        """已装依赖的 {包名: 版本}（读的是 `--target` 装到 `libs/` 的那些）。

        不能用 `pip list`——那列的是**解释器自己** site-packages 里的包，
        而我们的依赖在 `libs/`，两者毫无关系。故直接扫 `*.dist-info` 目录名。

        ⚠️ 同名前缀的 dist-info 只保留**版本号较大**的那个，不能用「后扫的覆盖
        先扫的」：`Path.glob` 的顺序在 Linux 与 Windows 上不同，同一份目录在
        两边会得出相反的版本号。2026-10-05 CI 就是在 Linux 上炸的这个
        （Windows 本地全绿：扫到 2.7.1，Linux 扫到 2.4.3）。
        残留重复通常来自 pip 升级中断或手工拷贝，概率低但后果是「基线算错、
        误判成没升级」，所以这里必须给确定结果。
        """
        found: dict[str, list[str]] = {}
        libs = self._libs_dir(instance)
        if not libs.is_dir():
            return {}
        for d in libs.glob("*.dist-info"):
            stem = d.name[: -len(".dist-info")]
            name, _, ver = stem.rpartition("-")
            if name and ver:
                found.setdefault(name.lower().replace("_", "-"), []).append(ver)
        return {k: max(v) for k, v in found.items()}

    def _freeze_baseline(self, instance) -> None:
        """记录已装依赖版本，作为升级通道的比对基线（替代 release tag）。"""
        installed = self._installed_versions(instance)
        if not installed:
            return
        blob = "\n".join(f"{k}=={v}" for k, v in sorted(installed.items()))
        self._last_tag = "pip:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    def _publish_baseline(self, instance) -> None:
        """把基线写进 DEPLOY_VERSION 供 wizard 取走落盘。

        base.deploy 里有这一步，本类覆写了 deploy() 就必须自己做——否则实例的
        version 永远是 None，升级通道与「已装依赖版本」无从比对（2026-10-05 补）。
        """
        key = getattr(instance, "id", None)
        if key and self._last_tag:
            DEPLOY_VERSION[key] = self._last_tag

    def verify_required(self, instance) -> list:
        """必备文件校验（base 版只查 `.exists()`）。

        `required_files` 里的 `libs` 是**目录**，`.exists()` 对空目录也返回 True
        —— pip 装到一半失败留下的空 `libs/` 会被判成「已装好」，然后实例
        一起就 ModuleNotFoundError。故这里对 `libs` 追加「非空」校验。
        """
        missing = super().verify_required(instance)
        if LIBS_DIR in self.m.get("required_files", []):
            libs = self._libs_dir(instance)
            if (not libs.is_dir() or not any(libs.iterdir())) and LIBS_DIR not in missing:
                missing.append(LIBS_DIR)
        return missing

    # ---------- 部署 ----------

    def deploy(self, instance) -> str:
        """部署。语义对齐 base.deploy()，但"已装好"的判据是**依赖已装**
        而非"目录存在"。

        目录存在但依赖没装（断点续跑 / 部署中途失败 / 用户手动放了骨架进去）时
        **必须继续装**，不能报 conflict——否则用户看到的是
        "目录冲突"弹窗，真实原因（依赖没装）却无处可查。
        """
        with program_dir_lock(self.m["name"]):
            if self._deps_installed(instance) and not self.verify_required(instance):
                return "ok"                # 真正装好了：幂等返回
            key = getattr(instance, "id", None)
            if key:
                DEPLOY_PROGRESS[key] = {"stage": "prepare", "done": 0, "total": 0}
            try:
                self._fetch_skeleton(instance)
                # 早失败：别装到一半才报「找不到解释器」
                self._require_interpreter(instance)
                self._ensure_libs(instance)
                self._write_entrypoint(instance)
                deps = self.m.get("dependencies") or {}
                self._pip_install(instance, *(f"{k}{v}" for k, v in deps.items()))
                self._write_pyproject_deps(instance, deps)
                if missing := self.verify_required(instance):
                    raise RuntimeError(f"部署后缺失必备文件: {missing}")
                self._freeze_baseline(instance)
                self._publish_baseline(instance)
            finally:
                if key:
                    DEPLOY_PROGRESS.pop(key, None)
        return "ok"

    def upgrade(self, instance) -> str | None:
        """升级依赖：pip install -U（不是换包，故与 base.upgrade 语义不同）。

        base.upgrade 走「下载新包 → 覆盖解压」，对 pip_project 不存在可下载的包。
        这里改为把清单里声明的依赖全部升到最新版，返回新的依赖快照基线。

        **必须同步 pyproject.toml**：清单里的版本下限（如 nonebot2>=2.4.0）在
        `nb run` 按 pyproject 同步依赖时会把刚装上的新版本降回去。故升级后
        把已装版本回写为新的下限（见 _bump_deps_floor）。
        """
        with program_dir_lock(self.m["name"]):
            if not self._deps_installed(instance):
                raise RuntimeError("实例尚未完成部署（依赖目录为空），请先重新部署")
            deps = self.m.get("dependencies") or {}
            key = getattr(instance, "id", None)
            if key:
                DEPLOY_PROGRESS[key] = {"stage": "pip", "done": 0, "total": len(deps)}
            try:
                self._pip_install(instance, *(f"{k}{v}" for k, v in deps.items()),
                                  upgrade=True)
                self._bump_deps_floor(instance)
                self._freeze_baseline(instance)
            finally:
                if key:
                    DEPLOY_PROGRESS.pop(key, None)
        self._publish_baseline(instance)
        return self._last_tag

    def _bump_deps_floor(self, instance) -> None:
        """把 pyproject 里**清单声明的依赖**的版本下限抬到已装版本。

        否则 `nb run` 会依pyproject 的旧下限把刚升级的包降回去（用户视角：
        「面板里点了升级，一重启又变回旧版」）。

        只处理清单里那几项直接依赖：传递依赖树有几十个，全写进 pyproject 会把
        用户文件变成 pip 的输出，且 nb run 并不需要它们被显式声明。
        """
        declared = self.m.get("dependencies") or {}
        if not declared:
            return
        installed = self._installed_versions(instance)
        deps = {k: f">={installed[k.lower()]}"
                for k in declared if k.lower() in installed}
        if not deps:
            return
        path = Path(instance.dir) / "pyproject.toml"
        if not path.exists():
            return
        try:
            text = path.read_text("utf-8")
            new = bump_pyproject_versions(text, deps)
        except (OSError, ValueError):
            return                                # 段落识别不了就不改，不阻断升级
        if new != text:
            write_atomic(path, new.encode("utf-8"))

    def _write_entrypoint(self, instance) -> None:
        """写最小 bot.py 入口（骨架未提供时）。load_plugins 自动发现 plugins/。

        ⚠️ 开头那句 `sys.path.insert` 是**整个方案的承重墙**，不能省：
        依赖装在 `<实例>/libs`（`pip install --target`），不是解释器的
        site-packages。不显式插进去，`import nonebot` 直接 ModuleNotFoundError。

        也不能改成靠环境变量传：`embeddable Python` 的 `._pth` 存在时**无视
        PYTHONPATH**（2026-10-05 实测），所以只有改`sys.path` 这条路可行。
        `sys.path` 而非 `PYTHONPATH` 在 Linux 版上也同样有效，故两版共用这一份。

        路径用 `Path(__file__).resolve().parent` 现算而非写死绝对路径：
        实例目录被整体移动/改名后仍能起来（备份还原、换盘都会发生）。
        """
        path = Path(instance.dir) / "bot.py"
        if path.exists():
            return
        path.write_text(
            '"""NoneBot2 入口（由 DiceManager 生成，可自由修改）。"""\n'
            "import sys\n"
            "from pathlib import Path\n\n"
            # 依赖目录（pip install --target 的产物）不在默认搜索路径里
            "sys.path.insert(0, str(Path(__file__).resolve().parent / "
            f"{LIBS_DIR!r}))\n\n"
            "import nonebot\n"
            "import nonebot.adapters.onebot as ob11\n\n"
            "nonebot.init()\n\n"
            "# ⚠️ 必须显式注册协议适配器。\n"
            "# nonebot2 2.5 已移除 nonebot.load_adapter()，改由 driver.register_adapter()；\n"
            "# 只 import 不注册 → nonebot.get_adapters() 为空 → 不连任何 IM 协议端，\n"
            "# 表现为「能启动、能响应 HTTP，但收不到任何消息」（2026-10-08 实测）。\n"
            "nonebot.get_driver().register_adapter(ob11.V11Adapter)\n\n"
            "nonebot.load_plugins('plugins')\n"
            "nonebot.run()\n",
            encoding="utf-8")

    def entrypoint_has_deps_path(self, instance) -> bool:
        """检查现有 bot.py 是否已引入 libs/（缺了就是打不开的实例，给出可操作提示）。"""
        path = Path(instance.dir) / "bot.py"
        if not path.exists():
            return False
        try:
            return LIBS_DIR in path.read_text("utf-8")
        except OSError:
            return False

    # ---------- 启动 ----------

    def build_start_cmd(self, instance) -> list[str]:
        return [str(self._instance_python(instance)), "bot.py"]

    def prepare_start(self, instance, runner=None) -> bool:
        """首启前确保 libs/ 与依赖就绪；返回是否做了补装。

        面板重启后 resume 直接拉起时也可能缺依赖（上次 pip 半途失败、或用户
        手动删了 libs），这时补装一次即可，不必让用户回部署向导。
        """
        if not self._deps_installed(instance):
            self._require_interpreter(instance)
            self._ensure_libs(instance)
            self._write_entrypoint(instance)
            deps = self.m.get("dependencies") or {}
            self._pip_install(instance,
                              *(f"{k}{v}" for k, v in deps.items()))
            return True
        return False

    def configure_login(self, instance, credentials) -> dict:
        return {"needs_login": False}                  # 不登录 QQ（OneBot 由登录端承载）

    # ---------- 互联配置（.env） ----------

    def _env_path(self, instance) -> Path:
        return Path(instance.dir) / self.m.get("config_path", ".env")

    def write_conn_config(self, instance, mode, direction, addr, token,
                          link_id: str | None = None) -> WriteResult:
        """写 .env 的 OneBot 连接项。

        ⚠️ 键名必须是 `ONEBOT_V11_*`（2026-10-08 实测）：
        适配器的 `nonebot/adapters/onebot/v11/config.py` 里字段 alias 是
        `onebot_v11_ws_urls` / `onebot_v11_access_token`，pydantic 按 alias 读
        环境变量 → 对应 `ONEBOT_V11_WS_URLS` / `ONEBOT_V11_ACCESS_TOKEN`。
        旧文档里的 `OB11_WS_URLS` / `OB11_TOKEN` **不被识别** ——
        写上去症状是「能启动、能响应 HTTP，但收不到消息」，
        且 `Loaded adapters:` 后为空、3002 端口上没有任何连接，极难定位。

          正向 WS（本端主动连登录端）：ONEBOT_V11_WS_URLS
          反向 WS（登录端连本端）：    ONEBOT_V11_REVERSE_WS_URLS
        两种方向都以本端为**客户端/服务端**的相对视角命名，故这里按 direction 取键。

        另需 DRIVER：nonebot2 默认是 ~none（无驱动器），不设则 OneBot 收发不工作。
        """
        if mode == "milky":
            return WriteResult(
                ok=False,
                manual="NoneBot2 当前仅适配 OneBot 协议（nonebot-adapter-onebot），"
                       "Milky 需另装适配器。请把关联登录端改为 OneBot 协议。")
        forward = direction != "reverse"
        key = ("ONEBOT_V11_WS_URLS" if forward
               else "ONEBOT_V11_REVERSE_WS_URLS")
        url = f"ws://{addr}/" if forward else f"ws://0.0.0.0:{addr.split(':')[-1].rstrip('/')}/"
        path = self._env_path(instance)

        atomic_write_dotenv(path, lambda c: {
            **c,
            "DRIVER": "~fastapi+~websockets",          # OneBot WS 需要驱动器
            "HOST": "127.0.0.1",# 面板单机部署；如需对外可改 0.0.0.0
            "PORT": str(instance.allocated_ports.get("bot") or 8080),
            key: json.dumps([url]),                 # python-dotenv 按 shell 规则解析，此处需 JSON 数组
            "ONEBOT_V11_ACCESS_TOKEN": token,
        })
        kind = "正向 WS" if forward else "反向 WS"
        return WriteResult(
            ok=True,
            path=str(path),
            manual=f"已写入 {path}：{kind} → {url}\n"
                   f"Token：{token}\n"
                   "⚠️ NoneBot2 不支持配置热加载，**需重启该实例**互联才会生效。")

    def _ensure_webui_binding(self, instance) -> None:
        """NoneBot2 无自带 WebUI：插件管理由 DiceManager 面板承担（见第二阶段）。"""
        return None

    def expose_webui(self, instance) -> str | None:
        """无 WebUI 端口可放行（.env 里的 PORT 是 nonebot 自身驱动器端口，
        属应用内部监听，面板不代为放通——需要时由用户在防火墙自行处理）。"""
        return None

    # ---------- 基础配置（无 WebUI：超级管理员 / 指令前缀）----------
    def config_schema(self, instance) -> list[dict] | None:
        """nonebot2 没有自带 WebUI，超级管理员与指令前缀恰是 nonebot2 的**标准**
        `.env` 配置（SUPERUSERS / COMMAND_START），由面板编辑最合适。

        ⚠️ 值需重启进程才生效：nonebot2 用 pydantic-settings 在启动时读一次 .env，
        与 NapCat/LLBot 不同，没有配置热加载（与 write_conn_config 同源约束）。
        """
        return [
            {"key": "SUPERUSERS", "label": "超级管理员（QQ 号）", "type": "list",
             "default": "",
             "placeholder": "123456, 789012",
             "hint": "拥有最高权限的 QQ 号，多个用逗号分隔；改后需重启实例生效"},
            {"key": "COMMAND_START", "label": "指令前缀", "type": "list",
             "default": DEFAULT_COMMAND_START,
             "placeholder": DEFAULT_COMMAND_START,
             "hint": "触发指令的前缀，多个用逗号分隔；留空则回落到 nonebot 默认 "
                     + DEFAULT_COMMAND_START + "。改后需重启实例生效"},
        ]

    def read_config(self, instance) -> dict:
        """读当前值。**返回显示用的纯文本**（`123456,789012`），不带原文件的括号引号。

        .env 里存的是 nonebot 的列表写法（`["2278442823"]`），原样回传的话界面会显示成
        `["2278442823"]` —— 看着像坏了，其实只是没解析。键不存在时给程序默认值，
        否则界面空白会让人以为配置丢了。
        """
        env = read_dotenv(self._env_path(instance))
        out = {}
        for key, dflt in _CONFIG_DEFAULTS.items():
            items = _parse_env_list(env.get(key))
            out[key] = ",".join(items) if items else dflt
        return out

    def write_config(self, instance, values: dict) -> WriteResult:
        """写回 .env。⚠️ **落盘必须是 JSON 数组**（原因与实测见 _CONFIG_DEFAULTS 注释）。

        界面用逗号分隔输入，这里转回 nonebot 的列表格式；空值回落到程序默认，
        避免写出 `KEY=` 空串（同样会让 nonebot 启动时解析失败）。
        """
        path = self._env_path(instance)

        def _val(key):
            items = _parse_env_list((values or {}).get(key, ""))
            if not items and _CONFIG_DEFAULTS.get(key):
                items = [_CONFIG_DEFAULTS[key]]
            return _dump_env_list(items)

        updates = {k: _val(k) for k in _CONFIG_DEFAULTS}
        atomic_write_dotenv(path, lambda c: {**c, **updates})
        return WriteResult(
            ok=True, path=str(path),
            manual="已写入 .env（SUPERUSERS / COMMAND_START），按 nonebot 要求的列表格式保存。\n"
                   "⚠️ nonebot2 不支持配置热加载，需重启该实例后生效。")

    def extra_manage_capabilities(self, instance) -> list[dict]:
        """nonebot2 无自带 WebUI，能管的就是**依赖与插件**。

        base 的 `manage_capabilities` 判定本程序无端口 → 不会产出 webui 条目，
        所以这里追加的这一条就是面板里的全部内容。

        两种状态都给 `missing`（哪怕是"完全没装"）——面板要靠它渲染
        「补装缺失项」按钮，只给一个 requires_deployed 就把补装入口藏了。
        """
        installed = self._installed_versions(instance)
        missing = self._deps_missing(instance)
        if not installed:
            return [{"kind": "python_deps", "label": "依赖与插件",
                     "requires_deployed": True, "missing": missing,
                     "disabled_reason": "实例尚未完成部署（依赖未装齐），请先重新部署"}]
        return [{"kind": "python_deps", "label": "依赖与插件",
                 "deployed": not missing, "missing": missing}]

    # ---------- 插件管理（pip install --target libs）----------

    def official_plugins(self, instance) -> list[dict]:
        """面板「安装插件」下拉的候选（目录内容与核对说明见 OFFICIAL_PLUGINS）。

        返回**副本**而非常量本身：前端只读，但一旦被下游就地改动，
        会污染之后所有实例的候选列表。
        """
        return [dict(p) for p in OFFICIAL_PLUGINS]

    # ---------- 插件搜索 ----------
    support_plugin_search = True
    # 两个来源，互为兜底：
    #   本地 OFFICIAL_PLUGINS（6 条，逐个对着 PyPI 核过）——断网也有结果
    #   https://registry.nonebot.dev/plugins.json（官方商店全量索引，
    #   2026-10-12 实测 943 条）——这才是「搜索」真正的的意义所在：
    #   下拉只能塞 6 条，而生态里有九百多个插件。
    NB_REGISTRY_URL = "https://registry.nonebot.dev/plugins.json"

    def _search_local(self, q: str) -> list[dict]:
        """本地目录匹配（name / spec / desc 三个字段都参与）。"""
        ql = q.lower()
        return [dict(p, version="", official=True, source="local")
                for p in OFFICIAL_PLUGINS
                if ql in f"{p['name']} {p['spec']} {p['desc']}".lower()]

    def _search_remote(self, q: str, limit: int) -> list[dict]:
        """查 nonebot 官方商店索引。

        字段口径（实测样例）：project_link 是 pip 包名（`nonebot-plugin-status`），
        module_name 是 import 名（`nonebot_plugin_status`）。前者才是能喂给 pip 的，
        但个别条目会填成 URL 或留空 —— 这时退回把 module_name 的下划线换成横杠，
        这是 nonebot 包的惯例命名，比直接丢掉这条有用。
        """
        data = http_get_json(self.NB_REGISTRY_URL)
        if not isinstance(data, list):
            return []
        ql = q.lower()
        # 按相关度排序而非按索引顺序返回：943 条里有大量 desc 里顺带提到关键词的
        # 包，按原顺序会给用户一堆「答非所问」的结果，真正想要的那个排在几十行后。
        # 「包名/标题以关键词开头」视为强相关（=0），仅正文命中的弱相关（=1）。
        # sort 是稳定的，同档内部保持索引原有顺序，不会每次搜索跳来跳去。
        hits: list[tuple[int, dict]] = []
        for it in data:
            if not isinstance(it, dict) or it.get("valid") is False:
                continue                      # valid=false = 上游标记的下架/失效条目
            spec = (it.get("project_link") or "").strip()
            if "/" in spec or " " in spec:    # 填成了 URL 之类的非包名内容
                spec = ""
            module = (it.get("module_name") or "").strip()
            if not spec:
                spec = module.replace("_", "-")
            if not spec:
                continue
            name = (it.get("name") or "").strip() or spec
            desc = (it.get("desc") or "").strip()
            if ql not in f"{name} {desc} {spec} {module}".lower():
                continue
            strong = 0 if (spec.lower().startswith(ql) or name.lower().startswith(ql)) else 1
            hits.append((strong, {"name": name, "spec": spec, "desc": desc,
                                  "repo": (it.get("homepage") or "").strip(),
                                  "version": (it.get("version") or "").strip(),
                                  "official": bool(it.get("is_official")),
                                  "source": "remote"}))
        hits.sort(key=lambda x: x[0])
        out = [h[1] for h in hits[:limit]]
        # 官方排在社区前面：搜索结果长了以后，官方出品的可信度差别对用户是有意义的
        return sorted(out, key=lambda p: 0 if p["official"] else 1)

    def search_plugins(self, instance, q: str, limit: int = 20) -> list[dict]:
        """本地目录 + 官方商店，本地结果排前面且与远端去重（同一 spec 只留一条）。"""
        q = (q or "").strip()
        if len(q) < 2:
            return []
        local = self._search_local(q)
        if len(local) >= limit or remote_search_disabled():
            return local[:max(1, limit)]
        seen = {p["spec"].lower() for p in local}
        remote = self._search_remote(q, limit - len(local))
        return (local + [p for p in remote if p["spec"].lower() not in seen])[:max(1, limit)]

    def list_plugins(self, instance) -> dict:
        """已装插件 / 缺依赖 / 目录形态插件，供「管理应用」面板渲染。

        插件分两类，来源不同、管理方式也不同：
        1. **PyPI 包**（nonebot-plugin-* 等）：装在 `libs/`，靠 dist-info 识别。
        2. **目录形态插件**（`plugins/<name>/` 下直接是 .py）：nonebot2 的原生
           插件形态，不在 pip 体系内。它们**不列进 pyproject**，卸载就是删目录，
           删之前必须提示用户目录里可能有自己写的数据。
        """
        libs = self._libs_dir(instance)
        pdir = Path(instance.dir) / "plugins"
        dirs = (sorted(p.name for p in pdir.iterdir()
                       if p.is_dir() and not p.name.startswith((".", "_")))
                if pdir.is_dir() else [])
        return {
            "packages": [
                {"name": name, "version": ver}
                for name, ver in sorted(self._installed_versions(instance).items())
                if _is_plugin_name(name)
            ],
            "dir_plugins": [{"name": n, "path": str(pdir / n)} for n in dirs],
            # 官方插件候选（下拉用）。随已装列表一起下发，前端才有得比对
            # 「哪些已经装过了、该从下拉里隐藏掉」。
            "catalog": self.official_plugins(instance),
            "missing": self._deps_missing(instance),
            "libs_path": str(libs),
        }

    def install_package(self, instance, spec: str) -> str:
        """装一个 pip 包到 `libs/` 并同步进 pyproject.toml。

        **两步缺一不可**：只 pip 不写 pyproject 的话，`nb run` 依 pyproject
        同步依赖时会把插件悄悄移除（用户视角：装完重启就没了）。
        """
        spec = (spec or "").strip()
        if not spec:
            raise ValueError("请填写包名")
        name = _dep_name(spec)
        if not name or any(ch.isspace() for ch in name):
            raise ValueError(f"包名不合法：{spec!r}")
        with program_dir_lock(self.m["name"]):
            self._require_interpreter(instance)
            self._pip_install(instance, spec)
            self._write_pyproject_deps(instance, {name: _spec_version(spec)})
            # ③ 登记进 bot.py。**缺这步插件就不会被加载**（见 _botpy_set_plugins
            # 的说明）：只扫 plugins/ 目录的 load_plugins 认不到 PyPI 插件，
            # 用户会看到「已安装」但功能不存在，且无从排查。
            if _is_plugin_name(name) and _plugin_module(name) not in _botpy_plugins(instance):
                _botpy_set_plugins(instance, _botpy_plugins(instance)
                                   + [_plugin_module(name)])
        return self._installed_versions(instance).get(name, "?")

    def uninstall_package(self, instance, name: str) -> str:
        """卸载一个 pip 插件：从 `libs/` 删文件 + 从 pyproject 移除声明。

        ⚠️ 不用 `pip uninstall`：pip 不认`--target` 装进去的包
        （`pip uninstall` 会说 "not installed"），只能按 dist-info 定位手删。
        返回被删的版本。
        """
        name = _dep_name(name)
        with program_dir_lock(self.m["name"]):
            ver = self._installed_versions(instance).get(name)
            if not ver:
                raise ValueError(f"{name} 不在已装依赖里")
            libs = self._libs_dir(instance)
            # ① 删包本体：dist-info 记录的 RECORD 列出该包所有文件。
            # 用**同名前缀通配**而不是精确版本：pip 升级中断会留下新旧两个
            # dist-info，只删一个的话另一个还在，下次扫描仍会算进已装依赖
            # （表现为「卸载了但依赖列表里还在」）。版本号本身已由
            # _installed_versions 取 max 定了，删的时候不该再挑。
            for dist in libs.glob(f"{name.replace('-', '_')}-*.dist-info"):
                rec = dist / "RECORD"
                if rec.is_file():
                    for rel in rec.read_text("utf-8", errors="replace").splitlines():
                        target = (libs / rel.split(",", 1)[0].strip()).resolve()
                        # 防目录穿越：RECORD 是包作者写的，不可全信
                        if not str(target).startswith(str(libs.resolve())):
                            continue
                        if target.is_dir():
                            shutil.rmtree(target, ignore_errors=True)
                        else:
                            target.unlink(missing_ok=True)
                shutil.rmtree(dist, ignore_errors=True)
            shutil.rmtree(libs / name, ignore_errors=True)     # 顶层包目录
            shutil.rmtree(libs / name.replace("-", "_"), ignore_errors=True)
            # ② 同步 pyproject，否则 nb run 会把它装回来
            _remove_pyproject_dep(Path(instance.dir) / "pyproject.toml", name)
            # ③ 撤掉 bot.py 里的登记，否则下次启动会去加载一个已删的包
            if _is_plugin_name(name):
                mod = _plugin_module(name)
                _botpy_set_plugins(instance,
                                   [m for m in _botpy_plugins(instance) if m != mod])
        return ver

    def sync_pyproject(self, instance) -> str:
        """把 `libs/` 里现有的包全量写进 pyproject.toml（用户手装过插件后用）。"""
        path = Path(instance.dir) / "pyproject.toml"
        installed = self._installed_versions(instance)
        if not installed:
            raise ValueError("libs/ 里没有已装依赖，无可同步")
        with program_dir_lock(self.m["name"]):
            self._write_pyproject_deps(
                instance, {k: f">={v}" for k, v in installed.items()})
            # 顺带补登记：libs 里已有但 bot.py 没登记的插件，同步完一样不会加载。
            # 这条路径正是给「手工 pip 装过插件」和老实例补救用的。
            mods = _botpy_plugins(instance)
            for k in installed:
                if _is_plugin_name(k) and _plugin_module(k) not in mods:
                    mods.append(_plugin_module(k))
            _botpy_set_plugins(instance, mods)
        return str(path)


def _is_plugin_name(name: str) -> bool:
    """是否为「用户装的插件」而非管理器铺的基础依赖。

    判据是命名约定：nonebot2 生态的插件包统一叫 `nonebot-plugin-*`。
    基础依赖（nonebot2/fastapi/pydantic…）不是插件，混进插件列表会让用户
    误以为能在面板里卸载 nonebot2 本体。
    """
    return name.startswith("nonebot-plugin-") or name.startswith("nonebot_plugin_")


def _spec_version(spec: str) -> str:
    """从 `pkg>=1.0` 里取出版本约束段（`>=1.0`）；没带约束则返回空串。"""
    for sep in ("[", "<", ">", "=", "!", "~"):
        if (at := spec.find(sep)) > 0:
            return spec[at:]
    return ""


def _remove_pyproject_dep(path: Path, name: str) -> None:
    """从 pyproject.toml 的 dependencies 数组里移除一个包（卸载用）。

    与 merge/bump 一样走文本级编辑，但这次是**删条目**——不能靠 tomllib
    反序列化再回写，那会把用户的 [tool.nonebot] 配置清掉。
    找不到该包时静默返回（幂等：重复卸载同一插件不该报错）。
    """
    if not path.exists():
        return
    try:
        text = path.read_text("utf-8")
        new = remove_pyproject_dep(text, name)
    except (OSError, ValueError):
        return                          # 段落识别不了就不改，不阻断卸载
    if new != text:
        write_atomic(path, new.encode("utf-8"))


def remove_pyproject_dep(text: str, name: str) -> str:
    """从 dependencies 数组文本里删掉一个条目；不存在则原样返回。"""
    lines = text.splitlines()
    key_at, arr_end, cur, single = _locate_deps(lines)
    keep = [x for x in cur if _dep_name(x) != name]
    if len(keep) == len(cur):
        return text
    if single:
        opening = lines[key_at].find("[")
        closing = lines[key_at].find("]", opening)
        merged = ", ".join(f'"{x}"' for x in keep)
        line = (lines[key_at][:opening + 1] + " " + merged + " "
                + lines[key_at][closing:])
        return "\n".join(lines[:key_at] + [line] + lines[key_at + 1:]) + "\n"
    indent = " " * 4
    head = lines[key_at]
    tail = "]" + lines[arr_end].split("]", 1)[1]
    rebuilt = [head] + [f'{indent}"{x}",' for x in keep] + [tail]
    return "\n".join(lines[:key_at] + rebuilt + lines[arr_end + 1:]) + "\n"


def _dep_name(spec: str) -> str:
    """从依赖声明串里取出包名（归一为小写）：`D>=1.0` → `d`。"""
    for sep in ("[", "<", ">", "=", "!", "~"):
        spec = spec.split(sep)[0]
    return spec.strip().lower()


def _strip_toml_str(s: str) -> str:
    """剥掉 TOML 字符串字面量的引号；不带引号的原样返回。

    顺序要紧：先 rstrip 逗号再剥引号。反了会把 "pkg>=1.0" 削成裸串，
    写回后 TOML 语义变了（且 tomllib 解析直接失败）。
    """
    s = s.strip().rstrip(",").strip()
    if len(s) >= 2 and s[0] in ('"', "'") and s[-1] == s[0]:
        return s[1:-1]
    if s[:1] in ('"', "'"):
        return s[1:]
    return s


def merge_pyproject_deps(text: str, deps: dict[str, str]) -> str:
    """把 deps 并入 pyproject.toml 文本的 project.dependencies 数组。

    文本级编辑，只改 [project] 段内的 dependencies 一处，其余字节原样保留
    （[tool.nonebot] 的 plugin_dirs/builtins、注释、数组换行风格都不动）。
    已有同名包则跳过——不覆盖用户自己钉的版本。

    找不到 dependencies 键时抛 ValueError，由调用方转成可读报错。
    """
    lines = text.splitlines()
    key_at, arr_end, cur, single = _locate_deps(lines)

    # 单行数组：整体重写该行
    if single:
        have = {_dep_name(x) for x in cur}
        # add 一律存**裸值**（与 cur 同形态），引号统一在回写处补，避免双重包裹
        add = [f"{k}{v}" for k, v in deps.items() if _dep_name(k) not in have]
        if not add:
            return text
        opening = lines[key_at].find("[")
        closing = lines[key_at].find("]", opening)
        merged = ", ".join(f'"{x}"' for x in cur + add)
        lines[key_at] = (lines[key_at][:opening + 1] + " " + merged
                         + " " + lines[key_at][closing:])
        return "\n".join(lines) + "\n"

    # 多行数组：闭合 ] 所在行单独处理，不计入元素
    have = {_dep_name(x) for x in cur}
    add = [f"{k}{v}" for k, v in deps.items() if _dep_name(k) not in have]
    if not add:
        return text
    indent = " " * 4
    items = cur + add
    head = lines[key_at]        # 整行保留：`[ # 注释` 这种行尾注释不能吞
    tail = "]" + lines[arr_end].split("]", 1)[1]
    # 统一加引号回写：items 里混有从原文件剥壳的值与新加的裸值，
    # 不补引号会写出裸串，TOML 解析直接失败
    rebuilt = [head] + [f'{indent}"{x}",' for x in items] + [tail]
    return "\n".join(lines[:key_at] + rebuilt + lines[arr_end + 1:]) + "\n"


def bump_pyproject_versions(text: str, deps: dict[str, str]) -> str:
    """把 pyproject 里已存在的依赖的版本约束换成 deps 给的新值（升级用）。

    与 merge 的区别：merge 只**新增**（已有则跳过，避免覆盖用户钉的版本），
    这里只**改版本**（包已在列表里），不新增也不删除条目。deps 里不在列表中的
    包直接忽略——那属于 merge 的职责。
    """
    lines = text.splitlines()
    key_at, arr_end, cur, single = _locate_deps(lines)

    changed = False
    out = []
    for spec in cur:
        new = f"{_dep_name(spec)}{deps[_dep_name(spec)]}" if _dep_name(spec) in deps else spec
        if new != spec:
            changed = True
        out.append(new)
    if not changed:
        return text
    if single:
        opening = lines[key_at].find("[")
        closing = lines[key_at].find("]", opening)
        merged = ", ".join(f'"{x}"' for x in out)
        lines[key_at] = (lines[key_at][:opening + 1] + " " + merged
                         + " " + lines[key_at][closing:])
        return "\n".join(lines) + "\n"
    indent = " " * 4
    head = lines[key_at]        # 整行保留：`[ # 注释` 这种行尾注释不能吞
    tail = "]" + lines[arr_end].split("]", 1)[1]
    rebuilt = [head] + [f'{indent}"{x}",' for x in out] + [tail]
    return "\n".join(lines[:key_at] + rebuilt + lines[arr_end + 1:]) + "\n"
def _locate_deps(lines: list[str]) -> tuple[int, int, list[str], bool]:
    """在 pyproject 文本行里定位 [project].dependencies 数组。

    返回 (键所在行, 数组闭合行, 现有元素列表, 是否顶格单行数组)。
    单行形态下 arr_end 无意义（与键同行），调用方不要碰。
    找不到或形态不支持时抛 ValueError。
    """
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == "[project]":
            start = i + 1
            break
    if start is None:
        raise ValueError("未找到 [project] 段")
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].strip().startswith("["):
            end = i
            break

    key_at = None
    for i in range(start, end):
        if lines[i].strip().startswith("dependencies"):
            key_at = i
            break
    if key_at is None:
        raise ValueError("[project] 段内未找到 dependencies 键")

    opening = lines[key_at].find("[")
    if opening < 0:
        raise ValueError("dependencies 不是数组形式（支持多行或顶格单行）")
    closing = lines[key_at].find("]", opening)
    if closing >= 0:                # 顶格单行数组
        cur = [_strip_toml_str(x.strip())
               for x in lines[key_at][opening + 1:closing].split(",")]
        return key_at, key_at, [x for x in cur if x], True

    # 多行数组：闭合 ] 所在行单独处理，不计入元素
    arr_end = None
    for i in range(key_at, end):
        if "]" in lines[i]:
            arr_end = i
            break
    if arr_end is None or arr_end == key_at:
        raise ValueError("dependencies 数组未闭合")
    items = [_strip_toml_str(lines[i]) for i in range(key_at + 1, arr_end)]
    return key_at, arr_end, [x for x in items if x], False
