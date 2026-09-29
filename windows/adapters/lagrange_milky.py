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
import copy

from adapters.base import WriteResult
from adapters.lagrange_base import LagrangeBase
from core.atomicio import atomic_write_json

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


class LagrangeMilkyAdapter(LagrangeBase):
    # Milky 对外服务端口是独立分配的 milky 端口，与 actual_port 无关
    HEALTH_PORT_KEYS = ["milky"]
    HEALTH_USE_ACTUAL_PORT = False

    # ---------- 配置合并（深合并，保留用户已改的嵌套键）----------
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

    def get_conn_token(self, instance) -> str | None:
        """回读 Milky.AccessToken，供骰子端经 login_ref 继承，保证两端 token 一致。"""
        data = self.read_json(self._config(instance))     # 缺失/损坏 → {} → None
        return (data.get("Milky") or {}).get("AccessToken")

    def list_accounts(self, instance) -> list[dict]:
        """回读 Lagrange.Milky 已登录账号（一个实例一个 Uin，单条）。"""
        data = self.read_json(self._config(instance))
        if not data:
            return []
        uin = ((data.get("Lagrange") or {}).get("Login") or {}).get("Uin") or 0
        if not uin:
            return []
        milky = data.get("Milky") or {}
        return [{"qq": str(uin),
                 "port": (milky.get("HttpServer") or {}).get("Port"),
                 "token": milky.get("AccessToken"),
                 "status": "unknown"}]

    # ---------- 互联配置（写 Milky 服务端）----------
    def write_conn_config(self, instance, mode, direction, addr, token,
                          link_id: str | None = None) -> WriteResult:
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
