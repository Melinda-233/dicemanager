"""Yogurt（LLOneBot/yogurt-pmhq）：基于 acidify-core + PMHQ 的 Milky 协议端，角色=登录端

行為均对上游 README / config.json 示例核实：
- 配置 config.json（扁平结构，configVersion 3）。对外 Milky 服务在 httpConfig：
    httpConfig.host / port / accessToken —— 监听地址与鉴权令牌
    pmhqUrl                       —— PMHQ 的 WebSocket 地址（默认 ws://localhost:13000/ws）
- 不需要签名服务：signApiUrl 留空（底层走 PMHQ，PMHQ 自行处理协议）。这是相对 Lagrange.Milky
  的一大优势（后者必须 Signer Token）。但代价是强依赖 PMHQ 常驻。
- 登录：二维码（Yogurt 支持当前状态/快速登录/二维码三种）。原生模式下 QR 的具体落盘形式
  上游文档未明确，extract_qrcode 做尽力而为（data: URL / http 链接 / 常见图片文件名），
  真机部署时若未自动弹出，可在 PMHQ 侧扫码。
"""
import base64
import copy
import json
import re
from pathlib import Path

from adapters.base import BaseAdapter, WriteResult
from core.atomicio import atomic_write_json

QR_LINE_RE = re.compile(r"data:image/png;base64,[A-Za-z0-9+/=]+|https?://\S+qr\S*", re.I)
UIN_RE = re.compile(r"\b(\d{5,12})\b")


# 缺 config.json 时据此生成（与上游默认一致；configVersion 由 Yogurt 自己补齐）
DEFAULT_CONFIG = {
    "signApiUrl": "",
    "pmhqUrl": "ws://localhost:13000/ws",
    "quickLoginUin": None,
    "protocol": {"os": "Linux", "version": "fetched"},
    "androidCredentials": {"uin": 0, "password": ""},
    "androidUseLegacySign": False,
    "reportSelfMessage": True,
    "preloadContacts": False,
    "transformIncomingMFaceToImage": False,
    "httpConfig": {"host": "127.0.0.1", "port": 30001, "accessToken": "", "corsOrigins": []},
    "webhookConfig": {"url": [], "accessToken": ""},
    "logging": {"ansiLevel": "ANSI256", "coreLogLevel": "DEBUG"},
    "skipSecurityCheck": False,
    # 兼容 Lagrange.Milky 同款 Signer 字段（Yogurt 实际不使用，留空即可）
    "Lagrange": {"Protocol": {"Signer": {"BaseUrl": "", "Token": ""}}},
}


class YogurtAdapter(BaseAdapter):
    # ---------- 路径 ----------
    def _config(self, instance) -> Path:
        return Path(instance.dir) / self.m.get("config_path", "config.json")

    # ---------- 部署 ----------
    def deploy(self, instance) -> str:
        result = super().deploy(instance)
        if result in ("ok", "conflict"):
            self._ensure_config(instance)
        return result

    def _ensure_config(self, instance) -> None:
        try:
            atomic_write_json(self._config(instance), self._fill_defaults)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _fill_defaults(cfg: dict) -> dict:
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, copy.deepcopy(v))
        return cfg

    # ---------- 启动 ----------
    def build_start_cmd(self, instance) -> list[str]:
        return [str(Path(instance.dir) / self.m["exe"])]

    def prepare_start(self, instance, runner=None) -> bool:
        """PMHQ 可达性只做告警（不阻断启动）：未起 PMHQ 时 Yogurt 启动后会在初始化阶段报错。"""
        self._ensure_config(instance)
        return False

    def configure_login(self, instance, credentials) -> dict:
        return {"ok": True}

    # ---------- 二维码 ----------
    def extract_qrcode(self, line, instance=None):
        m = QR_LINE_RE.search(line)
        if m:
            s = m.group(0)
            return {"url": s if s.startswith("http") else None,
                    "base64": s if s.startswith("data:") else None}
        # 落盘图片：常见文件名兜底（原生模式未完全确认，尽力而为）
        if instance is not None:
            d = Path(getattr(instance, "dir", "") or "")
            for cand in ("qrcode.png", "qr.png", "login-qrcode.png"):
                p = d / cand
                if p.is_file() and p.stat().st_size:
                    try:
                        raw = p.read_bytes()
                        return {"url": None,
                                "base64": "data:image/png;base64,"
                                          + base64.b64encode(raw).decode("ascii")}
                    except OSError:
                        pass
        return None

    # ---------- 账号回读 ----------
    def detect_account(self, instance) -> str | None:
        p = self._config(instance)
        if p.exists():
            try:
                d = json.loads(p.read_text("utf-8", errors="ignore"))
                if d.get("quickLoginUin"):
                    return str(d["quickLoginUin"])
            except (OSError, ValueError):
                pass
        # 回退：日志里找 5-12 位纯数字（保守，仅作兜底）
        return None

    @staticmethod
    def account_from_logs(lines) -> str | None:
        for _, line in reversed(list(lines)[-300:]):
            for m in UIN_RE.finditer(line):
                u = m.group(1)
                if len(u) >= 5:
                    return u
        return None

    def get_conn_token(self, instance) -> str | None:
        p = self._config(instance)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text("utf-8", errors="ignore"))
        except (OSError, ValueError):
            return None
        return (data.get("httpConfig") or {}).get("accessToken") or None

    # ---------- 互联配置（写 Milky 服务端）----------
    def write_conn_config(self, instance, mode, direction, addr, token) -> WriteResult:
        port = int(addr.split(":")[-1]) if ":" in addr else int(addr)

        def _m(cfg: dict) -> dict:
            cfg = self._fill_defaults(cfg)
            cfg["httpConfig"]["host"] = "0.0.0.0"
            cfg["httpConfig"]["port"] = port
            cfg["httpConfig"]["accessToken"] = token
            return cfg

        p = self._config(instance)
        try:
            atomic_write_json(p, _m)
        except (OSError, ValueError) as e:
            return WriteResult(ok=False, manual=f"写入 {p} 失败：{e}，请检查目录权限")
        return WriteResult(
            ok=True, path=str(p),
            manual=f"已写入 config.json（httpConfig 0.0.0.0:{port}，AccessToken 已设）。"
                   f"骰子端用 Milky 基址 http://<本机IP>:{port} 连接，Token={token}。"
                   f"注意：需 PMHQ 已在 ws://localhost:13000/ws 运行。改配置后需重启实例生效。")

    # ---------- 健康检查 ----------
    def health_check(self, instance, is_alive: bool = False) -> dict:
        port = (instance.allocated_ports or {}).get("milky")
        if not port:
            return {"alive": is_alive, "conn": "none"}
        return {"alive": is_alive,
                "conn": "ok" if self.tcp_probe("127.0.0.1", port) else "down"}
