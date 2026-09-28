"""三条 WS 通道的最小集成测试（总览 / 日志 / 登录）。

这三条通道此前零自动化覆盖，而它们恰是历史故障高发区：
  - hook 写成单参数 cb(line)，与 process.on_line 的 cb(seq, line) 约定不符，
    TypeError 被 `except Exception: pass` 吞掉 → 实时推送整条静默失效；
  - 跨线程投递没走 loop.call_soon_threadsafe（asyncio.Queue 非线程安全）；
  - 登录通道在非 AWAIT_LOGIN 状态也会自动拉起进程，浏览器挂着登录页反复重连
    会建出残缺实例目录，后续部署直接判 conflict。
这些都只有真机连着看才观察得到，这里用 TestClient 把契约钉死。

鉴权走 Sec-WebSocket-Protocol（与生产一致），token 直接取模块级 Auth 单例。
"""
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="dm_ws_")
os.environ["DM_STATE_DIR"] = _tmp
os.environ["DM_LOG_DIR"] = str(Path(_tmp) / "logs")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest                                          # noqa: E402
from fastapi.testclient import TestClient              # noqa: E402

from api.app import app                                # noqa: E402
from api.auth import auth                              # noqa: E402
from api.context import ctx                            # noqa: E402
from core.registry import State                        # noqa: E402

TOKEN = auth._cred["token"]
HEADERS = {"sec-websocket-protocol": TOKEN}


class FakeAdapter:
    """替身适配器：避开真实程序的目录/端口副作用，只验证通道行为。

    extract_qrcode 认 "[qr]" 标记行，返回标记后的内容做去重键。
    """
    def __init__(self, manifest):
        self.m = manifest

    def build_start_cmd(self, instance):
        return ["echo", "fake"]

    def prepare_start(self, instance, runner=None):
        return False

    def expose_webui(self, instance):
        return None

    def health_check(self, instance, is_alive=False):
        return {"alive": is_alive, "conn": "none"}

    def extract_qrcode(self, line, instance):
        return line.split("[qr]", 1)[1].strip() if "[qr]" in line else None

    def extract_verify(self, line, instance):
        return None

    def extract_login_failed(self, line, instance):
        return None

    def get_actual_port(self, lines):
        return None

    def get_webui_token(self, lines):
        return None

    def detect_account(self, instance):
        return None

    def is_up(self, instance) -> bool:
        """替身：真探 ob11 端口，让「句柄失效但端口通」的 reparent 回归测试能跑通。"""
        import socket as _s
        port = (getattr(instance, "allocated_ports", {}) or {}).get("ob11")
        if not port:
            return False
        with _s.socket() as s:
            s.settimeout(1.0)
            return s.connect_ex(("127.0.0.1", int(port))) == 0


def _mk(iid, dice, state=State.CONFIGURED.value, **kw):
    """建实例 + 注入替身适配器；返回该实例的 ManagedProcess（ring 入口）。"""
    d = Path(_tmp) / iid
    d.mkdir(parents=True, exist_ok=True)
    ctx.registry.create(iid, dice=dice, arch="standalone", dir_=str(d), port=3000,
                        allocated_ports={"webui": 3080, "ob11": 3001})
    ctx.registry.update(iid, state=state, **kw)
    ctx._adapter_cache[dice] = FakeAdapter({"name": dice, "exe": "fake"})
    return ctx.pm.get(iid)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean():
    ctx._adapter_cache.clear()
    yield
    for r in ctx.registry.all():
        ctx.registry.remove(r["id"])
    ctx.registry.purge_tombstones(days=0)
    ctx._adapter_cache.clear()


