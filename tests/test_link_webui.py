"""总览页连接管理契约：/link 兼容矩阵校验 + 解除关联 + /webui 端口与令牌读取。

兼容性完全由 manifest 的 compatible_login 声明驱动，测试直接调用端点函数
（ctx 模块级单例，DM_STATE_DIR 指向临时目录，参考 smoke_local.py 的做法）。
"""
import os
import sys
import tempfile
from pathlib import Path

tmp = tempfile.mkdtemp(prefix="dm_link_")
os.environ["DM_STATE_DIR"] = tmp
os.environ["DM_LOG_DIR"] = os.path.join(tmp, "logs")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest                                       # noqa: E402
from fastapi import HTTPException                   # noqa: E402

from api.context import ctx                         # noqa: E402
from api.rest import LinkReq, instance_webui, link_login   # noqa: E402


def _mk(iid, dice):
    ctx.registry.create(iid, dice=dice, arch="standalone", dir_=f"/tmp/{iid}", port=3000,
                        allocated_ports={"webui": 3080, "ob11": 3001})


@pytest.fixture(autouse=True)
def _clean():
    yield
    for r in ctx.registry.all():
        ctx.registry.remove(r["id"])
    ctx.registry.purge_tombstones(days=0)


def test_link_ok_and_persisted():
    _mk("sealdice-l1", "sealdice")
    _mk("napcat-l1", "napcat")
    r = link_login("sealdice-l1", LinkReq(login_ref="napcat-l1"))
    assert r == {"ok": True}
    assert ctx.registry.get("sealdice-l1").login_ref == "napcat-l1"


def test_link_rejects_incompatible_pair():
    _mk("shiki-l1", "shiki")
    _mk("sealdice-l2", "sealdice")
    with pytest.raises(HTTPException) as e:
        link_login("shiki-l1", LinkReq(login_ref="sealdice-l2"))
    assert e.value.status_code == 400
    assert ctx.registry.get("shiki-l1").login_ref is None


def test_link_rejects_self_missing_and_unknown_instance():
    _mk("napcat-l2", "napcat")
    with pytest.raises(HTTPException) as e:            # 自己关联自己
        link_login("napcat-l2", LinkReq(login_ref="napcat-l2"))
    assert e.value.status_code == 400
    _mk("sealdice-l3", "sealdice")
    with pytest.raises(HTTPException) as e:            # 目标不存在
        link_login("sealdice-l3", LinkReq(login_ref="napcat-not-exists"))
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:            # 实例本身不存在
        link_login("no-such-inst", LinkReq(login_ref="napcat-l2"))
    assert e.value.status_code == 404


def test_unlink_sets_none():
    _mk("sealdice-l4", "sealdice")
    _mk("napcat-l3", "napcat")
    link_login("sealdice-l4", LinkReq(login_ref="napcat-l3"))
    link_login("sealdice-l4", LinkReq(login_ref=None))     # null = 解除
    assert ctx.registry.get("sealdice-l4").login_ref is None


def test_webui_prefers_actual_port_and_returns_token():
    _mk("napcat-l4", "napcat")
    ctx.registry.update("napcat-l4", actual_port=5999, webui_token="tok-xyz")
    assert instance_webui("napcat-l4") == {"port": 5999, "token": "tok-xyz"}
    # 实际端口未回填（未启动）→ 用分配端口；令牌缺失不报错
    _mk("sealdice-l5", "sealdice")
    assert instance_webui("sealdice-l5") == {"port": 3080, "token": None}
    ctx.registry.update("napcat-l4", actual_port=None)
    assert instance_webui("napcat-l4")["port"] == 3080


def test_webui_unknown_instance_404():
    with pytest.raises(HTTPException) as e:
        instance_webui("ghost")
    assert e.value.status_code == 404
