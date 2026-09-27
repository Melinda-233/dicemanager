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
- 登录：上游只支持扫码（密码登录标红不支持）。二维码以 Unicode 方块字符画打到 stdout
  （ConsoleCompatibilityMode=false 用 ▄▀█，true 用 .^@），**不打印 data URL**，
  同时落盘 `qr-{Account:Uin}.png`（Uin 默认 0 → qr-0.png）。
  所以二维码取自磁盘 png，基类那条「日志里找 data:image… 」的正则抓不到。
- 自更新默认关闭（UpdaterConfig.EnableAutoUpdate=false），无需干预。
"""
import copy
import json

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

    # ---------- 互联配置 ----------
    def get_conn_token(self, instance) -> str | None:
        """回读 AccessToken，供骰子端经 login_ref 继承，保证两端 token 一致。"""
        p = self._config(instance)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text("utf-8", errors="ignore"))
        except (OSError, ValueError):
            return None
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

    def write_conn_config(self, instance, mode, direction, addr, token) -> WriteResult:
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
