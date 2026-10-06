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
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from adapters.base import DEPLOY_PROGRESS, DEPLOY_VERSION, BaseAdapter, WriteResult
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
        """子进程环境：剔掉「本机怎么装」的变量，保留「这个仓库怎么装」的。

        **保留 `npm_config_registry`**（用户设的国内镜像源）—— 这是最不该
        剔的一个：服务器/国内机器走官方源会卡到超时（2026-10-06 实测：
        create-koishi 走官方源 600 秒超时，用户明明设了 npmmirror）。
        用户主动设的 registry 是「他要这批包从哪来」，必须尊重。

        剔掉的是「装到哪」这类与实例无关的本机配置：
        - `NODE_OPTIONS`：用户全局的调试参数会打进我们的子进程
        - `npm_config_prefix` / `globalconfig`：会改变 npm 的全局安装位置，
          让面板的安装行为随「谁在跑」变化
        """
        # ⚠️ 键比较必须**大小写不敏感**：Windows 上 os.environ 会把键规范化成
        # 大写（设npm_config_registry 实际存成 NPM_CONFIG_REGISTRY），
        # 区分大小写比较会「剔不掉 prefix、也拦不住大写的 NODE_OPTIONS」。
        drop = {x.upper() for x in
                ("NODE_OPTIONS", "npm_config_prefix", "npm_config_globalconfig")}
        return {k: v for k, v in os.environ.items() if k.upper() not in drop}

    def _registry(self, instance) -> str | None:
        """本次npm 用的registry：**用户的优先，清单的兜底**。

        优先级：环境变量 `npm_config_registry` > `DM_NPM_REGISTRY` >
        清单 `skeleton.registry`。

        ⚠️ 清单里写死官方源是**陷阱**（2026-10-06 实测踩到）：服务器上
        `npm create` 走官方源 600 秒超时，而用户早就设了国内镜像。
        故清单的 registry 只在没有用户意图时兜底。
        """
        # 注意：ruff SIM112 会建议改成大写 NPM_CONFIG_REGISTRY，但 **npm 只认
        # 小写**（Windows 上大小写皆可），改大写在 Linux 上读不到——故保留小写。
        env = os.environ.get("npm_config_registry", "").strip()  # noqa: SIM112
        if not env:
            env = os.environ.get("DM_NPM_REGISTRY", "").strip()
        if env:
            return env
        return (self.m.get("skeleton") or {}).get("registry")

    # ---------- 部署 ----------

    def _scaffold(self, instance) -> None:
        """跑 create-koishi 把官方模板铺到实例目录（幂等：已有 package.json 则跳过）。

        ⚠️ **必须给「位置参数」项目名**（`npm create koishi@latest <name>`）——
        这是无 TTY 环境能否跑通的关键。读create-koishi 源码的 getName()：

            if (argv._[0]) return argv._[0];        // ← 给了位置参数就不问
            else await prompts({message: 'Project name:'})// 否则交互提问

        而 `--yes` **管不到这里**（它只让 install() 跳过"是否装依赖"）。
        漏掉位置参数的表现（2026-10-06 服务器实测）：脚手架打印
        `? Project name: › koishi-app` 然后**永久挂住**直到超时——
        网络完全正常（curl 同一 URL 0.1s），所以只看"超时"会误判成网络问题。

        ⚠️ **位置参数要传 `.` 而不是实例名**（2026-10-06 服务器实测）：
        脚手架把它当**项目目录名**，`getName()` 之后是
        `path.resolve(cwd, project)` —— 传 `koishi-app` 会在实例目录下
        再建一层 `koishi-app/`，而我们要的是直接铺在实例目录。传 `.`
        即 cwd（实测产物正确落在当前目录）。

        ⚠️ **stdin 必须关掉**（`DEVNULL`）—— 这是挂死的**真正原因**，
        比 `--yes` 缺失更关键。create-koichi 的最后一问
        （`? Install and start it now?`）**不受 `--yes` 影响**：npm 把自己
        那个 `--yes` 吃掉了，不会转发给脚手架脚本（服务器实测：
        带与不带 `--yes` 都会问同一个问题）。
        实测对比：stdin 开着 → 永久挂住直到超时；
        stdin 接 DEVNULL → **1.7 秒返回**，拿到的模板完全正确。
        我们本来就要自己装依赖（进度要落到面板上），所以它问不问都无所谓。
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
        # 位置参数 `.` = 铺在当前目录（实例目录），见 docstring
        cmd = [npm, "create", sk.get("scaffold", "koishi") + "@latest", "."]
        if tpl := sk.get("template"):
            cmd += ["--template", tpl]
        if reg := self._registry(instance):
            cmd += ["--registry", reg]
        cmd += ["--forced"]
        # stdin=DEVNULL 是**关键**：不给它会挂在脚手架最后一问上（见 docstring）
        r = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                           stdin=subprocess.DEVNULL,
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
        "哪些是必需"的语义变模糊，故统一交给 npm 一次性装：
        清单声明项先写进 package.json 的 dependencies，再 `npm install`。

        ⚠️ npm 自身的超时/重试参数也要给足：默认 fetch-retries=2、timeout 5分钟，
        网络差时某个包会反复重试把整个部署拖到半小时以上。参数含义：
          - fetch-retries=4 / fetch-retry-maxtimeout=120s：单个包多试几次
          - fetch-timeout=300000：单次请求 5 分钟上限（默认无上限，
            **卡住的连接会让整体挂死**——本项目实测过）
        """
        node = self._require_node(instance)
        npm = self._npm(node)
        self._merge_deps_to_pkgjson(instance)
        cmd = [npm, "install", "--no-audit", "--no-fund",
               "--fetch-retries=4", "--fetch-retry-maxtimeout=120000",
               "--fetch-timeout=300000"]
        try:
            r = subprocess.run(cmd, cwd=str(instance.dir), capture_output=True,
                               text=True, timeout=NPM_TIMEOUT,
                               env=self._clean_env())
        except subprocess.TimeoutExpired as e:
            #裸的 TimeoutExpired 对用户毫无意义，给一句能行动的
            raise RuntimeError(
                f"npm install 超时（超过 {NPM_TIMEOUT // 60} 分钟仍未完成）。"
                f"常见原因：网络慢或某个包的下载卡住。"
                f"建议在服务器上先执行 "
                f"`npm config set registry https://registry.npmmirror.com` "
                f"换国内镜像源，然后重新部署该实例。") from e
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-8:]
            raise RuntimeError("npm install 失败：\n" + "\n".join(tail)
                               + "\n（网络问题可先设国内镜像源："
                                 "npm config set registry "
                                 "https://registry.npmmirror.com）")

    def _merge_deps_to_pkgjson(self, instance, extra: dict | None = None) -> None:
        """把依赖并入 package.json 的 dependencies。

        `extra` 为空时并入清单声明的 `dependencies`（部署用）；
        非空时并入 `extra`（装单个插件时用）。

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
        for k, v in (extra if extra is not None else self._declared_deps()).items():
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
        tag = self._version_tag(instance)
        if tag:
            DEPLOY_VERSION[instance.id] = tag

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

    # ---------- 升级通道（npm install -U，不是换包） ----------

    def upgrade(self, instance) -> str | None:
        """升级依赖：npm update（把 package.json 里的依赖升到各自最新版）。

        base.upgrade 走「下载新包 → 覆盖解压」，对 npm_project 不存在可下载的包。
        这里改为升依赖，返回**新的依赖快照基线**（与 nonebot2 语义一致）。

        **为什么不是 `npm update --latest`**：`--latest` 会无视 package.json
        的版本范围直接跳到最新，可能引入破坏性变更；而 `npm update` 尊重
        `^`/`~` 范围，是「在声明的兼容范围内取最新」—— 与 nonebot2 的
        `pip install -U`（同样尊重声明的下限）口径一致。

        **必须同步 package.json**：升级后把已装版本回写成新的**下限**
        （见 _bump_deps_floor），否则下次 `npm install` 会按旧下限
        把刚升的包降回去（用户视角：「点了升级，一重启又变回旧版」）。
        """
        with program_dir_lock(self.m["name"]):
            if not self._deps_installed(instance):
                raise RuntimeError("实例尚未完成部署（依赖目录为空），请先重新部署")
            key = getattr(instance, "id", None)
            if key:
                DEPLOY_PROGRESS[key] = {"stage": "npm", "done": 0,
                                        "total": len(self._declared_deps())}
            try:
                self._run_npm(instance, ["update"], stage="升级")
                self._bump_deps_floor(instance)
                self._publish_baseline(instance)
            finally:
                if key:
                    DEPLOY_PROGRESS.pop(key, None)
        return self._version_tag(instance)

    def _bump_deps_floor(self, instance) -> None:
        """把 package.json 里各依赖的版本约束抬成「已装版本的下限」。

        形如`^4.18.0` → `^4.19.1`（已是精确版本的补 `>=`）。
        **只改已存在的条目**，不新增也不删除 —— 新增/删除是
        `install_package` / `uninstall_package` 的职责。
        """
        path = Path(instance.dir) / "package.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        deps = data.get("dependencies")
        if not isinstance(deps, dict):
            return
        installed = self._installed_dep_versions(instance)
        changed = False
        for name, spec in list(deps.items()):
            ver = installed.get(name)
            if not ver:
                continue                       # 没装/装不上，保持原约束
            new = _raise_floor(spec, ver)
            if new != spec:
                deps[name] = new
                changed = True
        if changed:
            write_atomic(path, json.dumps(data, ensure_ascii=False,
                                          indent=2).encode("utf-8"))

    # ---------- 插件管理（npm install / remove + 同步 package.json） ----------

    def extra_manage_capabilities(self, instance) -> list[dict]:
        """提供「依赖与插件」面板（kind 与 nonebot2 一致，前端同一套渲染）。

        与 nonebot2 的差别：Koishi 侧的动作是 `npm install/remove <包名>`，
        没有 pyproject 那种"声明与实装分离"的问题——package.json 既是
        声明也是记录，故装/卸只需改 package.json + 重跑 npm。
        """
        if not self._deps_installed(instance):
            return [{"kind": "python_deps", "label": "依赖与插件",
                     "requires_deployed": True,
                     "disabled_reason": "实例尚未完成部署（依赖未装齐），请先重新部署"}]
        return [{"kind": "python_deps", "label": "依赖与插件",
                 "deployed": True, "missing": []}]

    def list_plugins(self, instance) -> dict:
        """已装依赖/插件 + 本地插件目录，供「管理应用」面板渲染。

        插件分两类（Koishi 生态的命名约定）：
        1. **npm 包**：官方 `@koishijs/plugin-*`、社区 `koishi-plugin-*`。
           装在 node_modules，靠 package.json 的 dependencies 识别。
        2. **目录形态插件**（`plugins/<name>/` 下直接是 .py/js）：不在 npm
           体系内，卸载就是删目录——面板只列出来，不提供卸载按钮。
        """
        pkg = self._read_pkg(instance)
        installed = self._installed_dep_versions(instance)
        declared = pkg.get("dependencies") or {}
        plugins = [
            {"name": name, "version": installed.get(name) or declared.get(name, ""),
             "official": name.startswith("@koishijs/")}
            for name in sorted(declared)
            if _is_koishi_plugin(name)
        ]
        base = [
            {"name": name, "version": installed.get(name) or declared.get(name, ""),
             "official": name.startswith("@koishijs/"), "core": True}
            for name in sorted(declared)
            if not _is_koishi_plugin(name)
        ]
        pdir = Path(instance.dir) / "plugins"
        dirs = (sorted(p.name for p in pdir.iterdir()
                       if p.is_dir() and not p.name.startswith((".", "_")))
                if pdir.is_dir() else [])
        return {
            "packages": plugins,
            "core_deps": base,
            "dir_plugins": [{"name": n, "path": str(pdir / n)} for n in dirs],
            "missing": self._deps_missing(instance),
            "libs_path": str(Path(instance.dir) / "node_modules"),
        }

    def install_package(self, instance, spec: str) -> str:
        """装一个 npm 插件并同步进 package.json。

        **两步缺一不可**：只 `npm install <pkg>` 而不写 package.json，
        下次 `npm install` / 恢复备份后依赖就没了（package.json 是
        「声明」也是「记录」，Node 没有 pip 那种 --target 式的分离）。
        """
        spec = (spec or "").strip()
        if not spec:
            raise ValueError("请填写包名")
        name = _npm_name(spec)
        if not name or any(ch.isspace() for ch in name):
            raise ValueError(f"包名不合法：{spec!r}")
        with program_dir_lock(self.m["name"]):
            self._merge_deps_to_pkgjson(instance, {name: _spec_version(spec)})
            self._run_npm(instance, ["install", name], stage="安装")
        ver = self._installed_dep_versions(instance).get(name, "?")
        self._bump_deps_floor(instance)
        self._publish_baseline(instance)
        return ver

    def uninstall_package(self, instance, name: str) -> str:
        """卸载一个 npm 插件：`npm remove` + 从 package.json 移除声明。

        ⚠️ 不做 `npm uninstall --no-save` 那种"只删文件留声明"的做法：
        那样下次 `npm install` 会把它装回来（与 nonebot2 卸载必须同步
        删pyproject 声明同理）。
        """
        name = _npm_name(name)
        pkg = self._read_pkg(instance)
        if name not in (pkg.get("dependencies") or {}):
            raise ValueError(f"{name} 不在 package.json 的依赖里")
        with program_dir_lock(self.m["name"]):
            self._run_npm(instance, ["remove", name], stage="卸载")
            self._remove_dep_from_pkgjson(instance, name)
        ver = self._installed_dep_versions(instance).get(name, "")
        self._publish_baseline(instance)
        return ver

    def sync_pyproject(self, instance) -> str:
        """把 node_modules 里现有的包补写进 package.json。

        场景：用户在实例目录手工 `npm install --no-save xxx` 装了插件，
        缺这一步，下次 `npm install` 会把它清掉。
        """
        path = Path(instance.dir) / "package.json"
        if not path.exists():
            raise ValueError("package.json 不存在，无法同步")
        #全量扫：手工 --no-save 装的包不在 package.json 里，只看声明补不上
        installed = self._installed_dep_versions(instance, only_declared=False)
        if not installed:
            raise ValueError("node_modules 里没有已装依赖，无可同步")
        with program_dir_lock(self.m["name"]):
            self._merge_deps_to_pkgjson(instance,
                                        {k: f">={v}" for k, v in installed.items()})
        return str(path)

    # ---------- npm 调用与版本读取 ----------

    def _run_npm(self, instance, args: list[str], stage: str = "npm") -> str:
        """跑一次 npm，失败时报出可操作的错误（与 _ensure_deps 同一套）。"""
        node = self._require_node(instance)
        npm = self._npm(node)
        cmd = [npm, *args, "--no-audit", "--no-fund",
               "--fetch-retries=4", "--fetch-retry-maxtimeout=120000",
               "--fetch-timeout=300000"]
        try:
            r = subprocess.run(cmd, cwd=str(instance.dir), capture_output=True,
                               text=True, timeout=NPM_TIMEOUT,
                               env=self._clean_env())
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"npm {stage}超时（超过 {NPM_TIMEOUT // 60} 分钟仍未完成）。"
                f"常见原因：网络慢或某个包的下载卡住。可先执行 "
                f"`npm config set registry https://registry.npmmirror.com` "
                f"换国内镜像源后重试。") from e
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-8:]
            raise RuntimeError(f"npm {stage}失败：\n" + "\n".join(tail))
        return r.stdout or ""

    def _read_pkg(self, instance) -> dict:
        path = Path(instance.dir) / "package.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _installed_dep_versions(self, instance, *, only_declared: bool = True) -> dict:
        """已装依赖的**实装版本**：读 node_modules/<pkg>/package.json。

        与 `_installed_versions_blob`（读 package.json 的声明）不同：
        那个是「声明是什么」，这个是「实际装了什么」。升级后抬下限要用
        实际版本，才不会出现「声明 ^4.18.0 但实装 4.19.1」时的错位。

        `only_declared=False` 时扫 node_modules 下**所有**已装包（含用户
        手工 `--no-save` 装的）—— `sync_pyproject` 要用它：那正是「手工装了
        没写进 package.json、差点被清掉」的补救场景，只看声明就永远补不上。

        查目录**遍历顺序无关**（按包名逐个定位，不依赖 glob 顺序）——
        跨平台顺序问题见 NoneBot2Adapter._installed_versions 的注释。
        """
        out: dict[str, str] = {}
        nm = Path(instance.dir) / "node_modules"
        if not nm.is_dir():
            return out
        if only_declared:
            names: list[str] = list(self._read_pkg(instance).get("dependencies") or {})
        else:
            names = _walk_packages(nm)
        for name in names:
            pj = nm / name / "package.json"
            if not pj.is_file():
                continue
            try:
                meta = json.loads(pj.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict) and meta.get("version"):
                out[name] = str(meta["version"])
        return out

    def _version_tag(self, instance) -> str | None:
        """版本基线标签（`npm:<hash>`），与 nonebot2 的 `pip:<hash>` 同形。

        标签相同即代表「依赖快照没变」—— 上游同 tag 即可判为已是最新。
        """
        blob = self._installed_versions_blob(instance)
        if not blob:
            return None
        return "npm:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    def _remove_dep_from_pkgjson(self, instance, name: str) -> None:
        path = Path(instance.dir) / "package.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        deps = data.get("dependencies")
        if not isinstance(deps, dict) or name not in deps:
            return
        deps.pop(name)
        write_atomic(path, json.dumps(data, ensure_ascii=False,
                                      indent=2).encode("utf-8"))

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


# ---------- 包名 / 版本约束的文本处理 ----------

_NPM_RANGE_PREFIX = ("^", "~", ">=", "<=", ">", "<", "=")


def _npm_name(spec: str) -> str:
    """从 npm 规格串取包名：`koishi-plugin-x@^1.0` → `koishi-plugin-x`。

    ⚠️ **scoped 包里的 `@` 不能当版本分隔符**：`@koishijs/plugin-adapter-onebot`
    开头的 `@` 是scope 标记，不是「@版本」。故只在**非首位**的 `@` 处切。
    """
    spec = (spec or "").strip()
    at = spec.find("@", 1)          # 从第2 个字符起找，避开 scope 的首字符
    return (spec[:at] if at > 0 else spec).strip()


def _spec_version(spec: str) -> str:
    """取版本约束段（`pkg@^1.0` → `^1.0`；无则空串）。

    ⚠️ **要跳过 `@` 本身**：`@koishijs/pkg@^6.0.0` 的分隔符是**第二个** `@`，
    直接 `spec[at:]` 会把 `@` 一起带进约束（写成 `^6.0.0` 之外的 `@^6.0.0`，
    npm 解析不了）。故从 `at + 1` 取。
    """
    spec = (spec or "").strip()
    at = spec.find("@", 1)          # 从第 2 个字符起找，避开 scope 的首字符
    return spec[at + 1:].strip() if at > 0 else ""


def _is_koishi_plugin(name: str) -> bool:
    """是否为「插件包」而非核心依赖。

    Koishi 生态的命名约定：官方 `@koishijs/plugin-*`、社区 `koishi-plugin-*`。
    核心依赖（koishi 本体、@satorijs/* 等）不算插件 —— 混进插件列表会
    让用户以为能卸载 koishi 本体，卸掉实例直接起不来。
    """
    return (name.startswith("koishi-plugin-")
            or name.startswith("@koishijs/plugin-"))


def _raise_floor(spec: str, installed: str) -> str:
    """把版本约束抬到「不低于已装版本」，保留原有的范围类型。

    - `^1.2.3` + 实装 `1.4.0` → `^1.4.0`（仍在同一 major 内，与原意一致）
    - `~1.2.3` + 实装 `1.2.9` → `~1.2.9`
    - `1.2.3`（精确）+ 实装 `1.4.0` → `^1.4.0`（精确版本升不动，改范围）
    - `>=1.0.0` + 实装 `1.4.0` → `>=1.4.0`
    - `*` / 空 → 不动（无约束可抬）

    ⚠️ 实装版本里带预发布标记（`4.0.0-beta.1`）时原样使用：那才是实际
    跑着的版本，写成下限才不会「下次装回正式版导致行为突变」。
    """
    spec = (spec or "").strip()
    if not spec or spec == "*" or spec == "latest":
        return spec
    for pfx in _NPM_RANGE_PREFIX:
        if spec.startswith(pfx):
            return f"{pfx}{installed}"
    return f"^{installed}"          # 精确版本 → 改成 caret 范围


def _walk_packages(nm: Path) -> list[str]:
    """列出 node_modules 下所有已装包（含 scoped，展开成 `a/b` 形态）。

    `node_modules/@scope/pkg` 是两级目录，故要单独处理 `@` 开头的项。
    逐项定位而非 glob 整树，结果**与遍历顺序无关**。
    """
    out: list[str] = []
    if not nm.is_dir():
        return out
    for p in nm.iterdir():
        if p.name.startswith("."):
            continue
        if p.name.startswith("@") and p.is_dir():
            out += [f"{p.name}/{q.name}" for q in p.iterdir()
                    if q.is_dir() and not q.name.startswith(".")]
        elif p.is_dir():
            out.append(p.name)
    return sorted(out)


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

    ⚠️ 段边界 = **与目标键同缩进**的下一个键，不是"顶层键"（踩过）：
    Koishi 的插件段都缩进两空格，而 `group:xxx:` 这些分组键**也是**两空格，
    与插件名同级。早期实现找的是"行首无缩进的键"，找不到就替换到文件末——
    结果把 `group:adapter` 整段（含 ~adapter-discord、database-sqlite）
    全部吃掉了，表现为「配置写完后别的插件凭空消失」。

    正确判据：目标键缩进 2 → 边界是下一个**缩进恰好 2**的键
    （`group:` 与其它插件都算）；目标键是缩进 0（顶层）→ 找下一个缩进 0 的键。
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
    # 段结束 = 下一个**同缩进**的键（含空行时以键行为准）
    seg_start = hit.start()
    indent = hit.group(1)
    seg_rest = body[hit.end():]
    nxt2 = re.search(rf"^{re.escape(indent)}[A-Za-z_~][\w-]*:", seg_rest, re.M)
    seg_end = hit.end() + (nxt2.start() if nxt2 else len(seg_rest))
    return f"{head}{body[:seg_start]}{block}{body[seg_end:]}{tail}"
