r"""Koishi：Node.js 项目（npm 依赖 + koishi.yml），经 OneBot 连登录端

与 NoneBot2 / AstrBot 的关系
------------------------------------------------
三者同为「registry 上游 + 语言运行时依赖 + 无可下载程序包」，但**语言栈不同**
（Python vs Node），所以不能像 AstrBot 那样继承 NoneBot2Adapter：
解释器探测、依赖安装、产物目录（`libs/` vs `node_modules/`）、配置格式、
插件管理命令全都不同。共用的只有「部署 = 脚手架 + 装依赖」这个流程骨架，
那个骨架在本文件里重写（比造一个跨语言基类更诚实——强行抽象会把差异藏进if 分支）。

已核实的事实（2026-10-05 解包 create-koishi@6.4.0 + 查 npm registry）
------------------------------------------------
- **上游 release 零资产**：GitHub 有 tag（4.18.11）但没发任何包，故没有
  「下载 zip 解压」这条路。官方做法是 `npm init koishi@latest`。
- 脚手架是 npm 包 `create-koishi`（6.4.0），**支持 `-y/--yes` 跳过全部交互**
  —— 面板没有 TTY，必须带这个 flag，否则会挂在 prompts 上等输入。
  另有 `-t/--template`、`-r/--ref`、`--registry` 可用。
- ⚠️ **`-y` 会连依赖安装一起跳过**（源码 `install()`：if (argv.yes) return）。
  所以「拉模板」与「装依赖」必须由我们分两步做。
- 模板包 `@koishijs/boilerplate` 含 40 个依赖，`scripts.start = "koishi start"`。
- ⚠️ **默认模板不含 OneBot 适配器**（给的是 qq/discord/telegram/kook/satori），
  故清单的 `dependencies` 必须显式声明 `@koishijs/plugin-adapter-onebot`
  （6.0.2，peerDeps 要 `koishi ^4.14.6`）。缺它就没有 OneBot 通道。
- Koishi 本体也在 npm（`koishi@4.18.11`，与 GitHub tag 号一致）→ 版本可比对。

OneBot 方向
------------------------------------------------
与 AstrBot 同：**反向 WS 服务端**（Koishi 监听 `/onebot`，登录端连它），
URL 形如 `ws://<host>:5140/onebot`。故 `write_conn_config` 的方向判定一致。

配置载体：`koishi.yml`（YAML）。**不自己解析 YAML**——项目未引写库，
而手写 YAML 序列化极易出错（缩进/引号/特殊字符）。故只做**文本级定位插入**，
在文件里追加/更新 `plugins:` 下`adapter-onebot:` 一段，其余字节原样保留。
写入形态必须用 YAML 块标量 (`|`) 才能安全承载任意字符串。
"""
import json
import os
import re
import subprocess
from pathlib import Path

from adapters.base import DEPLOY_VERSION, BaseAdapter, WriteResult
from core.atomicio import write_atomic
from core.interpreter import find_node, node_with_npm
from core.locks import program_dir_lock

# npm install 可能几分钟（模板有 40 个依赖，含 puppeteer 这类要下Chromium 的）
NPM_TIMEOUT = 1800
# 脚手架拉取（含下载 + 解压）给10 分钟，registry 慢时留余量
SCAFFOLD_TIMEOUT = 600
# node_modules 里判定「依赖已装」的包（与清单 dependencies 对应）
_DEPS_DIRS = ("node_modules",)


