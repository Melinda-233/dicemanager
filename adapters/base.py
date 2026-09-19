"""适配器基类与公共契约（终检后统一签名）"""
import json, re, shutil, socket, urllib.request, zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from core.locks import program_dir_lock

@dataclass
class WriteResult:
    """write_conn_config 统一返回值：manual 非空 = 需人工回填（NapCat 兜底）。"""
    ok: bool = True
    manual: str | None = None

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
            self._download(instance)
            if missing := self.verify_required(instance):
                raise RuntimeError(f"部署后缺失必备文件: {missing}")
        return "ok"

    def _download(self, instance):
        """流式落盘再解压：避免整包读入内存（大安装包动辄上百 MB）。"""
        url = self._resolve_download()
        target = Path(instance.dir)
        target.mkdir(parents=True, exist_ok=True)
        tmp = target / ".dm_download.zip"
        try:
            with urllib.request.urlopen(url, timeout=600) as resp, \
                    open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f)
            zipfile.ZipFile(tmp).extractall(target)
        finally:
            tmp.unlink(missing_ok=True)

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
            assets = json.load(urllib.request.urlopen(req, timeout=30))["assets"]
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

    # ---------- 登录页推送提取 ----------
    def extract_qrcode(self, line: str):
        m = re.search(r"data:image/png;base64,[A-Za-z0-9+/=]+|https?://\S+qrcode\S*", line)
        return {"url": m.group(0) if m.group(0).startswith("http") else None,
                "base64": m.group(0) if m.group(0).startswith("data:") else None} if m else None

    def extract_verify(self, line: str):
        m = re.search(r"ticket url:\s*(https?://\S+)", line)      # 滑块验证锚点
        return {"url": m.group(1)} if m else None