"""下载前探测（probe_package）：点下载之前就说清「要下什么、多大、哪个版本」。

## 为什么要有这个功能（真实故障驱动）

2026-10-08 逐个程序真机验证时抓到两类「部署失败」，用户视角都是同一句
「部署失败」，完全无从下手：

1. **上游改资产名** → 硬编码 URL 直接 404
   llbot v8.3.0 把 `LLBot-CLI-linux-x64.zip` 改成 `LuckyLillia-CLI-linux-x64.zip`。
2. **滚动 tag 的资产名每次都变**
   Dice-Next 的资产叫 `DiceNext-beta-3.0.0-927-linux-amd64-2026-10-06-2`（带日期），
   光看清单里的固定 URL 判断不了「今天会下到哪个包」。

所以把「解析 URL + 问 Content-Length」提到部署之前。关键约束：
**必须复用 `_resolve_download()`**（同一套解析逻辑）——另写一份必然漂移，
而漂移正是 llbot 问题的成因之一。

## 钉住的行为

- 已有本地缓存 → 标记 cached，部署根本不走下载，不该去联网问
- 解析失败 → 把可行动的报错带回来，**不抛异常**（探测是提前告知，不该阻断）
- Content-Length 拿不到（405 / 超时 / 镜像不支持 HEAD）→ size=None，
  **绝不能编一个假大小**
- 只做 HEAD，不下载正文
"""
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

