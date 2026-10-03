"""账号管理与配额：归属隔离、配额判定、越权收口。

覆盖三条主线：
  1. core/quota.py 配额计数与判定（含 official 通道、-1 不限、多登录端累计）
  2. Instance.owner 归属与 registry.owned_by 的越权语义
  3. core/roles.py 角色派生口径与既有测试一致
"""
import pytest

from core import quota, roles
from core.registry import Registry
from core.roles import is_login_program

# 最小适配器桩：{name: (manifest, cls)}，与 ctx.adapters 同形
def _adapters():
    return {
        "sealdice": ({"name": "sealdice", "compatible_login": ["napcat", "lagrange"]}, object),
        "napcat":   ({"name": "napcat", "compatible_login": []}, object),
        "lagrange": ({"name": "lagrange", "compatible_login": []}, object),
    }

# ---------- 角色派生 ----------

def test_login_programs_derived_from_compatible_login():
    ads = _adapters()
    assert roles.login_programs(ads) == {"napcat", "lagrange"}
    assert is_login_program("napcat", ads) is True
    assert is_login_program("sealdice", ads) is False
    assert roles.app_programs(ads) == {"sealdice"}


def test_builtin_is_excluded_from_login_set():
    """builtin 表示自带登录，不是可关联的登录端程序，不该进集合。"""
    ads = {"solo": ({"compatible_login": ["builtin"]}, object)}
    assert roles.login_programs(ads) == set()


def test_role_split_matches_manifest_integrity_definition():
    """与 tests/test_manifest_integrity.py::test_role_split_is_consistent 同一口径。

    这里不重复断言具体程序名（那由manifest_integrity 负责），只保证两个函数
    用的是 compatible_login 且排除 builtin 这条规则。
    """
    ads = _adapters()
    derived = roles.login_programs(ads)
    manual = set()
    for manifest, _ in ads.values():
        for t in (manifest.get("compatible_login") or []):
            if t != "builtin":
                manual.add(t)
    assert derived == manual


# ---------- 配额 ----------

def _rec(iid, dice, owner="alice", accounts=None):
    return {"id": iid, "dice": dice, "owner": owner, "accounts": accounts or [],
            "links": [], "state": "RUNNING"}

def test_usage_counts_login_qq_across_all_login_instances():
    """配额计的是**登录端已登录 QQ 号总数**：一个登录端登 2 个 + 另一个登 1 个 = 3。"""
    recs = [_rec("nap-1", "napcat", accounts=[{"qq": "1"}, {"qq": "2"}]),
            _rec("nap-2", "lagrange", accounts=[{"qq": "3"}])]
    assert quota.usage("alice", recs, _adapters())["login_qq"] == 3


def test_usage_counts_app_instances():
    recs = [_rec("sea-1", "sealdice"), _rec("sea-2", "sealdice"), _rec("nap-1", "napcat")]
    assert quota.usage("alice", recs, _adapters())["app"] == 2


def test_usage_isolated_by_owner():
    recs = [_rec("a1", "sealdice", owner="alice"),
            _rec("b1", "sealdice", owner="bob"),
            _rec("b2", "sealdice", owner="bob")]
    assert quota.usage("alice", recs, _adapters())["app"] == 1
    assert quota.usage("bob", recs, _adapters())["app"] == 2


def test_legacy_records_without_owner_belong_to_admin():
    """迁移前的老实例没有 owner 字段，必须归 admin，否则升级后从所有人视图消失。"""
    recs = [{"id": "old", "dice": "sealdice", "accounts": []}]
    assert quota.owner_of(recs[0]) == "admin"
    assert quota.usage("admin", recs, _adapters())["app"] == 1


def test_duplicate_qq_across_login_instances_counted_once():
    """同一 QQ 号不应被重复计费（防御性：极端情况下会在两个登录端都有配置）。"""
    recs = [_rec("nap-1", "napcat", accounts=[{"qq": "1"}]),
            _rec("nap-2", "lagrange", accounts=[{"qq": "1"}])]
    assert quota.usage("alice", recs, _adapters())["login_qq"] == 1


