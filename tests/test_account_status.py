"""登录端账号「是否在线」判据的回归测试。

背景：accounts[].status 字段与前端渲染（拓扑图圆点 / 侧栏徽章）早就有了，
但六个登录端适配器的 list_accounts 全都硬编码 status="unknown" —— 字段存在却
永远是同一个值，等于没有。本文件钉住判定口径：

  ① **日志锚点优先**（用户 2026-10-13 要求「端口可达≠在线，改读日志」）：
     各登录端把核实过的「上线/掉线」锚点填进 LOGIN_ANCHOR_ONLINE /
     LOGIN_ANCHOR_OFFLINE，命中即给出明确结论，并**覆盖**端口探测——
     这正修掉「端口还通却已掉线」的误报。
  ② 日志无锚点（没核实过该程序格式 / 实例刚启动还没打印 / 日志已轮转）时，
     回落「进程存活 + 账号端口可达」这一可靠弱信号。
  ③ 进程都没跑 → 确定离线；拿不到端口 → 未知（**不**并进离线）。

⚠️ 语义边界：端口可达 ≠ QQ 在线（掉线时 OneBot 端口常在监听），故「端口通」只是
下限；日志里的掉线锚点才是能精确判定离线的硬证据。未核实过日志格式的程序，绝不
照抄别家锚点，宁可回落端口兜底，不瞎判。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from adapters.base import BaseAdapter  # noqa: E402
from adapters.llbot import LLBotAdapter  # noqa: E402
from adapters.napcat import NapCatAdapter  # noqa: E402
from adapters.yogurt import YogurtAdapter  # noqa: E402


def _inst():
    # id=None：避免 _instance_log_path 在 log_path 缺省时惰性 import api
    # （触发 auth.json 副作用，污染后续鉴权测试）。本文件统一显式传 log_path。
    return SimpleNamespace(id=None, dice="yogurt", dir="/tmp/none", qq=None)


@pytest.fixture
def probe(monkeypatch):
    """把 TCP 探测换成「端口在白名单里才通」，避免真起端口。"""
    open_ports: set = set()
    monkeypatch.setattr(
        BaseAdapter, "tcp_probe",
        staticmethod(lambda host, port, timeout=2.0: int(port) in open_ports))
    return open_ports


def test_stopped_when_process_dead(probe):
    """进程都没跑 → 确定离线；此时端口探得通也不能改成在线。"""
    probe.add(3001)
    out = YogurtAdapter({}).stamp_accounts(
        _inst(), [{"qq": "10001", "port": 3001}], alive=False)
    assert out[0]["status"] == "stopped"


def test_running_when_alive_and_port_open(probe):
    probe.add(3001)
    out = YogurtAdapter({}).stamp_accounts(
        _inst(), [{"qq": "10001", "port": 3001}], alive=True)
    assert out[0]["status"] == "running"


def test_stopped_when_port_closed(probe):
    """进程在跑但该账号端口不通 → 这个号确实不在服务，标离线而非未知。"""
    out = YogurtAdapter({}).stamp_accounts(
        _inst(), [{"qq": "10001", "port": 3001}], alive=True)
    assert out[0]["status"] == "stopped"


def test_unknown_without_port(probe):
    """拿不到端口 → 未知。宁可灰着，也不把「没判据」说成离线。"""
    out = YogurtAdapter({}).stamp_accounts(_inst(), [{"qq": "10001"}], alive=True)
    assert out[0]["status"] == "unknown"


def test_default_hook_returns_none(probe):
    """基类钩子默认 None：没核实过日志锚点的程序一律走兜底，不许猜。"""
    probe.add(3001)
    assert YogurtAdapter({}).account_status(_inst(), "10001", 3001) is None


def test_hook_overrides_fallback(probe):
    """适配器有更准证据时钩子优先 —— 兜底不得把它盖回去。"""
    probe.add(3001)

    class Sharp(YogurtAdapter):
        def account_status(self, instance, qq, port=None, log_path=None):
            return "stopped"              # 例如已从日志确认该号掉线

    out = Sharp({}).stamp_accounts(
        _inst(), [{"qq": "10001", "port": 3001}], alive=True)
    assert out[0]["status"] == "stopped"


def test_hook_exception_falls_back(probe):
    """钩子抛异常时回落到兜底：一个程序的判据写错，不该让账号从总览消失。"""
    probe.add(3001)

    class Boom(YogurtAdapter):
        def account_status(self, instance, qq, port=None, log_path=None):
            raise RuntimeError("日志解析炸了")

    out = Boom({}).stamp_accounts(
        _inst(), [{"qq": "10001", "port": 3001}], alive=True)
    assert out[0]["status"] == "running"


def test_non_dict_entries_skipped(probe):
    """历史数据里 accounts 可能混着裸 QQ 号字符串：跳过而不是崩。"""
    out = YogurtAdapter({}).stamp_accounts(
        _inst(), ["10001", {"qq": "10002", "port": 3001}], alive=False)
    assert out == [{"qq": "10002", "port": 3001, "status": "stopped"}]


def test_source_not_mutated(probe):
    """只改输出副本：盘上的 accounts 必须原样不动（status 恒 unknown）。

    状态每 2s 重算，若写回 registry 会与「变化才写盘」的签名比对打架，
    造成无谓的写放大。
    """
    probe.add(3001)
    src = [{"qq": "10001", "port": 3001, "status": "unknown"}]
    YogurtAdapter({}).stamp_accounts(_inst(), src, alive=True)
    assert src[0]["status"] == "unknown"


def test_multi_account_stamped_independently(probe):
    """多账号逐个判定：同一个登录端里可以在线与离线并存。"""
    probe.add(3001)                       # 只放行 3001，3002 不通
    out = YogurtAdapter({}).stamp_accounts(
        _inst(), [{"qq": "10001", "port": 3001}, {"qq": "10002", "port": 3002}],
        alive=True)
    assert [a["status"] for a in out] == ["running", "stopped"]


# ---- 日志锚点判定（用户 2026-10-13：端口可达≠在线，改读日志）----

def test_llbot_kick_anchor_overrides_port(probe, tmp_path):
    """llbot 用户真机验证的掉线日志：进程在跑、端口通，也必须判离线。"""
    probe.add(3001)
    log = tmp_path / "llbot.log"
    log.write_text(
        '[W] core [Kick] code=1001 title="下线通知" '
        'desc="你的账号当前登录已失效，请重新登录。"\n', encoding="utf-8")
    out = LLBotAdapter({}).stamp_accounts(
        _inst(), [{"qq": "10001", "port": 3001}], alive=True, log_path=log)
    assert out[0]["status"] == "stopped"


def test_account_status_hook_reads_log(tmp_path):
    """account_status 钩子本身从日志锚点返回结论，不依赖端口。"""
    log = tmp_path / "llbot.log"
    log.write_text('[W] core [Kick] code=1001 title="下线通知"\n', encoding="utf-8")
    assert LLBotAdapter({}).account_status(
        _inst(), "10001", 3001, log_path=log) == "stopped"


def test_napcat_online_anchor_running(probe, tmp_path):
    """napcat 上线锚点（登录成功）且未命中掉线 → running。"""
    probe.add(3001)
    log = tmp_path / "napcat.log"
    log.write_text("[INFO] 登录成功 (qq=10001)\n", encoding="utf-8")
    out = NapCatAdapter({}).stamp_accounts(
        _inst(), [{"qq": "10001", "port": 3001}], alive=True, log_path=log)
    assert out[0]["status"] == "running"


def test_napcat_offline_later_than_online(probe, tmp_path):
    """同一日志先上线后掉线 → 取更晚者，判离线（覆盖端口）。"""
    probe.add(3001)
    log = tmp_path / "napcat.log"
    log.write_text(
        "[INFO] 登录成功 (qq=10001)\n"
        "[WARN] KickedOffline qq=10001\n", encoding="utf-8")
    out = NapCatAdapter({}).stamp_accounts(
        _inst(), [{"qq": "10001", "port": 3001}], alive=True, log_path=log)
    assert out[0]["status"] == "stopped"


def test_napcat_online_later_than_offline(probe, tmp_path):
    """先掉线后重新上线 → 取更晚者，判在线。"""
    log = tmp_path / "napcat.log"
    log.write_text(
        "[WARN] KickedOffline qq=10001\n"
        "[INFO] 登录成功 (qq=10001)\n", encoding="utf-8")
    out = NapCatAdapter({}).stamp_accounts(
        _inst(), [{"qq": "10001", "port": 3001}], alive=True, log_path=log)
    assert out[0]["status"] == "running"


def test_log_without_anchor_falls_back_to_port(probe, tmp_path):
    """日志无任一侧锚点 → 不瞎判，回落端口兜底（这里 3001 不通 → stopped）。"""
    log = tmp_path / "llbot.log"
    log.write_text("some unrelated info line\n", encoding="utf-8")
    out = LLBotAdapter({}).stamp_accounts(
        _inst(), [{"qq": "10001", "port": 3001}], alive=True, log_path=log)
    assert out[0]["status"] == "stopped"


# ⚠️ 本文件**不要 import api.* **：api.auth 在导入时就会创建模块级单例并立即
# 写 auth.json，而本文件按字母序排得很靠前——一旦在这里导入，后面所有依赖鉴权
# 状态的测试（test_webui_expose / test_yogurt / tests/win/*）会在 setup 阶段
# 集体失败（实测 282 个 ERROR，移走本文件即恢复 546 passed）。
# 判据的**适配层**部分在这里测；WS 推送那层由既有的 test_optim.py 覆盖。
