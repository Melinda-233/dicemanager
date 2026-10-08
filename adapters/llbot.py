"""LLBot：--qq= 快速登录 + JSON5 热更新配置 + v8 AUTH TOKEN 条件

CLI 参数（官方 README）：--qq= 快速登录、--update 检查并执行更新、--pmhq 切换模式。
默认配置 default_config.json 结构（实测）：
  webui: { enable, host, port }                       # 管理界面，默认 3080
  ob11:  { enable, connect: [ { type: "ws", enable, host, port, token, ... } ] }  # 3001
  milky: { enable, http: { host, port } }             # 3010
  satori:{ enable, host, port, token }                # 5600
端口只能靠改配置文件（CLI 无端口参数），所以分配到的端口必须在首启前写回，
否则多开时第二个实例仍会去抢默认端口。配置热更新：写完 1s 内 LLBot 自动重载。

**为何不开账号密码登录**（2026-10-07 核实上游 main 分支源码）：登录由 WebUI /
`--qq=` 快速登录驱动，配置文件（default_config.json / bin/llbot/data/config_*.json）
里**没有账号密码字段**；源码里的 `tempPassword` 是扫码登录的临时票据（TLV 0x106），
不是用户密码。故 login_modes 保持默认的 ["qrcode"] —— 强行给前端开二选一会让
用户填完密码却仍每次扫码（写了不生效比不给选项更糟）。
"""
import base64
import re
import time
from pathlib import Path

from adapters.base import LOOPBACK_HOSTS, BaseAdapter, WriteResult
from core.atomicio import atomic_write_json


