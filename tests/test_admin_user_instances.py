# -*- coding: utf-8 -*-
"""管理员「按用户名列实例」端点。

为什么需要它：账号列表只给「用了几号几端」的数字，看不出**具体是哪些程序、
跑没跑、占哪个端口**。实例出问题时管理员第一个要看的就是这份清单。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DM_STATE_DIR", tempfile.mkdtemp())

from api.rest import admin_user_instances  # noqa: E402


def _rec(iid: str, dice: str, owner: str, **kw) -> dict:
    base = {"id": iid, "dice": dice, "owner": owner, "arch": "standalone",
            "state": "RUNNING", "dir": f"/opt/dm/package/{iid}",
            "actual_port": 3080, "qq": "10001", "allocated_ports": {},
            "links": [], "accounts": []}
    base.update(kw)
    return base


class _FakePm:
    """按 id 返回探针；未登记的 id 抛异常（模拟进程句柄不可用）。"""

    def __init__(self, alive_ids=()):
        self._alive = set(alive_ids)
        self.gets = []

    def get(self, iid):
        self.gets.append(iid)
        if iid not in self._alive:
            raise KeyError(iid)
        return type("P", (), {"probe": lambda self=None: {"alive": True, "mem_mb": 42}})()


def _patch(monkeypatch, recs, pm):
    import api.context as c
    monkeypatch.setattr(c.ctx.registry, "all", lambda: recs)
    monkeypatch.setattr(c.ctx.pm, "get", pm.get)


def test_lists_only_that_users_instances(monkeypatch):
    recs = [_rec("llbot-a", "llbot", "alice"), _rec("dice-b", "sealdice", "bob"),
            _rec("llbot-c", "llbot", "alice")]
    _patch(monkeypatch, recs, _FakePm({"llbot-a", "llbot-c"}))
    r = admin_user_instances("alice", None)
    ids = sorted(i["id"] for i in r["instances"])
    assert ids == ["llbot-a", "llbot-c"], "只能看到该用户名下的实例"
    assert r["username"] == "alice"


def test_legacy_records_without_owner_belong_to_admin(monkeypatch):
    """老记录没有 owner 字段 → 归属 admin（与 registry.owned_by 同口径）。

    若这里按「无 owner = 无归属」过滤，admin 会看不到自己早期建的实例。
    """
    recs = [_rec("old-1", "sealdice", "admin"), _rec("alice-1", "llbot", "alice")]
    old = dict(recs[0]); old.pop("owner")
    recs[0] = old
    _patch(monkeypatch, recs, _FakePm())
    r = admin_user_instances("admin", None)
    assert [i["id"] for i in r["instances"]] == ["old-1"]


def test_reports_fields_the_admin_needs(monkeypatch):
    recs = [_rec("llbot-a", "llbot", "alice", arch="allinone", qq="975809162",
                 actual_port=None, allocated_ports={"webui": 3085})]
    _patch(monkeypatch, recs, _FakePm({"llbot-a"}))
    i = admin_user_instances("alice", None)["instances"][0]
    assert i["dice"] == "llbot" and i["state"] == "RUNNING"
    assert i["qq"] == "975809162"
    assert i["live"] is True and i["mem_mb"] == 42
    assert i["port"] == 3085, "actual_port 为空时回退 allocated_ports.webui"
    assert i["dir"], "部署目录要返回（前端默认折叠展示）"
    assert i["arch"] == "allinone"


def test_one_broken_instance_does_not_break_whole_list(monkeypatch):
    """某个实例的状态读不出来时，列表页不该整体失败 —— 仍要返回其余实例。"""
    recs = [_rec("bad-1", "llbot", "alice"), _rec("ok-1", "sealdice", "alice")]
    _patch(monkeypatch, recs, _FakePm({"ok-1"}))     # bad-1 抛 KeyError
    r = admin_user_instances("alice", None)
    got = {i["id"]: i["live"] for i in r["instances"]}
    assert got == {"bad-1": False, "ok-1": True}, "坏的标为未存活，好的照常返回"


def test_empty_user_returns_empty_list(monkeypatch):
    _patch(monkeypatch, [_rec("bob-1", "llbot", "bob")], _FakePm())
    r = admin_user_instances("nobody", None)
    assert r["instances"] == []
