"""适配器基类与公共契约（终检后统一签名）"""
import base64
import json
import os
import re
import shutil
import socket
import urllib.request
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from core import ghdl
from core import packages as pkgstore
from core.atomicio import atomic_write_json
from core.locks import program_dir_lock

# 网络暴露（server / desktop 分化 C4）：
#   server → core.firewall.open_port 真正放通防火墙端口（对外提供 WebUI/连接端口）
#   desktop → no-op：单机本地工具只监听 127.0.0.1，无需放行任何端口
# expose_webui 调用 open_port(port) 两侧都可用，desktop 侧恒返回 None。
if os.name == "posix":
    from core.firewall import open_port
else:
    # 签名须与 server 侧逐字一致：mypy 要求条件定义的函数变体同签名，
    # 省略注解会被推成 def open_port(port: Any) -> Any 而报错。
    def open_port(port: int | None) -> str | None:
        return None

# 视为「仅本机监听」的绑定地址：开放 WebUI 时统一放开为 0.0.0.0
LOOPBACK_HOSTS = ("", "127.0.0.1", "localhost", "::1")

# 部署进度（inst_id → {stage, done, total}）：step2 同步部署期间前端轮询展示，
# 避免大包下载数分钟里界面「假死」。进程内存态即可，管理器重启后部署本就要重跑。
DEPLOY_PROGRESS: dict[str, dict] = {}
# 部署完成的版本记录（inst_id → release tag）：base 不持有 registry，
# 由向导 step2 部署完后取走落盘（升级通道比对基线）
DEPLOY_VERSION: dict[str, str] = {}


def deploy_version_of(inst_id: str) -> str | None:
    return DEPLOY_VERSION.pop(inst_id, None)


def deploy_progress_of(inst_id: str) -> dict:
    """取部署进度供前端轮询。**读取即消费 error 态**。

    error 态是一次性的通知（「这次下载为什么失败」），不是要长期保留的状态：
    留在 DEPLOY_PROGRESS 里会内存泄漏（每次失败部署留一条），且下次同实例
    重新部署时若还没重置，前端会先读到上次的旧错误。读走即清最省事。
    """
    p = DEPLOY_PROGRESS.get(inst_id)
    if p and p.get("stage") == "error":
        DEPLOY_PROGRESS.pop(inst_id, None)
        return p
    return p or {"stage": "idle"}


def _download_error_hint(e: Exception) -> str:
    """把下载/部署的原始异常翻成用户能据此行动的提示。

    urllib 的报错（URLError/socket.timeout/HTTPError）对普通用户是天书：
    只说「<urlopen error timed out>」既不知道是哪一步坏，也不知道该重试还是换网络。
    这里按异常类型给出下一步动作，原始信息仍保留在末尾（排查要用）。
    """
    raw = str(e) or e.__class__.__name__
    low = raw.lower()
    if isinstance(e, urllib.error.HTTPError):
        hint = {403: "下载源拒绝访问（403），通常是 GitHub 限流或该资产已删除",
                404: "下载地址已失效（404），程序可能已发新版本或改了资产名",
                429: "下载被限流（429），请稍后重试"}.get(
                    e.code, f"下载返回 HTTP {e.code}")
    elif "timed out" in low or "timeout" in low:
        hint = "下载超时，网络不通或 GitHub 直连被阻断（可稍后重试，或改用镜像 / 上传压缩包）"
    elif "urlopen error" in low and ("name or service not known" in low
                                     or "nodename nor servname" in low
                                     or "temporary failure" in low):
        hint = "域名解析失败，服务器网络不通或 DNS 异常"
    elif "urlopen error" in low and "connection" in low:
        hint = "连接被拒绝/重置，可能是网络策略拦截或下载源已不可用"
    elif "certificate" in low or "ssl" in low:
        hint = "HTTPS 证书校验失败，可能是系统时间不准或证书链有问题"
    elif "sha256" in low:
        hint = "下载包校验不通过（可能被中途篡改或代理改写），请重试或上传正确的压缩包"
    elif "not a valid zip" in low or "压缩包" in low:
        hint = "下载内容不是有效的压缩包（可能下到了 HTML 错误页），请重试或上传压缩包"
    elif "磁盘" in raw or "no space" in low:
        hint = "磁盘空间不足，请清理后重试"
    elif "rc=-9" in low or "rc=-15" in raw or "killed" in low or "memoryerror" in low:
        # 服务器上真实发生过：2G 小内存机 pip 装依赖时被 systemd OOM kill，
        # 表现为子进程 rc=-15（SIGTERM）。用户看到「rc=-15」完全无从判断。
        hint = "内存不足，进程被系统终止（本机内存较小，请先停止其他实例再重试）"
    else:
        hint = "部署过程中断"
    return f"{hint}（{raw[:200]}）" if raw and raw not in hint else hint


