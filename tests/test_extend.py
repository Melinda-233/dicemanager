"""2026-09-26 拓展项回归：备份导出双口径 / 定时任务 / 事件通知 / 日志检索 / 升级版本基线。
ctx 模块级单例 + DM_STATE_DIR 指向临时目录（参考 test_optim.py）。"""
import io
import json
import os
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

tmp = tempfile.mkdtemp(prefix="dm_ext_")
os.environ["DM_STATE_DIR"] = tmp
os.environ["DM_LOG_DIR"] = os.path.join(tmp, "logs")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest                                       # noqa: E402
from fastapi import HTTPException                   # noqa: E402

from api.context import ctx                         # noqa: E402
from core.backup import export_dir, restore_into    # noqa: E402


# ---------- 拓展1：备份导出（整目录 / 应用数据两种口径） ----------

def _mk_instance_dir(root: Path) -> Path:
    d = root / "inst"
    (d / "data" / "saves").mkdir(parents=True)
    (d / "sealdice-core").write_text("PROGRAM", encoding="utf-8")
    (d / "data" / "saves" / "save.db").write_text("SAVE-DATA", encoding="utf-8")
    (d / "data" / "serve.yaml").write_text("imSession: {}", encoding="utf-8")
    return d


def test_export_full_roundtrip(tmp_path):
    src = _mk_instance_dir(tmp_path)
    out = tmp_path / "full.tar.gz"
    info = export_dir(src, out, scope="full")
    assert info["scope"] == "full" and info["files"] == 3
    dest = tmp_path / "restored"
    r = restore_into(out, dest)
    assert r["files"] == 3
    assert (dest / "sealdice-core").read_text(encoding="utf-8") == "PROGRAM"
    assert (dest / "data" / "saves" / "save.db").read_text(encoding="utf-8") == "SAVE-DATA"


def test_export_data_scope_only_declared_paths(tmp_path):
    """data 口径只打包 manifest data_paths 声明的数据路径，不含程序本体。"""
    src = _mk_instance_dir(tmp_path)
    out = tmp_path / "data.tar.gz"
    info = export_dir(src, out, scope="data", data_paths=["data"])
    assert info["scope"] == "data"
    names = {m.name for m in tarfile.open(out).getmembers() if m.isfile()}
    assert "data/saves/save.db" in names
    assert not any(n == "sealdice-core" for n in names)


def test_export_data_scope_requires_paths(tmp_path):
    src = _mk_instance_dir(tmp_path)
    with pytest.raises(ValueError, match="data_paths"):
        export_dir(src, tmp_path / "x.tar.gz", scope="data", data_paths=None)
    with pytest.raises(ValueError, match="非法"):
        export_dir(src, tmp_path / "x.tar.gz", scope="data", data_paths=["../escape"])
    with pytest.raises(ValueError, match="口径"):
        export_dir(src, tmp_path / "x.tar.gz", scope="weird")


# ---------- 拓展7：定时任务 ----------

class _StubPM:
    def __init__(self):
        self.stops, self.alive = [], True

    def is_alive(self, _iid):
        return self.alive

    def stop(self, iid):
        self.stops.append(iid)
        self.alive = False


class _StubWizard:
    def __init__(self, adapters):
        self.pm = _StubPM()
        self.adapters = adapters
        self.started = []

    def start_instance(self, iid):
        self.started.append(iid)
        self.pm.alive = True
        return None, False


def _stub_wizard(monkeypatch):
    """给 ctx.scheduler 塞 stub wizard；adapters 用真实清单（data_paths 声明验证）。"""
    from core.scheduler import Scheduler
    w = _StubWizard(ctx.adapters)
    sched = Scheduler(Path(tmp, "schedules_test.json"), Path(tmp, "backups"),
                      ctx.registry, w)
    ctx.registry.create("sealdice-s1", dice="sealdice", arch="standalone",
                        dir_=str(Path(tmp, "s1")), port=0, allocated_ports={})
    Path(tmp, "s1").mkdir(exist_ok=True)
    (Path(tmp, "s1", "data")).mkdir(exist_ok=True)
    (Path(tmp, "s1", "data", "x.db")).write_text("D", encoding="utf-8")
    return sched, w


def test_scheduler_backup_and_restart_run(monkeypatch):
    sched, w = _stub_wizard(monkeypatch)
    now = datetime(2026, 9, 26, 8, 30)
    b = sched.add("sealdice-s1", "backup", 8, 30, scope="data", keep=2)
    r = sched.add("sealdice-s1", "restart", 8, 30)
    assert sched.tick(now) and set(sched.tick(now)) == set()   # last_day 防重复
    out = Path(tmp, "backups")
    tars = list(out.glob("*sealdice-s1-*.tar.gz"))
    assert len(tars) == 1                                      # backup 任务产出 1 份
    # restart 任务确实走了向导启动路径
    assert "sealdice-s1" in w.started


