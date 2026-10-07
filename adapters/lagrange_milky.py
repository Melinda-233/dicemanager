"""Lagrange.Milky（LagrangeDev/Lagrange.Core 的 Milky 协议实现）：NTQQ 协议端，角色=登录端

行為均对上游源码（Lagrange.Milky/Resources/appsettings.json）核实：
- 配置 appsettings.json，Milky 段控制对外服务：
    Milky.HttpServer.Host / Port  —— 监听地址（默认 127.0.0.1:3000）
    Milky.AccessToken             —— 鉴权令牌（骰子端连它时要带）
    Milky.Event.WebSocket.Enabled —— /event WS 事件流（默认开）
    Milky.Api.Http.Enabled        —— HTTP API（默认开）
- 协议栈在 Lagrange 段：Lagrange.Protocol.Signer.Token 必填（Lagrange V2 Sign API），
  否则程序起不来（故经登录凭据注入，缺失时启动日志会明确报错）。
- 登录：二维码（二维码取自磁盘 qr-{Uin}.png，同 OneBot 版）。**也支持账密**：
  `Lagrange.Login.Uin` / `Password`（见本文件 DEFAULT_CONFIG，与上游默认一致），
  Password 非空即走账密、为空才是扫码。
- 自更新默认关，无需干预。
"""
import copy
from pathlib import Path

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

    # ---------- 登录方式 ----------
    def login_modes(self) -> list[str]:
        """扫码 + 账号密码二选一（字段见本文件 DEFAULT_CONFIG 的 Lagrange.Login段）。

        Milky 版与 OneBot 版**字段路径不同**（这里是 Lagrange.Login.Uin/Password，
        OneBot 版是 Account.Uin/Password）——所以不能把账密实现写进 LagrangeBase，
        否则两边会写错段。
        """
        return ["qrcode", "account"]

    # Lagrange.Login.Protocol 的可选值（同上游默认结构）
    LOGIN_PROTOCOLS = [{"id": "Windows", "label": "Windows（兼容性最好）"},
                       {"id": "Linux", "label": "Linux"},
                       {"id": "macOS", "label": "macOS"}]

    def save_login_credentials(self, instance, credentials) -> dict:
        """写 Lagrange.Login.Uin / Password / Protocol，使其以后免扫码启动即登录。

        **密码明文落盘**：上游格式本身如此（程序直接读明文），不是管理器偷懒。
        只写程序配置文件，不进管理器状态库 → 备份/导出不会把密码带走。
        """
        uin = str(credentials.get("qq") or "").strip()
        pwd = str(credentials.get("password") or "")
        if not uin or not pwd:
            return {"restart": False, "manual": "账号与密码均不能为空"}
        protocol = str(credentials.get("protocol") or "Linux")
        path = self._config(instance)

        def _m(cfg: dict) -> dict:
            cfg = self._fill_defaults(cfg)
            login = cfg["Lagrange"].setdefault("Login", {})
            login["Uin"] = int(uin)
            login["Password"] = pwd
            login["Protocol"] = protocol
            login["AutoReLogin"] = True
            return cfg
        atomic_write_json(path, _m)
        # 换号要清残留登录态（qr-{Uin}.png / device.json）——QQ 单点登录会踢号
        try:
            for pat in (f"qr-{uin}.png", "device.json", "keystore.json"):
                for f in Path(instance.dir).glob(pat):
                    f.unlink()
        except OSError:
            pass
        return {"restart": True, "manual": "", "path": str(path)}

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
