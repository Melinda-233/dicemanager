"""health_check 声明式端口机制的回归测试。

此前 8 个适配器各写一份近乎逐字相同的 health_check（只差「探哪个端口」），
现统一由 BaseAdapter.HEALTH_PORT_KEYS 系列类属性驱动。这里钉住四种口径，
防止后续有人加适配器时又把复制粘贴的覆写写回来、或改基类时悄悄改了语义。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from adapters.base import BaseAdapter  # noqa: E402
from adapters.dicenext import DiceNextAdapter  # noqa: E402
from adapters.lagrange_milky import LagrangeMilkyAdapter  # noqa: E402
from adapters.llbot import LLBotAdapter  # noqa: E402
from adapters.shiki import ShikiAdapter  # noqa: E402
from adapters.yogurt import YogurtAdapter  # noqa: E402


def _inst(ports=None, actual=None):
    from types import SimpleNamespace
    return SimpleNamespace(id="t1", allocated_ports=ports or {}, actual_port=actual)


@pytest.fixture
def probe(monkeypatch):
    """把 TCP 探测换成「端口在白名单里才通」，避免真起端口。"""
    open_ports: set = set()
    monkeypatch.setattr(BaseAdapter, "tcp_probe",
                        staticmethod(lambda host, port, timeout=2.0: int(port) in open_ports))
    return open_ports


def test_default_probes_ob11_then_actual_port(probe):
    """默认口径：先 ob11，没有则退回实例实际监听端口（正向 WS 监听型）。"""
    ad = YogurtAdapter({})                       # 任意子类都行，此处只看基类默认
    ad.HEALTH_PORT_KEYS = ["ob11"]               # 显式回到默认，避免受子类声明影响
    ad.HEALTH_USE_ACTUAL_PORT = True
    probe.add(3001)
    assert ad.health_check(_inst({"ob11": 3001}), True)["conn"] == "ok"
    assert ad.health_check(_inst({}, actual=3001), True)["conn"] == "ok"   # 退回 actual
    assert ad.health_check(_inst({"ob11": 3999}), True)["conn"] == "down"  # 端口不通


def test_milky_adapters_probe_milky_port_only(probe):
    """Milky 系探独立分配的 milky 端口，且**不**退回 actual_port。"""
    for ad in (LagrangeMilkyAdapter({}), YogurtAdapter({})):
        probe.add(3000)
        assert ad.health_check(_inst({"milky": 3000}), True)["conn"] == "ok"
        assert ad.health_check(_inst({"milky": 3000}, actual=9999), True)["conn"] == "ok"
        # 没有 milky 端口就是未配置，即便 actual_port 通也不算连上
        assert ad.health_check(_inst({}, actual=3000), True) == {"alive": True, "conn": "none"}


def test_dicenext_probes_webui_port(probe):
    """正向模式下自己不监听 ob11，探 WebUI 端口当存活信号。"""
    ad = DiceNextAdapter({})
    probe.add(18088)
    assert ad.health_check(_inst({"webui": 18088}), True)["conn"] == "ok"
    assert ad.health_check(_inst({}, actual=18088), True)["conn"] == "ok"  # 仍允许 actual 兜底


def test_llbot_missing_port_is_down_not_none(probe):
    """LLBot 的 ob11 端口在配置里固定存在，探测不到即断连，不是「未配置」。"""
    ad = LLBotAdapter({})
    probe.add(3001)
    assert ad.health_check(_inst({"ob11": 3001}), True)["conn"] == "ok"
    assert ad.health_check(_inst({}), True) == {"alive": True, "conn": "down"}
    assert ad.health_check(_inst({}, actual=3001), True)["conn"] == "down"


def test_alive_only_adapters_ignore_ports(probe):
    """整合包（内置客户端，不监听互联端口）：进程存活即已连接，端口一概不看。"""
    for ad in (ShikiAdapter({}), ):
        assert ad.HEALTH_PORT_KEYS == []          # 声明本身也是契约
        probe.add(3001)
        assert ad.health_check(_inst({"ob11": 3001}), True)["conn"] == "ok"
        # 进程死了，端口即便通着也必须报 down（不能凭端口误判存活）
        assert ad.health_check(_inst({"ob11": 3001}), False) == {"alive": False, "conn": "down"}


def test_read_json_tolerates_missing_and_broken(tmp_path):
    """read_json 统一了各适配器回读配置的异常口径：缺失/损坏/非对象一律 {}。"""
    assert BaseAdapter.read_json(tmp_path / "nope.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert BaseAdapter.read_json(broken) == {}
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2]", encoding="utf-8")
    assert BaseAdapter.read_json(arr) == {}
    ok = tmp_path / "ok.json"
    ok.write_text('{"a": 1}', encoding="utf-8")
    assert BaseAdapter.read_json(ok) == {"a": 1}
