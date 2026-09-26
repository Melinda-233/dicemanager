"""定时任务守护：每日定时重启 / 定时备份（导出到状态目录 backups/，滚动保留）

- schedules.json（STATE_DIR）：[{id, inst_id, kind, hh, mm, enabled, scope, keep, last_day}]
- 守护线程 30s 一跳：匹配「当前时刻的 HH:MM 且今天还没跑过」即执行；
  last_day 持久化防重启后同一分钟重复执行。
- 备份复用 core.backup.export_dir（scope=full 整目录 / data 应用数据局部），
  与手动导出同一实现——「整实例目录备份」与「应用端数据局部备份」两种口径在
  kind=backup 时同样成立。
- 执行失败写管理器日志，绝不重试到死：错过就等下一天。
"""
import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path

from core.atomicio import write_atomic
from core.locks import instance_lock

log = logging.getLogger("dicemanager.scheduler")

CHECK_INTERVAL = 30


class Scheduler:
    def __init__(self, schedules_path: Path, backup_dir: Path, registry, wizard):
        self._path = Path(schedules_path)
        self.backup_dir = Path(backup_dir)
        self._reg = registry
        self._wizard = wizard
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running: set[str] = set()          # 正在执行的任务 id（防重入）

    # ---------- CRUD ----------
    def list_all(self) -> list[dict]:
        return self._load()

    def add(self, inst_id: str, kind: str, hh: int, mm: int,
            scope: str = "data", keep: int = 7) -> dict:
        if kind not in ("restart", "backup"):
            raise ValueError(f"未知任务类型: {kind}")
        if kind == "backup" and scope not in ("full", "data"):
            raise ValueError(f"未知备份口径: {scope}")
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("时间需为 0-23 时 / 0-59 分")
        self._reg.get(inst_id)                   # 不存在直接 KeyError → 404
        task = {"id": uuid.uuid4().hex[:8], "inst_id": inst_id, "kind": kind,
                "hh": int(hh), "mm": int(mm), "enabled": True,
                "scope": scope, "keep": max(1, min(int(keep), 60)),
                "last_day": ""}
        with instance_lock("schedules"):        # 与写盘互斥（轻量，读写都短）
            data = self._load()
            data.append(task)
            self._flush(data)
        return task

    def remove(self, task_id: str) -> bool:
        with instance_lock("schedules"):
            data = self._load()
            rest = [t for t in data if t["id"] != task_id]
            if len(rest) == len(data):
                return False
            self._flush(rest)
        return True

    def run_now(self, task_id: str) -> dict:
        """手动立即执行（测试/补跑）；记 last_day 防止今晚到点再自动跑一次。"""
        task = next((t for t in self._load() if t["id"] == task_id), None)
        if not task:
            raise KeyError(f"任务不存在: {task_id}")
        if task["id"] in self._running:
            raise RuntimeError("该任务正在执行中")
        self._running.add(task["id"])
        try:
            self._run(task, datetime.now())
            today = datetime.now().strftime("%Y-%m-%d")
            with instance_lock("schedules"):
                data = self._load()
                for t in data:
                    if t["id"] == task_id:
                        t["last_day"] = today
                self._flush(data)
            return {"ok": True, "task": task["id"], "kind": task["kind"]}
        finally:
            self._running.discard(task["id"])

    # ---------- 持久化 ----------
    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            d = json.loads(self._path.read_text("utf-8"))
            return d if isinstance(d, list) else []
        except (OSError, ValueError):
            return []

    def _flush(self, data: list[dict]) -> None:
        write_atomic(self._path, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    # ---------- 调度循环 ----------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="dicemanager-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(CHECK_INTERVAL):
            try:
                self.tick()
            except Exception as e:               # 单轮失败不杀死守护线程
                log.warning("[scheduler] 调度轮失败: %s", e)

    def tick(self, now: datetime | None = None) -> list[str]:
        """扫一遍到期任务并执行；返回已执行的任务 id（测试直接调 tick(now=...) 驱动）。"""
        now = now or datetime.now()
        ran: list[str] = []
        for task in self._load():
            if not task.get("enabled") or task["id"] in self._running:
                continue
            if (task["hh"], task["mm"]) != (now.hour, now.minute):
                continue
            if task.get("last_day") == now.strftime("%Y-%m-%d"):
                continue
            self._running.add(task["id"])
            try:
                self._run(task, now)
                ran.append(task["id"])
                task["last_day"] = now.strftime("%Y-%m-%d")
                with instance_lock("schedules"):
                    data = self._load()
                    for t in data:
                        if t["id"] == task["id"]:
                            t["last_day"] = task["last_day"]
                    self._flush(data)
            except Exception as e:
                log.warning("[scheduler] 任务 %s(%s) 执行失败: %s",
                            task["id"], task["kind"], e)
            finally:
                self._running.discard(task["id"])
        return ran

    def _run(self, task: dict, now: datetime) -> None:
        inst_id = task["inst_id"]
        try:
            inst = self._reg.get(inst_id)
        except KeyError:
            log.warning("[scheduler] 实例 %s 已删除，任务 %s 跳过（请清理该任务）",
                        inst_id, task["id"])
            return
        if task["kind"] == "restart":
            with instance_lock(inst_id):
                proc_alive = self._wizard.pm.is_alive(inst_id)
                if proc_alive:
                    self._wizard.pm.stop(inst_id)
                self._wizard.start_instance(inst_id)
            log.info("[scheduler] 已定时重启 %s (%s)", inst.dice, inst_id)
            return
        # backup：停机快照口径与手动导出一致（运行中导出可能拿到不一致文件）
        was_running = self._wizard.pm.is_alive(inst_id)
        if was_running:
            with instance_lock(inst_id):
                self._wizard.pm.stop(inst_id)
        try:
            from core.backup import export_dir
            # sched 标记：与升级前快照（*-preupgrade-*）区分，_prune 只认带此标记的文件
            name = f"{inst.dice}-{inst_id}-sched-{task['scope']}-{now:%Y%m%d-%H%M}.tar.gz"
            out = self.backup_dir / name
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            export_dir(Path(inst.dir), out, scope=task["scope"],
                       data_paths=self._data_paths(inst))
            self._prune(inst_id, task["scope"], task["keep"])
            log.info("[scheduler] 已定时备份 %s → %s", inst_id, out.name)
        finally:
            if was_running:
                try:
                    with instance_lock(inst_id):
                        self._wizard.start_instance(inst_id)
                except Exception as e:           # 备份后拉回失败必须大声说
                    log.error("[scheduler] %s 备份后重启失败: %s", inst_id, e)

    def _prune(self, inst_id: str, scope: str, keep: int) -> None:
        """同实例+同口径的旧备份滚动清理（只删自己生成的命名模式）。

        匹配必须带 `-sched-<scope>-` 全标记，两个原因：
        ① 产物目录里还有升级前快照（`<dice>-<id>-preupgrade-<ts>.tar.gz`），同样含 `<id>-` 子串，
           裸前缀匹配会把升级快照算进 keep 滚动并误删（2026-09-26 统一目录时修正）；
        ② 同一实例可能同时挂 full / data 两条定时备份，只按实例匹配会让 口径 A 的 keep
           把口径 B 的产物一并删掉（2026-09-26 补 scope）。
        """
        marker = f"{inst_id}-sched-{scope}-"
        olds = sorted(self.backup_dir.glob(f"*{marker}*.tar.gz"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for p in olds[keep:]:
            try:
                p.unlink()
            except OSError as e:
                log.warning("[scheduler] 旧备份清理失败 %s: %s", p, e)

    def _data_paths(self, inst) -> list[str] | None:
        manifest, _ = self._wizard.adapters[inst.dice]
        return manifest.get("data_paths")
