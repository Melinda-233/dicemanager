"""启动密码契约：首次启动必须给出可登录的明文，且明文不落盘。

历史回归：改成 PBKDF2 哈希存储时把明文打印也一起删了，新装用户再也拿不到密码
（deploy/install.sh 与 deploy/README.md 都让用户去日志里找 [auth] 行），只能删 auth.json 重置。
"""
import importlib
import json
import sys

import pytest
from fastapi import HTTPException


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
    assert first.login(pwd) == first._cred["token"]


def test_plain_password_never_persisted(auth_module, tmp_path):
    pwd = auth_module.auth.admin_password
    raw = (tmp_path / "auth.json").read_text("utf-8")
    assert pwd not in raw, "明文不得落盘"
    assert set(json.loads(raw)) == {"password_hash", "salt", "token"}


def test_restart_has_no_plain_but_still_accepts_password(auth_module, tmp_path):
    pwd = auth_module.auth.admin_password
    token = json.loads((tmp_path / "auth.json").read_text("utf-8"))["token"]
    second = auth_module.Auth(auth_module.AUTH_FILE)   # 模拟重启：文件已存在
    assert second.admin_password is None, "非首次启动不应再持有明文"
    assert second.login(pwd) == token


def test_wrong_password_rejected(auth_module):
    with pytest.raises(HTTPException) as e:
        auth_module.auth.login("definitely-wrong")
    assert e.value.status_code == 401


def test_change_password_rotates_token_and_invalidates_old(auth_module, tmp_path):
    from fastapi.security import HTTPAuthorizationCredentials
    a = auth_module.auth
    old_pwd = a.admin_password
    old_token = a.login(old_pwd)

    new_token = a.change_password(old_pwd, "brand-new-pw")

    # ① token 轮换：新 token ≠ 旧 token，且立即生效
    assert new_token != old_token
    assert a.login("brand-new-pw") == new_token
    # ② 旧密码不再能登录
    with pytest.raises(HTTPException) as e:
        a.login(old_pwd)
    assert e.value.status_code == 401
    # ③ 旧 token 已作废（verify_http 抛 401）
    with pytest.raises(HTTPException) as e:
        a.verify_http(HTTPAuthorizationCredentials(scheme="Bearer", credentials=old_token))
    assert e.value.status_code == 401
    assert a.verify_http(HTTPAuthorizationCredentials(scheme="Bearer", credentials=new_token)) is None
    # ④ 落盘内容同步更新：新 token + 明文仍不落盘
    d = json.loads((tmp_path / "auth.json").read_text("utf-8"))
    assert d["token"] == new_token
    assert "brand-new-pw" not in (tmp_path / "auth.json").read_text("utf-8")


def test_change_password_rejects_weak_or_wrong_old(auth_module, tmp_path):
    a = auth_module.auth
    old_pwd = a.admin_password
    token_before = a._cred["token"]
    # 旧密码错误 → 401，且凭据不变
    with pytest.raises(HTTPException) as e:
        a.change_password("definitely-wrong", "brand-new-pw")
    assert e.value.status_code == 401
    assert a._cred["token"] == token_before
    # 新密码过短 → 400，凭据不变
    with pytest.raises(HTTPException) as e:
        a.change_password(old_pwd, "12345")
    assert e.value.status_code == 400
    assert a._cred["token"] == token_before
    # 失败的改密尝试不应触发登录限速计数（_verify 只在旧密码错误时计数）
    a.change_password(old_pwd, "brand-new-pw")          # 正常改密不受影响
    assert a.login("brand-new-pw")


def test_changed_password_survives_restart(auth_module, tmp_path):
    a = auth_module.auth
    a.change_password(a.admin_password, "brand-new-pw")
    second = auth_module.Auth(auth_module.AUTH_FILE)    # 模拟重启：从盘上加载
    assert second.login("brand-new-pw")
    with pytest.raises(HTTPException):
        second.login(a.admin_password)
