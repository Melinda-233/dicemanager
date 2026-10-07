"""首次启动 / 密码重置流程回归：Auth 的「是否已初始化」必须以磁盘 auth.json 为准。

背景（2026-09-29 报障）：「密码文件不存在，但访问 WebUI 未显示设置密码」。
根因：Auth 是 import 时构建的模块级单例，早期实现只在 __init__ 读一次磁盘，
此后 _initialized / _cred 常驻内存。于是运行中删除 auth.json（忘记密码时的
官方重置办法）后：
  - GET /api/needs-setup 仍返回 needs_setup=False → 前端停在普通登录页，
    永远走不到「设置管理密码」界面；
  - POST /api/login 返回 401「密码错误」而不是 428「首次启动，请先设置管理密码」；
  - POST /api/setup 还会返 409，必须重启进程才能恢复。
现由 Auth._refresh() 在每个鉴权入口与磁盘核对，上述场景无需重启即生效。
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.auth import Auth  # noqa: E402


def test_missing_file_is_uninitialized(tmp_path):
    """auth.json 不存在 = 首次启动，必须报「需设置密码」。"""
    assert Auth(tmp_path / "auth.json").is_initialized is False


def test_delete_file_resets_to_uninitialized_without_restart(tmp_path):
    """设置过密码后删除 auth.json（重置口令）→ 立刻回到未初始化态。"""
    f = tmp_path / "auth.json"
    a = Auth(f)
    a.setup_password("abcdef")
    assert a.is_initialized is True

    f.unlink()
    assert a.is_initialized is False          # 前端据此切到「设置管理密码」
    with pytest.raises(HTTPException) as ei:
        a.login("abcdef")
    assert ei.value.status_code == 428        # 428 引导去设置，而不是 401「密码错误」


def test_setup_allowed_again_after_delete(tmp_path):
    """删除后重新设置密码不能再返 409（内存里还留着旧态是旧实现的坑）。"""
    f = tmp_path / "auth.json"
    a = Auth(f)
    a.setup_password("abcdef")
    f.unlink()
    token = a.setup_password("newpass1")
    assert token and a.is_initialized is True
    # auth.json 落的是 v2 结构（users + sessions），token 是 sessions 的键。
    # 断言**语义**（该 token 确实是 admin 的有效会话）而不是 v1 的顶层 token
    # 字段 —— 那个字段早已不在落盘形态里，照抄旧形状等于把测试钉在废弃格式上。
    saved = json.loads(f.read_text("utf-8"))
    assert saved["sessions"][token]["username"] == "admin"
    # 新密码能登录（登录会新签发会话，与 setup 返回的不是同一个 token）
    assert a.login("newpass1")


def test_file_appearing_later_is_picked_up(tmp_path):
    """进程启动时无文件、之后由外部写入 → 本进程无需重启即可识别。"""
    f = tmp_path / "auth.json"
    a = Auth(f)
    assert a.is_initialized is False
    Auth(f).setup_password("abcdef")          # 模拟另一进程完成首次设置
    assert a.is_initialized is True
    assert a.login("abcdef")


def test_setup_rejects_short_password(tmp_path):
    with pytest.raises(HTTPException) as ei:
        Auth(tmp_path / "auth.json").setup_password("123")
    assert ei.value.status_code == 400


def test_corrupt_file_is_tolerated(tmp_path):
    """损坏文件按未初始化处理：_refresh 在每请求路径上，绝不能抛异常。"""
    f = tmp_path / "auth.json"
    f.write_text("{ not json", "utf-8")
    a = Auth(f)
    assert a.is_initialized is False
    assert a.is_initialized is False           # 重复调用同样稳定，不抛
