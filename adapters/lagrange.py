"""Lagrange.OneBot（LagrangeDev/Lagrange.Core v1）：纯 C# NTQQ 协议实现，角色=登录端

行为均对上游 v1 分支源码核实（master 已是 V2/库形态，不再发 OneBot 程序）：
- 发行：release 只有 nightly 一个 tag（无 latest），资产
  `Lagrange.OneBot_linux-x64_net9.0_SelfContained.tar.gz`，自带运行时、可执行文件即
  `Lagrange.OneBot`（Dockerfile 里也是 exec /app/bin/Lagrange.OneBot）。
- 配置 appsettings.json：网络写在 `Implementations[]`，按 Type 区分两种方向（schema 核实）：
    ForwardWebSocket  —— 本端监听 Host:Port（**无 Suffix 字段**），骰子端来连
    ReverseWebSocket  —— 本端连出 Host:Port + Suffix（默认 /onebot/v11/ws）
  两端都靠 AccessToken 鉴权。
- **缺 appsettings.json 时程序会 Console.ReadKey(true) 等按键**（Program.cs 原话
  "Please Edit the appsettings.json ... and press any key to continue"）——无头部署必然
  卡死或抛异常。故部署阶段就预置配置，不依赖 Step5 的 prepare_start。
- 登录：扫码（上游主路径）+ 账号密码（feature table 的 UnusalDevice Password）。
  二维码以 Unicode 方块字符画打到 stdout
  （ConsoleCompatibilityMode=false 用 ▄▀█，true 用 .^@），**不打印 data URL**，
  同时落盘 `qr-{Account:Uin}.png`（Uin 默认 0 → qr-0.png）。
  所以二维码取自磁盘 png，基类那条「日志里找 data:image… 」的正则抓不到。
  密码登录：`Account.Password` 非空即走账密（为空才是扫码），官方文档的说法是
  "After QRCode Login, write password and uin back to appsettings.json" ——
  即首次扫码后把账密回写，之后启动免扫码。
- 自更新默认关闭（UpdaterConfig.EnableAutoUpdate=false），无需干预。
"""
import copy
from pathlib import Path

from adapters.base import WriteResult
from adapters.lagrange_base import LagrangeBase
from core.atomicio import atomic_write_json

DEFAULT_SUFFIX = "/onebot/v11/ws"

# 与上游 Resources/appsettings.json 一致的最小可用骨架（缺文件时据此生成）
DEFAULT_CONFIG = {
    "Logging": {"LogLevel": {"Default": "Information", "Microsoft": "Warning",
                             "Microsoft.Hosting.Lifetime": "Information"}},
    "SignServerUrl": "",
    "SignProxyUrl": "",
    "MusicSignServerUrl": "",
    "Account": {"Uin": 0, "Protocol": "Linux",
                "AutoReconnect": True, "GetOptimumServer": True},
    "Message": {"IgnoreSelf": True, "StringPost": False},
    "QrCode": {"ConsoleCompatibilityMode": False},
    "Implementations": [],
}