def test_ws_overview_payload_carries_login_ref_and_qq(client):
    """总览节点必须带 login_ref / qq：连接管理面板靠这两项渲染（旧 payload 缺）。"""
    _mk("sealdice-w1", "sealdice", login_ref="napcat-w1")
    _mk("napcat-w1", "napcat", qq="10001")

    with client.websocket_connect("/ws/overview", headers=HEADERS) as ws:
        payload = ws.receive_json()["payload"]

    nodes = {n["id"]: n for n in payload["nodes"]}
    assert set(nodes) == {"sealdice-w1", "napcat-w1"}
    assert nodes["sealdice-w1"]["login_ref"] == "napcat-w1"
    assert nodes["napcat-w1"]["qq"] == "10001"
    assert any(e["src"] == "sealdice-w1" and e["dst"] == "napcat-w1"
               for e in payload["edges"])
    assert {"ratio", "total_mb", "used_mb", "alert"} <= set(payload["resmon"])


def test_ws_overview_shows_alive_when_handle_stale_but_port_up(client):
    """回归：llbot 等 launcher+worker 结构，面板句柄失效（launcher 重启 / 面板重启后
    worker reparent 到 init）但端口仍在监听时，总览必须把节点判为存活，而非凭空变灰/消失。"""
    import socket
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    proc = _mk("llbot-r1", "llbot", state=State.RUNNING.value,
               allocated_ports={"webui": 3080, "ob11": port})
    proc.is_alive = lambda: False          # 模拟句柄失效：launcher 已退出 / 被 reparent
    try:
        with client.websocket_connect("/ws/overview", headers=HEADERS) as ws:
            payload = ws.receive_json()["payload"]
        nodes = {n["id"]: n for n in payload["nodes"]}
        assert nodes["llbot-r1"]["process_alive"] is True
    finally:
        srv.close()


def test_ws_overview_rejects_bad_token(client):
    """WS 鉴权失败必须关 4401，不是「连上但没数据」。"""
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/overview",
                                      headers={"sec-websocket-protocol": "wrong"}):
            pass


def test_ws_overview_node_not_dropped_on_2tuple_backfill(client):
    """回归（2026-09-28）：busy 机器人每轮都产出新日志行，_backfill_from_logs 会把
    (seq,line) 二元组传给 get_actual_port。若 _consume_new_lines 错配成纯字符串，
    适配器里 `for _, line in lines` 解包抛 ValueError，整轮 except 跳过→节点从总览消失
    （llbot 等繁忙机器人几乎每轮都触发）。修复后 _consume_new_lines 必须返回二元组，
    节点照常出现、且 actual_port 能从日志回读。"""
    class BusyAdapter:
        """模拟 llbot 适配器：get_actual_port 按 (seq,line) 二元组契约解包。"""
        def __init__(self, manifest): self.m = manifest
        def build_start_cmd(self, instance): return ["echo", "fake"]
        def prepare_start(self, instance, runner=None): return False
        def expose_webui(self, instance): return None
        def health_check(self, instance, is_alive=False): return {"alive": is_alive, "conn": "none"}
        def extract_qrcode(self, line, instance): return None
        def extract_verify(self, line, instance): return None
        def extract_login_failed(self, line, instance): return None
        def get_actual_port(self, lines):
            import re as _re
            for _, line in reversed(list(lines)[-200:]):
                m = _re.search(r"ob11.*?端口[:：]?\s*(\d{4,5})", line)
                if m: return int(m.group(1))
            return None
        def get_webui_token(self, lines): return None
        def detect_account(self, instance): return None
        def is_up(self, instance): return False

    proc = _mk("llbot-busy", "llbot", state=State.RUNNING.value,
               allocated_ports={"webui": 3080, "ob11": 3001})
    ctx._adapter_cache["llbot"] = BusyAdapter({"name": "llbot", "exe": "fake"})
    proc.is_alive = lambda: True            # 句柄存活 → 触发 backfill（本应有新日志）
    proc.ring.append((next(proc._seq), "ob11 监听端口 3999"))   # 模拟每轮新行
    try:
        with client.websocket_connect("/ws/overview", headers=HEADERS) as ws:
            payload = ws.receive_json()["payload"]
        nodes = {n["id"]: n for n in payload["nodes"]}
        assert "llbot-busy" in nodes, "节点被异常丢弃（契约错配导致整轮跳过）"
        assert nodes["llbot-busy"]["process_alive"] is True
        # actual_port 应从日志回读为 3999（证明 backfill 真的跑了且没崩）
        assert ctx.registry.get("llbot-busy").actual_port == 3999
    finally:
        delattr(proc, "is_alive")           # 还原类方法，避免影响其他用例


