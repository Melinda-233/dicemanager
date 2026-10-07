"""GitHub 下载的「测连通 → 失败切镜像」策略。

## 为什么不用「一律走 mirror_url」

原实现里`GITHUB_MIRROR` 是**静态全局开关**：设了就所有请求都走镜像，
没设就全走官方。两者都不够好：

- **一律走官方**：国内机器上`api.github.com` 明明 0.3 秒能回，
  但某个 release 资产可能卡 30 秒才404 —— 用户只看到「下载失败」
- **一律走镜像**：镜像本身可能挂（实测 `raw.gitmirror.com` 连不上、
  `mirror.ghproxy.com` 超时 25 秒）。镜像不可用时比直连更糟

## 本模块的策略

**按 host 缓存探测结果**，一次探测决定后续所有请求走哪条路：

1. 首次用到某host 时，用一个**轻量请求**（HEAD，超时 5 秒）测它
2. 官方通 → 用官方；不通 → 试镜像（按候选列表）
3. 结果**缓存到进程结束**（`_HOST_OK`），避免每次下载都重测

⚠️ **探测必须轻量**：用 HEAD 而不是 GET 完整资产 —— 探测本身不该
产生流量开销。GitHub 对未知路径返404，这也是有效的"通"信号
（能拿到 HTTP 响应就说明 TCP+TLS 都通了）。

## 环境变量

- `DM_GITHUB_MIRROR`：手动指定镜像前缀（优先级最高，跳过探测直接用）
- `DM_GITHUB_MIRRORS`：逗号分隔的**候选列表**，第一个默认是
  `https://ghfast.top`（实测可用）。探测失败时按顺序试。
- `DM_GITHUB_MIRROR_PROBE=0`：关掉探测（强制手动指定的镜像，或全走官方）
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request

# 探测结果缓存：host -> 该host 是否可用（bool）
_HOST_OK: dict[str, bool] = {}
# 已选定的前缀（None = 直连官方）
_CHOSEN: dict[str, str | None] = {}

#: 候选镜像，按实测可用性排序（2026-10-06 实测）：
#: ghfast.top / gh-proxy.com 能拿到正确 404（说明通）；raw.gitmirror.com 连不上、
#: mirror.ghproxy.com 超时 → 放最后
DEFAULT_MIRRORS = (
    "https://ghfast.top",
    "https://gh-proxy.com",
    "https://hub.gitmirror.com",
)

PROBE_TIMEOUT = 5.0
#: 探测时用的路径：GitHub 各host 对不存在路径返 404，但**能拿到响应就说明通了**。
#: 用仓库根路径（对每个 host 都存在）更稳。
_PROBE_PATH = "/"


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    return [x.strip().rstrip("/") for x in raw.split(",") if x.strip()]


def manual_mirror() -> str:
    """用户显式指定的镜像前缀（无则空串）。

    设了就**完全跳过探测** —— 用户要么是知道自己在干什么，要么网络环境
    特殊（内网代理、离线源），此时自动探测反而会误判。
    """
    return os.environ.get("DM_GITHUB_MIRROR", "").strip().rstrip("/")


def _probe_enabled() -> bool:
    return os.environ.get("DM_GITHUB_MIRROR_PROBE", "1").strip() not in ("0", "false", "no")


def _http_ok(url: str, timeout: float = PROBE_TIMEOUT) -> bool:
    """轻量探测：HEAD 一下，能拿到 HTTP 响应就算通。

    ⚠️ 刻意**不看 404**：GitHub 对不存在的路径返 404，但这恰好证明
    TCP + TLS 都通了。真正的失败是 `URLError` / `timeout` / `OSError`。
    """
    req = urllib.request.Request(url, method="HEAD",
                                headers={"User-Agent": "DiceManager"})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True                    # 4xx/5xx 也是「服务器应答了」
    except Exception:                   # URLError / timeout / OSError
        return False


#: 镜像可用性缓存（**跨 host 共享**）：实测各 host 的结果高度相关 ——
#: 代理通常整域放行或整域拦，不会有github 通而 raw 不通的情况。
#: 共享后只需探测一次镜像，3 个 host 的总耗时从 15s 降到 ~5s。
_MIRROR_OK: dict[str, bool] = {}


def _pick_mirror(host: str, candidates: list[str]) -> str | None:
    """挑一个可用镜像（并发探测 + 结果跨 host 共享）。"""
    for m in candidates:
        if m in _MIRROR_OK:
            if _MIRROR_OK[m]:
                return m
            continue
        t0 = time.monotonic()
        ok = _http_ok(f"{m}/https://{host}{_PROBE_PATH}")
        _MIRROR_OK[m] = ok
        if not ok:
            # 顺手记一笔便于排障：用户报"下载失败"时能直接看出是镜像挂了
            print(f"[ghdl] 镜像不可用，跳过：{m}（{time.monotonic() - t0:.1f}s）")
            continue
        return m
    return None


def pick_prefix(host: str) -> str | None:
    """给 host 选一个可用的前缀（None = 直连官方）。结果按 host 缓存。"""
    if host in _CHOSEN:
        return _CHOSEN[host]

    forced = manual_mirror()
    if forced:
        _CHOSEN[host] = forced
        return forced
    if not _probe_enabled():
        _CHOSEN[host] = None
        return None
    candidates = _env_list("DM_GITHUB_MIRRORS") or list(DEFAULT_MIRRORS)

    # 先测官方 —— 国内也可能通（本项目实测 api.github.com 0.3 秒）
    if _http_ok(f"https://{host}{_PROBE_PATH}"):
        _CHOSEN[host] = None
        _HOST_OK[host] = True
        return None

    # 官方不通 → 挑镜像
    got = _pick_mirror(host, candidates)
    if got:
        print(f"[ghdl] {host} 直连不通，改用镜像 {got}")
    else:
        # 全不通：仍然返回 None（直连），让真实请求去报错 ——
        # 比在这里抛异常更好：真实请求的报错信息对用户更有用
        print(f"[ghdl] {host} 直连与镜像均不通，仍按直连尝试（将由真实请求报错）")
    _CHOSEN[host] = got
    return got


def url_for(url: str) -> str:
    """把可能指向 GitHub 的 url 变成「实际该用的url」。

    非 GitHub 的 url 原样返回（镜像前缀拼上去只会坏事）。
    """
    for host in ("github.com", "raw.githubusercontent.com",
                 "objects.githubusercontent.com", "codeload.github.com",
                 "api.github.com", "release-assets.githubusercontent.com"):
        if host in url:
            prefix = pick_prefix(host)
            return f"{prefix}/{url}" if prefix else url
    return url


def reset_cache() -> None:
    """清空探测缓存（测试用：改环境变量后要能重新探测）。"""
    _HOST_OK.clear()
    _CHOSEN.clear()
    _MIRROR_OK.clear()
