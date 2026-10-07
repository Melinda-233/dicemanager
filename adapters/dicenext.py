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
    # 骰子不直接监听 ob11（正向模式连出），用 WebUI 端口作存活探针
    HEALTH_PORT_KEYS = ["webui"]

    def verify_required(self, instance) -> list:
        """先按清单逐个检查，再对「上游改过二进制名」做兜底。

        原来的实现只做兜底匹配（根目录里有名字以 `dicenext` 开头的文件就算就绪），
        匹配不到就**无条件报缺失** —— 于是清单登记的文件明明躺在那儿也会被判缺件
        （2026-10-08 实测：包里 start.sh 存在，校验却报「缺 start.sh」）。
        根因是 Dice-Next 3.x 的二进制叫 `dice-next-server`（**有连字符**），
        `.startswith("dicenext")` 匹配不上。

        顺序不能反：清单是唯一可靠来源（能挡住误传的源码包），前缀匹配只是
        兼容上游改名的补充，且它的判定本就宽松（认名字像就行，不保证能跑）。
        """
        d = Path(instance.dir)
        missing = [f for f in self.m["required_files"] if not (d / f).exists()]
        if not missing:
            return []
        # 兜底：根目录里已有「名字像启动体」的文件 → 视为就绪（上游改名后的兼容）
        if d.exists() and any(
                f.is_file()
                # 连字符/下划线都算：`dicenext` / `dice-next` / `dice_next` 都命中
                and f.name.lower().replace("-", "").replace("_", "").startswith("dicenext")
                for f in d.iterdir()):
            return []
        return missing   # 缺失时报清单登记名，提示更具体

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

    def write_conn_config(self, instance, mode, direction, addr, token,
                          link_id: str | None = None) -> WriteResult:
        forward = direction != "reverse"
        port = addr.split(":")[-1].rstrip("/")
        # 一连多：每条关联用各自 link_id 作为适配器名，互不覆盖；旧单关联兜底名 dicemanager
        name = link_id or "dicemanager"
        entry = {
            "name": name,
            "type": "onebot_v11",
            "connection_mode": "forward_ws" if forward else "reverse_ws",
            "endpoint": (f"ws://{addr}/" if forward else port),
            "access_token": token or "",
            "enabled": True,
        }
        path = Path(instance.dir) / self.m["config_path"]

        def _m(cfg: dict) -> dict:
            cfg.setdefault("adapters", [])
            # 按 name 查重：命中即改（一连多时各 link 用各自 link_id 互不覆盖），
            # 用户自建的其它适配器条目保留
            cfg["adapters"] = [e for e in cfg["adapters"] if e.get("name") != name]
            cfg["adapters"].append(entry)
            return cfg

        atomic_write_json(path, _m)                 # Dice!Next 用纯 JSON
        kind = "正向" if forward else "反向"
        if forward:
            detail = "骰子主动连登录端 " + str(entry["endpoint"])
        else:
            detail = "骰子监听 " + port + "，登录端反连 ws://<骰子IP>:" + port + "/"
        return WriteResult(
            ok=True, path=str(path),
            manual=f"已写入 config/adapters.json（{kind} WS）。\n"
                   f"Dice!Next 面板「适配器管理」里 {name} 连接启用即生效：\n"
                   f"  {detail}")

    def get_actual_port(self, lines) -> int | None:
        return None                                 # 端口由 allocated/webui 决定，无需日志回读
