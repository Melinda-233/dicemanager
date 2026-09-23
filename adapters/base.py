"""适配器基类与公共契约（终检后统一签名）"""
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

from core import packages as pkgstore
from core.locks import program_dir_lock

# 国内服务器直连 github.com 常超时/被墙：设 DM_GITHUB_MIRROR 后自动走镜像前缀。
# 例：DM_GITHUB_MIRROR=https://ghfast.top/     → https://ghfast.top/https://github.com/...
# 也可指向自建反代；留空则直连。
GITHUB_MIRROR = os.environ.get("DM_GITHUB_MIRROR", "").strip().rstrip("/")

def mirror_url(url: str) -> str:
    """需要访问 GitHub 时统一走这里，便于在国内服务器切换到镜像/代理。"""
    return f"{GITHUB_MIRROR}/{url}" if GITHUB_MIRROR else url

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

    # ---------- 部署 ----------
    def deploy(self, instance) -> str:
        with program_dir_lock(self.m["name"]):
            p = Path(instance.dir)
            if p.exists():
                missing = self.verify_required(instance)
                return "ok" if not missing else "conflict"    # 冲突 → 前端弹窗
            archive = self._acquire_archive()     # 本地包优先，没有才在线下载
            if expected := self.m.get("sha256"):
                self._verify_sha256(archive, expected)
            self._extract(archive, instance)
            if missing := self.verify_required(instance):
                raise RuntimeError(f"部署后缺失必备文件: {missing}")
        return "ok"

    def _acquire_archive(self) -> Path:
        """取包：本地 packages/<dice>.<ext> 直接用；否则下载并缓存供后续复用。"""
        cached = pkgstore.find_archive(self.m["name"])
        if cached:
            return cached
        url = mirror_url(self._resolve_download())
        tmp = pkgstore.pkg_dir() / f"{self.m['name']}.dl.tmp"
        try:
            with urllib.request.urlopen(url, timeout=600) as resp, \
                    open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f)
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
                tf.extractall(target, filter="data")
        # 归一化：压缩包常带唯一顶层目录（如 SnowLuma-linux-x64/），
        # 把其内容直接上移到实例目录，避免 instance.dir/xxx/launcher.sh 这种错位
        entries = [p for p in target.iterdir()]
        if len(entries) == 1 and entries[0].is_dir():
            sub = entries[0]
            for child in sub.iterdir():
                shutil.move(str(child), str(target / child.name))
            sub.rmdir()

    def _resolve_download(self) -> str:
        strat = self.m.get("download_strategy", "direct")
        if strat == "direct":
            return self.m["download"]
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
            mreq = urllib.request.Request(mirror_url(req.full_url), headers=req.headers)
            assets = json.load(urllib.request.urlopen(mreq, timeout=30))["assets"]
            pattern = self.m.get("asset_name_pattern")
            if pattern:                               # 正则精确选资产（如 linux-x64 完整包）
                for a in assets:
                    if re.search(pattern, a["name"]):
                        return a["browser_download_url"]
            suffix = self.m.get("asset_suffix", ".zip")   # 退化为后缀匹配
            for a in assets:
                if a["name"].endswith(suffix):
                    return a["browser_download_url"]
            raise RuntimeError(f"release 未找到匹配资产（pattern={pattern}, suffix={suffix}）")
        raise ValueError(f"未知下载策略: {strat}")

    def verify_required(self, instance) -> list:
        return [f for f in self.m["required_files"]
                if not (Path(instance.dir) / f).exists()]

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

    @staticmethod
    def gen_token(n: int = 16) -> str:
        """互联令牌：OneBot 两端的 token 必须一致，由后端生成避免空 token。"""
        import secrets
        return secrets.token_urlsafe(n)

    # ---------- 登录与互联 ----------
    @abstractmethod
    def configure_login(self, instance, credentials: dict) -> dict: ...
    @abstractmethod
    def write_conn_config(self, instance, mode: str, direction: str,
                          addr: str, token: str) -> WriteResult: ...

    @staticmethod
    def tcp_probe(host: str, port, timeout: float = 2.0) -> bool:
        with socket.socket() as s:
            s.settimeout(timeout)
            return s.connect_ex((host, int(port))) == 0

    def health_check(self, instance, is_alive: bool = False) -> dict:
        """默认：进程存活 + 端口 TCP 探测；conn: ok/down/none。"""
        port = instance.allocated_ports.get("ob11") or instance.actual_port
        if not port:
            return {"alive": is_alive, "conn": "none"}
        return {"alive": is_alive,
                "conn": "ok" if self.tcp_probe("127.0.0.1", port) else "down"}

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

    def extract_verify(self, line: str, instance=None):
        m = re.search(r"ticket url:\s*(https?://\S+)", line)      # 滑块验证锚点
        return {"url": m.group(1)} if m else None
