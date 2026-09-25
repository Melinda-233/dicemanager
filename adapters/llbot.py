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
import base64
import re
import time
from pathlib import Path

from adapters.base import LOOPBACK_HOSTS, BaseAdapter, WriteResult
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

    def _config_paths(self, instance) -> list[Path]:
        """default_config.json 是未登录时的引导配置；登录成功后 LLBot 生成
        data/config_{qq}.json 按账号配置并覆盖前者（webui/ob11 等全部以它为准，
        2026-09-25 线上实测）。两份都要写：qq 未知时只有前者；qq 已知而按账号
        文件还没生成时，用引导配置播种一份（否则登录后监听地址会被打回回环）。"""
        base = self._config_path(instance)
        paths = [base]
        if instance.qq:
            per_uin = (Path(instance.dir) / "bin/llbot/data"
                       / f"config_{instance.qq}.json")
            if not per_uin.exists() and base.exists():
                per_uin.parent.mkdir(parents=True, exist_ok=True)
                per_uin.write_text(base.read_text("utf-8"), encoding="utf-8")
            paths.append(per_uin)
        return paths

    def build_start_cmd(self, instance) -> list[str]:
        cmd = [str(Path(instance.dir) / self.m["exe"])]
        if instance.qq:
            cmd.append(f"--qq={instance.qq}")                  # 等号形式
        return cmd

    # ---------- 启动前准备 ----------
    def prepare_start(self, instance, runner=None) -> bool:
        """执行位保障（zip 解压不保证保留 +x）+ 端口写回 + 首启 --update。"""
        for rel in (self.m["exe"], "bin/llbot/node", "bin/pmhq/pmhq"):
            exe = Path(instance.dir) / rel
            if exe.exists() and not exe.stat().st_mode & 0o111:
                exe.chmod(exe.stat().st_mode | 0o111)
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
        """端口表分配到的实际端口 → 配置文件（引导 + 按账号），避免多开时端口漂移。"""
        ports = instance.allocated_ports or {}

        def _m(cfg: dict) -> dict:
            if ports.get("webui"):
                w = cfg.setdefault("webui", {})
                w["port"] = int(ports["webui"])
                w.setdefault("enable", True)
                # 外网可访问：回环/缺省监听地址放开为 0.0.0.0（用户自定义地址保留）
                if str(w.get("host") or "").strip().lower() in LOOPBACK_HOSTS:
                    w["host"] = "0.0.0.0"
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

        for path in self._config_paths(instance):
            atomic_write_json(path, _m, source_json5=True)

    def configure_login(self, instance, credentials) -> dict:
        ver = self._parse_ver(credentials["version"]) \
              if credentials.get("version") else (9, 9, 9)     # 未提供版本按 v8 从严
        token = (credentials.get("auth_token") or "").strip()
        if ver >= self.V8 and not token:
            return {"conflict": "v8.0.9+ 需在快速登录平台申请 AUTH TOKEN"}
        qq = credentials.get("qq", "")
        atomic_write_json(self._config_path(instance),
                          lambda c: {**c, "QQ": qq}, source_json5=True)
        restart = False
        if token:
            # LLBot 启动时读 data/auth_token.txt（缺失直接报错退出），必须落盘且重启进程才生效
            token_file = Path(instance.dir) / "bin/llbot/data/auth_token.txt"
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(token + "\n", encoding="utf-8")
            restart = True
        return {"ok": True, "qq": qq, "restart": restart}

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
        # JSON5 读、纯 JSON 写（LLBot 1s 轮询热更新，JSON5 是超集）；
        # 引导 + 按账号两份都写，登录态下按账号那份才是热更新真正生效的
        paths = self._config_paths(instance)
        for path in paths:
            atomic_write_json(path, _m, source_json5=True)
        return WriteResult(ok=True, manual="LLBot 配置热更新，约 1 秒后自动生效，无需重启。",
                           path=str(paths[-1]))

    def get_actual_port(self, lines) -> int | None:
        for _, line in reversed(list(lines)[-200:]):
            m = re.search(r"[Oo]b11.*?端口[:：]?\s*(\d{4,5})", line)
            if m: return int(m.group(1))
        return None

    def extract_qrcode(self, line: str, instance=None) -> dict | None:
        """LLBot 无头登录的二维码三路输出：stdout 字符画（多行，还原不可靠）、
        落盘 PNG、二维码生成服务 URL（create-qr-code，含连字符，基类正则不匹配）。
        命中落盘行时直接读 bin/llbot/data/temp/login-qrcode.png 转 base64，
        前端 <img> 可直接渲染。重复码由 ws_login 的 payload 去重挡住。

        健壮性：触发行与文件写完之间存在竞态，PNG 未就绪时短重试兜底（否则这张码
        永久丢失，只能重启进程重新生成）；魔数校验挡住截断/覆盖中的半张图，
        避免把损坏 base64 推给前端且被去重逻辑记住。"""
        if instance is None or "二维码文件已保存" not in line:
            return None
        png = Path(instance.dir) / "bin/llbot/data/temp/login-qrcode.png"
        for _ in range(3):                       # 3 × 0.2s：tail 线程最多阻塞 0.6s，可接受
            data = png.read_bytes() if png.exists() else b""
            if data.startswith(b"\x89PNG"):
                b64 = base64.b64encode(data).decode()
                return {"url": None, "base64": f"data:image/png;base64,{b64}"}
            time.sleep(0.2)
        return None

    def health_check(self, instance, is_alive=False) -> dict:
        port = instance.allocated_ports.get("ob11")
        ok = self.tcp_probe("127.0.0.1", port) if port else False
        return {"alive": is_alive, "conn": "ok" if ok else "down"}