def _safe_tar_members(tf, target: Path):
    """tarfile 无 filter= 参数时的降级成员筛选（Python < 3.10.12）。

    手工复刻 filter="data" 的关键约束：剔除绝对路径/盘符、剔除向上穿越的 ..、
    剔除符号链接与设备文件（否则可用链接把实例目录外的文件替换掉）。
    """
    kept = []
    for m in tf.getmembers():
        if m.issym() or m.islnk() or not (m.isfile() or m.isdir()):
            continue
        rel = os.path.normpath(m.name.replace("\\", "/"))
        if os.path.isabs(rel) or ":" in rel.split("/")[0] or rel.startswith(".."):
            continue
        m.name = rel                             # 归一化后再交给 extractall
        kept.append(m)
    return kept

def mirror_url(url: str) -> str:
    """需要访问 GitHub 时统一走这里：**按 host 探测连通性，失败才切镜像**。

    ⚠️ 旧实现是静态开关（`DM_GITHUB_MIRROR` 设了就全走镜像），两个问题：
    - 一律走镜像时，**镜像本身可能挂**（实测 `raw.gitmirror.com` 连不上、
      `mirror.ghproxy.com` 超时 25 秒）—— 镜像不可用比直连更糟
    - 一律走官方时，国内某些路径会卡 30 秒才404，用户只看到「下载失败」

    现在按 host 缓存探测结果：官方通就用官方，不通才按候选列表试镜像。
    详见 core/ghdl.py（含各环境变量语义）。

    手动设 `DM_GITHUB_MIRROR` 仍是最高优先级，且**跳过探测** ——
    用户显式指定时不该被自动探测推翻。
    """
    return ghdl.url_for(url)

@dataclass
class WriteResult:
    """write_conn_config 统一返回值。

    ok=False 表示写入失败；manual 非空表示有需要用户知晓的提示（成功时的重启说明、
    失败时的人工兜底指引都走这里）；path 为实际写入的配置文件路径，便于前端展示。
    """
    ok: bool = True
    manual: str | None = None
    path: str | None = None