class LLBotAdapter(BaseAdapter):
    V8 = (8, 0, 9)

    # 可执行文件的候选名（**顺序即优先级，清单登记名必须排第一**）。
    # 上游 v8.3.0 起把二进制从 `llbot` 改名为 `LuckyLillia`（同为 ~2MB ELF，
    # 与资产名 LLBot-CLI-* → LuckyLillia-CLI-* 同一波改名），而 bin/llbot/ 下的
    # 配置与运行时目录名**没变**。
    # 为什么不能直接把清单 exe 改成 LuckyLillia：已部署的实例目录里仍是 `llbot`，
    # 一改就启动不了（服务器上那个跑了多天的实例就是这么来的）。
    # 所以「清单登记名优先 + 候选兜底」，两边都能跑。
    # ⚠️ 顺序反了的后果：新旧包共存时优先挑到新名，与「升级路径可预期」相悖。
    EXE_ALIASES = ("llbot", "LuckyLillia")      # 清单登记名在前

    def _resolve_exe(self, instance) -> str:
        """返回该实例**实际存在**的可执行文件名。

        找不到时回清单登记名 —— 让 verify_required 报出用户预期的那个名字，
        而不是「LuckyLillia 或 llbot」这种二选一。
        """
        d = Path(instance.dir)
        for name in self.EXE_ALIASES:
            if (d / name).is_file():
                return name
        return self.m["exe"]

    # LLBot 的 ob11 端口在配置里固定存在，探测不到即为断连（不是「未配置」）
    HEALTH_USE_ACTUAL_PORT = False
    HEALTH_NO_PORT = "down"

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
        cmd = [str(Path(instance.dir) / self._resolve_exe(instance))]
        if instance.qq:
            cmd.append(f"--qq={instance.qq}")                  # 等号形式
        return cmd

    def verify_required(self, instance) -> list:
        """可执行文件允许改名（上游 v8.3.0 改名为 LuckyLillia），走候选判定；
        其余必备文件（bin/llbot/default_config.json 等）仍按清单严格校验。

        顺序要点：**exe 与其他文件要各自独立判定，然后合并报告**。
        早先写成「其他文件缺了就先 return，exe 另判」—— 结果 exe 缺失时
        反而不报（源码包只缺配置时，只看到配置那条，看不到 exe 也缺）。
        """
        d = Path(instance.dir)
        missing: list[str] = []
        # ① 可执行文件：任一候选存在即就绪（都缺时回清单登记名，提示更具体）
        if not any((d / n).is_file() for n in self.EXE_ALIASES):
            missing.append(self.m["exe"])
        # ② 其余必备文件：严格按清单
        missing += [f for f in self.m["required_files"]
                    if f != self.m["exe"] and not (d / f).exists()]
        return missing

    # ---------- 启动前准备 ----------
    def prepare_start(self, instance, runner=None) -> bool:
        """执行位保障（zip 解压不保证保留 +x）+ 端口写回 + 首启 --update。"""
        # 遍历候选名而非只管清单那一个：新版包里是 LuckyLillia，
        # 只 chmod 清单名会留下「没执行位 → 启动报 Permission denied」。
        for rel in (*self.EXE_ALIASES, "bin/llbot/node", "bin/pmhq/pmhq"):
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

    # ---------- AUTH TOKEN 复用 ----------
    # v8.0.9+ 的 AUTH TOKEN 是向快速登录平台按「QQ 号」申请的，不是通用密钥：
    # 换实例（新机器、重装、清空目录）后让用户去翻旧目录里的 auth_token.txt，
    # 几乎一定会漏填 → 部署过了但进程启动即退出（LLBot 缺这个文件直接报错）。
    # 同一台机器上已有跑着的 llbot 实例，其 token 就能直接复用。
    TOKEN_REL = "bin/llbot/data/auth_token.txt"

    def peek_auth_token(self, registry=None) -> dict:
        """从本机其它 llbot 实例回收已用过的 AUTH TOKEN，供新建实例预填。

        返回 {token, from_instance, from_qq}；无可复用时 token 为 None。
        刻意不做「自动带入并跳过用户确认」——token 属于凭据，前端要显式告知
        来源，用户仍能改；静默复用会让「我填的是哪个 token」变得不可知。
        """
        # (mtime, token, instance_id) —— mtime 是 float（p.stat().st_mtime），
        # 别标成 str：mypy 会因key[0] > newest[0] 拿 float 和 str 比而报错
        newest: tuple[float, str, str] | None = None
        # 只看 llbot：别的程序没有这个文件概念
        candidates = []
        if registry is not None:
            try:
                candidates = [r for r in registry.all() if r.get("dice") == "llbot"]
            except Exception:
                candidates = []
        for rec in candidates:
            p = Path(rec.get("dir") or "") / self.TOKEN_REL
            try:
                if not p.is_file():
                    continue
                tok = p.read_text(encoding="utf-8", errors="ignore").strip()
                if not tok:
                    continue
                key = (p.stat().st_mtime, tok, rec.get("id", ""))
            except OSError:
                continue            # 实例目录已被删/无权限：跳过，不影响其他候选
            if newest is None or key[0] > newest[0]:
                newest = key
        if newest is None:
            return {"token": None, "from_instance": None, "from_qq": None}
        # 回带 QQ 号：token 是按号申请的，让人能确认「拿的是哪个号的」
        qq = ""
        for rec in candidates:
            if rec.get("id") == newest[2]:
                qq = str(rec.get("qq") or "")
                break
        return {"token": newest[1], "from_instance": newest[2], "from_qq": qq or None}

    def write_conn_config(self, instance, mode, direction, addr, token,
                          link_id: str | None = None) -> WriteResult:
        # 多连一/一连多：每条关联用各自 link_id 作为连接名，互不覆盖；旧单关联兜底名 dicemanager
        name = link_id or "dicemanager"
        entry = {"type": "ws" if direction != "reverse" else "ws-reverse",
                 "enable": True,
                 "name": name,
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
            # 按 link_id 去重（多连一/一连多互不覆盖），同类型其它条目保留
            conn = [e for e in conn
                    if not (e.get("type") == entry["type"] and e.get("name") == name)]
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

    def list_accounts(self, instance) -> list[dict]:
        """回读 LLBot 已登录的全部 QQ 账号（bin/llbot/data/config_{qq}.json 各一个）。

        这是「登录端可登多个 QQ」的数据来源；每个账号的 ob11 connect 条目携带其
        互联端口与 token（即该账号连接的骰子端地址），多连一按账号分发时取用。"""
        d = Path(instance.dir) / "bin/llbot/data"
        out: list[dict] = []
        if not d.is_dir():
            return out
        for f in sorted(d.glob("config_*.json")):
            m = re.match(r"config_(\d{5,12})\.json$", f.name)
            if not m:
                continue
            qq = m.group(1)
            cfg = self.read_json(f)
            ob = cfg.get("ob11") or {}
            port = token = None
            for e in (ob.get("connect") or []):
                if isinstance(e, dict):
                    port = e.get("port") or port
                    token = e.get("token") or token
            out.append({"qq": qq, "port": port, "token": token, "status": "unknown"})
        return out

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
