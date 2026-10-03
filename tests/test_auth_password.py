"""启动密码契约：首次启动必须给出可登录的明文，且明文不落盘。

历史回归：改成 PBKDF2 哈希存储时把明文打印也一起删了，新装用户再也拿不到密码
（deploy/install.sh 与 deploy/README.md 都让用户去日志里找 [auth] 行），只能删 auth.json 重置。
"""
import importlib
import json
import sys

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials


@pytest.fixture
def auth_module(tmp_path, monkeypatch):
    """在临时 DM_STATE_DIR 下重新加载 api.auth（模块级常量在 import 时求值）。"""
    monkeypatch.setenv("DM_STATE_DIR", str(tmp_path))
    sys.modules.pop("api.auth", None)
    mod = importlib.import_module("api.auth")
    yield mod
    sys.modules.pop("api.auth", None)      # 不把「指向已删除临时目录」的模块留给后续用例


def test_first_boot_gives_loginable_password(auth_module, tmp_path):
    first = auth_module.auth                     # 首次启动：auth.json 不存在
    pwd = first.admin_password
    assert pwd, "首次启动必须给出可登录的明文密码（用户靠日志里的 [auth] 行登录）"
    # v2：登录签发新会话 token，身份为 admin
    me = first.current(first.login(pwd))
    assert me.username == "admin" and me.is_admin


def test_plain_password_never_persisted(auth_module, tmp_path):
    pwd = auth_module.auth.admin_password
    raw = (tmp_path / "auth.json").read_text("utf-8")
    assert pwd not in raw, "明文不得落盘"
    # v2 模型：users + sessions 两张表。字段集合变更须同步本断言，否则数据模型改了也测不出来
    d = json.loads(raw)
    assert set(d) == {"version", "users", "sessions"}
    # 用户条目不得含任何明文密码字段（只有 hash/salt）
    assert set(d["users"]["admin"]) >= {"role", "password_hash", "salt"}
    assert "password" not in d["users"]["admin"]


def test_restart_has_no_plain_but_still_accepts_password(auth_module, tmp_path):
    pwd = auth_module.auth.admin_password
    sessions = json.loads((tmp_path / "auth.json").read_text("utf-8"))["sessions"]
    token = next(iter(sessions))
    second = auth_module.Auth(auth_module.AUTH_FILE)   # 模拟重启：文件已存在
    assert second.admin_password is None, "非首次启动不应再持有明文"
    # v2 每次登录签发**新会话**（v1 返回固定 token），所以断言「重启后仍可登录」
    # 应落在 current() 能解析出admin 身份上，而不是 token 相等
    assert second.login(pwd) is not None
    assert second.current(token).username == "admin"


def test_wrong_password_rejected(auth_module):
    with pytest.raises(HTTPException) as e:
        auth_module.auth.login("definitely-wrong")
    assert e.value.status_code == 401


def test_change_password_rotates_token_and_invalidates_old(auth_module, tmp_path):
    a = auth_module.auth
    old_pwd = a.admin_password
    old_token = a.login(old_pwd)

    new_token = a.change_password(old_pwd, "brand-new-pw")

    # ① token 轮换：新 token ≠ 旧 token，且立即生效
    assert new_token != old_token
    assert a.login("brand-new-pw", "admin")            # 新密码可登录
    # ② 旧密码不再能登录
    with pytest.raises(HTTPException) as e:
        a.login(old_pwd)
    assert e.value.status_code == 401
    # ③ 旧 token 已作废（verify_http 抛 401）；新 token 可用并解析出 admin 身份
    with pytest.raises(HTTPException) as e:
        a.verify_http(HTTPAuthorizationCredentials(scheme="Bearer", credentials=old_token))
    assert e.value.status_code == 401
    me = a.verify_http(HTTPAuthorizationCredentials(scheme="Bearer", credentials=new_token))
    assert me.username == "admin" and me.role == "admin" and me.is_admin
    # ④ 落盘内容同步更新：新 token + 明文仍不落盘
    assert new_token in json.loads((tmp_path / "auth.json").read_text("utf-8"))["sessions"]
    assert "brand-new-pw" not in (tmp_path / "auth.json").read_text("utf-8")