def test_ws_logs_replays_history_once_with_increasing_seq(client):
    """历史回放：ring 里的行不多不少推一遍，seq 严格递增（去重窗口的基线）。"""
    proc = _mk("napcat-w2", "napcat")
    for i in range(3):
        proc.note(f"hello-{i}")

    with client.websocket_connect("/ws/logs/napcat-w2", headers=HEADERS) as ws:
        got = [ws.receive_json() for _ in range(3)]

    assert [m["type"] for m in got] == ["line"] * 3
    seqs = [m["seq"] for m in got]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    assert [m["text"].split("hello-")[1] for m in got] == ["0", "1", "2"]
    assert all(m["error"] is False for m in got)


def test_ws_logs_marks_error_lines(client):
    proc = _mk("napcat-w3", "napcat")
    proc.note("boom")
    proc.note("[ERROR] disk full")

    with client.websocket_connect("/ws/logs/napcat-w3", headers=HEADERS) as ws:
        got = [ws.receive_json() for _ in range(2)]

    assert got[0]["error"] is False
    assert got[1]["error"] is True


def test_ws_logs_uses_manifest_error_keywords(client):
    """清单声明的 error_keywords 必须生效。

    该字段此前写了却无人读取，程序特有的错误行（如 C# 的 Unhandled exception）
    会被通用正则漏标。
    """
    proc = _mk("sealdice-w7", "sealdice")
    ctx._adapter_cache["sealdice"] = FakeAdapter(
        {"name": "sealdice", "exe": "fake",
         "error_keywords": ["Unhandled exception"]})
    proc.note("Unhandled exception.  ")          # 通用关键字抓不到
    proc.note("all good")

    with client.websocket_connect("/ws/logs/sealdice-w7", headers=HEADERS) as ws:
        got = [ws.receive_json() for _ in range(2)]

    assert got[0]["error"] is True
    assert got[1]["error"] is False


def test_error_re_for_falls_back_when_instance_unknown():
    """实例不存在 / 程序已下架：退回通用关键字，不能把整条通道搞挂。"""
    from api.ws_logs import ERROR_RE, error_re_for
    assert error_re_for("no-such-instance") is ERROR_RE


def test_ws_login_replays_qrcode_with_dedup(client):
    """Lagrange 把二维码打成多行字符画：同一张码回放里只推一次，换码要能推新的。"""
    proc = _mk("napcat-w4", "napcat")
    proc.note("[qr]AAAA")
    proc.note("[qr]AAAA")            # 同码：应被去重
    proc.note("[qr]BBBB")            # 换码：应推送

    with client.websocket_connect("/ws/login/napcat-w4", headers=HEADERS) as ws:
        got = [ws.receive_json() for _ in range(2)]

    assert [m["type"] for m in got] == ["qrcode", "qrcode"]
    assert [m["payload"] for m in got] == ["AAAA", "BBBB"]


def test_ws_login_autolaunch_only_in_await_login(client, monkeypatch):
    """自动拉起仅限 AWAIT_LOGIN：否则浏览器挂登录页反复重连会建出残缺实例目录。"""
    calls = []
    monkeypatch.setattr(ctx.pm, "launch",
                        lambda iid, cmd, cwd, env=None: calls.append(iid))

    _mk("napcat-w5", "napcat", state=State.CONFIGURED.value)
    with client.websocket_connect("/ws/login/napcat-w5", headers=HEADERS) as ws:
        pass                                        # 无历史事件，直接关闭
    assert calls == []                              # CONFIGURED 不得拉起

    _mk("napcat-w6", "napcat", state=State.AWAIT_LOGIN.value)
    with client.websocket_connect("/ws/login/napcat-w6", headers=HEADERS) as ws:
        pass
    assert calls == ["napcat-w6"]                   # AWAIT_LOGIN + 进程未存活 → 拉起