class BaseAdapter(ABC):
    def __init__(self, manifest: dict):
        self.m = manifest
        self._last_tag: str | None = None    # 最近一次解析到的上游 release tag

    # ---------- 部署 ----------
    def deploy(self, instance) -> str:
        with program_dir_lock(self.m["name"]):
            # 进度键取 instance.id；测试桩常用无 id 的 SimpleNamespace，跳过进度跟踪
            key = getattr(instance, "id", None)
            # 无论走哪条分支都先重置：上一轮残留的 error 态（内存里没被读走的那种）
            # 会让这次部署一进来就显示旧错误，用户以为还没点就失败了。
            if key:
                DEPLOY_PROGRESS[key] = {"stage": "prepare", "done": 0, "total": 0}
            p = Path(instance.dir)
            if p.exists():
                missing = self.verify_required(instance)
                if key:
                    DEPLOY_PROGRESS.pop(key, None)   # 冲突/已就绪：无进度可言，别留 prepare 态
                return "ok" if not missing else "conflict"    # 冲突 → 前端弹窗
            try:
                archive = self._acquire_archive(instance)  # 本地包优先，没有才在线下载
                if expected := self.m.get("sha256"):
                    self._verify_sha256(archive, expected)
                if key:
                    DEPLOY_PROGRESS[key] = {"stage": "extract", "done": 0, "total": 0}
                self._extract(archive, instance)
                if missing := self.verify_required(instance):
                    raise RuntimeError(f"部署后缺失必备文件: {missing}")
                if key and (tag := self._last_tag):
                    DEPLOY_VERSION[key] = tag     # 升级通道的比对基线
            except Exception as e:
                # 失败原因留一小会儿给轮询读到：前端轮询的异常分支要能显示「为什么失败」
                # （断网/超时/校验不过），否则用户只看到进度条停在半路，无从判断该重试还是换网络。
                # doStep 的 HTTP 响应也会带回同一消息，两条路径不冲突（轮询只是更早到）。
                if key:
                    DEPLOY_PROGRESS[key] = {"stage": "error", "done": 0, "total": 0,
                                            "error": _download_error_hint(e)}
                raise
            finally:
                # error 态不立刻清：留给在途的那次轮询读走；成功态立即清（前端已进下一步）
                if key and DEPLOY_PROGRESS.get(key, {}).get("stage") != "error":
                    DEPLOY_PROGRESS.pop(key, None)
        return "ok"

    def _acquire_archive(self, instance) -> Path:
        """取包：本地 packages/<dice>.<ext> 直接用；否则下载并缓存供后续复用。"""
        cached = pkgstore.find_archive(self.m["name"])
        if cached:
            return cached
        url = mirror_url(self._resolve_download())
        tmp = pkgstore.pkg_dir() / f"{self.m['name']}.dl.tmp"
        key = getattr(instance, "id", None)
        try:
            with urllib.request.urlopen(url, timeout=600) as resp, \
                    open(tmp, "wb") as f:
                total = 0                            # 假响应/无 Content-Length 时退化为未知总量
                try:
                    total = int(resp.headers.get("Content-Length") or 0)
                except (AttributeError, TypeError, ValueError):
                    total = 0
                if key:
                    DEPLOY_PROGRESS[key] = {"stage": "download", "done": 0, "total": total}
                done = 0
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if key:
                        DEPLOY_PROGRESS[key] = {
                            "stage": "download", "done": done, "total": max(total, done)}
            if pkgstore.detect_kind(tmp) is None:
                raise RuntimeError("下载内容不是有效的 zip/tar 压缩包")
            pkgstore.commit_archive(self.m["name"], tmp, "download")
        finally:
            tmp.unlink(missing_ok=True)
        cached = pkgstore.find_archive(self.m["name"])   # 刚 commit 过，必然命中
        if not cached:
            raise RuntimeError("程序包缓存写入失败，请重试")
        return cached

    def _verify_sha256(self, archive: Path, expected: str) -> None:
        import hashlib
        h = hashlib.sha256()
        with open(archive, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != expected.lower():
            if pkgstore.find_archive(self.m["name"]):
                raise RuntimeError(
                    "本地程序包 sha256 与清单不一致：请上传正确版本的压缩包，"
                    "或在 WebUI 删除该包后重新部署")
            raise RuntimeError(f"下载包 sha256 校验失败（期望 {expected[:12]}…）")

    def _extract(self, archive: Path, instance):
        target = Path(instance.dir)
        target.mkdir(parents=True, exist_ok=True)
        kind = pkgstore.detect_kind(archive)
        if kind == ".zip":
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(target)
        else:                                    # tar 系（gzip/xz/bz2 由 tarfile 自动识别）
            import tarfile
            # filter="data" 防 tar 内绝对路径/../ 穿越解压（等价 zip 的取成员名安全做法）
            with tarfile.open(archive, "r:*") as tf:
                try:
                    tf.extractall(target, filter="data")
                except TypeError:                # Python < 3.10.12 无 filter 参数
                    tf.extractall(target, members=_safe_tar_members(tf, target))
        # 归一化：压缩包的顶层目录深度不固定，把必备文件「就地」暴露到实例根目录。
        #
        # 为什么不能只处理「单层顶层目录」（旧做法）：包形态有三种，逐一实测过
        #   ① 条目全在根            → 无需处理
        #   ② 单一顶层目录          → 上移一层即可（旧做法覆盖）
        #   ③ 顶层一个目录 + 里面多级嵌套（如 .NET publish 产物：
        #      ./Lagrange.OneBot/bin/Release/net9.0/linux-x64/publish/Lagrange.OneBot，
        #      四层深）→ 旧做法会把 bin/ 连同 publish 一起上移，
        #      Lagrange.OneBot 仍落在 bin/Release/... 下 → verify_required 报
        #      「部署后缺失必备文件」，实例卡在 DEPLOYING。
        # 所以这里以清单的 required_files 为准绳：找到它实际在哪一层，
        # 就把那一层的内容上移到实例根目录。
        self._flatten_to_required(target)

    def _flatten_to_required(self, target: Path) -> None:
        """把必备文件所在目录的内容上移到实例根，让 exe 路径 = dir/<exe>。

        以 required_files 定位而非猜深度：
        - 已在根目录 → 什么都不做（形态 ①）
        - 顶层单目录 → 上移一层（形态 ②）
        - 顶层单目录 + 深嵌套 → 上移「含 exe 的那一层」（形态 ③）
        找不到时原样保留，交给 verify_required 报明确的缺失清单（别在这里猜）。
        """
        required = self.m.get("required_files") or []
        if not required:
            return                                    # 清单没声明 → 无从判断，保持原样
        # 守卫必须要求是**文件**：`required_files` 是文件清单，而形态③的顶层壳目录
        # 可能与 exe 同名（Lagrange.OneBot/bin/.../publish/Lagrange.OneBot）。
        # 只判 `.exists()` 会把那个空目录当成「已就绪」而直接返回 —— 正是这类包
        # 部署后卡在 DEPLOYING 的原因（exe 其实还在四层深）。
        if all((target / r).is_file() for r in required):
            return                                    # 形态 ①：已在根，无需处理

        # BFS 找「必备文件全在同一层」的最浅目录。逐层下探而不是猜固定深度：
        # 包的顶层深度是上游打包决定的（.NET publish 四层、带版本号两层、根平铺零层），
        # 写死任何深度都会在另一种形态上复发。
        src: Path | None = None
        queue = [target]
        seen: set[Path] = set()
        while queue and src is None:
            d = queue.pop(0)
            if d in seen:
                continue
            seen.add(d)
            if d != target and all((d / r).is_file() for r in required):
                src = d                               # 首个命中即最浅
                break
            if len(seen) > 2000:                      # 防御超大/异常深的包
                break
            try:
                queue.extend(c for c in d.iterdir() if c.is_dir())
            except OSError:
                continue
        if src is None:
            return                                    # 找不到 → 留给 verify_required 如实报错

        for child in src.iterdir():
            dest = target / child.name
            if not dest.exists():
                shutil.move(str(child), str(dest))
            elif child.is_dir() and dest.is_dir():
                # 同名目录（形态③：顶层壳名 Lagrange.OneBot 与 exe 同名）→
                # 必须**递归合并**而不是跳过整个子树。旧写法在这里 continue，
                # 结果 publish 层里 exe 的所有同级文件（appsettings.json 等）一个没搬，
                # 部署过了但程序读到的是空配置。
                self._merge_tree(child, dest)
        self._prune_empty_dirs(src, target)

    @staticmethod
    def _merge_tree(src: Path, dest: Path) -> None:
        """把 src 目录树并入已存在的 dest：文件同名则跳过（不覆盖用户已有内容）。"""
        for child in src.iterdir():
            d = dest / child.name
            if not d.exists():
                shutil.move(str(child), str(d))
            elif child.is_dir() and d.is_dir():
                BaseAdapter._merge_tree(child, d)

    @staticmethod
    def _prune_empty_dirs(src: Path, target: Path) -> None:
        """自底向上删空目录。

        只清**本次搬运路径上**的空壳（src 那条链），不全局扫：包里自带的空目录
        （如 data/、logs/ 这类运行时目录）虽空但有语义，删了可能让程序行为异常。
        """
        p: Path | None = src
        while p and p.resolve() != target.resolve():
            try:
                if p.is_dir() and not any(p.iterdir()):
                    p.rmdir()
                    p = p.parent
                    continue
            except OSError:
                pass
            return

    def _resolve_download(self) -> str:
        return self._resolve_release()[0]

    def _resolve_release(self) -> tuple[str, str | None]:
        """解析下载地址；返回 (url, release_tag|None)。tag 供升级通道记录版本基线。"""
        strat = self.m.get("download_strategy", "direct")
        if strat == "direct":
            return self.m["download"], None
        if strat == "manual":
            # 上游不发行可直接运行的程序包（如 Dice! 只发平台 dll 模块）：
            # 本地无包时明确引导上传离线包，避免下到「能解压但跑不起来」的错包
            raise RuntimeError(
                "该程序不提供在线下载，请先在 WebUI「离线程序包」上传程序包后重新部署"
                + (f"（{self.m['prerequisite']}）" if self.m.get("prerequisite") else ""))
        if strat == "resolve_latest_via_api":
            rp = urlparse(self.m["release_page"]).path        # /owner/repo/releases
            repo = rp.split("/releases")[0].strip("/")
            # release_tag：上游没有 latest release（如 Lagrange 只有 nightly 滚动 tag）
            # 时按固定 tag 取，避免 /releases/latest 直接 404
            tag = self.m.get("release_tag")
            api = (f"https://api.github.com/repos/{repo}/releases/latest" if not tag
                   else f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
            req = urllib.request.Request(
                api,
                headers={"Accept": "application/vnd.github+json", "User-Agent": "DiceManager"})
            # GitHub API 同样受国内网络影响，走 mirror_url（官方不通则加镜像前缀）。
            # ⚠️ 保持传 **Request 对象**而非字符串：测试要 mock 它的 .full_url，
            #    传字符串会让 mock 拿不到该属性（2026-10-07 踩过）。
            mreq = urllib.request.Request(mirror_url(req.full_url), headers=req.headers)
            release = json.load(urllib.request.urlopen(mreq, timeout=30))
            tag = release.get("tag_name") or tag
            assets = release["assets"]
            pattern = self.m.get("asset_name_pattern")
            if pattern:                               # 正则精确选资产（如 linux-x64 完整包）
                for a in assets:
                    if re.search(pattern, a["name"]):
                        self._last_tag = tag
                        return a["browser_download_url"], tag
            suffix = self.m.get("asset_suffix", ".zip")   # 退化为后缀匹配
            for a in assets:
                if a["name"].endswith(suffix):
                    self._last_tag = tag
                    return a["browser_download_url"], tag
            raise RuntimeError(f"release 未找到匹配资产（pattern={pattern}, suffix={suffix}）")
        if strat == "pip_project":
            # 无可下载的程序包（上游在 PyPI）：部署走适配器覆写的 deploy()，
            # 升级语义是 pip install -U 而非换包，故到不了这里。留这条分支是为了
            # 将来若有调用方误用时报出人话，而不是掉到末尾的「未知下载策略」。
            raise RuntimeError(
                "该程序为 Python 依赖项目（pip_project），无可下载的程序包；"
                "依赖升级请用「管理应用」或 pip install -U")
        if strat == "npm_project":
            # 同理：Node 依赖项目（Koishi）在 npm registry 而非 GitHub Release，
            # 无可下载资产；升级语义是 npm install -U。
            raise RuntimeError(
                "该程序为 Node 依赖项目（npm_project），无可下载的程序包；"
                "依赖升级请用「管理应用」或 npm install -U")
        raise ValueError(f"未知下载策略: {strat}")

    def verify_required(self, instance) -> list:
        return [f for f in self.m["required_files"]
                if not (Path(instance.dir) / f).exists()]

    # ---------- 升级通道 ----------
    def latest_tag(self) -> str | None:
        """上游最新版本号；无法判定（直链固定 URL / manual / pip_project）返回 None。"""
        strat = self.m.get("download_strategy", "direct")
        if strat in ("manual", "direct", "pip_project", "npm_project"):
            # pip_project / npm_project 无上游 release 概念，版本基线是「已装依赖快照」
            # （由适配器自己 freeze，见 NoneBot2Adapter._freeze_baseline），
            # 不走 release tag 比对，升级语义是 pip install -U 而非换包。
            return None
        _, tag = self._resolve_release()
        return tag

    def upgrade(self, instance) -> str | None:
        """原地升级（实例须已停机，调用方负责备份与重启）：

        绕过缓存重新下载最新包 → 覆盖解压（包内文件覆盖、包外文件保留 =
        数据/存档不动）→ 校验必备文件。返回新版本 tag（直链策略为 None）。
        """
        with program_dir_lock(self.m["name"]):
            url, tag = self._resolve_release()
            archive = pkgstore.find_archive(self.m["name"])
            tmp = pkgstore.pkg_dir() / f"{self.m['name']}.dl.tmp"
            try:
                with urllib.request.urlopen(mirror_url(url), timeout=600) as resp, \
                        open(tmp, "wb") as f:
                    shutil.copyfileobj(resp, f)
                if pkgstore.detect_kind(tmp) is None:
                    raise RuntimeError("下载内容不是有效的 zip/tar 压缩包")
                # 覆盖本地缓存：升级后新装实例也拿到新版本
                pkgstore.commit_archive(self.m["name"], tmp, "download")
                archive = pkgstore.find_archive(self.m["name"])
                assert archive is not None           # 刚 commit 过，必然命中
            finally:
                tmp.unlink(missing_ok=True)
            if not archive:                    # 理论不可能（刚 commit 过），但类型与防御都要收紧
                raise RuntimeError("程序包缺失：下载缓存后仍找不到本地包，无法升级")
            if expected := self.m.get("sha256"):
                self._verify_sha256(archive, expected)
            self._extract(archive, instance)
            if missing := self.verify_required(instance):
                raise RuntimeError(f"升级后缺失必备文件: {missing}（程序包内容不完整？）")
        return tag

    # ---------- 启动命令：唯一权威入口（安全修正：前端不再传 cmd）----------
    @abstractmethod
    def build_start_cmd(self, instance) -> list[str]: ...

    def prepare_start(self, instance, runner=None) -> bool:
        """启动前的准备动作（默认无）。

        runner: 形如 run(cmd, cwd, label) 的可调用对象，用于执行同步命令并把输出汇入
        该实例的日志流（LLBot 首启 --update 用它）。
        返回值=True 表示执行了只应做一次的动作，由调用方落盘 instance.first_run_done。
        """
        return False

    # ---------- WebUI 对外开放（登录/启动前调用）----------
    def expose_webui(self, instance) -> str | None:
        """确保程序自带 WebUI 可被外部访问：先由适配器修正配置文件里的回环
        监听（_ensure_webui_binding），再尽力经 ufw 放行端口。任何失败都不
        阻断启动；返回给前端的提示，无动作时 None。"""
        self._ensure_webui_binding(instance)
        return open_port(self._webui_port(instance))

    def _ensure_webui_binding(self, instance) -> None:
        """有 WebUI 监听配置文件的适配器覆写：把回环绑定放开为 0.0.0.0。
        默认无配置文件可改（如 Lagrange 无 WebUI）。具体实现复用 _open_bind_host。"""
        return None

    # ---------- 管理应用（总览页「管理应用」按钮的后端）----------
    def manage_capabilities(self, instance) -> list[dict]:
        """该实例能提供哪些「管理」入口，供前端渲染「管理应用」面板。

        为什么要有这一层而不是前端按dice 名硬编码：程序形态差异很大——
        有的自带 WebUI（NapCat/LLBot 扫码、OlivaDice 配置），有的什么都没有
        （NoneBot2 只有一堆 pip 依赖可管）。让前端写 if (dice === 'nonebot2')
        就等于把后端契约漏进前端，改清单就会漏改前端。

        默认实现：只报一条 `webui`（端口可推导时）。适配器有额外能力就覆写
        并 `super()` 追加——这样"有 WebUI"这条永远由本方法统一判定，
        不会因某个适配器覆写而丢掉 WebUI 入口。
        """
        caps: list[dict] = []
        if self._webui_port(instance):
            caps.append({"kind": "webui", "label": "打开 WebUI"})
        caps.extend(self.extra_manage_capabilities(instance))
        return caps

    def extra_manage_capabilities(self, instance) -> list[dict]:
        """本程序特有的管理能力（默认无）。子类覆写时记得调super()."""
        return []

    def _webui_port(self, instance) -> int | None:
        """本实例的 WebUI 端口；无 WebUI 返回 None。

        实际端口优先于清单默认值：端口被占时程序常自动 +1 换端口，
        用默认值会打不开（这类"点开是空白页"的坑之前踩过）。
        """
        return ((instance.allocated_ports or {}).get("webui")
                or self.m.get("webui_default_port"))

    @staticmethod
    def _open_bind_host(cfg: Path, *, host_key: str, port: int | None = None,
                        port_key: str | None = None,
                        extra_defaults: dict | None = None) -> None:
        """把「监听在 JSON 配置里的 WebUI」从回环放开为 0.0.0.0。

        文件已存在 → 仅把 host_key 在回环集合内时改写（端口/其余字段不动，避免丢用户改动）；
        文件缺失且给了 port → 按 {host_key:0.0.0.0, port_key:port, **extra_defaults} 新建。
        NapCat（host/port/loginRate）/ SnowLuma（webuiHost/webuiPort）共用，消除近字重复。
        """
        def _open(c: dict) -> dict:
            if str(c.get(host_key) or "").strip().lower() in LOOPBACK_HOSTS:
                c[host_key] = "0.0.0.0"
            return c

        if cfg.exists():
            atomic_write_json(cfg, _open)
        elif port:
            base = dict(extra_defaults or {})
            base[host_key] = "0.0.0.0"
            if port_key:
                base[port_key] = int(port)
            atomic_write_json(cfg, lambda c: {**c, **base})

    @staticmethod
    def gen_token(n: int = 16) -> str:
        """互联令牌：OneBot 两端的 token 必须一致，由后端生成避免空 token。"""
        import secrets
        return secrets.token_urlsafe(n)

    @staticmethod
    def read_json(path) -> dict:
        """异常安全的配置读取：文件缺失/损坏/非对象一律返回 {}。

        各适配器回读 token、UIN 时四处重复 try/except + exists 判空，口径容易走偏
        （有的吞 OSError、有的只吞 ValueError），统一由此收敛。"""
        try:
            data = json.loads(Path(path).read_text("utf-8", errors="ignore"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    # ---------- 登录与互联 ----------
    @abstractmethod
    def configure_login(self, instance, credentials: dict) -> dict: ...

    # ---------- 登录方式（能力声明，不写程序名分支）----------
    # 覆写本方法即视为支持密码登录，前端据此在登录区给出「扫码 / 账号密码」二选一。
    # 默认只支持扫码——**密码登录必须逐个程序确认过配置字段后再开**，不能因为
    # 「QQ 协议端一般都能账密登录」就一律放开：字段写错时程序会静默忽略并退回扫码，
    # 用户以为设了密码其实每次都在扫码（而扫码的滑块/风控成本远高于账密登录）。
    def login_modes(self) -> list[str]:
        return ["qrcode"]

    def save_login_credentials(self, instance, credentials: dict) -> dict:
        """把账号密码写入程序自己的配置，使其以后能免扫码快速登录。

        ⚠️ **密码明文落盘**（QQ 协议端的既定事实：Lagrange 的 Account.Password、
        NapCat 的 account.password 都是明文，管理器无法代为加密——程序自己读不出来）。
        因此实现方**只写程序配置文件，不落在管理器状态库**，备份/导出也就不会把密码
        带出去；对应的代价是服务器上能读该目录的人就拿到了密码，前端必须明示风险。

        返回 {"restart": bool, "manual": str, "path": str|None}：
          · restart=True → 凭据在启动时读取，需要重启进程才生效（向导据此提示）
          · manual非空 → 该程序无法由面板代劳，文字说明交由用户手动完成
        默认不支持（login_modes 未含 account 时不会被调用）。
        """
        return {"restart": False, "manual": "该程序不支持由面板写入账号密码"}
    @abstractmethod
    def write_conn_config(self, instance, mode: str, direction: str,
                          addr: str, token: str,
                          link_id: str | None = None) -> WriteResult: ...

    def list_accounts(self, instance) -> list[dict]:
        """登录端：回读已登录的全部 QQ 账号 [{qq, token, port, status}]。

        默认空列表（应用端 / 无多账号能力的登录端）；各登录端适配器按需覆写。
        这是「登录端可登多个 QQ」与「多连一按账号分发」的数据来源。"""
        return []

    @staticmethod
    def tcp_probe(host: str, port, timeout: float = 2.0) -> bool:
        with socket.socket() as s:
            s.settimeout(timeout)
            return s.connect_ex((host, int(port))) == 0

    # ---------- 健康检查的端口口径（子类只需声明，不必复写 health_check）----------
    # 依次探测的端口键；空列表 = 本程序不监听互联端口（如整合包），直接以进程存活为准
    HEALTH_PORT_KEYS: list[str] = ["ob11"]
    # 端口键全部落空时，是否退回实例实际监听端口（正向 WS 监听型才是 True）
    HEALTH_USE_ACTUAL_PORT = True
    # 无端口可探时的 conn 取值（LLBot 必定有 ob11，缺了就是 down 而非 none）
    HEALTH_NO_PORT = "none"

    def _health_port(self, instance) -> int:
        ports = instance.allocated_ports or {}
        for key in self.HEALTH_PORT_KEYS:
            if ports.get(key):
                return int(ports[key])                       # type: ignore[arg-type]
        if self.HEALTH_USE_ACTUAL_PORT:
            return int(getattr(instance, "actual_port", 0) or 0)
        return 0

    def health_check(self, instance, is_alive: bool = False) -> dict:
        """默认：进程存活 + 端口 TCP 探测；conn: ok/down/none。

        各程序只差「探测哪个端口」，统一由 HEALTH_PORT_KEYS 声明；需要按进程存活判定的
        整合包把键列表置空即可，无需各写一份近乎逐字相同的覆写。"""
        if not self.HEALTH_PORT_KEYS:                        # 整合包：进程活着即视为已连接
            return {"alive": is_alive, "conn": "ok" if is_alive else "down"}
        port = self._health_port(instance)
        if not port:
            return {"alive": is_alive, "conn": self.HEALTH_NO_PORT}
        return {"alive": is_alive,
                "conn": "ok" if self.tcp_probe("127.0.0.1", port) else "down"}

    def is_up(self, instance) -> bool:
        """服务端口是否可达（与 health_check 同口径，但只关心端口、不关心进程句柄）。

        用于总览/实例列表的「存活」展示兜底：launcher+worker 结构的程序（llbot 等），
        面板只持有 launcher 句柄，worker(node) 才是真正监听端口的进程；launcher 重启或
        面板重启后 worker 被 reparent 到 init 时句柄失效，但端口仍通——此时应以端口为准
        判为存活，否则节点会在总览里凭空变灰/消失。控制面（stop/restart 闸门）仍用
        is_alive() 的句柄口径，二者职责分离。"""
        try:
            return self.health_check(instance, is_alive=False).get("conn") == "ok"
        except Exception:
            return False

    def diagnose_conn(self, instance) -> list[dict]:
        """互联诊断钩子（拓展2）：返回 [{ok, step, detail}]，只管「本端配置」这层。
        进程/端口/token 一致性等通用层由 REST 端点统一检测。默认无专属项。"""
        return []

    def get_actual_port(self, lines) -> int | None:
        return None

    def get_webui_token(self, lines) -> str | None:
        """从启动日志回读 WebUI 令牌（NapCat 等）；不需要的适配器返回 None。"""
        return None

    def detect_account(self, instance) -> str | None:
        """从日志/配置文件回读已登录的 QQ 号；无法识别返回 None。"""
        return None

    # ---------- 登录页推送提取 ----------
    def extract_qrcode(self, line: str, instance=None):
        """instance 可选：二维码不在日志里（而是落盘 png）的适配器需要它取实例目录。"""
        m = re.search(r"data:image/png;base64,[A-Za-z0-9+/=]+|https?://\S+qrcode\S*", line)
        return {"url": m.group(0) if m.group(0).startswith("http") else None,
                "base64": m.group(0) if m.group(0).startswith("data:") else None} if m else None

    @staticmethod
    def _qr_from_disk(instance, *patterns: str) -> dict | None:
        """回读落盘二维码 png：patterns 为 glob（如 'qr-*.png'）或具体文件名。

        取最新一个非空的，返回 {url:None, base64:...}；无盘文件返回 None。
        Lagrange（qr-*.png）/ Yogurt（qrcode.png 等）共用，消除 base64 编码重复。
        """
        d = Path(getattr(instance, "dir", "") or "")
        if not d.is_dir():
            return None
        cands = []
        for pat in patterns:
            if "*" in pat:
                cands += [p for p in d.glob(pat) if p.is_file()]
            else:
                p = d / pat
                if p.is_file():
                    cands.append(p)
        if not cands:
            return None
        png = max(cands, key=lambda p: p.stat().st_mtime)
        try:
            raw = png.read_bytes()
        except OSError:
            return None
        if not raw:
            return None
        return {"url": None,
                "base64": "data:image/png;base64," + base64.b64encode(raw).decode("ascii")}

    def extract_verify(self, line: str, instance=None):
        m = re.search(r"ticket url:\s*(https?://\S+)", line)      # 滑块验证锚点
        return {"url": m.group(1)} if m else None

    def extract_login_failed(self, line: str, instance=None):
        """登录失败锚点（密码错误/账号冻结等）。默认无——各适配器按上游实际文案
        覆写后，登录页才会收到 login_failed 事件（避免猜测文案造成误报）。"""
        return None


def link_id(login_ref: str, account_qq: str | None) -> str:
    """每条 骰子端↔登录端 关联的全局唯一键（用于配置里的端点/连接去重与认领）。

    向导、各适配器共用同一格式，保证 sealdice 端点 id 与 wizard 解析口径一致。"""
    return f"{login_ref}|{account_qq or ''}"
