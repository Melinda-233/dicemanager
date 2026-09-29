"""NapCatQQ：WebUI 端口/token 日志回读 + 本地 onebot11 配置写入

术语对齐 NapCat 官方文档（napneko.github.io/config/basic）：
  正向 WS = websocketServers：NapCat 监听端口，等骰子端来连
  反向 WS = websocketClients：NapCat 主动连骰子端
配置文件：./config/onebot11_<qq>.json；v4.5.3+ 支持 ./config/onebot11.json 作为默认配置
WebUI 令牌：启动日志形如
  [info] [NapCat] [WebUi] WebUi User Panel Url: http://127.0.0.1:6099/webui?token=xxxxx
  （旧版本另有 [WebUi] Login Token is xxxx / WebUI Local Panel Url 两种写法）
端口占用时 NapCat 自行 +1（上限 100 次），真实端口只能从日志回读。
"""
import json
import re
import urllib.request
from pathlib import Path

from adapters.base import BaseAdapter, WriteResult
from core.atomicio import atomic_write_json

# 面板地址行：同时拿到端口与 token
PANEL_URL_RE = re.compile(
    r"(?:User|Local)\s*Panel\s*Url:\s*https?://[\d.]+:(\d{4,5})/webui\?token=([A-Za-z0-9._~-]+)",
    re.I)
# 旧版单独打印 token / 端口的行
LOGIN_TOKEN_RE = re.compile(r"Login\s*Token\s*is\s*([A-Za-z0-9._~-]+)", re.I)
WEBUI_PORT_RE = re.compile(r"\[WebUi\][^\n]*?(\d{4,5})/webui", re.I)
# 登录成功后回读账号：只认明确锚点，避免把日志里的其它数字误当 QQ
ACCOUNT_RE = re.compile(r"(?:登录成功|账号|uin)\D{0,10}(\d{5,12})", re.I)


