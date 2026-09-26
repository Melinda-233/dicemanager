"""2026-09-26 优化项回归：停止落状态 / 删除级联解除关联 / token 过期 / run_once 互斥。
ctx 模块级单例 + DM_STATE_DIR 指向临时目录（参考 smoke_local.py / test_link_webui.py）。"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

tmp = tempfile.mkdtemp(prefix="dm_optim_")
os.environ["DM_STATE_DIR"] = tmp
os.environ["DM_LOG_DIR"] = os.path.join(tmp, "logs")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest                                       # noqa: E402
from fastapi import HTTPException                   # noqa: E402

from api.context import ctx                         # noqa: E402
from api.rest import VALID_OPS, delete_instance, instance_op  # noqa: E402


def _mk(iid, dice):
    ctx.registry.create(iid, dice=dice, arch="standalone", dir_=f"/tmp/{iid}", port=3000,
                        allocated_ports={"webui": 3080, "ob11": 3001})


@pytest.fixture(autouse=True)
def _clean():
    yield
    for r in ctx.registry.all():
        ctx.registry.remove(r["id"])
    ctx.registry.purge_tombstones(days=0)


def _to_running(iid):
    from core.registry import State
    for st in (State.DEPLOYING, State.AWAIT_LOGIN, State.RUNNING):
        ctx.registry.transition(iid, st)


def test_stop_transitions_running_to_configured():
    """停止后 state 不再停在 RUNNING（与「进程已死」矛盾）。"""
    from core.registry import State
    _mk("sealdice-o1", "sealdice")
    _to_running("sealdice-o1")
    assert instance_op("sealdice-o1", "stop")["ok"] is True
    assert ctx.registry.get("sealdice-o1").state == State.CONFIGURED.value


def test_stop_keeps_await_login_state():
    """AWAIT_LOGIN 期间停止（登录进程被拉起后手动停）不动状态机，仍可继续登录流程。"""
    from core.registry import State
    _mk("napcat-o1", "napcat")
    ctx.registry.transition("napcat-o1", State.DEPLOYING)
    ctx.registry.transition("napcat-o1", State.AWAIT_LOGIN)
    instance_op("napcat-o1", "stop")
    assert ctx.registry.get("napcat-o1").state == State.AWAIT_LOGIN.value


def test_delete_cascades_unlink_and_reports():
    """删除登录端：引用它的骰子端 login_ref 自动清空，且响应里如实上报。"""
    _mk("llbot-o1", "llbot")
    _mk("sealdice-o2", "sealdice")
    _mk("sealdice-o3", "sealdice")
    from api.rest import LinkReq, link_login
    link_login("sealdice-o2", LinkReq(login_ref="llbot-o1"))
    link_login("sealdice-o3", LinkReq(login_ref="llbot-o1"))
    r = delete_instance("llbot-o1", confirm=True)
    assert sorted(r["unlinked"]) == ["sealdice-o2", "sealdice-o3"]
    assert ctx.registry.get("sealdice-o2").login_ref is None
    assert ctx.registry.get("sealdice-o3").login_ref is None
    with pytest.raises(KeyError):
        ctx.registry.get("llbot-o1")


def test_auth_token_expiry_and_ws_handshake():
    """token 超过 TTL → HTTP 401（文案「登录已过期」）/ WS 拒绝；子协议与查询参数两路鉴权。"""
    from api.auth import AUTH_TTL, Auth
    tf = Path(tmp, "auth_optim.json")
    tf.write_text(json.dumps({"password_hash": "x", "salt": "ab", "token": "tok-1",
                              "issued_at": time.time() - AUTH_TTL - 10}), encoding="utf-8")
    a = Auth(tf)
    cred = SimpleNamespace(credentials="tok-1")
    with pytest.raises(HTTPException) as e:
        a.verify_http(cred)
    assert e.value.status_code == 401 and "过期" in e.value.detail
    assert a.verify_ws(SimpleNamespace(query_params={"token": "tok-1"})) is False

    # 未过期：子协议头匹配 → (True, proto)；查询参数匹配 → (True, None)；都不匹配 → 拒绝
    tf2 = Path(tmp, "auth_optim2.json")
    tf2.write_text(json.dumps({"password_hash": "x", "salt": "ab", "token": "tok-2",
                               "issued_at": time.time()}), encoding="utf-8")
    b = Auth(tf2)
    ws_ok = SimpleNamespace(headers={"sec-websocket-protocol": "tok-2"}, query_params={})
    ok, sub = b.ws_handshake(ws_ok)
    assert (ok, sub) == (True, "tok-2")
    ws_legacy = SimpleNamespace(headers={}, query_params={"token": "tok-2"})
    ok2, sub2 = b.ws_handshake(ws_legacy)
    assert (ok2, sub2) == (True, None)
    ws_bad = SimpleNamespace(headers={"sec-websocket-protocol": "evil"}, query_params={})
    assert b.ws_handshake(ws_bad) == (False, None)


def test_run_once_and_start_mutual_exclusion():
    """run_once 执行期间 start() 必须被拒（此前只查常驻进程，存在竞态窗口）。"""
    from core.process import ManagedProcess
    log_dir = Path(tmp, "logs")
    log_dir.mkdir(parents=True, exist_ok=True)   # 直接构造 ManagedProcess 时不自动建目录
    mp = ManagedProcess("optim-once", log_dir)
    py = sys.executable
    t = threading.Thread(
        target=mp.run_once, args=([py, "-c", "import time; time.sleep(1.5)"], str(tmp)),
        kwargs={"label": "optim-test"}, daemon=True)
    t.start()
    time.sleep(0.4)                      # 确保 run_once 已置 busy 标志
    try:
        with pytest.raises(RuntimeError):
            mp.start([py, "-c", "import time; time.sleep(5)"], str(tmp))
        with pytest.raises(RuntimeError):
            mp.run_once([py, "-c", "print(1)"], str(tmp))
    finally:
        t.join(timeout=10)
    # 结束后互斥解除
    mp.start([py, "-c", "import time; time.sleep(5)"], str(tmp))
    try:
        assert mp.is_alive()
    finally:
        mp.stop()


def test_process_note_and_crash_flag_reset():
    """note() 进 ring 与日志；crash_looped 标记 start() 后自动解除。"""
    from core.process import ManagedProcess
    log_dir = Path(tmp, "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    mp = ManagedProcess("optim-note", log_dir)
    mp.note("测试事件")
    assert any("测试事件" in ln for _, ln in mp.ring)
    assert (Path(tmp, "logs", "optim-note.log")).exists()
    assert mp.crash_looped is False
    mp.crash_looped = True
    py = sys.executable
    mp.start([py, "-c", "import time; time.sleep(5)"], str(tmp))
    try:
        assert mp.crash_looped is False          # 人工重启脱离熔断态
        assert "mem_mb" in mp.probe()
    finally:
        mp.stop()


def test_valid_ops_guard():
    """op 白名单显式校验（assert 会被 -O 剥离的老问题）。"""
    with pytest.raises(HTTPException) as e:
        instance_op("ghost", "reboot")
    assert e.value.status_code == 404
    assert "restart" in VALID_OPS and "reboot" not in VALID_OPS