class KoishiAdapter(BaseAdapter):
    # Koishi 自带 5140 控制台，但「已连接」看的是 OneBot WS 是否建立，
    # 那个端口在登录端侧（Koishi 是服务端），面板探不到，故置空。
    HEALTH_PORT_KEYS: list[str] = []

    # ---------- 运行时 ----------

    def _require_node(self, instance) -> str:
        """找一个版本达标的 node；找不到给可操作报错（而非装到一半才炸）。"""
        node = find_node(self.m.get("node_candidates") or None,
                         requires=self.m.get("node_requires"))
        if not node:
            raise RuntimeError(self._no_node_msg())
        return node

    def _no_node_msg(self) -> str:
        need = (self.m.get("node_requires") or [[18, 0]])[0]
        return (f"未找到 Node.js ≥{need[0]}（管理器会自行探测 node、nodejs 等）。"
                f"请安装 Node.js LTS 并确保在 PATH 中，或设置 DM_NODE "
                f"指向 node 可执行文件。")

    def _npm(self, node: str) -> str:
        """node 同目录的 npm 入口（Windows 上必须是 npm.cmd）。"""
        npm = node_with_npm(node)
        if not npm:
            raise RuntimeError(
                f"在 {Path(node).resolve().parent} 下找不到 npm。"
                f"请确认 Node.js 安装完整（Windows 上应有 npm.cmd）。")
        return npm

    def _clean_env(self) -> dict:
        """子进程环境：剔除可能干扰的 npm/node 变量。

        剔 `NODE_OPTIONS`（用户全局的调试参数会打进我们的进程），
        并把 `npm_config_prefix` 之类清掉——面板不该被用户的 npm 全局配置
        牵着走（那是「我本机怎么装」的事，不是「实例怎么装」的事）。
        """
        drop = {"NODE_OPTIONS", "npm_config_prefix", "npm_config_globalconfig",
                "npm_config_userconfig", "npm_config_cache"}
        return {k: v for k, v in os.environ.items() if k not in drop}

    # ---------- 部署 ----------

    def _scaffold(self, instance) -> None:
        """跑 create-koishi 把官方模板铺到实例目录（幂等：已有 package.json 则跳过）。

        ⚠️ 必须带 `-y`：脚手架用 `prompts` 交互，面板无 TTY，不带会直接挂死。
        ⚠️ `-y` 也会跳过它自己的依赖安装（源码 install() 里 if (argv.yes) return），
        所以装依赖是我们自己的下一步——这反而更好：进度能落到面板上。
        """
        root = Path(instance.dir)
        if (root / "package.json").exists():
            return
        node = self._require_node(instance)
        npm = self._npm(node)
        sk = self.m.get("skeleton") or {}
        # ⚠️ 用 `npm create` 而非 `npm init`：`npm init <x>` 会自动补 `create-`
        # 前缀（等价 npm create），传完整包名 `create-koishi` 会变成
        # `create-create-koishi` → npm 报 404。`npm create` 则按原样解析。
        # （这个坑是端到端真跑抓出来的：单测只验"参数含 --yes"，看不出包名重复。）
        cmd = [npm, "create", sk.get("scaffold", "koishi") + "@latest", "--yes"]
        if tpl := sk.get("template"):
            cmd += ["--template", tpl]
        if reg := sk.get("registry"):
            cmd += ["--registry", reg]
        cmd += ["--forced"]
        r = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                           timeout=SCAFFOLD_TIMEOUT, env=self._clean_env())
        if r.returncode != 0 or not (root / "package.json").exists():
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-8:]
            raise RuntimeError(
                "拉取 Koishi 官方模板失败（create-koishi）：\n" + "\n".join(tail))

    def _declared_deps(self) -> dict:
        return self.m.get("dependencies") or {}

    def _ensure_deps(self, instance) -> None:
        """npm install（把清单声明的依赖一并装上）。

        分两步（先 `npm install` 装模板自带，再单独装清单声明项）会让
        "哪些是必需"的语义变模糊，故统一交给npm 一次性装：
        清单声明项先写进 package.json 的 dependencies，再 `npm install`。
        """
        node = self._require_node(instance)
        npm = self._npm(node)
        self._merge_deps_to_pkgjson(instance)
        r = subprocess.run([npm, "install", "--no-audit", "--no-fund"],
                           cwd=str(instance.dir), capture_output=True,
                           text=True, timeout=NPM_TIMEOUT, env=self._clean_env())
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-8:]
            raise RuntimeError("npm install 失败：\n" + "\n".join(tail))

    def _merge_deps_to_pkgjson(self, instance) -> None:
        """把清单 `dependencies` 并入 package.json 的 dependencies。

        为什么必须写进去：Node 的依赖只有进了 package.json（+package-lock.json）
        才会在 `npm install` 时被装上。只在命令行临时 `npm install koishi`
        的话，重装/换机/恢复备份后依赖就没了。
        """
        path = Path(instance.dir) / "package.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        deps = data.get("dependencies")
        if deps is None:
            deps = {}
            data["dependencies"] = deps
        if not isinstance(deps, dict):
            return
        changed = False
        for k, v in self._declared_deps().items():
            if k not in deps:                # 不覆盖用户/模板自己钉的版本
                deps[k] = v or "*"
                changed = True
        if changed:
            write_atomic(path, json.dumps(data, ensure_ascii=False,
                                          indent=2).encode("utf-8"))

    def _deps_installed(self, instance) -> bool:
        """判据：清单声明的依赖**全部**在 node_modules 里。

        用 `any` 不行——只装了 koishi 本体、没装 onebot 适配器时，
        面板会以为装好了，而实例启动后连不上登录端（最难排查的一类故障）。
        """
        nm = Path(instance.dir) / "node_modules"
        if not nm.is_dir():
            return False
        declared = self._declared_deps()
        if not declared:
            return True
        return not self._deps_missing(instance)

    def _deps_missing(self, instance) -> list[str]:
        nm = Path(instance.dir) / "node_modules"
        if not nm.is_dir():
            return list(self._declared_deps())
        missing = []
        for k in self._declared_deps():
            # scoped 包（@koishijs/xxx）的目录名就是原样，无需转换
            if not (nm / k).is_dir():
                missing.append(k)
        return missing

    def verify_required(self, instance) -> list:
        """基类只查 exists()，对**空目录**返回 True。

        npm 装到一半失败会留下空的 `node_modules/`，基类会判成「装好了」，
        deploy 于是误判幂等返回 ok，用户到启动时才见 "Cannot find module"。
        故这里额外查依赖完整性。
        """
        missing = [f for f in self.m["required_files"]
                   if not (Path(instance.dir) / f).exists()]
        if not missing and self._deps_missing(instance):
            missing = [f"node_modules/{p}" for p in self._deps_missing(instance)]
        return missing

    def deploy(self, instance) -> str:
        with program_dir_lock(self.m["name"]):
            self._scaffold(instance)
            if self._deps_installed(instance):
                return "conflict"                # 已装齐，别动用户的 node_modules
            self._ensure_deps(instance)
            self._publish_baseline(instance)
        missing = self.verify_required(instance)
        if missing:
            raise RuntimeError(f"部署后仍缺失: {missing}")
        return "ok"

    def _publish_baseline(self, instance) -> None:
        """记录版本基线。

        ⚠️ 覆写了 `deploy()` 就**丢了** `base.deploy` 里的这行
        （基类只在 deploy 末尾 publish），不自己写的话 wizard 取不到基线，
        实例 `version` 永远是 None。
        """
        blob = self._installed_versions_blob(instance)
        if blob:
            import hashlib
            DEPLOY_VERSION[instance.id] = "npm:" + hashlib.sha256(
                blob.encode("utf-8")).hexdigest()[:12]

    def _installed_versions_blob(self, instance) -> str:
        """已装依赖快照（版本基线的输入）。

        读 `package.json` 的 dependencies 而非扫 node_modules：快得多，
        且**与跨平台无关**（不依赖目录遍历顺序——这正是 nonebot2 在 CI 上
        踩过的坑，见 NoneBot2Adapter._installed_versions 的注释）。
        真正"装没装"的判断交给 `_deps_installed`（查目录存在性）。
        """
        path = Path(instance.dir) / "package.json"
        if not path.exists():
            return ""
        try:
            deps = json.loads(path.read_text("utf-8")).get("dependencies") or {}
        except (OSError, ValueError):
            return ""
        return "\n".join(f"{k}@{v}" for k, v in sorted(deps.items()))

    # ---------- 启动 ----------

    def _cli(self, instance) -> Path:
        """`node_modules/.bin/koishi` —— Koishi 的 CLI 入口（npm 生成的可执行包装）。

        走它而不是 `npm start`：后者会多一层 node + npm 两个进程，而面板跟踪的
        是**直接子进程**（停止/重启都靠它），npm 会吞信号 → 停止实例后留孤儿。
        """
        bindir = Path(instance.dir) / "node_modules" / ".bin"
        for name in ("koishi.cmd", "koishi"):
            p = bindir / name
            if p.is_file():
                return p
        raise RuntimeError(
            f"未找到 Koishi CLI（{bindir / 'koishi'}）。node_modules 可能没装好，"
            f"请删除该实例目录后重新部署。")

    def build_start_cmd(self, instance) -> list[str]:
        """`[<node>, "<node_modules/.bin/koishi[.cmd]>", "start"]`。

        ⚠️ Windows 上必须用 `.cmd`：那是 npm 生成的可执行包装，无扩展名的那个
        直接跑会报「不是有效的 Win32 应用程序」。
        """
        node = self._require_node(instance)
        return [node, str(self._cli(instance)), "start"]

    def configure_login(self, instance, credentials) -> dict:
        return {"needs_login": False}

    def prepare_start(self, instance, runner=None) -> bool:
        """补装缺失依赖（面板重启后 resume 也会走到这里）。

        node_modules 可能被用户手动删掉或装到一半失败，这时补一次即可，
        不必让用户回部署向导。
        """
        if self._deps_installed(instance):
            return False
        self._require_node(instance)
        self._ensure_deps(instance)
        return True

    # ---------- 互联配置（koishi.yml 的 plugins 段） ----------

    def _cfg_path(self, instance) -> Path:
        return Path(instance.dir) / (self.m.get("config_path") or "koishi.yml")

    def _onebot_port(self, instance) -> int:
        alloc = (instance.allocated_ports or {}).get("ob11") \
            or (instance.allocated_ports or {}).get("bot")
        return int(alloc or self.m.get("webui_default_port") or 5140)

    def write_conn_config(self, instance, mode, direction, addr, token,
                          link_id: str | None = None) -> WriteResult:
        """把 OneBot 反向 WS 写进 `koishi.yml` 的 `plugins:` 段。

        与 AstrBot 同为反向 WS：Koishi 监听、登录端连它。方向不对直接报错——
        写坏的表征是「部署成功但连不上」，比直接报错糟糕得多。

        YAML 写入用**块标量**承载路径与 token：任意字符串都能安全落盘，
        不会被 YAML 的引号/特殊字符规则坑到。
        """
        if mode == "milky":
            return WriteResult(
                ok=False,
                manual="Koishi 当前仅适配 OneBot v11（@koishijs/plugin-adapter-onebot），"
                       "Milky 需另装适配器。请把关联登录端改为 OneBot 协议。")
        if direction != "reverse":
            return WriteResult(
                ok=False,
                manual="Koishi 只支持反向 WebSocket（由 Koishi 监听、登录端连它），"
                       "不接受正向 WS。请在向导里把互联方向选为「反向」。")

        path = self._cfg_path(instance)
        port = self._onebot_port(instance)
        ws_path = self.m.get("ob11_reverse_path") or "/onebot"
        try:
            _merge_onebot_yaml(path, port, token, ws_path)
        except (OSError, ValueError) as e:
            return WriteResult(
                ok=False,
                manual=f"无法写入 {path.name}：{e}。"
                       f"请在 Koishi 控制台手工配置「适配器 → OneBot v11」："
                       f"协议 ws（反向服务端）、路径 {ws_path}、"
                       f"端口 {port}，token 与本面板互联配置保持一致。")
        return WriteResult(
            ok=True,
            path=str(path),
            manual=f"已在 {path.name} 写入 OneBot v11 反向 WS 配置"
                   f"（监听 {port}{ws_path}）。"
                   f"请让关联的登录端以 ws://<本机IP>:{port}"
                   f"{ws_path} 连过来。\n"
                   f"另需把配置里的 selfId 填成该登录端的 QQ 号"
                   f"（OneBot 适配器要求它非空才能连上）。\n"
                   f"⚠️ Koishi 无配置热加载，**需重启该实例**才会生效。")

    def _ensure_webui_binding(self, instance) -> None:
        """Koishi 的 5140 控制台默认绑0.0.0.0（server 插件默认 selfMode=false），
        无需改配置。留空是因为真要放开时得改 server 插件的 host 键，
        而那个键在不同插件版本里名字不一，猜错会把配置写坏。
        """
        return None