def test_change_password_rejects_weak_or_wrong_old(auth_module, tmp_path):
    a = auth_module.auth
    old_pwd = a.admin_password
    sessions_before = set(json.loads((tmp_path / "auth.json").read_text("utf-8"))["sessions"])
    # 旧密码错误 → 401，且凭据不变
    with pytest.raises(HTTPException) as e:
        a.change_password("definitely-wrong", "brand-new-pw")
    assert e.value.status_code == 401
    assert set(json.loads((tmp_path / "auth.json").read_text("utf-8"))["sessions"]) == sessions_before
    # 新密码过短 → 400，凭据不变
    with pytest.raises(HTTPException) as e:
        a.change_password(old_pwd, "12345")
    assert e.value.status_code == 400
    assert set(json.loads((tmp_path / "auth.json").read_text("utf-8"))["sessions"]) == sessions_before
    # 失败的改密尝试不应触发登录限速计数（_verify 只在旧密码错误时计数）
    a.change_password(old_pwd, "brand-new-pw")          # 正常改密不受影响
    assert a.login("brand-new-pw")


def test_changed_password_survives_restart(auth_module, tmp_path):
    a = auth_module.auth
    a.change_password(a.admin_password, "brand-new-pw")
    second = auth_module.Auth(auth_module.AUTH_FILE)    # 模拟重启：从盘上加载
    assert second.login("brand-new-pw", "admin")
    with pytest.raises(HTTPException):
        second.login(a.admin_password, "admin")


# ---------- 多用户（账号管理） ----------

def test_v1_credential_migrates_to_admin_user_keeping_token(auth_module, tmp_path):
    """旧版单条凭据升级为 v2：旧密码与旧 token 都必须继续可用（升级不该全员掉线）。"""
    salt = "0123456789abcdef0123456789abcdef"
    pwd = "legacy-pw-123"
    old_token = "legacy-token-value"
    (tmp_path / "auth.json").write_text(json.dumps({
        "password_hash": auth_module._hash_password(pwd, bytes.fromhex(salt)),
        "salt": salt, "token": old_token, "issued_at": 1700000000.0}), encoding="utf-8")

    mod = importlib.import_module("api.auth")
    a = mod.Auth(mod.AUTH_FILE)

    assert a.admin_password is None, "旧文件加载不该再生成明文"
    assert a.current(old_token) is not None, "旧 token 迁移后必须仍有效"
    assert a.login(pwd, "admin")                            # 旧密码应仍可登录 admin
    assert a._data["users"]["admin"]["role"] == "admin"


def test_create_user_and_login_isolated_from_admin(auth_module):
    a = auth_module.auth
    a.create_user("alice", "alice-pw", "user", "Alice",
                  {"login_qq": 2, "app": 1})

    me = a.current(a.login("alice-pw", "alice"))
    assert me.username == "alice" and me.role == "user" and not me.is_admin
    assert me.display_name == "Alice"
    assert me.quota == {"login_qq": 2, "app": 1}

    # 密码互不影响：alice 的密码登不进 admin 账号
    with pytest.raises(HTTPException) as e:
        a.login("alice-pw", "admin")
    assert e.value.status_code == 401


def test_login_rejects_wrong_password_without_leaking_user_existence(auth_module):
    """不存在的用户名与错误密码必须同为 401，否则可枚举账号。"""
    a = auth_module.auth
    a.create_user("bob", "bob-pw-123")
    for username in ("bob", "ghost"):
        with pytest.raises(HTTPException) as e:
            a.login("totally-wrong", username)
        assert e.value.status_code == 401


def test_rate_limit_bucketed_per_user_not_global(auth_module):
    """限速按 (ip|用户名) 分桶：一个账号的失败不得锁死其他账号。"""
    a = auth_module.auth
    a.create_user("carol", "carol-pw-123")
    for _ in range(5):                                  # 磨光 alice 的配额
        with pytest.raises(HTTPException) as e:
            a.login("nope", "admin")
        assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        a.login("nope", "admin")
    assert e.value.status_code == 429
    assert a.login("carol-pw-123", "carol")              # carol 不受影响


