"""SnowLuma：NTQQ↔OneBot 桥接 + WebUI（5099），角色等同 NapCat（登录端）

真实部署要点（已对 GitHub 源码核实）：
- 发行包为 .tar.gz（linux-x64 完整版自带运行时，launcher.sh 启动）；
  解压后根目录即 launcher.sh，已把顶层目录归一化。
- WebUI 默认 http://localhost:5099，初始密码打印在启动日志（需从日志回读）。
- OneBot v11 服务端默认 ws://127.0.0.1:3001/，accessToken 在 onebot.json 里随机生成；
  骰子端（Dice-Next 等）forward_ws 指向该地址并携带同一 token 才能互通。
- 登录在 SnowLuma 自带 WebUI 完成（接入 QQ 进程），dicemanager 不代劳。
"""
import json
import re
from pathlib import Path

from adapters.base import BaseAdapter, WriteResult


class SnowLumaAdapter(BaseAdapter):
    def build_start_cmd(self, instance) -> list[str]:
        return [str(Path(instance.dir) / self.m["exe"])]      # launcher.sh

    def configure_login(self, instance, credentials) -> dict:
        # SnowLuma 的 QQ 登录在自身 WebUI 完成，dicemanager 只引导；
        # 用 needs_login=True 让向导停在登录页展示指引（不代劳、也不开 WS）
        return {"needs_login": True,
                "manual": "SnowLuma 的 QQ 登录在其自带 WebUI 完成，dicemanager 不代劳：\n"
                          "1) 启动后浏览器访问 http://<本机>:5099\n"
                          "2) 初始账号 admin，初始密码见启动日志\n"
                          "3) 按引导接入 QQ 进程并确认 OneBot 连接已启用\n"
                          "完成后点「继续」进入互联配置。"}

    def write_conn_config(self, instance, mode, direction, addr, token) -> WriteResult:
        # SnowLuma 是 OneBot v11 服务端：它不连出，故无需写自身配置；
        # 真正要写的是骰子端（见 dicenext.py）。这里只给操作提示。
        return WriteResult(
            ok=True,
            manual="SnowLuma 即为 OneBot v11 服务端（默认 ws://127.0.0.1:3001/）。\n"
                   "骰子端用 forward_ws 指向该地址、accessToken 用 SnowLuma 管理后台"
                   "「OneBot」里显示的令牌即可。")

    def get_actual_port(self, lines) -> int | None:
        for _, line in reversed(list(lines)[-200:]):
            m = re.search(r"[Oo]ne[Bb]ot.*?[:：]?\s*(\d{4,5})", line)
            if m:
                return int(m.group(1))
        return None

    def get_webui_token(self, lines) -> str | None:
        for _, line in reversed(list(lines)[-200:]):
            m = re.search(r"(?:初始密码|initial password|webui.{0,12}password|"
                          r"login token)\D{0,20}([A-Za-z0-9+/=_\-]{10,})",
                          line, re.IGNORECASE)
            if m:
                return m.group(1)
        return None

    def detect_account(self, instance) -> str | None:
        d = Path(getattr(instance, "dir", ""))
        # onebot.json 里记录账号，优先从这里取
        for cand in (d / "config" / "onebot.json", d / "onebot.json",
                     d / ".snowluma" / "config" / "onebot.json"):
            if cand.exists():
                try:
                    data = json.loads(cand.read_text("utf-8", errors="ignore"))
                except (OSError, ValueError):
                    continue
                for acc in (data.get("accounts") or []):
                    if str(acc.get("uin") or acc.get("qq") or ""):
                        return str(acc["uin"] or acc["qq"])
                # 新版可能在 networks 里
                for srv in (data.get("networks", {}).get("wsServers") or []):
                    if str(srv.get("account") or ""):
                        return str(srv["account"])
        return None

    def get_conn_token(self, instance) -> str | None:
        """回读 SnowLuma 的 OneBot accessToken，供骰子端经 login_ref 继承，保证两端一致。"""
        d = Path(getattr(instance, "dir", ""))
        cands = (d / "config" / "onebot.json", d / "onebot.json",
                 d / ".snowluma" / "config" / "onebot.json",
                 Path.home() / ".snowluma" / "config" / "onebot.json")
        for cand in cands:
            if not cand.exists():
                continue
            try:
                data = json.loads(cand.read_text("utf-8", errors="ignore"))
            except (OSError, ValueError):
                continue
            for srv in (data.get("networks", {}).get("wsServers") or []):
                if srv.get("accessToken"):
                    return str(srv["accessToken"])
        return None

    def health_check(self, instance, is_alive: bool = False) -> dict:
        port = instance.allocated_ports.get("ob11") or instance.actual_port
        if not port:
            return {"alive": is_alive, "conn": "none"}
        return {"alive": is_alive,
                "conn": "ok" if self.tcp_probe("127.0.0.1", port) else "down"}
