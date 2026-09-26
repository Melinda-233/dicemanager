"""Lagrange.Milky（LagrangeDev/Lagrange.Core 的 Milky 协议实现）：NTQQ 协议端，角色=登录端

行為均对上游源码（Lagrange.Milky/Resources/appsettings.json）核实：
- 配置 appsettings.json，Milky 段控制对外服务：
    Milky.HttpServer.Host / Port  —— 监听地址（默认 127.0.0.1:3000）
    Milky.AccessToken             —— 鉴权令牌（骰子端连它时要带）
    Milky.Event.WebSocket.Enabled —— /event WS 事件流（默认开）
    Milky.Api.Http.Enabled        —— HTTP API（默认开）
- 协议栈在 Lagrange 段：Lagrange.Protocol.Signer.Token 必填（Lagrange V2 Sign API），
  否则程序起不来（故经登录凭据注入，缺失时启动日志会明确报错）。
- 登录：二维码。Lagrange.Core 的 QrCode 行为同 OneBot 版——把方块字符画打到 stdout，
  同时落盘 qr-{Uin}.png（Uin 默认 0 → qr-0.png）。所以二维码取自磁盘 png。
- 自更新默认关，无需干预。
"""
import base64
import copy
import json
import re
from pathlib import Path

from adapters.base import BaseAdapter, WriteResult
from core.atomicio import atomic_write_json

# 二维码字符画整行（兼容模式为 ASCII 的 . ^ @）
QR_ART_RE = re.compile(r"^[▄▀█.^@ ]{16,}$")
KEYSTORE_UIN_RE = re.compile(r'"Uin"\s*:\s*(\d{5,12})')
BOT_UIN_RE = re.compile(r"Bot\s*Uin:?\s*(\d{5,12})", re.I)

# 缺 appsettings.json 时据此生成的最小可用骨架（与上游默认一致）
DEFAULT_CONFIG = {
    "Logging": {"LogLevel": {"Default": "Information"}},
    "Lagrange": {
        "Protocol": {
            "Signer": {"BaseUrl": "https://sign.lagrangecore.org/api/", "Token": ""}
        },
        "Login": {"Uin": 0, "Password": None, "AutoReLogin": True,
                  "UseOnlineCaptchResolver": True},
    },
    "Milky": {
        "AccessToken": None,
        "HttpServer": {"Host": "127.0.0.1", "Port": 3000},
        "Api": {"Http": {"Enabled": True}},
        "Event": {"WebSocket": {"Enabled": True}},
    },
}