def test_create_user_validates_input(auth_module):
    a = auth_module.auth
    for name in ("a", "bad name", "x" * 40, "admin2@x"):
        with pytest.raises(HTTPException) as e:
            a.create_user(name, "pw-123456")
        assert e.value.status_code == 400, name
    a.create_user("dup", "pw-123456")                    # 首次创建成功
    with pytest.raises(HTTPException) as e:               # 重名 → 409
        a.create_user("dup", "pw-123456")
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:
        a.create_user("weakpw", "123")                   # 密码过短
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        a.create_user("root2", "pw-123456", role="superuser")   # 未知角色
    assert e.value.status_code == 400


def test_delete_user_revokes_sessions_and_protects_admin(auth_module):
    a = auth_module.auth
    a.create_user("dave", "dave-pw-123")
    tok = a.login("dave-pw-123", "dave")
    assert a.current(tok) is not None

    a.delete_user("dave", by="admin")
    assert a.current(tok) is None, "删账号必须吊销其会话"
    assert "dave" not in [u["username"] for u in a.list_users()]
    # admin 自身受保护
    with pytest.raises(HTTPException) as e:
        a.delete_user("admin")
    assert e.value.status_code == 400


def test_delete_keeps_at_least_one_admin(auth_module):
    a = auth_module.auth
    a.create_user("root2", "root2-pw-1", "admin")
    # admin + root2 两个管理员：删掉 root2 是允许的
    a.delete_user("root2")
    assert [u["username"] for u in a.list_users()] == ["admin"]
    # 仅剩一个管理员时，任何删除请求都必须被挡住
    with pytest.raises(HTTPException) as e:
        a.delete_user("admin")                          # admin 是保留名
    assert e.value.status_code == 400
    # 不能删自己（即便它是唯一管理员）
    a.create_user("root3", "root3-pw-1", "admin")
    a.create_user("admin2", "admin2-pw1")
    with pytest.raises(HTTPException) as e:
        a.delete_user("admin", by="admin")
    assert e.value.status_code == 400


def test_set_quota_normalizes_and_set_password_revokes(auth_module):
    a = auth_module.auth
    a.create_user("erin", "erin-pw-123")
    # 负数 = 不限；非法值回落默认
    assert a.set_quota("erin", {"login_qq": -1, "app": 9})["quota"] == {"login_qq": -1, "app": 9}
    assert a.set_quota("erin", {"login_qq": "abc"})["quota"]["login_qq"] == \
        auth_module.DEFAULT_QUOTA["login_qq"]
    with pytest.raises(HTTPException) as e:
        a.set_quota("ghost", {})
    assert e.value.status_code == 404

    tok = a.login("erin-pw-123", "erin")
    a.set_password("erin", "erin-new-pw")
    assert a.current(tok) is None, "管理员改密后旧 token 必须失效"
    assert a.login("erin-new-pw", "erin")


def test_revoke_sessions_keeps_account(auth_module):
    a = auth_module.auth
    a.create_user("frank", "frank-pw-123")
    t1 = a.login("frank-pw-123", "frank")
    t2 = a.login("frank-pw-123", "frank")
    a.revoke_sessions("frank")
    assert a.current(t1) is None and a.current(t2) is None
    assert "frank" in [u["username"] for u in a.list_users()], "踢下线不应删账号"


def test_logout_only_revokes_own_session(auth_module):
    a = auth_module.auth
    pwd = a.admin_password
    t1 = a.login(pwd)
    t2 = a.login(pwd)
    a.logout(t1)
    assert a.current(t1) is None and a.current(t2) is not None


def test_list_users_never_exposes_password_material(auth_module):
    a = auth_module.auth
    a.create_user("gina", "gina-pw-123")
    blob = json.dumps(a.list_users())
    assert "gina-pw-123" not in blob
    assert "password_hash" not in blob and "salt" not in blob
