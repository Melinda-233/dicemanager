"""多账号 + 多连一 / 一连多 模型回归测试（Windows 版）。

与 Linux 版 tests/test_multi_account.py 同口径，验证 Windows 分支已镜像
links/accounts 模型与向导 step4 逐条关联逻辑。覆盖：
- /link 端点接受完整 links 数组（一连多：同登录端不同账号；多连一：不同骰子端同登录端）
- 关联含 account_qq 持久化与兼容矩阵校验
- 向导 step4 逐条关联写互联配置：按 account_qq 派生端口/token、按 link_id 去重互不覆盖、
  逐条方向覆盖生效、账号改绑生效

注意：本文件不设 DM_STATE_DIR（Windows 套件有断言默认路径落项目 data 的用例），
改为在 fixture 里把共享单例 ctx.registry 临时替换为临时目录下的注册表，跑完还原——
既隔离写盘又不污染进程级路径口径。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest                                       # noqa: E402
import yaml                                         # noqa: E402
from fastapi import HTTPException                   # noqa: E402

from api.context import ctx                         # noqa: E402
from api.rest import LinkReq, link_login            # noqa: E402
from core.registry import Registry                  # noqa: E402

# 实例目录用临时根下的子目录（Windows 无 /tmp，避免落到盘符根）
IROOT = Path(tempfile.mkdtemp(prefix="dm_multi_")) / "inst"


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    """把共享 ctx 的注册表临时指向 tmp_path，跑完还原（避免写真实 data/instances.json）。"""
    old_reg = ctx.registry
    new_reg = Registry(tmp_path / "instances.json")
    ctx.registry = new_reg
    ctx.wizard.reg = new_reg
    try:
        yield
    finally:
        ctx.registry = old_reg
        ctx.wizard.reg = old_reg


def _mk(iid, dice, accounts=None):
    d = IROOT / iid
    ctx.registry.create(iid, dice=dice, arch="standalone", dir_=str(d), port=3000,
                        allocated_ports={"webui": 3080, "ob11": 3001})
    if accounts is not None:
        ctx.registry.update(iid, accounts=accounts)


def test_link_accepts_links_array_one_to_many():
    """一连多：单个骰子端关联同一登录端的不同账号，全部持久化。"""
    _mk("sealdice-m1", "sealdice")
    _mk("napcat-m1", "napcat", accounts=[
        {"qq": "111", "port": 3010, "token": "t1", "status": "running"},
        {"qq": "222", "port": 3011, "token": "t2", "status": "running"},
    ])
    r = link_login("sealdice-m1", LinkReq(links=[
        {"login_ref": "napcat-m1", "account_qq": "111"},
        {"login_ref": "napcat-m1", "account_qq": "222"},
    ]))
    assert r["ok"] is True
    assert r["links"] == [
        {"login_ref": "napcat-m1", "account_qq": "111"},
        {"login_ref": "napcat-m1", "account_qq": "222"},
    ]
    rec = ctx.registry.get("sealdice-m1")
    assert len(rec.links) == 2
    # legacy 单关联字段取第一条，仍为兼容读取方兜底
    assert rec.login_ref == "napcat-m1"


def test_link_many_to_one_three_dice_same_login():
    """多连一：三个骰子端各关联同一登录端（带/不带账号），均合法。"""
    _mk("napcat-m2", "napcat", accounts=[{"qq": "999", "port": 3009, "token": "tz",
                                          "status": "running"}])
    for i, acct in enumerate(["999", None, "999"]):
        did = f"sealdice-m2-{i}"
        _mk(did, "sealdice")
        r = link_login(did, LinkReq(links=[{"login_ref": "napcat-m2",
                                            "account_qq": acct}]))
        assert r["ok"] is True
        assert r["links"][0]["account_qq"] == acct


def test_link_rejects_incompatible_in_array():
    _mk("shiki-m1", "shiki")
    _mk("sealdice-m3", "sealdice")
    with pytest.raises(HTTPException) as e:
        link_login("shiki-m1", LinkReq(links=[{"login_ref": "sealdice-m3"}]))
    assert e.value.status_code == 400


def test_wizard_step4_writes_per_account_config():
    """向导 step4 为一条一连多（两账号）的骰子端写海豹端点：按 link_id 去重互不覆盖。"""
    nap = "napcat-s4"
    dice = "sealdice-s4"
    _mk(nap, "napcat", accounts=[
        {"qq": "111", "port": 3010, "token": "t1", "status": "running"},
        {"qq": "222", "port": 3011, "token": "t2", "status": "running"},
    ])
    _mk(dice, "sealdice")
    link_login(dice, LinkReq(links=[
        {"login_ref": nap, "account_qq": "111"},
        {"login_ref": nap, "account_qq": "222"},
    ]))
    # 准备海豹可读写的 dice.yaml（_endpoints_file 命中 imSession 即返回该文件）
    ddir = IROOT / dice
    (ddir / "data").mkdir(parents=True, exist_ok=True)
    (ddir / "data" / "dice.yaml").write_text("imSession: {}\n", "utf-8")

    r = ctx.wizard.run_step(dice, 4, {"links": [
        {"login_ref": nap, "account_qq": "111", "direction": "forward"},
        {"login_ref": nap, "account_qq": "222", "direction": "forward"},
    ]})
    assert r["result"] == "ok"
    assert "111" in r["preview"] and "222" in r["preview"]

    # 海豹端点按 link_id 去重：两个不同 id 各一条，互不影响
    doc = yaml.safe_load((ddir / "data" / "dice.yaml").read_text("utf-8"))
    eps = doc["imSession"]["endPoints"]
    ids = {e["baseInfo"]["id"] for e in eps}
    assert ids == {f"{nap}|111", f"{nap}|222"}, ids
    by_id = {e["baseInfo"]["id"]: e for e in eps}
    assert by_id[f"{nap}|111"]["adapter"]["accessToken"] == "t1"
    assert by_id[f"{nap}|222"]["adapter"]["accessToken"] == "t2"
    # 端口按账号派生（非登录端默认端口）
    assert "3010" in by_id[f"{nap}|111"]["adapter"]["connectUrl"]
    assert "3011" in by_id[f"{nap}|222"]["adapter"]["connectUrl"]

    # 每条 link 持久化各自的互联三要素
    rec = ctx.registry.get(dice)
    by_qq = {l["account_qq"]: l for l in rec.links}
    assert by_qq["111"]["conn_addr"].endswith("3010")
    assert by_qq["222"]["conn_addr"].endswith("3011")


def test_wizard_step4_per_link_direction_override():
    """逐条方向覆盖：一条正向、一条反向，分别落到端点 isReverse。"""
    nap = "napcat-s5"
    dice = "sealdice-s5"
    _mk(nap, "napcat", accounts=[{"qq": "111", "port": 3010, "token": "t1",
                                  "status": "running"}])
    _mk(dice, "sealdice")
    link_login(dice, LinkReq(links=[{"login_ref": nap, "account_qq": "111"}]))
    ddir = IROOT / dice
    (ddir / "data").mkdir(parents=True, exist_ok=True)
    (ddir / "data" / "dice.yaml").write_text("imSession: {}\n", "utf-8")
    r = ctx.wizard.run_step(dice, 4, {"links": [
        {"login_ref": nap, "account_qq": "111", "direction": "reverse"},
    ]})
    assert r["result"] == "ok"
    doc = yaml.safe_load((ddir / "data" / "dice.yaml").read_text("utf-8"))
    ep = doc["imSession"]["endPoints"][0]
    assert ep["adapter"]["isReverse"] is True
    assert ctx.registry.get(dice).links[0]["conn_direction"] == "reverse"


def test_wizard_step4_account_qq_change_updates_link():
    """向导 step4 把某关联账号从 111 改绑到 222，link 记录随之更新。"""
    nap = "napcat-s6"
    dice = "sealdice-s6"
    _mk(nap, "napcat", accounts=[
        {"qq": "111", "port": 3010, "token": "t1", "status": "running"},
        {"qq": "222", "port": 3011, "token": "t2", "status": "running"},
    ])
    _mk(dice, "sealdice")
    link_login(dice, LinkReq(links=[{"login_ref": nap, "account_qq": "111"}]))
    ddir = IROOT / dice
    (ddir / "data").mkdir(parents=True, exist_ok=True)
    (ddir / "data" / "dice.yaml").write_text("imSession: {}\n", "utf-8")
    ctx.wizard.run_step(dice, 4, {"links": [
        {"login_ref": nap, "account_qq": "222"},
    ]})
    rec = ctx.registry.get(dice)
    assert rec.links[0]["account_qq"] == "222"
    assert rec.links[0]["conn_addr"].endswith("3011")


def test_sealdice_health_and_diagnose_claim_endpoints_by_link_id():
    """多关联后海豹端点以 link_id 命名（login_ref|account_qq）：health_check /
    diagnose_conn 必须按 links 认领端点，不能因 id ≠ 实例 id 而误报「没有本实例端点」
    或把状态取到别的端点上。"""
    from types import SimpleNamespace

    nap = "napcat-h1"
    dice = "sealdice-h1"
    _mk(nap, "napcat", accounts=[
        {"qq": "111", "port": 3010, "token": "t1", "status": "running"},
        {"qq": "222", "port": 3011, "token": "t2", "status": "running"},
    ])
    _mk(dice, "sealdice")
    link_login(dice, LinkReq(links=[
        {"login_ref": nap, "account_qq": "111"},
        {"login_ref": nap, "account_qq": "222"},
    ]))
    ddir = IROOT / dice
    (ddir / "data").mkdir(parents=True, exist_ok=True)
    (ddir / "data" / "dice.yaml").write_text("imSession: {}\n", "utf-8")
    ctx.wizard.run_step(dice, 4, {"links": [
        {"login_ref": nap, "account_qq": "111"},
        {"login_ref": nap, "account_qq": "222"},
    ]})

    inst = ctx.registry.get(dice)
    ns = SimpleNamespace(**inst.__dict__)                 # 与 ws_overview/诊断口径一致（含 links）
    ad = ctx.wizard.get_adapter("sealdice")

    # 造状态：账号 111 已连接(1)，账号 222 断开(0)
    cfg = ddir / "data" / "dice.yaml"
    doc = yaml.safe_load(cfg.read_text("utf-8"))
    for e in doc["imSession"]["endPoints"]:
        e["baseInfo"]["state"] = 1 if e["baseInfo"]["id"].endswith("111") else 0
    cfg.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), "utf-8")

    # 任一关联已连接 → 整体视为连通（不再退化成「取最后一条」）
    assert ad.health_check(ns, is_alive=True)["conn"] == "ok"

    items = ad.diagnose_conn(ns)
    assert not any("没有本实例" in i["detail"] for i in items)
    # 两条端点各给 config + state 两组结论
    assert sum(1 for i in items if i["step"] == "config") == 2
    assert any(i["step"] == "state" and i["ok"] for i in items)     # 111 已连接
    assert any(i["step"] == "state" and not i["ok"] for i in items)  # 222 断开


def test_all_migrates_legacy_login_ref_and_delete_cascades(tmp_path):
    """all() 必须与 get() 同口径做 legacy 迁移：/api/instances（向导读取 links）与
    删除级联都以 all() 为数据源；若返回原始记录，旧实例（仅 login_ref）的关联会被漏掉。"""
    import json
    from api.rest import delete_instance

    reg_path = tmp_path / "legacy.json"
    reg_path.write_text(json.dumps({
        "napcat-l1": {"id": "napcat-l1", "dice": "napcat", "arch": "standalone",
                      "dir": str(IROOT / "napcat-l1"), "port": 3080, "state": "RUNNING"},
        "sealdice-l1": {"id": "sealdice-l1", "dice": "sealdice", "arch": "standalone",
                        "dir": str(IROOT / "sealdice-l1"), "port": 3211, "state": "RUNNING",
                        "login_ref": "napcat-l1", "conn_token": "tt",
                        "conn_addr": "127.0.0.1:3001", "conn_direction": "forward"},
    }, ensure_ascii=False), "utf-8")
    reg = Registry(reg_path)

    rec = {r["id"]: r for r in reg.all()}["sealdice-l1"]
    assert [l["login_ref"] for l in rec["links"]] == ["napcat-l1"]
    assert rec["links"][0]["conn_addr"] == "127.0.0.1:3001"
    assert rec["accounts"] == []

    # 删除登录端 → 级联解除 legacy 引用（旧实现读 all() 的 links 恒空，会漏解除）
    ctx.registry = reg
    ctx.wizard.reg = reg
    res = delete_instance("napcat-l1", confirm=True)
    assert res["unlinked"] == ["sealdice-l1"]
    assert reg.get("sealdice-l1").links == []