def test_scheduler_backup_prune_keeps_n(monkeypatch):
    sched, _ = _stub_wizard(monkeypatch)
    out = Path(tmp, "backups")
    out.mkdir(parents=True, exist_ok=True)
    for i in range(5):                                         # 手造 5 份旧备份
        # 必须带 -sched- 标记：定时任务的 keep 滚动只认自己生成的命名模式
        # （升级前快照 *-preupgrade-* 同样含 `<id>-`，不能卷进来被删）
        p = out / f"sealdice-sealdice-s1-sched-data-2026090{i}-0000.tar.gz"
        with tarfile.open(p, "w:gz") as tf:
            ti = tarfile.TarInfo("marker")
            ti.size = 0
            tf.addfile(ti, io.BytesIO(b""))
        p.write_bytes(p.read_bytes())                          # 触碰 mtime 顺序
    task = sched.add("sealdice-s1", "backup", 9, 0, scope="data", keep=2)
    sched.run_now(task["id"])
    left = [p for p in out.glob("*sealdice-s1-*.tar.gz")]
    assert len(left) == 2                                      # 5 旧 + 1 新 → 只留 2 份最新


def test_scheduler_prune_isolated_by_scope(monkeypatch):
    """同实例可同时挂 full / data 两条定时备份，各自滚动互不误删。

    `_prune` 若只按实例 id 匹配，data 口径的 keep 会把 full 口径的产物算进去一并删掉。
    """
    sched, _ = _stub_wizard(monkeypatch)
    out = Path(tmp, "backups")
    out.mkdir(parents=True, exist_ok=True)
    for scope in ("full", "data"):
        for i in range(4):
            p = out / f"sealdice-sealdice-s1-sched-{scope}-2026090{i}-0000.tar.gz"
            with tarfile.open(p, "w:gz") as tf:
                ti = tarfile.TarInfo("marker")
                ti.size = 0
                tf.addfile(ti, io.BytesIO(b""))
    task = sched.add("sealdice-s1", "backup", 9, 0, scope="data", keep=2)
    sched.run_now(task["id"])
    assert len(list(out.glob("*-sched-data-*.tar.gz"))) == 2   # 4 旧 + 1 新 → 留 2
    assert len(list(out.glob("*-sched-full-*.tar.gz"))) == 4   # full 口径不受牵连


def test_scheduler_rejects_bad_input(monkeypatch):
    sched, _ = _stub_wizard(monkeypatch)
    with pytest.raises(ValueError):
        sched.add("sealdice-s1", "reboot", 8, 30)
    with pytest.raises(ValueError):
        sched.add("sealdice-s1", "restart", 25, 30)
    with pytest.raises(KeyError):
        sched.add("ghost-instance", "restart", 8, 30)


# ---------- 拓展10：日志检索 ----------

def test_logs_search_endpoint():
    from api.rest import logs_search
    ctx.registry.create("sealdice-l1", dice="sealdice", arch="standalone",
                        dir_=f"/tmp/sealdice-l1", port=0, allocated_ports={})
    proc = ctx.pm.get("sealdice-l1")
    proc.note("onebot v11 连接成功")
    proc.note("随机日志行")
    r = logs_search("连接成功")
    assert any(x["instance"] == "sealdice-l1" and "连接成功" in x["line"]
               for x in r["results"])
    with pytest.raises(HTTPException):
        logs_search("a")                          # 至少 2 字符


# ---------- 拓展11：升级通道（版本基线 + 升级流程） ----------

def test_deploy_records_version_baseline(monkeypatch, tmp_path):
    """部署解析到 release tag → DEPLOY_VERSION 基线；wizard step2 落盘 instance.version。"""
    from adapters.base import DEPLOY_VERSION
    from services.wizard import Wizard
    from core.registry import State
    # 假适配器：deploy 直接 ok，带 tag
    class FakeAdapter:
        def __init__(self, m):
            self.m = m
        def deploy(self, instance):
            DEPLOY_VERSION[instance.id] = "v9.9.9"
            return "ok"
    monkeypatch.setitem(ctx.adapters, "sealdice", (ctx.adapters["sealdice"][0], FakeAdapter))
    w = Wizard(ctx.registry, ctx.adapters, ctx.ports, ctx.pm, Path(tmp, "logs"))
    iid = w.create_instance("sealdice", "standalone")
    try:
        r = w.run_step(iid, 2, {})
        assert r["result"] == "ok"
        assert ctx.registry.get(iid).version == "v9.9.9"
    finally:
        monkeypatch.undo()
        ctx.registry.remove(iid)


def test_upgrade_check_unsupported_for_manual():
    from api.rest import upgrade_check
    ctx.registry.create("shiki-u1", dice="shiki", arch="standalone",
                        dir_="/tmp/shiki-u1", port=0, allocated_ports={})
    try:
        r = upgrade_check("shiki-u1")
        assert r["supported"] is False and "离线" in r["message"]
    finally:
        ctx.registry.remove("shiki-u1")
