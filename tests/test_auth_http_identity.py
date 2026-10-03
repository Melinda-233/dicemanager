"""HTTP 层的身份注入与越权收口回归（2026-10-02 真机验证补录）。

为什么必须走 HTTP：此前全部鉴权测试都**进程内直调端点函数**，而直调时
`user` 参数为 None→ `_u()` 回落 admin，恰好掩盖了一个 P0：

    user: Annotated[CurrentUser, Depends(current_user)] | None = None   # ← 错

FastAPI 对「Union 在 Annotated 外」的写法**完全不注入**，端点永远拿到 None，
于是 HTTP 路径上所有身份都被回落成 admin——归属校验、配额拦截、管理员接口
全部形同虚设，任何登录用户等同管理员。既有单测因走直调而全绿。

正确写法是 Union 在 Annotated 内：`Annotated[CurrentUser | None, Depends(...)]`。

因此本文件的两条铁律：
  1. 涉及身份/归属/配额的断言，一律经 TestClient 走真实 HTTP；
  2. 签名一律用 `Annotated[X | None, Depends(...)]`，并由test_no_union_outside_
     annotated_forms 静态扫描全仓库锁定写法。
"""
import ast
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from api import rest
from api.app import app
from core.atomicio import write_atomic

ROOT = Path(__file__).resolve().parent.parent


# ---------- 签名写法静态锁定 ----------

def test_no_union_outside_annotated_forms():
    """全仓库禁止 `Annotated[X, Depends(..)] | None` 写法。

    该写法 FastAPI 不注入 → 端点拿 None → 回落 admin，是本轮 P0 的根因。
    只要有人再写出来就立即失败，不等到真机验证才发现。
    """
    bad: list[str] = []
    for py in sorted(ROOT.rglob("*.py")):
        if ".workbuddy" in py.parts or "web" in py.parts or ".git" in py.parts:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.arg, ast.AnnAssign)):
                continue
            ann = node.annotation
            if ann is None:
                continue
            # AnnAssign 的 annotation 直接就是表达式；arg.annotation 同理
            if not (isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr)):
                continue
            for side in (ann.left, ann.right):
                if (isinstance(side, ast.Subscript)
                        and isinstance(side.value, ast.Name)
                        and side.value.id == "Annotated"):
                    bad.append(f"{py.relative_to(ROOT)}:{node.lineno}")
    assert not bad, ("禁止 Annotated[X, Depends(..)] | None 写法"
                     f"（FastAPI 不注入 → 身份回落 admin）：{bad}")


def test_identity_params_use_expected_form():
    """带Depends 的身份参数必须是 Union 在 Annotated 内。"""
    src = (ROOT / "api" / "rest.py").read_text(encoding="utf-8")
    assert "Annotated[CurrentUser | None, Depends(current_user)]" in src
    assert "Annotated[CurrentUser, Depends(current_user)] | None" not in src


def test_admin_router_declares_require_auth():
    """admin_router 必须同时挂 require_auth 与 require_admin。

    require_admin 只从 request.state.user 读身份，而写 state 的是 require_auth。
    少挂一个 → 管理员接口恒 401（真机验证实测踩过）。
    """
    deps = []
    for d in rest.admin_router.dependencies:
        deps.append(getattr(d, "dependency", None))
    names = {getattr(f, "__name__", "") for f in deps if f}
    assert "require_auth" in names, f"admin_router 缺 require_auth：{names}"
    assert "require_admin" in names, f"admin_router 缺 require_admin：{names}"


# ---------- HTTP 层真机行为 ----------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """起一个带 admin + alice 两个账号、若干预置实例的真实 HTTP 客户端。"""
    from api import auth as auth_mod
    from api.context import ctx

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("DM_STATE_DIR", str(state))

    a = auth_mod.Auth(state / "auth.json")
    a.set_password("admin", "admin-pwd-1")
    a.create_user("alice", "alice-pwd-1", "user", "爱丽丝",
                  {"login_qq": 2, "app": 1})
    monkeypatch.setattr(auth_mod, "auth", a, raising=False)
    monkeypatch.setattr(rest, "auth", a, raising=False)

    # registry 必须**同一个对象**同时给 ctx.registry 与 ctx.wizard.reg：
    # wizard 在构造时就把 registry 存为 self.reg，只换 ctx.registry 会让写入落A
    # 文件、读取走 B 文件，表现为 create_instance 成功后 get() 抛 KeyError。
    reg = ctx.registry.__class__(state / "instances.json")
    monkeypatch.setattr(ctx, "registry", reg)
    monkeypatch.setattr(ctx.wizard, "reg", reg)
    recs = {
        "i-admin-app": {"id": "i-admin-app", "dice": "sealdice", "arch": "standalone",
                        "dir": str(state / "a1"), "port": 19101, "allocated_ports": {},
                        "state": "STOPPED", "owner": "admin", "links": []},
        "i-alice-app": {"id": "i-alice-app", "dice": "sealdice", "arch": "standalone",
                        "dir": str(state / "a2"), "port": 19102, "allocated_ports": {},
                        "state": "STOPPED", "owner": "alice", "links": []},
        "i-legacy": {"id": "i-legacy", "dice": "sealdice", "arch": "standalone",
                     "dir": str(state / "a3"), "port": 19103, "allocated_ports": {},
                     "state": "STOPPED", "links": []},
    }
    write_atomic(state / "instances.json",
                 __import__("json").dumps(recs).encode("utf-8"))

    with TestClient(app) as c:
        yield c


