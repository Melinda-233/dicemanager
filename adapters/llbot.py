"""LLBot：--qq= 快速登录 + JSON5 热更新配置 + v8 AUTH TOKEN 条件

CLI 参数（官方 README）：--qq= 快速登录、--update 检查并执行更新、--pmhq 切换模式。
默认配置 default_config.json 结构（实测）：
  webui: { enable, host, port }                       # 管理界面，默认 3080
  ob11:  { enable, connect: [ { type: "ws", enable, host, port, token, ... } ] }  # 3001
  milky: { enable, http: { host, port } }             # 3010
  satori:{ enable, host, port, token }                # 5600
端口只能靠改配置文件（CLI 无端口参数），所以分配到的端口必须在首启前写回，
否则多开时第二个实例仍会去抢默认端口。配置热更新：写完 1s 内 LLBot 自动重载。
"""
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

    def _config_path(self, instance) -> Path:
        return Path(instance.dir) / self.m["config_path"]

    def build_start_cmd(self, instance) -> list[str]:
        cmd = [str(Path(instance.dir) / self.m["exe"])]
        if instance.qq:
            cmd.append(f"--qq={instance.qq}")                  # 等号形式
        return cmd

    # ---------- 启动前准备 ----------
    def prepare_start(self, instance, runner=None) -> bool:
        """把分配到的端口写回配置（每次幂等）+ 首启执行一次 --update。"""
        try:
            self._apply_allocated_ports(instance)
        except (OSError, ValueError):
            pass                            # 配置文件还没生成（未部署）时不阻断启动
        action = self.m.get("post_start_action")
        if instance.first_run_done or not action or runner is None:
            return False
        try:
            runner([str(Path(instance.dir) / self.m["exe"]), action],
                   instance.dir, "首次启动前检查更新")
        except Exception:
            return False                    # 更新失败不阻断启动（可能是内网环境）
        return True

    def _apply_allocated_ports(self, instance) -> None:
        """端口表分配到的实际端口 → 配置文件，避免多开时端口漂移。"""
        ports = instance.allocated_ports or {}

        def _m(cfg: dict) -> dict:
            if ports.get("webui"):
                w = cfg.setdefault("webui", {})
                w["port"] = int(ports["webui"])
                w.setdefault("enable", True)
            ob = cfg.setdefault("ob11", {})
            ob.setdefault("enable", True)
            conn = ob.setdefault("connect", [])
            if ports.get("ob11"):
                hit = False
                for e in conn:
                    if e.get("type") == "ws":
                        e["port"] = int(ports["ob11"]); e["enable"] = True; hit = True
                        break
                if not hit:
                    conn.append({"type": "ws", "enable": True, "host": "0.0.0.0",
                                 "port": int(ports["ob11"]), "heartInterval": 60000,
                                 "token": "", "reportSelfMessage": False,
                                 "reportOfflineMessage": False})
            if ports.get("milky"):
                cfg.setdefault("milky", {}).setdefault("http", {})["port"] = int(ports["milky"])
            if ports.get("satori"):
                cfg.setdefault("satori", {})["port"] = int(ports["satori"])
            return cfg

        atomic_write_json(self._config_path(instance), _m, source_json5=True)

    def configure_login(self, instance, credentials) -> dict:
        ver = self._parse_ver(credentials["version"]) \
              if credentials.get("version") else (9, 9, 9)     # 未提供版本按 v8 从严
        if ver >= self.V8 and not credentials.get("auth_token"):
            return {"conflict": "v8.0.9+ 需在快速登录平台申请 AUTH TOKEN"}
        qq = credentials.get("qq", "")
        atomic_write_json(self._config_path(instance),
                          lambda c: {**c, "QQ": qq}, source_json5=True)
        return {"ok": True, "qq": qq}

    def write_conn_config(self, instance, mode, direction, addr, token) -> WriteResult:
        entry = {"type": "ws" if direction != "reverse" else "ws-reverse",
                 "enable": True,
                 "host": "0.0.0.0" if direction != "reverse" else "127.0.0.1",
                 "port": int(addr.split(":")[-1]),
                 "token": token, "heartInterval": 30000,
                 "messagePostFormat": "array", "reportSelfMessage": False,
                 "debug": False}
        if direction == "reverse":
            entry["url"] = addr if addr.startswith("ws") else f"ws://{addr}/ws"

        def _m(cfg: dict) -> dict:
            ob = cfg.setdefault("ob11", {})
            ob["enable"] = True                                 # 互联写入即启用协议
            conn = ob.setdefault("connect", [])
            conn = [e for e in conn
                    if not (e.get("type") == entry["type"] and e.get("name") == "dicemanager")]
            entry["name"] = "dicemanager"
            # 端口冲突时让位：同端口的旧条目改到下一个端口，避免 LLBot 启动报占用
            used = {e.get("port") for e in conn}
            if entry["port"] in used:
                entry["port"] = entry["port"] + 1
            conn.append(entry)
            ob["connect"] = conn
            return cfg
        # JSON5 读、纯 JSON 写（LLBot 1s 轮询热更新，JSON5 是超集）
        atomic_write_json(self._config_path(instance), _m, source_json5=True)
        return WriteResult(ok=True, manual="LLBot 配置热更新，约 1 秒后自动生效，无需重启。",
                           path=str(self._config_path(instance)))

    def get_actual_port(self, lines) -> int | None:
        for _, line in reversed(list(lines)[-200:]):
            m = re.search(r"[Oo]b11.*?端口[:：]?\s*(\d{4,5})", line)
            if m: return int(m.group(1))
        return None

    def health_check(self, instance, is_alive=False) -> dict:
        port = instance.allocated_ports.get("ob11")
        ok = self.tcp_probe("127.0.0.1", port) if port else False
        return {"alive": is_alive, "conn": "ok" if ok else "down"}