class LagrangeAdapter(LagrangeBase):
    # ---------- 配置合并（浅 setdefault）----------
    @staticmethod
    def _fill_defaults(cfg: dict) -> dict:
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, copy.deepcopy(v))
        if not isinstance(cfg.get("Implementations"), list):
            cfg["Implementations"] = []
        return cfg

    def configure_login(self, instance, credentials) -> dict:
        # 二维码经 /ws/login 推送（来自磁盘 qr-*.png）；qq 登录后由 keystore 回读
        return {"ok": True}

    # ---------- 登录方式 ----------
    def login_modes(self) -> list[str]:
        """扫码 + 账号密码二选一（上游 feature table 的 UnusalDevice Password）。

        上游 appsettings.json 的 Account 段即为此设计：`{"Uin": 0, "Password": ""}`，
        官方文档明确 "After QRCode Login, write password and uin back to appsettings.json"
        —— Password 为空才走扫码。写了就免扫码快速登录。
        """
        return ["qrcode", "account"]

    # Account.Protocol 的可选值（上游 appsettings.json 的 Protocol 字段）
    LOGIN_PROTOCOLS = [{"id": "Windows", "label": "Windows（兼容性最好）"},
                       {"id": "Linux", "label": "Linux"},
                       {"id": "macOS", "label": "macOS"}]

    def save_login_credentials(self, instance, credentials) -> dict:
        """写 Account.Uin / Password / Protocol，使其以后免扫码启动即登录。

        **密码明文落盘**：这是上游格式本身的要求（程序直接读明文），不是管理器的
        偷懒——加密就没法让程序自己登录了。因此只写程序配置文件，不进管理器状态库
        （备份/导出不会把密码带走）。
        """
        uin = str(credentials.get("qq") or "").strip()
        pwd = str(credentials.get("password") or "")
        if not uin or not pwd:
            return {"restart": False, "manual": "账号与密码均不能为空"}
        protocol = str(credentials.get("protocol") or "Linux")
        path = self._config(instance)
        atomic_write_json(path, lambda cfg: {
            **cfg,
            "Account": {**(cfg.get("Account") or {}),
                        "Uin": int(uin), "Password": pwd,
                        "Protocol": protocol, "AutoReconnect": True},
        })
        # 换号时必须清 keystore.json：里面存的是上一个账号的登录态，与新账号混用
        # 会直接触发 QQ 的「已在别处登录」把号踢下线。删失败不阻断——账密登录不
        # 依赖它（那是扫码登录的产物），只是残留状态。
        try:
            before = (self.read_json(path).get("Account") or {}).get("Uin")
            if before and int(before) != int(uin):
                ks = Path(instance.dir) / "keystore.json"
                if ks.exists():
                    ks.unlink()
        except (OSError, ValueError, TypeError):
            pass
        return {"restart": True, "manual": "", "path": str(path)}

    def list_accounts(self, instance) -> list[dict]:
        """回读 Lagrange 已登录账号（一个实例通常一个 Account.Uin）。

        Lagrange.OneBot 多账号需多实例，故这里通常返回单条；返回结构保持一致即可。"""
        data = self.read_json(self._config(instance))
        if not data:
            return []
        uin = (data.get("Account") or {}).get("Uin") or 0
        if not uin:
            return []
        port = token = None
        for impl in (data.get("Implementations") or []):
            if isinstance(impl, dict):
                token = impl.get("AccessToken") or token
                if impl.get("Type") == "ForwardWebSocket":
                    port = impl.get("Port") or port
        return [{"qq": str(uin), "port": port, "token": token, "status": "unknown"}]

    # ---------- 互联配置 ----------
    def get_conn_token(self, instance) -> str | None:
        """回读 AccessToken，供骰子端经 login_ref 继承，保证两端 token 一致。"""
        data = self.read_json(self._config(instance))       # 缺失/损坏 → {} → None
        for impl in data.get("Implementations") or []:
            if isinstance(impl, dict) and impl.get("AccessToken"):
                return str(impl["AccessToken"])
        return None

    @staticmethod
    def _host_port(addr: str):
        """解析 127.0.0.1:6700 / ws://host:6700/path → (host, port)。"""
        a = addr.strip()
        if "://" in a:
            a = a.split("://", 1)[1]
        a = a.split("/")[0]
        host, _, port = a.rpartition(":") if ":" in a else (a, "", "")
        return host, int(port) if port.isdigit() else 0

    def write_conn_config(self, instance, mode, direction, addr, token,
                          link_id: str | None = None) -> WriteResult:
        if direction == "reverse":                 # 本端主动连骰子端
            host, port = self._host_port(addr)
            if not port:
                port = int((instance.allocated_ports or {}).get("ob11") or 8080)
            entry = {"Type": "ReverseWebSocket", "Host": host, "Port": port,
                     "Suffix": DEFAULT_SUFFIX, "ReconnectInterval": 5000,
                     "HeartBeatInterval": 5000, "HeartBeatEnable": True,
                     "AccessToken": token}
            hint = f"Lagrange 启动后主动连接 ws://{host}:{port}{DEFAULT_SUFFIX}"
        else:                                      # 本端监听，骰子端来连（无 Suffix 字段）
            port = int(addr.split(":")[-1]) if ":" in addr else int(addr)
            entry = {"Type": "ForwardWebSocket", "Host": "0.0.0.0", "Port": port,
                     "HeartBeatInterval": 5000, "HeartBeatEnable": True,
                     "AccessToken": token}
            hint = f"骰子端用正向 WS 连 ws://<本机IP>:{port}/"

        def _m(cfg: dict) -> dict:
            cfg = self._fill_defaults(cfg)
            impls = [e for e in cfg["Implementations"]
                     if not (isinstance(e, dict) and e.get("Type") == entry["Type"])]
            impls.append(entry)                    # 同类型只保留一条，用户其它条目不动
            cfg["Implementations"] = impls
            return cfg

        p = self._config(instance)
        try:
            atomic_write_json(p, _m)
        except (OSError, ValueError) as e:
            return WriteResult(ok=False, manual=f"写入 {p} 失败：{e}，请检查目录权限")

        return WriteResult(
            ok=True, path=str(p),
            manual=f"已写入 appsettings.json（{entry['Type']}）。{hint}，"
                   f"AccessToken 填 {token}。改配置后需重启实例生效。")
