"""登录端账号「是否在线」判据的回归测试。

背景：accounts[].status 字段与前端渲染（拓扑图圆点 / 侧栏徽章）早就有了，
但六个登录端适配器的 list_accounts 全都硬编码 status="unknown" —— 字段存在却
永远是同一个值，等于没有。本文件钉住新加的判定口径：

  ① 适配器 account_status() 钩子优先（有更准证据的程序用它）
  ② 否则回落「进程存活 + 账号端口可达」
  ③ 进程都没跑 → 确定离线；拿不到端口 → 未知（**不**并进离线）

⚠️ 判据的语义边界必须钉死：端口可达 ≠ QQ 一定在线（掉线时端口常常还在监听），
所以「端口通 → running」只是可靠的下限，**反向**（端口不通 → 不在线）才是硬结论。
将来某程序核实了「登录成功/掉线」的日志锚点，应覆写 account_status 升级为精确
判定，而不是放宽这里的兜底口径。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from adapters.base import BaseAdapter  # noqa: E402
from adapters.yogurt import YogurtAdapter  # noqa: E402


def _inst():
    return SimpleNamespace(id="t1", dice="yogurt", dir="/tmp/none", qq=None)


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
        def account_status(self, instance, qq, port=None):
            return "stopped"              # 例如已从日志确认该号掉线

    out = Sharp({}).stamp_accounts(
        _inst(), [{"qq": "10001", "port": 3001}], alive=True)
    assert out[0]["status"] == "stopped"


def test_hook_exception_falls_back(probe):
    """钩子抛异常时回落到兜底：一个程序的判据写错，不该让账号从总览消失。"""
    probe.add(3001)

    class Boom(YogurtAdapter):
        def account_status(self, instance, qq, port=None):
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
# ⚠️ 本文件**不要 import api.* **：api.auth 在导入时就会创建模块级单例并立即
# 写 auth.json，而本文件按字母序排得很靠前——一旦在这里导入，后面所有依赖鉴权
# 状态的测试（test_webui_expose / test_yogurt / tests/win/*）会在 setup 阶段
# 集体失败（实测 282 个 ERROR，移走本文件即恢复 546 passed）。
# 判据的**适配层**部分在这里测；WS 推送那层由既有的 test_optim.py 覆盖。