# ---------- koishi.yml 文本级编辑 ----------

# `plugins:` 段与下一个同级键（或文件末尾）。用于定位插入位置。
_PLUGINS_RE = re.compile(r"^plugins:", re.M)
# 行首无缩进的键 = 顶层键
_TOPKEY_RE = re.compile(r"^([A-Za-z_][\w-]*):", re.M)


def _merge_onebot_yaml(path: Path, port: int, token: str, ws_path: str) -> None:
    """把 adapter-onebot 段并入 koishi.yml 的 plugins 下（幂等）。

    字段**逐字取自上游 schema**（`@satorijs/adapter-onebot@6.0.2` 的
    `OneBotBot.Config = BaseConfig ∪ WsServer.Config`）：

        BaseConfig{ selfId: string(required)、token: string、
                    protocol: "http"|"ws"|"ws-reverse"，默认 ws-reverse }
        WsServer    { protocol: const("ws-reverse")、path: string = "/onebot" }

    ⚠️ `protocol: "ws-reverse"` 是 Koishi 特有的取值（**不是** `ws` 加一段
    `websocket:` —— 那是 NapCat 等「实现端」的写法）。写错的表征是
    「部署成功但连不上」，日志里只看得到连接超时，极难定位。
    ⚠️ `selfId` 是 required，留空会让插件加载不了，故留空串占位，
    由用户在 Koishi 控制台补上（面板拿不到登录端 QQ，见调用处的提示）。

    只做**文本级插入**，不反序列化 YAML 再回写：项目未引 YAML 写库，
    整文件回写会清掉用户的注释与键序（Koishi 配置里注释很多）。
    重复调用整段替换而非追加——YAML 同名键后者覆盖前者，追加会攒出多份。
    """
    # token 用块标量承载：任意字符串都安全，不会被 YAML 引号规则坑到
    tok = f"    token: |\n      {token}\n" if token else "    token: ''\n"
    block = (
        "  adapter-onebot:\n"
        "    protocol: ws-reverse\n"
        "    selfId: ''\n"
        f"    path: {ws_path}\n"
        f"    port: {port}\n"
        f"{tok}"
    )
    text = path.read_text("utf-8") if path.exists() else ""
    merged = _replace_or_append_plugin(text, "adapter-onebot", block)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, merged.encode("utf-8"))


