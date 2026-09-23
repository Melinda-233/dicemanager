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
import base64
import copy
import json
import re
from pathlib import Path

from adapters.base import BaseAdapter, WriteResult
from core.atomicio import atomic_write_json

# 二维码字符画整行（兼容模式为 ASCII 的 . ^ @）
QR_ART_RE = re.compile(r"^[▄▀█.^@ ]{16,}$")
BOT_UIN_RE = re.compile(r"Bot\s*Uin:?\s*(\d{5,12})", re.I)
KEYSTORE_UIN_RE = re.compile(r'"Uin"\s*:\s*(\d{5,12})')
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


class LagrangeAdapter(BaseAdapter):
    # ---------- 路径 ----------
    def _config(self, instance) -> Path:
        return Path(instance.dir) / self.m.get("config_path", "appsettings.json")

    # ---------- 部署 ----------
    def deploy(self, instance) -> str:
        """部署完即预置配置：缺 appsettings.json 会让程序停在 Console.ReadKey。

        不能只靠 prepare_start——扫码登录时进程在 Step3 就可能被拉起，早于 Step5。
        """
        result = super().deploy(instance)
        if result in ("ok", "conflict"):       # conflict=目录已存在（断点续跑）
            self._ensure_config(instance)
            self._ensure_executable(instance)
        return result

    def _ensure_config(self, instance) -> None:
        """补齐缺省键但不覆盖用户已改过的配置。"""
        try:
            atomic_write_json(self._config(instance), self._fill_defaults)
        except (OSError, ValueError):          # 预置失败不阻断部署，启动时还会再兜一次
            pass

    @staticmethod
    def _fill_defaults(cfg: dict) -> dict:
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, copy.deepcopy(v))
        if not isinstance(cfg.get("Implementations"), list):
            cfg["Implementations"] = []
        return cfg

    def _ensure_executable(self, instance) -> None:
        exe = Path(instance.dir) / self.m["exe"]
        try:
            if exe.exists() and not exe.stat().st_mode & 0o111:
                exe.chmod(exe.stat().st_mode | 0o755)   # tar 解压可能丢执行位
        except OSError:
            pass

    # ---------- 启动 ----------
    def build_start_cmd(self, instance) -> list[str]:
        return [str(Path(instance.dir) / self.m["exe"])]

    def prepare_start(self, instance, runner=None) -> bool:
        """启动前兜底：配置文件与执行位（幂等，非首启一次性动作）。"""
        self._ensure_config(instance)
        self._ensure_executable(instance)
        return False

    def configure_login(self, instance, credentials) -> dict:
        # 二维码经 /ws/login 推送（来自磁盘 qr-*.png）；qq 登录后由 keystore 回读
        return {"ok": True}

    # ---------- 二维码 ----------
    def extract_qrcode(self, line, instance=None):
        """命中字符画行 → 读盘 qr-*.png 回传；字符画有多行，相同图片由 ws_login 去重。"""
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
            if m := BOT_UIN_RE.search(line):          # 日志锚点 "Bot Uin: 12345"
                return m.group(1)
        return None

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

    # ---------- 互联配置 ----------
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
