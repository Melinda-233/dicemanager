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
    login_ref: Optional[str] = None
    state: str = State.UNDEPLOYED.value
    warnings: list = field(default_factory=list)
    webui_token: Optional[str] = None
    # 互联配置（Step4）：登录端生成 token 后落盘，骰子端自动沿用，保证两端一致
    conn_token: Optional[str] = None
    conn_addr: Optional[str] = None
    conn_direction: Optional[str] = None
    # 首启一次性动作（如 LLBot --update）只做一次，重启不重复
    first_run_done: bool = False
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
    def _to_instance(rec: dict) -> Instance:
        return Instance(**{k: v for k, v in rec.items()
                           if k in Instance.__dataclass_fields__})

    # ---------- 读 ----------
    def all(self) -> list[dict]:
        """公开只读入口（外部不访问 _load）。"""
        return [v for k, v in self._load().items() if not k.startswith("__tombstone__")]

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
               login_ref=None, bot_mode="onebot") -> Instance:
        inst = Instance(id=iid, dice=dice, arch=arch, dir=dir_, port=port,
                        allocated_ports=allocated_ports or {}, login_ref=login_ref,
                        bot_mode=bot_mode)
        self._flush(atomic_write_json(self._path, lambda t: {**t, iid: asdict(inst)}))
        return inst

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
