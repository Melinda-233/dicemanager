"""NapCatQQ：WebUI API 写配置 + 端口/token 日志回读 + 手动兜底"""
import json, re, urllib.request
from pathlib import Path
from adapters.base import BaseAdapter, WriteResult
from core.atomicio import atomic_write_json

class NapCatAdapter(BaseAdapter):
    def build_start_cmd(self, instance) -> list[str]:
        return [str(Path(instance.dir) / self.m["exe"])]       # 无 CLI 快速登录参数

    def configure_login(self, instance, credentials) -> dict:
        return {"ok": True}                # 二维码经 /ws/login 推送；qq 由 REST 落盘，勿存 self
                                           # （适配器实例被 ctx 缓存跨请求共享，写 self 线程不安全）

    def _webui(self, instance):
        wf = Path(instance.dir) / "webui.json"
        tok = json.loads(wf.read_text("utf-8"))["token"] if wf.exists() else instance.webui_token
        return instance.actual_port or instance.allocated_ports.get("webui"), tok

    def write_conn_config(self, instance, mode, direction, addr, token) -> WriteResult:
        try:
            port, tok = self._webui(instance)
            network = {"websocketServers": [], "clients": []}
            if direction == "reverse":                         # NapCat 作服务端
                network["websocketServers"] = [{
                    "enable": True, "name": "dicemanager",
                    "host": "0.0.0.0", "port": int(addr.split(":")[-1]),
                    "messagePostFormat": "array", "reportSelfMessage": False,
                    "token": token, "enableForcePushEvent": True, "debug": False}]
            else:                                              # NapCat 作客户端
                network["websocketClients"] = [{
                    "enable": True, "name": "dicemanager",
                    "url": f"ws://{addr}/ws", "messagePostFormat": "array",
                    "reportSelfMessage": False, "token": token,
                    "debug": False, "heartInterval": 30000, "reconnectInterval": 5000}]
            body = json.dumps({"config": json.dumps({"network": network})}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/OB11Config/SetConfig", data=body,
                headers={"Authorization": f"Bearer {tok}",
                         "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
            # 本地镜像落盘（NapCat 亦按此文件加载）
            if instance.qq:
                atomic_write_json(Path(instance.dir) / f"config/onebot11_{instance.qq}.json",
                                  lambda c: {**c, "network": network})
            return WriteResult(ok=True)
        except Exception:
            return WriteResult(ok=False, manual=
                "WebUI API 不可达：请在 NapCat WebUI「网络配置」手动创建后回填")

    def get_actual_port(self, lines) -> int | None:
        for _, line in lines:              # ring 为 (seq, line) 对
            m = re.search(r"\[WebUI\].*?:(\d{4,5})/webui", line)   # 占用+1 由 NapCat 自行处理
            if m: return int(m.group(1))
        return None