class NapCatAdapter(BaseAdapter):
    # ---------- 路径 ----------
    def _config_dir(self, instance) -> Path:
        return Path(instance.dir) / "config"

    def _webui_json(self, instance) -> Path:
        """NapCat 把 webui.json 放在 config/ 下（一键安装版为 napcat/config/）。"""
        p = self._config_dir(instance) / "webui.json"
        return p if p.exists() else Path(instance.dir) / "webui.json"

    # ---------- 启动 ----------
    def build_start_cmd(self, instance) -> list[str]:
        return [str(Path(instance.dir) / self.m["exe"])]       # 无 CLI 快速登录参数

    def configure_login(self, instance, credentials) -> dict:
        # 二维码经 /ws/login 推送；qq 由 REST / 日志回读落盘，勿存 self
        # （适配器实例被 ctx 缓存跨请求共享，写 self 线程不安全）
        return {"ok": True}

    # ---------- 日志回读 ----------
    def get_actual_port(self, lines) -> int | None:
        for _, line in reversed(list(lines)[-300:]):
            m = PANEL_URL_RE.search(line)
            if m: return int(m.group(1))
            m = WEBUI_PORT_RE.search(line)
            if m: return int(m.group(1))
        return None

    def get_webui_token(self, lines) -> str | None:
        for _, line in reversed(list(lines)[-300:]):
            m = PANEL_URL_RE.search(line)
            if m: return m.group(2)
            m = LOGIN_TOKEN_RE.search(line)
            if m: return m.group(1)
        return None

    def detect_account(self, instance) -> str | None:
        """优先扫 config/onebot11_<qq>.json（登录后 NapCat 自己生成，最可靠）。"""
        cd = self._config_dir(instance)
        if cd.exists():
            for f in sorted(cd.glob("onebot11_*.json")):
                m = re.match(r"onebot11_(\d{5,12})\.json$", f.name)
                if m: return m.group(1)
        return None

    def list_accounts(self, instance) -> list[dict]:
        """回读 NapCat 已登录的全部 QQ 账号（每个 onebot11_<qq>.json 一个账号）。

        返回 [{qq, token, port, status}]：这是「登录端可登多个 QQ」的数据来源，
        多连一按账号分发时每个骰子端挑其中一个账号的端口/token。"""
        cd = self._config_dir(instance)
        out: list[dict] = []
        if not cd.is_dir():
            return out
        for f in sorted(cd.glob("onebot11_*.json")):
            m = re.match(r"onebot11_(\d{5,12})\.json$", f.name)
            if not m:
                continue
            qq = m.group(1)
            d = self.read_json(f)
            net = d.get("network") or {}
            port = token = None
            for k in ("websocketServers", "websocketClients"):
                for e in (net.get(k) or []):
                    if isinstance(e, dict):
                        port = e.get("port") or port
                        token = e.get("token") or token
            out.append({"qq": qq, "port": port, "token": token, "status": "unknown"})
        return out

    @staticmethod
    def account_from_logs(lines) -> str | None:
        for _, line in reversed(list(lines)[-300:]):
            m = ACCOUNT_RE.search(line)
            if m: return m.group(1)
        return None

    def webui_credentials(self, instance):
        """WebUI 的 (port, token)：webui.json 优先，其次日志回读落盘的字段。"""
        d = self.read_json(self._webui_json(instance))     # 缺失/损坏 → {} → 走日志回读
        if d.get("token"):
            port = (d.get("port") or instance.actual_port
                    or instance.allocated_ports.get("webui"))
            return int(port), d["token"]
        return (instance.actual_port or instance.allocated_ports.get("webui"),
                instance.webui_token)

    def _ensure_webui_binding(self, instance) -> None:
        """webui.json（官方字段 host/port/prefix/token/loginRate，默认 host 已是
        0.0.0.0）：缺失时按分配端口预置；已存在仅把回环 host 放开，端口与令牌
        不动（NapCat 自管并会在退出时覆写该文件，代写反而丢用户改动）。
        路径解析走 _webui_json；放开/新建逻辑复用基类 _open_bind_host。"""
        wf = self._webui_json(instance)
        if not wf.exists():
            wf = self._config_dir(instance) / "webui.json"   # 新建一律写官方位置
        allocated = (instance.allocated_ports or {}).get("webui")
        self._open_bind_host(wf, host_key="host",
                             port=int(allocated) if allocated else None,
                             port_key="port", extra_defaults={"loginRate": 3})

    # ---------- 互联配置 ----------
    def _entry(self, direction, addr, token, name: str) -> dict:
        """按 NapCat 文档构造 network 下的条目（name 唯一，用于重复写入去重）。

        name 取 link_id（每条 骰子↔登录端 关联唯一），多连一/一连多时不同关联各占
        一条、互不覆盖；旧单关联兜底名 dicemanager。"""
        if direction == "reverse":                     # NapCat 主动连骰子端
            url = addr if addr.startswith("ws") else f"ws://{addr}/ws"
            return {"name": name, "enable": True, "url": url,
                    "messagePostFormat": "array", "reportSelfMessage": False,
                    "reconnectInterval": 5000, "token": token,
                    "debug": False, "heartInterval": 30000}
        port = int(addr.split(":")[-1]) if ":" in addr else int(addr)
        return {"name": name, "enable": True, "host": "0.0.0.0",
                "port": port, "messagePostFormat": "array",
                "reportSelfMessage": False, "token": token,
                "enableForcePushEvent": True, "debug": False,
                "heartInterval": 30000}                # 正向：NapCat 监听

    def _target_files(self, instance):
        """带 QQ 号的实例配置优先；无 QQ 号时退回 v4.5.3+ 的默认配置文件。"""
        cd = self._config_dir(instance)
        qq = instance.qq or self.detect_account(instance)
        if qq:
            return [cd / f"onebot11_{qq}.json"], qq
        return [cd / "onebot11.json"], None

    def write_conn_config(self, instance, mode, direction, addr, token,
                          link_id: str | None = None) -> WriteResult:
        name = link_id or "dicemanager"
        entry = self._entry(direction, addr, token, name)
        key = "websocketServers" if direction != "reverse" else "websocketClients"

        def _m(cfg: dict) -> dict:
            net = cfg.setdefault("network", {})
            for k in ("httpServers", "httpClients", "websocketServers", "websocketClients"):
                net.setdefault(k, [])
            # 按 link_id 去重（多连一/一连多互不覆盖），用户自建的其它条目保留
            net[key] = [e for e in net[key] if e.get("name") != name] + [entry]
            cfg.setdefault("musicSignUrl", "")
            cfg.setdefault("enableLocalFile2Url", False)
            cfg.setdefault("parseMultMsg", False)
            return cfg

        files, qq = self._target_files(instance)
        written = []
        for f in files:
            try:
                atomic_write_json(f, _m)
                written.append(str(f))
            except (OSError, ValueError) as e:
                return WriteResult(ok=False, manual=f"写入 {f} 失败：{e}，请检查目录权限")

        # WebUI API 仅作补充（可免重启即时生效），失败不影响本地配置
        api_note = ""
        port, wtok = self.webui_credentials(instance)
        if port and wtok:
            try:
                body = json.dumps({"config": json.dumps({"network": {key: [entry]}})}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/OB11Config/SetConfig", data=body,
                    headers={"Authorization": f"Bearer {wtok}",
                             "Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10)
                api_note = "已同时经 WebUI API 热更新。"
            except Exception:
                api_note = "WebUI API 未连通（未启动或令牌未回读），仅写入本地配置。"

        hint = ("已写入 " + "、".join(written) + "。" + api_note +
                "NapCat 需重启生效（尚未启动则下一步启动即生效）。")
        if not qq:
            hint += (" 未识别到 QQ 号（扫码登录后自动识别），已写入默认配置 onebot11.json"
                     "（需 NapCat v4.5.3+）；旧版本请在 WebUI「网络配置」手动新建。")
        return WriteResult(ok=True, manual=hint, path=written[0])
