"""Dice!Next：TRPG 骰娘（C++），角色等同 sealdice（骰子）

真实部署要点（已对 README 与官方配置文档核实）：
- 发行包为 .tar.gz（linux-amd64）；解压后二进制名形如 DiceNext（build_start_cmd 用 glob 兜底）。
- WebUI 默认 http://localhost:18088，首次访问必须设管理口令。
- 两条接入通道（清单 bot_modes 驱动）：
  · onebot：不自带 QQ 登录，靠 OneBot 适配器连登录端（NapCat / SnowLuma / LLBot）；
    适配器配置写在 config/adapters.json，面板「适配器管理」即时生效。
    forward_ws（骰子主动连登录端，endpoint=ws://host:port/）
    或 reverse_ws（登录端反连骰子，endpoint=监听端口号）。
  · official：Dice-Next 3.x **程序内置** QQ 官方机器人适配器
    （server/src/adapter/qq_official_adapter.h），面板只负责把 AppID/AppSecret
    写进同一条 config/adapters.json（type=qq_official），不需要登录端、不需要桥接进程。
    为什么是写配置文件而不是调它 API：上游启动时以 adapters.json 为准覆盖内部适配器表
    （main.cpp 3159-3195），文件即权威来源，比调 WebUI 接口少一层版本耦合。
"""
import json
import os
from pathlib import Path

from adapters.base import BaseAdapter, WriteResult
from core.atomicio import atomic_write_json

# QQ 官方机器人在 adapters.json 里的保留名（用户自己加的条目不会用这个名字）
OFFICIAL_NAME = "qq_official"
# 上游 main.cpp:3113-3122 支持的官方专属字段，这里只取最保守的一组：
# 走 WS 网关（webhook 要公网 HTTPS 入口，面板部署的形态给不出）；
# 富媒体/按钮默认关——不是所有客户端都渲染 markdown，骰子输出靠纯文本最稳，
# 需要的人在 Dice-Next 面板里自己开，面板不会覆盖非面板书写的字段。
_OFFICIAL_DEFAULTS = {
    "receive_mode": "gateway",
    "qq_rich_replies": "off",
    "qq_interactions": "links",
}


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

    # ---------- QQ 官方机器人通道 ----------

    def official_mode(self) -> dict:
        """清单里 official 通道的声明（可能带 credentials 数组）；没有则空 dict。"""
        for b in (self.m.get("bot_modes") or []):
            if isinstance(b, dict) and b.get("id") == "official":
                return b
        return {}

    def requires_official_credentials(self) -> bool:
        """True = 凭据必须由面板写进配置文件（Dice-Next）。

        False = 程序自己在 WebUI 里收（海豹），面板只管把人送进它的 WebUI。
        以清单是否声明 credentials 为唯一判据：写死判断程序名会在上游改形态时静默出错。
        """
        return bool(self.official_mode().get("credentials"))

    def configure_official(self, instance, credentials) -> dict:
        """把官方机器人凭据写进 config/adapters.json（替换旧的 qq_official 条目）。

        返回 {"ok", "path", "app_id", "preview", "manual"}；失败只有 "message"。
        不返回 secret——它会落盘，但没有任何理由再经接口回到前端。
        """
        app_id = str((credentials or {}).get("app_id") or "").strip()
        app_secret = str((credentials or {}).get("app_secret") or "").strip()
        if not app_id or not app_secret:
            return {"ok": False,
                    "message": "QQ 官方机器人需要 AppID 与 AppSecret，两个都在开放平台"
                               "「开发设置」页；AppSecret 只在生成时显示一次。"}
        entry = {"name": OFFICIAL_NAME, "type": "qq_official", "enabled": True,
                 "app_id": app_id, "app_secret": app_secret, **_OFFICIAL_DEFAULTS}
        path = Path(instance.dir) / self.m["config_path"]

        def _m(cfg: dict) -> dict:
            cfg.setdefault("adapters", [])
            # 同一个机器人只能有一条：按 type 与保留名双重过滤（上游把 type 认成
            # qq_official 就够，但用户可能把 name 也改成 qq_official，一并挡掉）
            cfg["adapters"] = [e for e in cfg["adapters"]
                               if not (isinstance(e, dict)
                                       and (e.get("type") == "qq_official"
                                            or e.get("name") == OFFICIAL_NAME))]
            cfg["adapters"].append(entry)
            return cfg

        atomic_write_json(path, _m)                 # Dice!Next 用纯 JSON
        try:
            os.chmod(path, 0o600)                   # 文件里有 AppSecret：只给服务账号自己读
        except OSError:
            pass
        return {
            "ok": True, "path": str(path), "app_id": app_id,
            "preview": f"已写入 QQ 官方机器人配置（AppID {app_id}）：启动后由骰子程序"
                       f"自己连官方网关，不需要登录端。",
            # 这条「会覆盖」的提醒必须给到用户：上游以文件为准，面板里手改的会被吃掉
            "manual": "Dice-Next 启动时会以 config/adapters.json 为准覆盖它内部的适配器表，"
                      "在它 WebUI 里手动加的适配器会被下一次面板写入覆盖；AppSecret 不回显，"
                      "换密后要重新填一次。启动后仍连不上官方网关时，先查开放平台的 "
                      "IP 白名单与机器人主体资格（个人主体只能进自己是群主的群）。",
        }

    def official_status(self, instance) -> dict:
        """回读已写入的官方配置，只回 AppID（secret 不回到任何只读接口）。"""
        path = Path(instance.dir) / self.m["config_path"]
        try:
            cfg = json.loads(path.read_text("utf-8"))
            for e in (cfg.get("adapters") or []):
                if isinstance(e, dict) and e.get("type") == "qq_official":
                    return {"configured": True, "app_id": e.get("app_id") or None,
                            "path": str(path)}
        except (OSError, ValueError):
            pass
        return {"configured": False, "app_id": None, "path": str(path)}