def _replace_or_append_plugin(text: str, key: str, block: str) -> str:
    """在 plugins: 下替换同名 key 的整段，没有则追加到 plugins: 末尾。

    段边界靠"下一个**行首无缩进**的键"判定——Koishi 的插件名都是两空格缩进
    （如 `  adapter-onebot:`），顶层键无缩进。找不到边界时退化为「替换到文件末」，
    这在最后一个插件的情况下是正确的。
    """
    m = _PLUGINS_RE.search(text)
    if not m:
        # 没有 plugins: 段就整体追加（顶层键）
        prefix = text if text.endswith("\n") or not text else text + "\n"
        return f"{prefix}\nplugins:\n{block}"
    # plugins: 到下一个顶层键之间的范围
    start = m.start()
    rest = text[m.end():]
    nxt = _TOPKEY_RE.search(rest)
    end = m.end() + (nxt.start() if nxt else len(rest))
    head, body, tail = text[:start], text[start:end], text[end:]

    body_lines = body.splitlines()
    bkey = re.compile(rf"^(\s*){re.escape(key)}:\s*$", re.M)
    hit = bkey.search(body)
    if not hit:
        # 追加到 body 末尾（保证与plugins: 之间有空行）
        add = "" if body_lines and not body_lines[-1].strip() else "\n"
        return f"{head}{body.rstrip()}\n{add}{block}{tail}"
    # 段结束 = 下一个行首无缩进的非空行
    seg_start = hit.start()
    indent = hit.group(1)
    seg_rest = body[hit.end():]
    nxt2 = re.search(rf"^{indent[:-2]}[A-Za-z_][\w-]*:", seg_rest, re.M) if indent else None
    seg_end = hit.end() + (nxt2.start() if nxt2 else len(seg_rest))
    return f"{head}{body[:seg_start]}{block}{body[seg_end:]}{tail}"
