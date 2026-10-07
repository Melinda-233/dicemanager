"""下载进度上报 + 失败提示的可行动化（deploy 期间前端轮询 /deploy-progress）。

背景：部署大包要几分钟，界面只有一句「正在下载…请稍候」= 用户看到的「假死」，
且失败时只看到进度条停住，不知道是断网、限流还是校验不过。

本测试盯两件事：

1. **进度真的在推进**：`DEPLOY_PROGRESS` 在下载期持续更新 done/total，
   总量未知时 total 退化为 0（前端据此走不确定态，而不是给假百分比）。
2. **失败原因可行动**：deploy 抛异常时，进度里留下 `stage=error` + 中文原因，
   且原因里带「下一步该干什么」（重试/换网络/改用镜像），不是把 urllib 的天书抛给用户。
   同时不能破坏既有契约：失败后进度键不能永远留着（内存泄漏）。

另外钉住一个易踩的点：**成功时进度必须被清掉**（否则下次轮询读到脏数据，
前端会一直显示上一次的进度条）。
"""
import os
import sys
import tempfile
import urllib.error
from pathlib import Path
from types import SimpleNamespace

tmp = tempfile.mkdtemp(prefix="dm_dlprog_")
os.environ.setdefault("DM_STATE_DIR", tmp)
os.environ.setdefault("DM_LOG_DIR", os.path.join(tmp, "logs"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from adapters import base as base_mod  # noqa: E402

# ---------- 错误提示：每类都要给出「下一步做什么」 ----------

def _hint(exc: Exception) -> str:
    return base_mod._download_error_hint(exc)


def test_http_404_says_asset_gone_not_bare_code():
    """404：告诉用户是资产失效/改名了，不是甩一个 HTTP Error 404。"""
    out = _hint(urllib.error.HTTPError("u", 404, "Not Found", {}, None))
    assert "404" in out
    assert "失效" in out
    assert "HTTP Error 404" in out          # 原始信息仍保留，排查要用


@pytest.mark.parametrize("code,word", [
    (403, "限流"),      # GitHub 限流，重试无用，得等或换源
    (429, "限流"),
])
def test_http_rate_limit_explains_not_to_retry_fast(code, word):
    assert word in _hint(urllib.error.HTTPError("u", code, "x", {}, None))


def test_timeout_suggests_retry_or_mirror():
    out = _hint(urllib.error.URLError(TimeoutError("timed out")))
    assert "超时" in out
    assert "重试" in out or "镜像" in out     # 必须给出路，而不是只说失败


def test_dns_failure_distinguished_from_timeout():
    """DNS 挂掉和超时是两回事，用户该查 DNS 而不是换个源重试。"""
    out = _hint(urllib.error.URLError("Name or service not known"))
    assert "解析" in out or "DNS" in out


def test_sha256_failure_says_package_not_trusted():
    out = _hint(RuntimeError("下载包 sha256 校验失败（期望 abc123def456）"))
    assert "sha256" in out.lower() or "校验" in out
    assert "上传" in out                       # 给出替代出路


def test_oom_exit_code_is_translated():
    """服务器真实发生过：2G 机器 pip 被 OOM kill → 子进程 rc=-15。

    界面上写「rc=-15」用户完全无从判断，必须翻译成「内存不足、停掉其他实例再试」。
    """
    out = _hint(RuntimeError("依赖安装失败（rc=-15）："))
    assert "内存" in out


def test_error_hint_never_empty_and_keeps_original():
    for e in [RuntimeError(""), ValueError("某个说不清的错"), OSError("boom")]:
        out = _hint(e)
        assert out and out.strip()


# ---------- 进度生命周期 ----------

def _inst(iid="i-prog", state="CREATED", dir: str | None = None):
    return SimpleNamespace(id=iid, dice="nonebot2", dir=dir or str(Path(tmp) / f"d-{iid}"),
                           state=state, allocated_ports={}, actual_port=None,
                           webui_token=None, warnings=[])


class _Ad(base_mod.BaseAdapter):
    """最小可用适配器：只把 _acquire_archive 换成可控的假下载。"""

    def __init__(self, boom: Exception | None = None, total: int = 0, chunk: int = 7):
        self.m = {"name": "fake", "role": "app", "arch": "linux_amd64",
                  "compatible_login": [], "start_cmd": "x", "install_root": "fake"}
        # 绕过基类 __init__ 会漏掉 _last_tag（deploy 里 `if key and (tag := self._last_tag)`
        # 会 AttributeError）。这里显式补上，等价于基类初始化的效果。
        self._last_tag: str | None = None
        self._boom, self._total, self._chunk = boom, total, chunk
        self.progress: list[dict] = []

    def _acquire_archive(self, instance):
        seen = []
        for done in range(self._chunk, self._chunk * 4, self._chunk):
            seen.append({"stage": "download", "done": done, "total": self._total})
        self.progress = seen
        if self._boom:
            raise self._boom
        p = Path(instance.dir) / "fake.tar.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        return p

    def verify_required(self, instance):
        return []

    def _extract(self, archive, instance):
        return None

    def _resolve_download(self):
        return "https://example.invalid/fake.tar.gz"

    def login_modes(self, instance=None):
        return ["qrcode"]

    # BaseAdapter 的三个抽象方法：本测试只关心 deploy/进度，补空实现即可
    def build_start_cmd(self, instance) -> list[str]:
        return ["fake"]

    def configure_login(self, instance, credentials: dict) -> dict:
        return {"ok": True}

    def write_conn_config(self, instance, mode: str, direction: str, *a, **kw) -> dict:
        return {"ok": True}


def test_progress_reports_increasing_done_during_download():
    """下载期 done 必须递增（前端进度条才有意义）。"""
    ad = _Ad(total=100)
    inst = _inst()
    ad.deploy(inst)
    dones = [s["done"] for s in ad.progress]
    assert dones == sorted(dones) and dones[-1] > dones[0]
    assert all(s["total"] == 100 for s in ad.progress)


def test_progress_total_zero_when_unknown_not_guessed():
    """无 Content-Length 时 total=0 → 前端走不确定态；绝不能编一个假总量。"""
    ad = _Ad(total=0)
    ad.deploy(_inst())
    assert all(s["total"] == 0 for s in ad.progress)


def test_success_clears_progress():
    """成功必须清掉进度键：否则前端下次轮询读到脏的旧进度，一直显示上次的条。"""
    iid = "i-success"
    _Ad().deploy(_inst(iid))
    assert iid not in base_mod.DEPLOY_PROGRESS


def test_failure_reports_error_stage_with_reason():
    """失败：进度里留下 stage=error + 中文原因，供轮询读走。"""
    iid = "i-fail"
    ad = _Ad(boom=urllib.error.URLError(TimeoutError("timed out")))
    with pytest.raises(urllib.error.URLError):
        ad.deploy(_inst(iid))
    got = base_mod.deploy_progress_of(iid)      # 走真实读取路径
    assert got["stage"] == "error"
    assert "超时" in got["error"]


def test_error_state_is_consumed_on_read():
    """error 态是「一次性通知」：读走即清，否则内存泄漏 + 下次读到旧错误。"""
    iid = "i-consume"
    with pytest.raises(RuntimeError):
        _Ad(boom=RuntimeError("boom")).deploy(_inst(iid))
    first = base_mod.deploy_progress_of(iid)
    assert first["stage"] == "error"
    second = base_mod.deploy_progress_of(iid)
    assert second == {"stage": "idle"}, f"error 态应被消费掉，实际 {second}"


def test_new_deploy_resets_stale_error_state():
    """上一轮失败残留的 error 态，不能让下一轮部署一开始就显示旧错误。"""
    iid = "i-stale"
    base_mod.DEPLOY_PROGRESS[iid] = {"stage": "error", "error": "上一轮的错"}
    _Ad().deploy(_inst(iid))          # 本轮成功
    assert iid not in base_mod.DEPLOY_PROGRESS


def test_existing_dir_branch_clears_progress():
    """目录已存在（重复部署/冲突）时也要清进度：那条分支不走 try/finally。"""
    iid = "i-exists"
    d = Path(tmp) / f"exists-{iid}"
    d.mkdir(parents=True, exist_ok=True)
    base_mod.DEPLOY_PROGRESS[iid] = {"stage": "error", "error": "旧的"}
    _Ad().deploy(_inst(iid, dir=str(d)))
    assert iid not in base_mod.DEPLOY_PROGRESS


def test_deploy_without_instance_id_skips_tracking():
    """测试桩常用无 id 的 SimpleNamespace：不该因此崩（进度跟踪要能优雅跳过）。"""
    inst = _inst()
    inst.id = None
    _Ad().deploy(inst)                 # 不抛异常即可
