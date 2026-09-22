"""Dice!Next：TRPG 骰娘（C++），角色等同 sealdice（骰子）

真实部署要点（已对 README 与官方配置文档核实）：
- 发行包为 .tar.gz（linux-amd64）；解压后二进制名形如 DiceNext（build_start_cmd 用 glob 兜底）。
- WebUI 默认 http://localhost:18088，首次访问必须设管理口令。
- 不自带 QQ 登录，靠 OneBot 适配器连登录端（NapCat / SnowLuma / LLBot）；
  适配器配置写在 config/adapters.json，面板「适配器管理」即时生效。
- OneBot v11 适配器：forward_ws（骰子主动连登录端，endpoint=ws://host:port/）
  或 reverse_ws（登录端反连骰子，endpoint=监听端口号）。
"""
from pathlib import Path

from adapters.base import BaseAdapter, WriteResult
from core.atomicio import atomic_write_json


class DiceNextAdapter(BaseAdapter):
    def build_start_cmd(self, instance) -> list[str]:
        d = Path(instance.dir)
        exe = d / self.m["exe"]
        if not exe.exists():                        # 二进制名兜底（DiceNext / dice-next / ...）
            hits = [f for f in d.iterdir()
                    if f.is_file() and f.name.lower().startswith("dicenext")]
            if hits:
                exe = hits[0]
        return [str(exe)]

    def configure_login(self, instance, credentials) -> dict:
        return {"needs_login": False}              # QQ 登录走 OneBot 适配器

    def write_conn_config(self, instance, mode, direction, addr, token) -> WriteResult:
        forward = direction != "reverse"
        port = addr.split(":")[-1].rstrip("/")
        entry = {
            "name": "dicemanager",
            "type": "onebot_v11",
            "connection_mode": "forward_ws" if forward else "reverse_ws",
            "endpoint": (f"ws://{addr}/" if forward else port),
            "access_token": token or "",
            "enabled": True,
        }
        path = Path(instance.dir) / self.m["config_path"]

        def _m(cfg: dict) -> dict:
            cfg.setdefault("adapters", [])
            # 按 name 查重：命中即改，避免重复添加
            cfg["adapters"] = [e for e in cfg["adapters"]
                               if e.get("name") != "dicemanager"]
            cfg["adapters"].append(entry)
            return cfg

        atomic_write_json(path, _m)                 # Dice!Next 用纯 JSON
        kind = "正向" if forward else "反向"
        return WriteResult(
            ok=True, path=str(path),
            manual=f"已写入 config/adapters.json（{kind} WS）。\n"
                   f"Dice!Next 面板「适配器管理」里 dicemanager 连接启用即生效：\n"
                   f"  {'骰子主动连登录端 ' + entry['endpoint'] if forward else '骰子监听 ' + port + '，登录端反连 ws://<骰子IP>:' + port + '/'}")

    def get_actual_port(self, lines) -> int | None:
        return None                                 # 端口由 allocated/webui 决定，无需日志回读

    def health_check(self, instance, is_alive: bool = False) -> dict:
        # 骰子不直接监听 ob11（正向模式连出），用 WebUI 端口作存活探针
        port = instance.allocated_ports.get("webui") or instance.actual_port
        if not port:
            return {"alive": is_alive, "conn": "none"}
        return {"alive": is_alive,
                "conn": "ok" if self.tcp_probe("127.0.0.1", port) else "down"}
