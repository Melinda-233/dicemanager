"""LLBot：--qq= 快速登录 + JSON5 热更新配置 + v8 AUTH TOKEN 条件"""
import re
from pathlib import Path
from adapters.base import BaseAdapter, WriteResult
from core.atomicio import atomic_write_json

class LLBotAdapter(BaseAdapter):
    V8 = (8, 0, 9)

    @staticmethod
    def _parse_ver(v) -> tuple:
        try:
            return tuple(int(x) for x in str(v).lower().lstrip("v").split("."))
        except ValueError:
            return (9, 9, 9)               # 无法识别按最新从严处理（原实现传字符串会 TypeError）

    def build_start_cmd(self, instance) -> list[str]:
        cmd = [str(Path(instance.dir) / self.m["exe"])]
        if instance.qq:
            cmd.append(f"--qq={instance.qq}")                  # 等号形式
        return cmd

    def configure_login(self, instance, credentials) -> dict:
        ver = self._parse_ver(credentials["version"]) \
              if credentials.get("version") else (9, 9, 9)     # 未提供版本按 v8 从严
        if ver >= self.V8 and not credentials.get("auth_token"):
            return {"conflict": "v8.0.9+ 需在快速登录平台申请 AUTH TOKEN"}
        qq = credentials.get("qq", "")
        atomic_write_json(Path(instance.dir) / self.m["config_path"],
                          lambda c: {**c, "QQ": qq}, source_json5=True)
        return {"ok": True, "qq": qq}

    def write_conn_config(self, instance, mode, direction, addr, token) -> WriteResult:
        entry = {"type": "ws" if direction == "forward" else "ws-reverse",
                 "enable": True,
                 "host": "127.0.0.1" if direction == "forward" else "0.0.0.0",
                 "port": int(addr.split(":")[-1]), "url": "/ws",
                 "token": token, "heartInterval": 30000,
                 "messagePostFormat": "array", "reportSelfMessage": False,
                 "debug": False}
        def _m(cfg: dict) -> dict:
            ob = cfg.setdefault("ob11", {})
            items = [e for e in ob.setdefault("connect", [])
                     if not (e.get("port") == entry["port"] and e.get("type") == entry["type"])]
            items.append(entry); ob["connect"] = items
            return cfg
        # JSON5 读、纯 JSON 写（LLBot 1s 轮询热更新，JSON5 是超集）
        atomic_write_json(Path(instance.dir) / self.m["config_path"], _m, source_json5=True)
        return WriteResult(ok=True)

    def get_actual_port(self, lines) -> int | None:
        for _, line in reversed(list(lines)[-200:]):
            m = re.search(r"[Oo]b11.*?端口[:：]?\s*(\d{4,5})", line)
            if m: return int(m.group(1))
        return None

    def health_check(self, instance, is_alive=False) -> dict:
        port = instance.allocated_ports.get("ob11")
        ok = self.tcp_probe("127.0.0.1", port) if port else False
        return {"alive": is_alive, "conn": "ok" if ok else "down"}