def test_check_allows_within_limit_and_blocks_over():
    recs = [_rec("sea-1", "sealdice")]
    q = {"login_qq": 1, "app": 1}
    assert quota.check("alice", q, recs, _adapters(), add_app=0)["app"] == 1
    with pytest.raises(quota.QuotaExceeded) as e:
        quota.check("alice", q, recs, _adapters(), add_app=1)   # 再建 1 个就超
    assert e.value.kind == "app" and e.value.limit == 1 and e.value.used == 1


def test_check_blocks_login_qq_over_limit():
    recs = [_rec("nap-1", "napcat", accounts=[{"qq": "1"}, {"qq": "2"}])]
    with pytest.raises(quota.QuotaExceeded) as e:
        quota.check("alice", {"login_qq": 2, "app": -1}, recs, _adapters(),
                    add_login_qq=1)
    assert e.value.kind == "login_qq"


def test_negative_quota_means_unlimited():
    recs = [_rec("sea-1", "sealdice"), _rec("sea-2", "sealdice"),
            _rec("sea-3", "sealdice")]
    q = {"login_qq": -1, "app": -1}
    assert quota.check("alice", q, recs, _adapters(), add_app=99) is not None


def test_login_instance_creation_does_not_consume_app_quota():
    """登录端实例本身不占 app 名额（配额按 QQ 号计），但要先过角色判定。"""
    recs = []
    cur = quota.check("alice", {"login_qq": 1, "app": 0}, recs, _adapters(),
                      add_app=0)
    assert cur["app"] == 0


def test_quota_exceeded_carries_actionable_message():
    """报错文案要能让用户知道该找谁提额。"""
    recs = [_rec("nap-1", "napcat", accounts=[{"qq": str(i)} for i in range(3)])]
    with pytest.raises(quota.QuotaExceeded) as e:
        quota.check("alice", {"login_qq": 3, "app": -1}, recs, _adapters(),
                    add_login_qq=1)
    assert "3/3" in e.value.message and "上限" in e.value.message


# ---------- 归属 ----------

def test_owned_by_blocks_cross_user_access(tmp_path):
    reg = Registry(tmp_path / "inst.json")
    reg.create("i1", dice="sealdice", arch="standalone", dir_="/tmp/i1", port=0,
               owner="alice")
    assert reg.owned_by("i1", "alice")["id"] == "i1"
    with pytest.raises(KeyError):
        reg.owned_by("i1", "bob")             # 越权 → KeyError（rest 层转 404）
    assert reg.owned_by("i1", "bob", is_admin=True)["id"] == "i1"


def test_owned_by_unknown_instance_raises_same_error_as_cross_user(tmp_path):
    """越权与不存在必须同错误，否则可据此探测他人实例 ID。"""
    reg = Registry(tmp_path / "inst2.json")
    reg.create("i1", dice="sealdice", arch="standalone", dir_="/tmp/i1", port=0,
               owner="alice")
    with pytest.raises(KeyError) as e1:
        reg.owned_by("ghost", "alice")
    with pytest.raises(KeyError) as e2:
        reg.owned_by("i1", "bob")
    assert "ghost" in str(e1.value)
    assert "i1" in str(e2.value)          # 报错文案统一为「实例不存在: <id>」


def test_create_defaults_owner_to_admin(tmp_path):
    """未显式传 owner 时归 admin（存量与后台创建的实例不至于失联）。"""
    reg = Registry(tmp_path / "inst3.json")
    reg.create("i1", dice="sealdice", arch="standalone", dir_="/tmp/i1", port=0)
    assert reg.owned_by("i1", "admin")["id"] == "i1"


def test_removed_instance_not_visible_to_owner(tmp_path):
    reg = Registry(tmp_path / "inst4.json")
    reg.create("i1", dice="sealdice", arch="standalone", dir_="/tmp/i1", port=0,
               owner="alice")
    reg.remove("i1")
    with pytest.raises(KeyError):
        reg.owned_by("i1", "alice")