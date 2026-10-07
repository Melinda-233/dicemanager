"""core/ghdl.py 的探测逻辑单测（网络全部 mock，只测决策）。"""

import pytest

from core import ghdl


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """每个用例都从干净状态开始（缓存 + 环境变量）。"""
    for k in ("DM_GITHUB_MIRROR", "DM_GITHUB_MIRRORS", "DM_GITHUB_MIRROR_PROBE"):
        monkeypatch.delenv(k, raising=False)
    ghdl.reset_cache()
    yield
    ghdl.reset_cache()


def test_official_reachable_uses_direct(monkeypatch):
    """官方通 → 直连，**不碰镜像**（国内也可能通，硬切镜像是浪费）。"""
    monkeypatch.setattr(ghdl, "_http_ok", lambda u, timeout=5.0: True)
    assert ghdl.pick_prefix("github.com") is None
    assert ghdl.url_for("https://github.com/a/b") == "https://github.com/a/b"


def test_official_down_falls_back_to_mirror(monkeypatch):
    """官方不通 → 选第一个可用镜像。"""
    def ok(url, timeout=5.0):
        return "ghfast.top" in url
    monkeypatch.setattr(ghdl, "_http_ok", ok)
    assert ghdl.pick_prefix("github.com") == "https://ghfast.top"
    assert ghdl.url_for("https://github.com/a/b") == \
        "https://ghfast.top/https://github.com/a/b"


def test_skips_dead_mirrors(monkeypatch):
    """镜像本身可能挂（实测 raw.gitmirror.com 连不上）→ 逐个跳过。"""
    def ok(url, timeout=5.0):
        return "gh-proxy.com" in url
    monkeypatch.setattr(ghdl, "_http_ok", ok)
    monkeypatch.setenv("DM_GITHUB_MIRRORS", "https://dead1,https://dead2,https://gh-proxy.com")
    assert ghdl.pick_prefix("github.com") == "https://gh-proxy.com"


def test_all_down_falls_back_to_direct(monkeypatch):
    """全挂 → 仍直连，让**真实请求**去报错（它的报错对用户更有用）。"""
    monkeypatch.setattr(ghdl, "_http_ok", lambda u, timeout=5.0: False)
    assert ghdl.pick_prefix("github.com") is None


def test_manual_mirror_skips_probe(monkeypatch):
    """手动指定 → 跳过探测（0 次网络调用），且优先级最高。"""
    calls = []

    def spy(url, timeout=5.0):
        calls.append(url)
        return True

    monkeypatch.setattr(ghdl, "_http_ok", spy)
    monkeypatch.setenv("DM_GITHUB_MIRROR", "https://my.proxy/")
    # 官方是"通"的，但手动指定仍应胜出
    assert ghdl.pick_prefix("github.com") == "https://my.proxy"
    assert calls == [], f"手动指定时不该探测，实际调了 {calls}"


def test_probe_can_be_disabled(monkeypatch):
    """DM_GITHUB_MIRROR_PROBE=0 → 强制直连，不做任何探测。"""
    calls = []
    monkeypatch.setattr(ghdl, "_http_ok",
                        lambda u, timeout=5.0: calls.append(u) or False)
    monkeypatch.setenv("DM_GITHUB_MIRROR_PROBE", "0")
    assert ghdl.pick_prefix("github.com") is None
    assert calls == []


def test_result_cached_per_host(monkeypatch):
    """探测结果按 host 缓存 —— 第二次不再发请求。"""
    calls = []
    monkeypatch.setattr(ghdl, "_http_ok",
                        lambda u, timeout=5.0: calls.append(u) or True)
    ghdl.pick_prefix("github.com")
    n = len(calls)
    ghdl.pick_prefix("github.com")
    assert len(calls) == n, "命中缓存时不该再探测"


def test_mirror_probe_shared_across_hosts(monkeypatch):
    """镜像探测结果**跨 host 共享**：代理整域放行/整域拦，不会逐 host 不同。

    这条是性能约定：3 个 host 只探 1 次镜像（实测省 3.3s）。
    """
    calls = []

    def ok(url, timeout=5.0):
        calls.append(url)
        return "ghfast.top" in url          # 官方不通、镜像通

    monkeypatch.setattr(ghdl, "_http_ok", ok)
    ghdl.pick_prefix("github.com")
    mirror_probes_1 = len([c for c in calls if "ghfast.top" in c])
    ghdl.pick_prefix("raw.githubusercontent.com")
    mirror_probes_2 = len([c for c in calls if "ghfast.top" in c])
    assert mirror_probes_2 == mirror_probes_1, \
        f"镜像被重复探测了 {mirror_probes_1}→{mirror_probes_2} 次，应共享结果"


def test_non_github_url_untouched(monkeypatch):
    """非 GitHub 的 url 原样返回（拼镜像前缀只会坏事）。"""
    monkeypatch.setattr(ghdl, "_http_ok", lambda u, timeout=5.0: False)
    monkeypatch.setenv("DM_GITHUB_MIRROR", "https://m")
    for u in ("https://example.com/x.zip", "http://127.0.0.1:8765/api"):
        assert ghdl.url_for(u) == u


def test_all_github_hosts_recognized(monkeypatch):
    """所有会用到的 GitHub host 都要能识别（漏一个 = 那个host 不走镜像）。"""
    monkeypatch.setattr(ghdl, "_http_ok", lambda u, timeout=5.0: False)
    monkeypatch.setenv("DM_GITHUB_MIRROR", "https://m")
    for host in ("github.com", "raw.githubusercontent.com",
                 "objects.githubusercontent.com", "codeload.github.com",
                 "api.github.com", "release-assets.githubusercontent.com"):
        got = ghdl.url_for(f"https://{host}/x")
        assert got.startswith("https://m/"), f"{host} 没走镜像：{got}"


def test_reset_cache_clears_mirror_knowledge(monkeypatch):
    """reset_cache 要连镜像的认知一起清（改候选列表后能重新选）。"""
    monkeypatch.setattr(ghdl, "_http_ok", lambda u, timeout=5.0: False)
    ghdl.pick_prefix("github.com")
    monkeypatch.setenv("DM_GITHUB_MIRRORS", "https://newmirror")
    ghdl.reset_cache()
    monkeypatch.setattr(ghdl, "_http_ok", lambda u, timeout=5.0: "newmirror" in u)
    assert ghdl.pick_prefix("github.com") == "https://newmirror"
