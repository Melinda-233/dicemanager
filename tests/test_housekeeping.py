"""2026-09-26 回收能力回归：程序包死缓存清理 + 实例日志随删除回收。

背景：程序包下载/上传后永久驻留且无 TTL，累积过一批「下载过但从未部署」的死缓存
（服务器上 olivadice/napcat/dicenext 共 94MB 从未被任何实例使用过）；同时实例删除
只从注册表除名、日志文件长期残留，反复增删会留下一堆查不到归属的孤儿日志。
"""
import io
import os
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from api.context import ctx  # noqa: E402
from api.rest import (delete_export, delete_instance, delete_unused_packages,  # noqa: E402
                      list_exports, list_packages, prune_exports)
from core import exports  # noqa: E402
from core import packages as pkgstore  # noqa: E402
from core.process import PANEL_LOG_STEM, ProcessManager  # noqa: E402
from core.scheduler import Scheduler  # noqa: E402
from conftest import put_package  # noqa: E402


def _zip_bytes(size: int = 1024) -> bytes:
    """生成约 size 字节的 zip（STORED 不压缩，体积可预测；默认小，够验证格式即可）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("dummy.bin", os.urandom(size))
    return buf.getvalue()


@pytest.fixture
def isolate(tmp_path, monkeypatch):
    """每个用例独占 state/log 目录：packages 路径函数按调用时环境变量取值。"""
    monkeypatch.setenv("DM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DM_LOG_DIR", str(tmp_path / "logs"))
    yield tmp_path
    for r in ctx.registry.all():
        try:
            ctx.registry.remove(r["id"])
        except Exception:
            pass
    ctx.registry.purge_tombstones(days=0)


def _mk(iid, dice):
    ctx.registry.create(iid, dice=dice, arch="standalone", dir_=f"/tmp/{iid}",
                        port=3000, allocated_ports={"webui": 3080})


# ---------- 程序包缓存 ----------

def test_list_packages_marks_in_use(isolate):
    put_package("sealdice", _zip_bytes(), source="upload")
    put_package("olivadice", _zip_bytes(), source="upload")
    _mk("sealdice-used001", "sealdice")
    rows = {r["dice"]: r for r in list_packages()}
    assert rows["sealdice"]["in_use"] is True          # 有实例在用 → 不可随便清
    assert rows["olivadice"]["in_use"] is False        # 无实例 → 死缓存


def test_delete_unused_packages_only_removes_unreferenced(isolate):
    put_package("sealdice", _zip_bytes(600 * 1024), source="upload")
    put_package("olivadice", _zip_bytes(600 * 1024), source="upload")
    _mk("sealdice-used002", "sealdice")
    r = delete_unused_packages()
    assert r["ok"] is True
    assert r["removed"] == ["olivadice"]
    assert 0.5 < r["freed_mb"] < 0.7              # 释放量按真实字节算，不能被精度抹成 0
    assert pkgstore.find_archive("olivadice") is None          # 死缓存已删
    assert pkgstore.find_archive("sealdice") is not None       # 在用的保留


def test_delete_unused_packages_noop_when_all_used(isolate):
    put_package("sealdice", _zip_bytes(), source="upload")
    _mk("sealdice-used003", "sealdice")
    r = delete_unused_packages()
    assert r["removed"] == [] and r["freed_mb"] == 0


def test_unused_route_registered_before_dice_route():
    """/packages/unused 必须先于 /packages/{dice} 注册，否则被当成 dice 名吞掉
    （同源事故：/instances/{id}/backup 曾被 /{op} 抢先匹配）。"""
    from api.app import app
    del_paths = [r.path for r in app.routes
                 if "/packages/" in getattr(r, "path", "")
                 and "DELETE" in (getattr(r, "methods", None) or set())]
    assert "/api/packages/unused" in del_paths
    assert "/api/packages/{dice}" in del_paths
    assert del_paths.index("/api/packages/unused") < del_paths.index("/api/packages/{dice}")


# ---------- 日志回收 ----------

def test_remove_logs_deletes_log_and_rotated_copy(isolate):
    log_dir = Path(isolate / "logs"); log_dir.mkdir(parents=True, exist_ok=True)
    pm = ProcessManager(log_dir)
    (log_dir / "llbot-abc12345.log").write_text("x", encoding="utf-8")
    (log_dir / "llbot-abc12345.log.1").write_text("x", encoding="utf-8")
    removed = pm.remove_logs("llbot-abc12345")
    assert set(removed) == {"llbot-abc12345.log", "llbot-abc12345.log.1"}
    assert not (log_dir / "llbot-abc12345.log").exists()
    assert not (log_dir / "llbot-abc12345.log.1").exists()


def test_sweep_orphan_logs_keeps_live_and_panel_logs(isolate):
    log_dir = Path(isolate / "logs"); log_dir.mkdir(parents=True, exist_ok=True)
    pm = ProcessManager(log_dir)
    for name in ("sealdice-live1234.log", "llbot-dead9999.log", "llbot-dead9999.log.1",
                 f"{PANEL_LOG_STEM}.log", f"{PANEL_LOG_STEM}.log.1"):
        (log_dir / name).write_text("x", encoding="utf-8")
    removed = pm.sweep_orphan_logs({"sealdice-live1234"})
    assert removed == ["llbot-dead9999.log", "llbot-dead9999.log.1"]
    assert (log_dir / "sealdice-live1234.log").exists()        # 存活实例的日志不动
    assert (log_dir / f"{PANEL_LOG_STEM}.log").exists()        # 面板自身日志不动
    assert (log_dir / f"{PANEL_LOG_STEM}.log.1").exists()


def test_delete_instance_removes_its_logs(isolate):
    # ctx.pm 是 import 期构建的单例，日志目录不随 monkeypatch 的 DM_LOG_DIR 改变，
    # 必须写在它自己的 log_dir 里才能被打到
    log_dir = Path(ctx.pm.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    _mk("llbot-dead0001", "llbot")
    (log_dir / "llbot-dead0001.log").write_text("running", encoding="utf-8")
    (log_dir / "llbot-dead0001.log.1").write_text("old", encoding="utf-8")
    from core.registry import State
    for st in (State.DEPLOYING, State.AWAIT_LOGIN):
        try:
            ctx.registry.transition("llbot-dead0001", st)
        except ValueError:
            pass
    r = delete_instance("llbot-dead0001", confirm=True, remove_dir=False)
    assert r["ok"] is True
    assert "llbot-dead0001.log" in r["removed_logs"]
    assert not (log_dir / "llbot-dead0001.log").exists()
    assert not (log_dir / "llbot-dead0001.log.1").exists()


def test_delete_instance_still_requires_confirm(isolate):
    _mk("llbot-dead0002", "llbot")
    try:
        delete_instance("llbot-dead0002", remove_dir=False)
    except HTTPException as e:
        assert e.status_code == 400
    else:
        raise AssertionError("缺 confirm 的删除未被拒绝")


# ---------- 备份产物回收（exports/） ----------
# 背景：升级每次留一份整目录快照、定时备份也落同一目录，此前既无可见性也无回收入口，
# 长期会堆出几百 MB。这里锁住「清单 / 单删 / 按天清理 / 目录穿越拒绝」四条口径。

def _mk_export(name, size: int = 4096, age_days: int = 0):
    d = exports.exports_dir(); d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"x" * size)
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(p, (old, old))
    return p


def test_prune_route_registered_before_name_route():
    """/exports/prune 必须先于 /exports/{name} 注册，否则 "prune" 被当成文件名匹配
    （同源事故：/packages/unused 与 /packages/{dice}）。"""
    from api.app import app
    del_paths = [r.path for r in app.routes
                 if "/exports/" in getattr(r, "path", "")
                 and "DELETE" in (getattr(r, "methods", None) or set())]
    assert "/api/exports/prune" in del_paths
    assert "/api/exports/{name}" in del_paths
    assert del_paths.index("/api/exports/prune") < del_paths.index("/api/exports/{name}")


def test_list_exports_reports_size_and_age(isolate):
    _mk_export("sealdice-abc12345-preupgrade-20260101-000000.tar.gz", size=4096, age_days=3)
    rows = list_exports()
    assert len(rows) == 1
    assert rows[0]["age_days"] == 3
    assert rows[0]["bytes"] == 4096


def test_delete_export_removes_only_the_named_file(isolate):
    _mk_export("llbot-dead0031-sched-data-20260101-000000.tar.gz")
    _mk_export("llbot-dead0031-preupgrade-20260102-000000.tar.gz")
    assert delete_export("llbot-dead0031-sched-data-20260101-000000.tar.gz")["ok"] is True
    names = [r["name"] for r in list_exports()]
    assert "llbot-dead0031-sched-data-20260101-000000.tar.gz" not in names
    assert "llbot-dead0031-preupgrade-20260102-000000.tar.gz" in names


def test_delete_export_rejects_traversal(isolate):
    """`../` 之类越界名称必须拒绝，绝不能删到 exports 目录之外。"""
    with pytest.raises(HTTPException) as e:
        delete_export("../instances.json")
    assert e.value.status_code == 404


def test_prune_exports_only_removes_stale(isolate):
    # 文件要够大：freed_mb 保留两位小数，几 KB 的样本会被精度抹成 0.0（与包缓存同源）
    _mk_export("old-preupgrade.tar.gz", size=600 * 1024, age_days=40)
    _mk_export("new-preupgrade.tar.gz", size=600 * 1024, age_days=1)
    r = prune_exports(days=30)
    assert r["removed"] == ["old-preupgrade.tar.gz"]
    assert 0.5 < r["freed_mb"] < 0.7               # 释放量按真实字节算
    assert [x["name"] for x in list_exports()] == ["new-preupgrade.tar.gz"]


def test_prune_exports_rejects_bad_days(isolate):
    with pytest.raises(HTTPException) as e:
        prune_exports(days=0)
    assert e.value.status_code == 400


def test_scheduled_prune_does_not_touch_preupgrade(isolate):
    """定时备份的 keep 滚动只认 -sched- 标记：升级前快照同样含 `<id>-` 子串，
    裸前缀匹配会把它算进滚动并误删（统一目录时修正，此用例锁住口径）。"""
    for i in range(3):
        _mk_export(f"llbot-dead0032-sched-data-2026010{i}-000000.tar.gz")
    _mk_export("llbot-dead0032-preupgrade-20260101-000000.tar.gz")
    sch = Scheduler(Path(isolate) / "state" / "schedules.json", exports.exports_dir(),
                    ctx.registry, ctx.wizard)
    sch._prune("llbot-dead0032", "data", keep=1)
    names = [r["name"] for r in list_exports()]
    assert "llbot-dead0032-preupgrade-20260101-000000.tar.gz" in names   # 升级快照必须还在
    assert len([n for n in names if "-sched-" in n]) == 1                # 定时备份只留最新一份
