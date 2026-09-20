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

    @staticmethod
    def account_from_logs(lines) -> str | None:
        for _, line in reversed(list(lines)[-300:]):
            m = ACCOUNT_RE.search(line)
            if m: return m.group(1)
        return None

    def webui_credentials(self, instance):
        """WebUI 的 (port, token)：webui.json 优先，其次日志回读落盘的字段。"""
        wf = self._webui_json(instance)
        if wf.exists():
            try:
                d = json.loads(wf.read_text("utf-8"))
                if d.get("token"):
                    port = (d.get("port") or instance.actual_port
                            or instance.allocated_ports.get("webui"))
                    return int(port), d["token"]
            except (ValueError, OSError):
                pass
        return (instance.actual_port or instance.allocated_ports.get("webui"),
                instance.webui_token)

    # ---------- 互联配置 ----------
    def _entry(self, direction, addr, token):
        """按 NapCat 文档构造 network 下的条目（name 唯一，用于重复写入去重）。"""
        if direction == "reverse":                     # NapCat 主动连骰子端
            url = addr if addr.startswith("ws") else f"ws://{addr}/ws"
            return {"name": "dicemanager", "enable": True, "url": url,
                    "messagePostFormat": "array", "reportSelfMessage": False,
                    "reconnectInterval": 5000, "token": token,
                    "debug": False, "heartInterval": 30000}
        port = int(addr.split(":")[-1]) if ":" in addr else int(addr)
        return {"name": "dicemanager", "enable": True, "host": "0.0.0.0",
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

    def write_conn_config(self, instance, mode, direction, addr, token) -> WriteResult:
        entry = self._entry(direction, addr, token)
        key = "websocketServers" if direction != "reverse" else "websocketClients"

        def _m(cfg: dict) -> dict:
            net = cfg.setdefault("network", {})
            for k in ("httpServers", "httpClients", "websocketServers", "websocketClients"):
                net.setdefault(k, [])
            # 同名条目替换，用户自建的其它条目保留
            net[key] = [e for e in net[key] if e.get("name") != "dicemanager"] + [entry]
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