class LagrangeMilkyAdapter(BaseAdapter):
    # ---------- 路径 ----------
    def _config(self, instance) -> Path:
        return Path(instance.dir) / self.m.get("config_path", "appsettings.json")

    # ---------- 部署 ----------
    def deploy(self, instance) -> str:
        """部署完即预置配置：避免缺 appsettings.json 导致首次启动行为不确定。"""
        result = super().deploy(instance)
        if result in ("ok", "conflict"):       # conflict=目录已存在（断点续跑）
            self._ensure_config(instance)
            self._ensure_executable(instance)
        return result

    def _ensure_config(self, instance) -> None:
        try:
            atomic_write_json(self._config(instance), self._fill_defaults)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _fill_defaults(cfg: dict) -> dict:
        """深合并默认配置：用户已改的键原样保留（含嵌套的 Milky 段），
        缺失的键/子键才补默认。浅 setdefault 会在用户仅改了 Milky.HttpServer
        时丢掉了 AccessToken/Api/Event 等兄弟键。"""

        def _deep(base, defaults):
            for k, v in defaults.items():
                if k not in base:
                    base[k] = copy.deepcopy(v)
                elif isinstance(v, dict) and isinstance(base[k], dict):
                    _deep(base[k], v)

        _deep(cfg, DEFAULT_CONFIG)
        return cfg

    def _ensure_executable(self, instance) -> None:
        exe = Path(instance.dir) / self.m["exe"]
        try:
            if exe.exists() and not exe.stat().st_mode & 0o111:
                exe.chmod(exe.stat().st_mode | 0o755)
        except OSError:
            pass

    # ---------- 启动 ----------
    def build_start_cmd(self, instance) -> list[str]:
        return [str(Path(instance.dir) / self.m["exe"])]

    def prepare_start(self, instance, runner=None) -> bool:
        self._ensure_config(instance)
        self._ensure_executable(instance)
        return False

    def configure_login(self, instance, credentials) -> dict:
        # Signer Token 经登录凭据注入（缺失则启动时 Signer 报错，由日志可见）
        tok = (credentials or {}).get("signer_token")
        if tok:
            self._set_signer_token(instance, tok)
        return {"ok": True}

    def _set_signer_token(self, instance, tok: str) -> None:
        p = self._config(instance)

        def _m(cfg: dict) -> dict:
            cfg = self._fill_defaults(cfg)
            cfg["Lagrange"]["Protocol"]["Signer"]["Token"] = tok
            return cfg
        try:
            atomic_write_json(p, _m)
        except (OSError, ValueError):
            pass

    # ---------- 二维码 ----------
    def extract_qrcode(self, line, instance=None):
        if instance is None or not QR_ART_RE.match(line.rstrip()):
            return None
        png = self._latest_qr_png(instance)
        if png is None:
            return None
        try:
            raw = png.read_bytes()
        except OSError:
            return None
        if not raw:
            return None
        return {"url": None,
                "base64": "data:image/png;base64,"
                          + base64.b64encode(raw).decode("ascii")}

    @staticmethod
    def _latest_qr_png(instance):
        d = Path(getattr(instance, "dir", "") or "")
        if not d.is_dir():
            return None
        cands = [p for p in d.glob("qr-*.png") if p.is_file()]
        return max(cands, key=lambda p: p.stat().st_mtime) if cands else None

    # ---------- 账号回读 ----------
    def detect_account(self, instance) -> str | None:
        ks = Path(instance.dir) / "keystore.json"      # 登录成功后程序自己落盘
        if ks.exists():
            try:
                if m := KEYSTORE_UIN_RE.search(ks.read_text("utf-8", errors="ignore")):
                    return m.group(1)
            except OSError:
                pass
        return None

    @staticmethod
    def account_from_logs(lines) -> str | None:
        for _, line in reversed(list(lines)[-300:]):
            if m := BOT_UIN_RE.search(line):
                return m.group(1)
        return None

    def get_conn_token(self, instance) -> str | None:
        """回读 Milky.AccessToken，供骰子端经 login_ref 继承，保证两端 token 一致。"""
        p = self._config(instance)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text("utf-8", errors="ignore"))
        except (OSError, ValueError):
            return None
        return data.get("Milky", {}).get("AccessToken")

    # ---------- 互联配置（写 Milky 服务端）----------
    def write_conn_config(self, instance, mode, direction, addr, token) -> WriteResult:
        port = int(addr.split(":")[-1]) if ":" in addr else int(addr)

        def _m(cfg: dict) -> dict:
            cfg = self._fill_defaults(cfg)
            cfg["Milky"]["HttpServer"]["Host"] = "0.0.0.0"
            cfg["Milky"]["HttpServer"]["Port"] = port
            cfg["Milky"]["AccessToken"] = token
            return cfg

        p = self._config(instance)
        try:
            atomic_write_json(p, _m)
        except (OSError, ValueError) as e:
            return WriteResult(ok=False, manual=f"写入 {p} 失败：{e}，请检查目录权限")
        return WriteResult(
            ok=True, path=str(p),
            manual=f"已写入 appsettings.json（Milky.HttpServer 0.0.0.0:{port}，"
                   f"AccessToken 已设）。骰子端用 Milky 基址 http://<本机IP>:{port} 连接，"
                   f"Token={token}。改配置后需重启实例生效。")

    # ---------- 健康检查 ----------
    def health_check(self, instance, is_alive: bool = False) -> dict:
        port = (instance.allocated_ports or {}).get("milky")
        if not port:
            return {"alive": is_alive, "conn": "none"}
        return {"alive": is_alive,
                "conn": "ok" if self.tcp_probe("127.0.0.1", port) else "down"}