def _tok(c, username, pwd):
    r = c.post("/api/login", json={"username": username, "password": pwd})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_http_identity_is_not_fallback_admin(client):
    """P0 回归：普通用户的 HTTP 身份不得被回落成 admin。"""
    t = _tok(client, "alice", "alice-pwd-1")
    r = client.get("/api/me", headers=_h(t))
    assert r.status_code == 200
    me = r.json()
    assert me["username"] == "alice", me
    assert me["role"] == "user", me
    assert me["unlimited"] is False, me


def test_http_admin_identity_preserved(client):
    t = _tok(client, "admin", "admin-pwd-1")
    me = client.get("/api/me", headers=_h(t)).json()
    assert me["username"] == "admin" and me["role"] == "admin", me


def test_http_instance_list_isolated_by_owner(client):
    """P0 回归：实例列表按 owner 过滤，admin 5→3 条（含无 owner 迁移实例）。"""
    ta = _tok(client, "admin", "admin-pwd-1")
    tu = _tok(client, "alice", "alice-pwd-1")
    admin_ids = sorted(i["id"] for i in client.get("/api/instances", headers=_h(ta)).json())
    user_ids = sorted(i["id"] for i in client.get("/api/instances", headers=_h(tu)).json())
    assert admin_ids == ["i-admin-app", "i-alice-app", "i-legacy"], admin_ids
    assert user_ids == ["i-alice-app"], user_ids


def test_http_cross_user_instance_access_is_404(client):
    """越权与不存在同状态码、同文案模板，避免探测他人实例 ID。

    只比较去掉 id 后的文案：detail 里回显调用方自己传进来的 id 不构成泄露
    （他本来就知道自己问了什么），可区分才是泄露。
    """
    import re
    tu = _tok(client, "alice", "alice-pwd-1")
    r1 = client.get("/api/instances/i-admin-app/export", headers=_h(tu))
    r2 = client.get("/api/instances/does-not-exist/export", headers=_h(tu))
    assert r1.status_code == r2.status_code == 404, (r1.status_code, r2.status_code)
    tpl = lambda r: re.sub(r":\s*\S+$", "", r.json()["detail"])   # noqa: E731
    assert tpl(r1) == tpl(r2) == "实例不存在", (r1.json(), r2.json())


def test_http_admin_endpoints_require_admin(client):
    """P0 回归：普通用户访问管理员接口 403，管理员 200。"""
    tu = _tok(client, "alice", "alice-pwd-1")
    ta = _tok(client, "admin", "admin-pwd-1")
    assert client.get("/api/admin/accounts", headers=_h(tu)).status_code == 403
    r = client.get("/api/admin/accounts", headers=_h(ta))
    assert r.status_code == 200, r.text
    assert {u["username"] for u in r.json()} == {"admin", "alice"}


def test_http_panel_restart_requires_admin(client):
    """面板重启是全局动作，必须收进管理员位（此前任何登录用户都能触发）。"""
    tu = _tok(client, "alice", "alice-pwd-1")
    assert client.post("/api/panel/restart", json={}, headers=_h(tu)).status_code == 403


def test_http_no_token_and_bad_token_rejected(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers=_h("nope")).status_code == 401


def test_http_quota_blocks_app_instance_creation(client):
    """alice 的 app=1 且已占满 → 再建应用端被拦，且文案给出提额指引。"""
    tu = _tok(client, "alice", "alice-pwd-1")
    r = client.post("/api/instances", headers=_h(tu),
                    json={"dice": "sealdice", "arch": "standalone",
                          "bot_mode": "official"})
    assert r.status_code == 400, r.text
    assert "账号管理" in r.json()["detail"], r.json()


def test_http_quota_allows_login_instance_creation(client):
    """登录端不占 app 名额（按 QQ 号计），不应被 app 配额误伤。"""
    tu = _tok(client, "alice", "alice-pwd-1")
    r = client.post("/api/instances", headers=_h(tu),
                    json={"dice": "napcat", "arch": "standalone", "bot_mode": "onebot"})
    assert r.status_code not in (400, 403), r.text


