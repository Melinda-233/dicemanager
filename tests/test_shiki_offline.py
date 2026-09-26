"""shiki（Dice!）离线部署测试。

背景：上游 GitHub 的 Dice! release 只发行各平台 QQ 协议原生模块
（w4123.Dice.*.dll，那个 w4123.Dice-2.6.5fix.zip 里也全是 dll），
主程序 Dice 可执行文件并不在 GitHub 发行。因此：
- download_strategy 改为 manual：本地无包时给明确引导，而不是下到
  「能解压但跑不起来」的错包；
- required_files 补上 Dice：部署后缺主程序必须报错，而非静默成功。
"""
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

os.environ["DM_STATE_DIR"] = tempfile.mkdtemp(prefix="dm_shiki_test_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json5
import pytest

from adapters.shiki import ShikiAdapter
from conftest import put_package
from core import packages as pkgstore

MANIFEST = json5.loads(
    (Path(__file__).resolve().parent.parent / "manifests" / "shiki.json")
    .read_text(encoding="utf-8"))


def _zip_bytes(*names) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in names:
            zf.writestr(n, "stub")
    return buf.getvalue()


def test_all_manifests_pass_load_registry():
    """全量清单必须能被 load_registry 接受（策略白名单/必填字段在这里把关）。

    曾踩坑：新增 manual 策略后忘记进 ALLOWED_STRATEGY，服务启动即挂；
    单测直接实例化适配器绕过了这道校验，故补这条端到端加载用例。
    """
    from adapters import load_registry
    reg = load_registry(Path(__file__).resolve().parent.parent / "manifests")
    assert "shiki" in reg
    assert len(reg) == len(list((Path(__file__).resolve().parent.parent
                                 / "manifests").glob("*.json")))


def test_manifest_declares_manual_and_requires_dice():
    """清单契约：不在线下载 + 部署后必须能找到 Dice 主程序。"""
    assert MANIFEST["download_strategy"] == "manual"
    assert MANIFEST["required_files"] == ["Dice"]
    assert "离线程序包" in MANIFEST["prerequisite"]


def test_manifest_is_dice_end_connected_via_onebot():
    """形态契约：shiki 是骰子端（独立程序），经 OneBot 连登录端，自身不登录。"""
    assert MANIFEST["arch"] == "standalone"
    assert MANIFEST["login_type"] == "external"
    for login in ("napcat", "snowluma", "llbot"):
        assert login in MANIFEST["compatible_login"]


def test_no_self_login_and_onebot_guidance():
    ad = ShikiAdapter(MANIFEST)
    inst = SimpleNamespace(dir="/tmp/shiki", allocated_ports={}, actual_port=None)
    assert ad.configure_login(inst, {})["needs_login"] is False
    r = ad.write_conn_config(inst, "ob11", "forward", "127.0.0.1:3001", "TOK123")
    assert r.ok and "127.0.0.1:3001" in r.manual and "TOK123" in r.manual
    r2 = ad.write_conn_config(inst, "ob11", "reverse", "127.0.0.1:6700", "TOK123")
    assert r2.ok and "6700" in r2.manual


def test_manual_strategy_errors_clearly_without_local_pkg():
    """本地无包时给出「请上传离线包」的明确指引，而不是去下 dll 包。"""
    pkgstore.remove_archive("shiki")
    ad = ShikiAdapter(MANIFEST)
    with pytest.raises(RuntimeError) as e:
        ad._resolve_download()
    assert "离线程序包" in str(e.value)


def test_deploy_rejects_package_without_dice_binary(tmp_path):
    """上传的包里没有 Dice 可执行文件 → 必须报错（此前 required_files 为空会静默 ok）。"""
    put_package("shiki", _zip_bytes("w4123.Dice.linux.amd64.dll"),
                          source="upload")
    ad = ShikiAdapter(MANIFEST)
    inst = SimpleNamespace(dir=str(tmp_path / "s_bad"), allocated_ports={},
                           actual_port=None)
    with pytest.raises(RuntimeError) as e:
        ad.deploy(inst)
    assert "缺失必备文件" in str(e.value)
    pkgstore.remove_archive("shiki")


def test_deploy_ok_with_official_package(tmp_path):
    """自备的官方程序包含 Dice → 部署成功，启动命令指向 Dice。"""
    pkgstore.remove_archive("shiki")
    put_package("shiki", _zip_bytes("Dice", "config.txt"), source="upload")
    ad = ShikiAdapter(MANIFEST)
    inst = SimpleNamespace(dir=str(tmp_path / "s_ok"), allocated_ports={},
                           actual_port=None)
    assert ad.deploy(inst) == "ok"
    assert ad.build_start_cmd(inst) == [str(tmp_path / "s_ok" / "Dice")]
    pkgstore.remove_archive("shiki")
