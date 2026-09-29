"""实例注册表与状态机：UNDEPLOYED→DEPLOYING→AWAIT_LOGIN→CONFIGURED→RUNNING"""
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from core.atomicio import atomic_write_json
from core.locks import instance_lock


class State(Enum):
    UNDEPLOYED = "UNDEPLOYED"; DEPLOYING = "DEPLOYING"
    AWAIT_LOGIN = "AWAIT_LOGIN"; CONFIGURED = "CONFIGURED"
    RUNNING = "RUNNING"; ERROR = "ERROR"

TRANSITIONS = {
    State.UNDEPLOYED:  {State.DEPLOYING, State.AWAIT_LOGIN},
    State.DEPLOYING:   {State.AWAIT_LOGIN, State.CONFIGURED, State.UNDEPLOYED},
    State.AWAIT_LOGIN: {State.CONFIGURED, State.RUNNING, State.UNDEPLOYED},
    State.CONFIGURED:  {State.RUNNING, State.UNDEPLOYED},
    State.RUNNING:     {State.CONFIGURED, State.UNDEPLOYED},
    State.ERROR:       {State.DEPLOYING, State.AWAIT_LOGIN, State.UNDEPLOYED},
}
# ERROR 特批：任意运行态可迁移到 ERROR；出边允许从错误恢复——重跑部署/登录向导或直接回滚删除
# AWAIT_LOGIN → RUNNING：二维码/账号登录流程中直接启动（登录随启动进程完成）

@dataclass
class Instance:
    id: str
    dice: str
    arch: str
    dir: str
    port: int
    actual_port: Optional[int] = None
    allocated_ports: dict = field(default_factory=dict)   # {webui:3080, ob11:3001,...}
    qq: Optional[str] = None
    # —— 兼容字段（legacy，单关联场景；由 links 同步，勿单独作为真相来源）——
    login_ref: Optional[str] = None
    # 实例状态机当前态（UNDEPLOYED→DEPLOYING→AWAIT_LOGIN→CONFIGURED→RUNNING）
    state: str = State.UNDEPLOYED.value
    # 部署/启动期间的告警（如 OlivaDice 缺件告警），随实例回显到前端
    warnings: list = field(default_factory=list)
    # 互联配置（Step4）：登录端生成 token 后落盘，骰子端自动沿用，保证两端一致
    conn_token: Optional[str] = None
    conn_addr: Optional[str] = None
    conn_direction: Optional[str] = None
    # —— 多账号 + 多连一 / 一连多 新模型 ——
    # 登录端：已登录 QQ 账号清单（由适配器 list_accounts 发现）
    #   每项 {qq, token, port, status}，status ∈ running/stopped/unknown
    accounts: list = field(default_factory=list)
    # 骰子端：关联的登录端列表（多连一=多个骰子端各有此列表指向同一登录端；
    #   一连多=单个骰子端此列表含多个登录端）。每项：
    #   {login_ref, account_qq(可选，绑定登录端的某个账号), conn_token, conn_addr, conn_direction}
    links: list = field(default_factory=list)
    # 首启一次性动作（如 LLBot --update）只做一次，重启不重复
    first_run_done: bool = False
    # 登录端 WebUI 令牌：从启动日志回读后经 ws_overview 落盘，供 WebUI API 热更新互联
    # 配置（Bearer）与 REST 探测接口使用。此前仅 JSON 落盘却未声明为字段，重载即丢失。
    webui_token: Optional[str] = None
    # 部署时的上游版本（release tag，升级通道比对用；直链/manual 无版本为 None）
    version: Optional[str] = None
    # 接入通道：onebot=常规协议端（需登录端）；official=官方机器人通道（无需登录端，
    # 由程序自身 WebUI 走官方凭证/扫码，面板不写互联配置）
    bot_mode: str = "onebot"
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))

