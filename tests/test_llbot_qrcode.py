"""LLBot 契约回归：manifest 结构（exe/必备文件）+ 二维码提取器（落盘 PNG → base64）。

背景（2026-09-24 线上故障）：用户误传 GitHub 源码包作为离线包，required_files 为空
导致部署假成功 → 实例永远无进程 → 登录页无二维码。exe 名也曾写错（LLBot-CLI，
实际二进制叫 llbot）。本测试锁定这两处 + 新的 PNG 提取逻辑。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.llbot import LLBotAdapter           # noqa: E402
from adapters import load_registry                # noqa: E402


def test_manifest_contract():
    manifests = load_registry(ROOT / "manifests")
    m, cls = manifests["llbot"]
    assert cls is LLBotAdapter
    assert m["exe"] == "llbot", f"exe 必须与官方 zip 根二进制同名: {m['exe']}"
    assert "llbot" in m["required_files"], "缺必备文件校验会把源码错包放行成部署成功"
    json.loads(json.dumps(m))                     # 可 JSON 化（无注释残留等）


def _fake_instance(tmp_path: Path):
    class Inst:
        dir = str(tmp_path)
    return Inst()


def test_extract_qrcode_reads_saved_png(tmp_path):
    png = tmp_path / "bin/llbot/data/temp"
    png.mkdir(parents=True)
    (png / "login-qrcode.png").write_bytes(b"\x89PNG-fake")
    a = LLBotAdapter({"exe": "llbot", "config_path": "bin/llbot/default_config.json"})
    hit = a.extract_qrcode("[] qq-protocol 二维码文件已保存: /x/login-qrcode.png",
                           _fake_instance(tmp_path))
    assert hit and hit["base64"].startswith("data:image/png;base64,")


def test_extract_qrcode_ignores_other_lines(tmp_path):
    a = LLBotAdapter({})
    assert a.extract_qrcode("普通日志行", _fake_instance(tmp_path)) is None
    assert a.extract_qrcode("========== 请使用手机QQ扫描二维码登录 ==========",
                            _fake_instance(tmp_path)) is None


def test_extract_qrcode_rejects_truncated_png(tmp_path):
    """触发行已打印但 PNG 尚未写完（无魔数）时必须拒绝，且不得抛异常。"""
    png = tmp_path / "bin/llbot/data/temp"
    png.mkdir(parents=True)
    (png / "login-qrcode.png").write_bytes(b"half-written")
    a = LLBotAdapter({"exe": "llbot", "config_path": "bin/llbot/default_config.json"})
    assert a.extract_qrcode("二维码文件已保存: /x/login-qrcode.png",
                            _fake_instance(tmp_path)) is None


def test_configure_login_writes_auth_token(tmp_path):
    """v8+ 的 AUTH TOKEN 必须落盘到 bin/llbot/data/auth_token.txt（缺失时 LLBot 报错退出）。"""
    cfg = tmp_path / "bin/llbot/default_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{}", encoding="utf-8")
    a = LLBotAdapter({"exe": "llbot", "config_path": "bin/llbot/default_config.json"})
    r = a.configure_login(_fake_instance(tmp_path),
                          {"version": "8.2.1", "auth_token": " tok ", "qq": "123"})
    assert r["ok"] and r["restart"]
    tok_file = tmp_path / "bin/llbot/data/auth_token.txt"
    assert tok_file.read_text(encoding="utf-8").strip() == "tok"
    assert json.loads(cfg.read_text(encoding="utf-8"))["QQ"] == "123"


def test_configure_login_requires_token_for_v8(tmp_path):
    a = LLBotAdapter({"exe": "llbot", "config_path": "bin/llbot/default_config.json"})
    r = a.configure_login(_fake_instance(tmp_path), {"version": "8.2.1"})
    assert r.get("conflict")
