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
        """取包：本地 packages/<dice>.zip 直接用；否则下载并缓存供后续复用。"""
        cached = pkgstore.find_archive(self.m["name"])
        if cached:
            return cached
        url = mirror_url(self._resolve_download())
        target = pkgstore.archive_path(self.m["name"])
        tmp = target.with_suffix(".zip.tmp")
        try:
            with urllib.request.urlopen(url, timeout=600) as resp, \
                    open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f)
            if not zipfile.is_zipfile(tmp):
                raise RuntimeError("下载内容不是有效的 zip 压缩包")
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        pkgstore.mark_source(self.m["name"], "download")
        return target

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
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(target)

    def _resolve_download(self) -> str:
        strat = self.m.get("download_strategy", "direct")
        if strat == "direct":
            return self.m["download"]
        if strat == "resolve_latest_via_api":
            rp = urlparse(self.m["release_page"]).path        # /owner/repo/releases
            repo = rp.split("/releases")[0].strip("/")
            req = urllib.request.Request(
                f"https://api.github.com/repos/{repo}/releases/latest",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "DiceManager"})
            mreq = urllib.request.Request(mirror_url(req.full_url), headers=req.headers)
            assets = json.load(urllib.request.urlopen(mreq, timeout=30))["assets"]
            suffix = self.m.get("asset_suffix", ".zip")
            for a in assets:
                if a["name"].endswith(suffix):
                    return a["browser_download_url"]
            raise RuntimeError(f"release 未找到 *{suffix} 资产")
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
    def extract_qrcode(self, line: str):
        m = re.search(r"data:image/png;base64,[A-Za-z0-9+/=]+|https?://\S+qrcode\S*", line)
        return {"url": m.group(0) if m.group(0).startswith("http") else None,
                "base64": m.group(0) if m.group(0).startswith("data:") else None} if m else None

    def extract_verify(self, line: str):
        m = re.search(r"ticket url:\s*(https?://\S+)", line)      # 滑块验证锚点
        return {"url": m.group(1)} if m else None