class Registry:
    """mtime 版本的内存读缓存：读走缓存（跨进程写盘后 mtime 变化自动失效），写穿盘。
    原来 all()/get() 每次全量重读 JSON，总览 2s 周期下是明显的 IO/CPU 放大点。"""

    def __init__(self, path):
        self._path = Path(path)
        self._cache: dict | None = None
        self._mtime: int | None = None
        self._io_lock = threading.Lock()

    # ---------- 缓存 ----------
    def _load(self) -> dict:
        with self._io_lock:
            try:
                mt = self._path.stat().st_mtime_ns
            except OSError:
                mt = None
            if self._cache is None or mt != self._mtime:
                self._cache = (json.loads(self._path.read_text("utf-8"))
                               if self._path.exists() else {})
                self._mtime = mt
            return self._cache

    def _flush(self, data: dict) -> None:
        """写盘后同步缓存（atomic_write_json 返回落盘内容）。"""
        with self._io_lock:
            self._cache = data
            try:
                self._mtime = self._path.stat().st_mtime_ns
            except OSError:
                self._mtime = None

    @staticmethod
    def _migrate_links(rec: dict) -> dict:
        """旧实例（仅 login_ref 单关联）迁移到 links 列表；并据 links[0] 同步 legacy 字段。"""
        links = rec.get("links") or []
        if not links and rec.get("login_ref"):
            links = [{"login_ref": rec["login_ref"],
                      "account_qq": rec.get("account_qq"),
                      "conn_token": rec.get("conn_token"),
                      "conn_addr": rec.get("conn_addr"),
                      "conn_direction": rec.get("conn_direction")}]
        if "accounts" not in rec:
            rec["accounts"] = []
        rec["links"] = links
        # 同步 legacy 单关联字段（给未改造的读取方兜底）
        if links:
            first = links[0]
            rec["login_ref"] = first.get("login_ref")
            rec["conn_token"] = first.get("conn_token")
            rec["conn_addr"] = first.get("conn_addr")
            rec["conn_direction"] = first.get("conn_direction")
        return rec

    @staticmethod
    def _to_instance(rec: dict) -> Instance:
        return Instance(**{k: v for k, v in Registry._migrate_links(dict(rec)).items()
                           if k in Instance.__dataclass_fields__})

    # ---------- 读 ----------
    def all(self) -> list[dict]:
        """公开只读入口（外部不访问 _load）。

        返回经 _migrate_links 归一化的记录副本：legacy 单关联（仅 login_ref）在这里补出
        links/accounts，使 list 接口、删除级联等消费方与 get() 看到一致的结构。此前直接
        回传原始缓存记录，导致未改造的旧实例在 /api/instances 里看不到 links（向导 step4
        无法回显既有链路），删除登录端时级联解除也会漏掉它们。
        """
        return [self._migrate_links(dict(v)) for k, v in self._load().items()
                if not k.startswith("__tombstone__")]

    def get(self, inst_id: str) -> Instance:
        rec = self._load().get(inst_id)
        if not rec: raise KeyError(f"实例不存在: {inst_id}")
        return self._to_instance(rec)

    def resume_pending(self) -> list[Instance]:
        """启动扫描：中间态实例允许向导继续或回滚。"""
        return [self._to_instance(v) for k, v in self._load().items()
                if not k.startswith("__tombstone__")
                and v.get("state") in (State.DEPLOYING.value, State.AWAIT_LOGIN.value)]

    # ---------- 写（写穿 + 同步缓存）----------
    def create(self, iid, dice, arch, dir_, port, allocated_ports=None,
               login_ref=None, links=None, bot_mode="onebot") -> Instance:
        if links is None and login_ref:
            links = [{"login_ref": login_ref}]
        links = [l for l in (links or []) if l.get("login_ref")]
        inst = Instance(id=iid, dice=dice, arch=arch, dir=dir_, port=port,
                        allocated_ports=allocated_ports or {}, links=links,
                        login_ref=links[0].get("login_ref") if links else None,
                        bot_mode=bot_mode)
        self._flush(atomic_write_json(self._path, lambda t: {**t, iid: asdict(inst)}))
        return inst

    def set_links(self, inst_id: str, links: list) -> None:
        """原子替换实例的关联登录端列表（多连一 / 一连多 的统一写入口）。

        links 每项 {login_ref, account_qq?, conn_token?, conn_addr?, conn_direction?}。
        同步 legacy 单关联字段（login_ref / conn_*）为 links[0]，供未改造读取方兜底。
        """
        cleaned = [l for l in (links or []) if l.get("login_ref")]
        first = cleaned[0] if cleaned else {}
        def _m(t: dict) -> dict:
            rec = t.get(inst_id, {})
            rec["links"] = cleaned
            rec["login_ref"] = first.get("login_ref")
            rec["conn_token"] = first.get("conn_token")
            rec["conn_addr"] = first.get("conn_addr")
            rec["conn_direction"] = first.get("conn_direction")
            t[inst_id] = rec
            return t
        self._flush(atomic_write_json(self._path, _m))

    def update(self, inst_id: str, **kw) -> None:
        with instance_lock(inst_id):
            def _m(t: dict) -> dict:
                t[inst_id] = {**t.get(inst_id, {}), **kw}; return t
            self._flush(atomic_write_json(self._path, _m))

    def transition(self, inst_id: str, to: State) -> None:
        with instance_lock(inst_id):
            def _m(t: dict) -> dict:
                rec = t[inst_id]
                cur = State(rec["state"])
                legal = cur in TRANSITIONS and to in TRANSITIONS[cur]
                if not (legal or to is State.ERROR):
                    raise ValueError(f"非法迁移 {cur.value} → {to.value}")
                rec["state"] = to.value
                return t
            self._flush(atomic_write_json(self._path, _m))

    def remove(self, inst_id: str) -> None:
        """墓碑式删除：保留 30 天防端口/目录误分配。"""
        with instance_lock(inst_id):
            def _m(t: dict) -> dict:
                rec = t.pop(inst_id, None)
                if rec:
                    rec["state"] = "removed"
                    rec["removed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    t[f"__tombstone__{inst_id}"] = rec
                return t
            self._flush(atomic_write_json(self._path, _m))

    def purge_tombstones(self, days: int = 30) -> None:
        cutoff = time.time() - days * 86400
        def _m(t: dict) -> dict:
            for k in [k for k in t if k.startswith("__tombstone__")]:
                ts = t[k].get("removed_at", "")
                if ts and time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S")) < cutoff:
                    del t[k]
            return t
        self._flush(atomic_write_json(self._path, _m))