def test_http_revoke_kills_session_immediately(client):
    ta = _tok(client, "admin", "admin-pwd-1")
    tu = _tok(client, "alice", "alice-pwd-1")
    assert client.get("/api/me", headers=_h(tu)).status_code == 200
    assert client.post("/api/admin/accounts/alice/revoke",
                       headers=_h(ta)).status_code == 200
    assert client.get("/api/me", headers=_h(tu)).status_code == 401


def test_http_quota_change_takes_effect_for_user(client):
    ta = _tok(client, "admin", "admin-pwd-1")
    tu = _tok(client, "alice", "alice-pwd-1")
    assert client.put("/api/admin/accounts/alice/quota", headers=_h(ta),
                      json={"login_qq": 9, "app": 9}).status_code == 200
    me = client.get("/api/me", headers=_h(tu)).json()
    assert me["quota"] == {"login_qq": 9, "app": 9}, me


def test_http_account_lifecycle(client):
    ta = _tok(client, "admin", "admin-pwd-1")
    assert client.post("/api/admin/accounts", headers=_h(ta),
                       json={"username": "bob", "password": "bob-pwd-1",
                             "role": "user", "quota": {"login_qq": 1, "app": 1}}
                       ).status_code == 200
    t = _tok(client, "bob", "bob-pwd-1")
    assert client.get("/api/me", headers=_h(t)).json()["usage"] == {"login_qq": 0, "app": 0}
    # 自助改密 → 旧 token 失效、新 token 可用
    r = client.post("/api/password", headers=_h(t),
                    json={"old_password": "bob-pwd-1", "new_password": "bob-pwd-2"})
    assert r.status_code == 200
    nt = r.json()["token"]
    assert client.get("/api/me", headers=_h(t)).status_code == 401
    assert client.get("/api/me", headers=_h(nt)).status_code == 200
    # 删除账号 → 会话失效；且不能删 admin
    assert client.delete("/api/admin/accounts/bob", headers=_h(ta)).status_code == 200
    assert client.get("/api/me", headers=_h(nt)).status_code == 401
    assert client.delete("/api/admin/accounts/admin",
                         headers=_h(ta)).status_code == 400


def test_http_logout_clears_session(client):
    ta = _tok(client, "admin", "admin-pwd-1")
    assert client.post("/api/logout", headers=_h(ta)).status_code == 200
    assert client.get("/api/me", headers=_h(ta)).status_code == 401


# ---------- instance_op 越权（2026-10-03 真机测试发现的 P0）----------

def test_http_instance_op_rejects_other_users_instance(client):
    """P0 回归：普通用户不得 start/stop/restart 他人实例。

    `instance_op` 此前拿到 user 后只做了 `_u(user)`，**从未调用归属校验**，
    而它是 15 个按 inst_id 操作的端点里唯一漏掉的一个——普通用户能直接
    停掉别人的骰子/登录端（DoS），或把别人未部署的实例拉起来。
    """
    tu = _tok(client, "alice", "alice-pwd-1")
    for op in ("start", "stop", "restart"):
        r = client.post(f"/api/instances/i-admin-app/{op}", headers=_h(tu))
        assert r.status_code == 404, f"{op} 未被归属校验拦住：{r.status_code} {r.text}"


def test_http_instance_op_unknown_op_does_not_leak_existence(client):
    """未知 op 也必须先过归属校验。

    若把 VALID_OPS 校验放在归属校验之前，普通用户会拿到
    「未知操作」400（说明实例 ID 存在）vs 越权实例的 404，
    拿它当实例存在性探测器。故断言两者都是同一份404。
    """
    import re
    tu = _tok(client, "alice", "alice-pwd-1")
    r_exist = client.post("/api/instances/i-admin-app/bogus-op", headers=_h(tu))
    r_absent = client.post("/api/instances/does-not-exist/bogus-op", headers=_h(tu))
    assert r_exist.status_code == r_absent.status_code == 404, (
        r_exist.status_code, r_absent.status_code)
    tpl = lambda r: re.sub(r":\s*\S+$", "", r.json()["detail"])   # noqa: E731
    assert tpl(r_exist) == tpl(r_absent) == "实例不存在", (r_exist.json(), r_absent.json())


def test_http_instance_op_allows_owner_and_admin(client):
    """修好后不能误伤：实例主人与管理员照常操作自己的实例。

    越权收口最怕把「自己管自己的实例」也拦住。这里用合法 op 打到注册表
    实际会走的分支上（stop 对未运行实例是幂等的），断言不是 404。
    """
    tu = _tok(client, "alice", "alice-pwd-1")
    ta = _tok(client, "admin", "admin-pwd-1")
    assert client.post("/api/instances/i-alice-app/stop",
                       headers=_h(tu)).status_code != 404
    assert client.post("/api/instances/i-alice-app/stop",
                       headers=_h(ta)).status_code != 404
    assert client.post("/api/instances/i-admin-app/stop",
                       headers=_h(ta)).status_code != 404
