"""SnowLuma：NTQQ↔OneBot 桥接 + WebUI（5099），角色等同 NapCat（登录端）

真实部署要点（已对 GitHub 源码核实）：
- 发行包为 .tar.gz（linux-x64 完整版自带运行时，launcher.sh 启动）；
  解压后根目录即 launcher.sh，已把顶层目录归一化。
- WebUI 默认 http://localhost:5099，初始密码打印在启动日志（需从日志回读）。
- OneBot v11 服务端默认 ws://127.0.0.1:3001/，accessToken 在 onebot.json 里随机生成；
  骰子端（Dice-Next 等）forward_ws 指向该地址并携带同一 token 才能互通。
- 登录在 SnowLuma 自带 WebUI 完成（接入 QQ 进程），dicemanager 不代劳。
"""
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

    def _ensure_webui_binding(self, instance) -> None:
        """config/runtime.json（字段经官方源码 packages/common/src/runtime.ts 核实）：
        SnowLuma 默认 webuiHost=127.0.0.1 仅本机监听——外网访问打不开的根因。
        缺失时按分配端口预置；已存在仅放开回环 host，端口/其余设置不动
        （用户可能在 WebUI「系统设置」里改过，改端口会与其打架）。放开/新建逻辑复用基类 _open_bind_host。"""
        cfg = Path(instance.dir) / "config" / "runtime.json"
        allocated = (instance.allocated_ports or {}).get("webui") \
            or self.m.get("webui_default_port") or 5099
        self._open_bind_host(cfg, host_key="webuiHost", port=int(allocated),
                             port_key="webuiPort")

    def write_conn_config(self, instance, mode, direction, addr, token,
                          link_id: str | None = None) -> WriteResult:
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
            data = self.read_json(cand)                   # 缺失/损坏 → {} → 继续下一个候选
            for acc in (data.get("accounts") or []):
                if str(acc.get("uin") or acc.get("qq") or ""):
                    return str(acc["uin"] or acc["qq"])
            # 新版可能在 networks 里
            for srv in (data.get("networks") or {}).get("wsServers") or []:
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
            data = self.read_json(cand)                   # 缺失/损坏 → {} → 继续下一个候选
            for srv in ((data.get("networks") or {}).get("wsServers") or []):
                if srv.get("accessToken"):
                    return str(srv["accessToken"])
        return None

    def list_accounts(self, instance) -> list[dict]:
        """回读 SnowLuma 已登录账号（onebot.json 的 accounts[]，每个账号一条）。

        账号的端口/token 取匹配的 wsServer（按 account 关联）。"""
        d = Path(getattr(instance, "dir", ""))
        for cand in (d / "config" / "onebot.json", d / "onebot.json",
                     d / ".snowluma" / "config" / "onebot.json"):
            data = self.read_json(cand)                   # 缺失/损坏 → {} → 继续下一个候选
            accounts = data.get("accounts") or []
            servers = {srv.get("account"): srv
                       for srv in ((data.get("networks") or {}).get("wsServers") or [])
                       if isinstance(srv, dict) and srv.get("account")}
            out = []
            for acc in accounts:
                uin = str(acc.get("uin") or acc.get("qq") or "")
                if not uin:
                    continue
                srv = servers.get(uin) or {}
                port = srv.get("port")
                if not port and ":" in (srv.get("path") or ""):
                    port = srv["path"].rpartition(":")[2]
                out.append({"qq": uin, "port": port,
                            "token": srv.get("accessToken"), "status": "unknown"})
            if out:
                return out
        return []