tmp = tempfile.mkdtemp(prefix="dm_probe_")
os.environ.setdefault("DM_STATE_DIR", tmp)
os.environ.setdefault("DM_LOG_DIR", os.path.join(tmp, "logs"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.base import BaseAdapter  # noqa: E402


def _raiser(exc: Exception):
    """造一个「调用即抛」的 _resolve_release 桩。

    别用 `(_ for _ in ()).throw(...)`：那样在**赋值时**就抛了，异常发生在
    probe_package 之外，测的就不是被测代码而是桩本身（写测试时踩过）。
    """
    def _boom():
        raise exc
    return _boom


class _Ad(BaseAdapter):
    def __init__(self, dice="fake", resolve=None, size=1234, cached=False):
        self.m = {"name": dice, "arch": "standalone", "install_root": "/opt",
                  "exe": "fake.bin", "required_files": ["fake.bin"],
                  "download": "https://example.invalid/fake.zip",
                  "download_strategy": "direct"}
        self._last_tag = None
        self._resolve = resolve
        self._size = size
        self._cached = cached

    def verify_required(self, instance):
        return []

    def build_start_cmd(self, instance) -> list[str]:
        return []

    def configure_login(self, instance, credentials) -> dict:
        return {"ok": True}

    def write_conn_config(self, instance, mode, direction, *a, **kw) -> dict:
        return {"ok": True}

    def login_modes(self, instance=None):
        return ["qrcode"]

    def _resolve_release(self):
        # 桩覆盖点：resolve 给了就用它（可能是「调用即抛」的桩函数），
        # 否则返回固定元组。直接改 self._resolve 是没用的 —— 被测代码调的是
        # _resolve_release()，不是 _resolve。
        if self._resolve is not None:
            return self._resolve()
        return "https://github.com/o/r/releases/download/v1.2.3/fake-linux-x64.zip", "v1.2.3"

    def _remote_size(self, url):            # 绕过真实网络，单独测 size 展示
        return self._size


def test_probe_reports_asset_tag_and_size():
    """最常见路径：解析成功 → 资产名 / 版本 / 大小齐全。"""
    r = _Ad().probe_package()
    assert r["asset"] == "fake-linux-x64.zip", "资产名要从 URL 末段取出"
    assert r["tag"] == "v1.2.3"
    assert r["size"] == 1234
    assert r["executable"] == "fake.bin"
    assert r["required_files"] == ["fake.bin"]
    assert r["cached"] is False


def test_probe_cached_skips_network():
    """本地已有包 → 部署走解压不下载，不该再去联网问上游。"""
    ad = _Ad()
    # 造一个本地包，命中 find_archive
    import adapters.base as B
    d = Path(tmp) / "pkgs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "fake.zip").write_bytes(b"x")
    orig = B.pkgstore.pkg_dir
    B.pkgstore.pkg_dir = lambda: d
    try:
        r = ad.probe_package()
    finally:
        B.pkgstore.pkg_dir = orig
    assert r["cached"] is True
    assert "url" not in r, "命中缓存时不该解析下载 URL"


def test_probe_size_none_when_unknown():
    """拿不到 Content-Length → size 为 None（前端显示「未知」），不能编数字。"""
    r = _Ad(size=None).probe_package()
    assert r["size"] is None
    assert r["asset"], "大小未知也要给出资产名"


def test_probe_resolve_failure_is_reported_not_raised():
    """解析失败要把可行动的报错带回来，**不抛** —— 探测是提前告知，不该阻断向导。"""
    ad = _Ad(resolve=_raiser(
        RuntimeError("release 未找到匹配资产（pattern=xxx, suffix=.zip）")))
    r = ad.probe_package()
    assert r.get("error"), "应带出错误文案"
    assert "pattern" in r["error"], "原始信息要保留（排查要用）"
    assert "asset" not in r


def test_probe_http_404_translated():
    """上游 404（资产改名/删除）→ 提示要说人话，不是「HTTP Error 404」。"""
    ad = _Ad(resolve=_raiser(
        urllib.error.HTTPError("u", 404, "Not Found", {}, None)))
    r = ad.probe_package()
    assert "404" in r["error"]
    assert "失效" in r["error"]


def test_probe_manual_strategy_explains_upload():
    """manual 策略（yogurt/milky）→ 明确引导上传离线包，而不是让用户干等。"""
    ad = _Ad()
    ad.m["download_strategy"] = "manual"
    ad._resolve = _raiser(RuntimeError(
        "该程序不提供在线下载，请先在 WebUI「离线程序包」上传程序包后重新部署"))
    r = ad.probe_package()
    assert "上传" in r["error"], "要说清下一步动作"


def test_probe_uses_same_resolver_as_download():
    """探测必须复用 _resolve_download（同一套解析），否则两处解析必然漂移。

    这条是本功能的生命线：llbot 问题的成因之一就是「清单里的 URL 与上游实际
    资产名脱节」，若探测另写一份 URL 拼装，它会和真下载给出不同答案。
    """
    ad = _Ad()
    assert ad.probe_package()["url"] == ad._resolve_download()


def test_probe_pip_project_strategy_explains():
    """依赖型程序（nonebot2/astrbot）→ 说明它们本来就没有可下载的包。"""
    ad = _Ad()
    ad.m["download_strategy"] = "pip_project"
    ad._resolve = _raiser(RuntimeError(
        "该程序为 Python 依赖项目（pip_project），无可下载的程序包；"
        "依赖升级请用「管理应用」或 pip install -U"))
    r = ad.probe_package()
    assert "pip" in r["error"] or "依赖" in r["error"]


def test_remote_size_failure_returns_none(monkeypatch):
    """HEAD 失败（405 / 超时 / 镜像不支持）→ None，不抛。

    用 monkeypatch 而非手工改模块属性：手工改 `del urllib.request.urlopen` 会把
    **整个进程**的 urlopen 删掉（那是标准库属性，不是 base 的），后续测试全崩。
    """
    import adapters.base as B

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 405, "Method Not Allowed", {}, None)

    monkeypatch.setattr(B.urllib.request, "urlopen", boom)
    monkeypatch.setattr(B, "mirror_url", lambda u: u)
    assert BaseAdapter._remote_size("https://x/y.zip") is None


def test_remote_size_reads_content_length(monkeypatch):
    """正常路径：Content-Length 转成 int 返回。"""
    import adapters.base as B

    class _Resp:
        headers = {"Content-Length": "2048"}

        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(B, "mirror_url", lambda u: u)
    monkeypatch.setattr(B.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert BaseAdapter._remote_size("https://x/y.zip") == 2048
