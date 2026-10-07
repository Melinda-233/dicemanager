# -*- coding: utf-8 -*-
"""LLBot AUTH TOKEN 复用（peek_auth_token）。

背景：v8.0.9+ 的 AUTH TOKEN 是**按 QQ 号**向快速登录平台申请的，不是通用密钥。
换实例（新机器 / 重装 / 清空目录）后让用户去翻旧目录里的 auth_token.txt，
几乎一定会漏填 —— 而 LLBot 缺这个文件是**启动即退出**，报的还是
「缺必备文件」之外的信息，很难自己定位。

所以新建实例时把本机已有 llbot 实例用过的 token 带出来。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DM_STATE_DIR", tempfile.mkdtemp())

from adapters.llbot import LLBotAdapter  # noqa: E402


def _adapter() -> LLBotAdapter:
    return LLBotAdapter({"name": "llbot", "exe": "llbot",
                         "config_path": "bin/llbot/default_config.json",
                         "required_files": ["llbot",
                                            "bin/llbot/default_config.json"]})


def _instance(root: Path, iid: str, qq: str | None = None) -> dict:
    """造一条实例记录，并在其目录下放好 auth_token.txt。"""
    d = root / iid
    tok_file = d / "bin" / "llbot" / "data" / "auth_token.txt"
    tok_file.parent.mkdir(parents=True, exist_ok=True)
    tok_file.write_text("TOKEN-" + iid + "\n", encoding="utf-8")
    return {"id": iid, "dice": "llbot", "dir": str(d), "qq": qq}


class _Reg:
    def __init__(self, recs):
        self._recs = recs

    def all(self):
        return list(self._recs)


def _set_mtime(rec: dict, ts: float) -> None:
    p = Path(rec["dir"]) / "bin" / "llbot" / "data" / "auth_token.txt"
    os.utime(p, (ts, ts))


def test_returns_none_when_no_llbot_instance(tmp_path):
    """本机没有 llbot 实例 → token 为 None，前端不显示任何提示。"""
    got = _adapter().peek_auth_token(_Reg([]))
    assert got == {"token": None, "from_instance": None, "from_qq": None}


def test_picks_token_from_existing_instance(tmp_path):
    rec = _instance(tmp_path, "llbot-aaa", qq="975809162")
    got = _adapter().peek_auth_token(_Reg([rec]))
    assert got["token"] == "TOKEN-llbot-aaa"
    assert got["from_instance"] == "llbot-aaa"
    assert got["from_qq"] == "975809162", "回带 QQ 号：token 是按号申请的，要能确认拿的是哪个"


def test_ignores_other_programs(tmp_path):
    """别的程序目录下的同名文件不该被当成 llbot 的 token。"""
    d = tmp_path / "sealdice-xxx" / "bin" / "llbot" / "data"
    d.mkdir(parents=True)
    (d / "auth_token.txt").write_text("NOT-LLBOT\n", encoding="utf-8")
    reg = _Reg([{"id": "sealdice-xxx", "dice": "sealdice",
                 "dir": str(tmp_path / "sealdice-xxx"), "qq": "1"}])
    assert _adapter().peek_auth_token(reg)["token"] is None


def test_newest_wins(tmp_path):
    """多个实例时取最近改动的那个 —— 用户换了 token 后新建实例应拿到新的。"""
    old = _instance(tmp_path, "llbot-old", qq="111")
    new = _instance(tmp_path, "llbot-new", qq="222")
    _set_mtime(old, 1000.0)
    _set_mtime(new, 2000.0)
    got = _adapter().peek_auth_token(_Reg([old, new]))
    assert got["from_instance"] == "llbot-new"
    assert got["from_qq"] == "222"


def test_skips_missing_or_empty_token_file(tmp_path):
    """token 文件缺失 / 空内容都不该让探测崩掉或返回空串。"""
    good = _instance(tmp_path, "llbot-good", qq="1")
    _set_mtime(good, 1000.0)
    # 空文件
    empty_dir = tmp_path / "llbot-empty"
    (empty_dir / "bin" / "llbot" / "data").mkdir(parents=True)
    (empty_dir / "bin" / "llbot" / "data" / "auth_token.txt").write_text("  \n", encoding="utf-8")
    empty = {"id": "llbot-empty", "dice": "llbot", "dir": str(empty_dir), "qq": "2"}
    _set_mtime(empty, 3000.0)
    # 目录被删（记录还在但文件已不存在）
    gone = {"id": "llbot-gone", "dice": "llbot", "dir": str(tmp_path / "nope"), "qq": "3"}

    got = _adapter().peek_auth_token(_Reg([good, empty, gone]))
    assert got["token"] == "TOKEN-llbot-good", "空文件与缺失目录都应被跳过"


def test_survives_broken_registry(tmp_path):
    """registry 抛异常时不能把整个探测带崩（前端会 await 它）。"""

    class _Bad:
        def all(self):
            raise RuntimeError("状态库读不了")

    assert _adapter().peek_auth_token(_Bad())["token"] is None
    assert _adapter().peek_auth_token(None)["token"] is None


def test_token_rel_matches_configure_login(tmp_path):
    """探测的路径必须与 configure_login 写入的路径一致。

    这两个是同一个 token 的读写两端：路径一旦漂移，探测到的 token 写不进
    程序读的那个文件，表现为「明明填了 token 还是启动即退出」。
    """
    import inspect
    src = inspect.getsource(LLBotAdapter.configure_login)
    assert LLBotAdapter.TOKEN_REL in src, "configure_login 必须写同一个路径"
