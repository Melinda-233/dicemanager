"""新向导第三步的端到端契约验证（真实 HTTP，独立进程，不经pytest/conftest）。

覆盖本轮改动风险最高的两条路径：
  A. 登录端流程：step2 部署 → step3 登录 → **step4 先写自身配置** → 反向关联应用端
     （应用端经 login_ref 继承登录端 conn_token，顺序错了两端 token 就不一致）
  B. 应用端流程：step2 → step3（external/none 自动跳过）→ step4 带 links → step5

不真跑二进制程序：deploy 用 fake adapter（返回 ok），互联配置写入落真实文件，
断言的是「两端落盘的 token 是否一致」——这正是互联最难排查的失败点。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

STATE = Path(tempfile.mkdtemp(prefix="dm_e2e_"))
os.environ.update(DM_STATE_DIR=str(STATE), DM_LOG_DIR=str(STATE / "logs"),
                  DM_LOCK_DIR=str(STATE / "locks"), DM_EDITION="server")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient          # noqa: E402
from api.app import app                             # noqa: E402
from adapters.base import WriteResult               # noqa: E402

PW = "e2e-password-123"
OK = []


def check(label, cond, extra=""):
    OK.append((label, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{(' — ' + str(extra)) if extra else ''}")


class FakeAdapter:
    """最小可用适配器：部署写一个标记文件，互联配置写JSON 便于断言 token。"""
    MANIFEST = {}

    def __init__(self, manifest):
        self.m = manifest

    def deploy(self, inst):
        d = Path(inst.dir); d.mkdir(parents=True, exist_ok=True)
        (d / ".deployed").write_text("ok", encoding="utf-8")
        return "ok"

    def configure_login(self, inst, credentials):
        return {"needs_login": False, "accounts": []}

    def write_conn_config(self, inst, mode, direction, addr, token, link_id=None):
        d = Path(inst.dir); d.mkdir(parents=True, exist_ok=True)
        # link_id 含 '|' 分隔符，Windows 文件名非法 —— 换成下划线再落盘
        safe = (link_id or "self").replace("|", "_").replace(":", "_")
        f = d / f"conn-{safe}.json"
        f.write_text(json.dumps({"mode": mode, "direction": direction,
                                 "addr": addr, "token": token}), encoding="utf-8")
        return WriteResult(ok=True, path=str(f))

    def gen_token(self):
        return "tok-" + os.urandom(4).hex()

    def list_accounts(self, inst):
        return []

    def detect_account(self, inst):
        return None

    def build_start_cmd(self, inst):
        return ["cmd", "/c", "exit", "0"]

    def prepare_start(self, inst, runner):
        return False

    def expose_webui(self, inst):
        return None


def main():
    import api.context as ctxmod
    ctx = ctxmod.ctx
    # 换成假适配器：清单仍用真的（角色划分/兼容矩阵必须按真清单校验）
    manifests = {n: m for n, (m, _) in ctx.adapters.items()}
    adapters = {}
    for n, (m, _) in ctx.adapters.items():
        #注册表里第二项必须是**类**（Wizard.get_adapter 会 cls(manifest) 实例化）
        FakeAdapter.MANIFEST = m
        adapters[n] = (m, type(f"Fake_{n}", (FakeAdapter,), {}))
    ctx.adapters = adapters
    ctx.wizard.adapters = adapters
    ctx.wizard._adapter_cache.clear()
    # install_root 指向临时目录，别往真目录里写
    for m, _ in adapters.values():
        m["install_root"] = str(STATE / "pkgs")
    (STATE / "pkgs").mkdir(parents=True, exist_ok=True)
    ctx.registry._path = STATE / "instances.json"

    c = TestClient(app)
    # Auth() 在 import 期就构造并生成了随机密码（server 版行为），直接改写它的内存态
    # 最省事：setup_password 会被 409挡（已初始化），删文件也不能让内存态同步回去。
    import api.auth as authmod
    (STATE / "auth.json").unlink(missing_ok=True)
    authmod.auth._data = authmod.auth._blank(PW)
    authmod.auth._initialized = True
    authmod.write_atomic(authmod.auth._file,
                         json.dumps(authmod.auth._data, ensure_ascii=False).encode("utf-8"))
    r = c.post("/api/login", json={"username": "admin", "password": PW})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    H = {"Authorization": f"Bearer {token}"}
    print(f"[auth] token ok, {len(manifests)} manifests\n")

    mans = c.get("/api/manifests", headers=H).json()
    login_progs = {o for m in mans.values()
                   for o in (m.get("compatible_login") or []) if o != "builtin"}
    app_progs = [n for n in mans if n not in login_progs]
    LOGIN_P = sorted(login_progs)[0]
    APP_P = sorted(app_progs)[0]
    # 挑一个确实把LOGIN_P 写进兼容矩阵的应用端
    APP_P = next(n for n in app_progs if LOGIN_P in (mans[n].get("compatible_login") or []))
    print(f"[矩阵] 登录端 {LOGIN_P} / 应用端 {APP_P}\n")

    def mk(dice, **kw):
        r = c.post("/api/instances", json={"dice": dice, **kw}, headers=H)
        assert r.status_code == 200, r.text
        return r.json()["id"]

    def step(i, n, payload=None):
        r = c.post(f"/api/instances/{i}/wizard", json={"step": n, "payload": payload or {}},
                   headers=H)
        assert r.status_code == 200, r.text
        return r.json()

    # ---------- A. 登录端 → 反向关联应用端 ----------
    print("[A] 登录端流程")
    lid = mk(LOGIN_P)
    step(lid, 2)                                     # 部署
    step(lid, 3, {"credentials": {}})                # 登录（fake 直接 needs_login=False）
    r4 = step(lid, 4, {})                # ★ 先写自身配置，生成 conn_token
    login_token = r4.get("token")
    check("登录端 step4 生成 conn_token", bool(login_token), login_token)

    aid = mk(APP_P)
    step(aid, 2)
    step(aid, 3, {"credentials": {}})                # external/none 自动过

    # 模拟向导 applyAppLinks：先 link，再对该应用端重写 step4
    r = c.post(f"/api/instances/{aid}/link",
               json={"links": [{"login_ref": lid, "account_qq": None}]}, headers=H)
    check("link 关联被接受", r.status_code == 200, r.text[:80])
    ra = step(aid, 4, {})
    check("应用端 step4 产出预览", bool(ra.get("preview")), ra.get("preview"))

    files = list((STATE / "pkgs" / APP_P).glob("conn-*.json"))
    check("应用端互联配置已落盘", len(files) == 1, files)
    if files:
        got = json.loads(files[0].read_text(encoding="utf-8"))
        check("★ 两端 token 一致（继承而非各自生成）",
              got["token"] == login_token, f"app={got['token']} login={login_token}")
        check("地址指向登录端分配端口", got["addr"].startswith("127.0.0.1:"), got["addr"])

    # 幂等：同 (login_ref, account) 重复关联不应产生第二条
    c.post(f"/api/instances/{aid}/link",
           json={"links": [{"login_ref": lid, "account_qq": None}]}, headers=H)
    step(aid, 4, {})
    check("重复关联不产生第二条 endpoint",
          len(list((STATE / "pkgs" / APP_P).glob("conn-*.json"))) == 1)

    # ---------- B. 应用端 → 关联登录端 → 启动 ----------
    print("\n[B] 应用端流程")
    bid = mk(APP_P, login_ref=lid)
    step(bid, 2)
    r3 = step(bid, 3, {"credentials": {}})
    check("应用端 step3 自动跳过登录", r3.get("skipped") or r3.get("needs_login") is False, r3)
    r4b = step(bid, 4, {"links": [{"login_ref": lid, "account_qq": None,
                                   "direction": "forward"}]})
    check("应用端带 links 的 step4 成功", r4b.get("result") == "ok", r4b.get("preview"))
    check("★ 创建时带 login_ref 的实例继承同一 token",
          r4b.get("token") == login_token, f"{r4b.get('token')} vs {login_token}")
    r5 = step(bid, 5, {})
    check("step5 启动", r5.get("result") == "ok", r5)

    # ---------- C. 跨用户/自关联防护仍然生效 ----------
    print("\n[C] 防护")
    r = c.post(f"/api/instances/{aid}/link", json={"login_ref": aid}, headers=H)
    check("不能关联自己", r.status_code == 400, r.status_code)

    # ---------- D. 登录方式能力暴露（扫码 / 账密二选一）----------
    print("\n[D] 登录方式能力")
    # 注意：本脚本把 ctx.adapters 换成了 FakeAdapter，桩的 login_modes 恒为 ["qrcode"]。
    # 能力声明必须按**真实适配器**验，否则这里测的是桩而不是产品行为。
    from adapters import load_registry
    real_adapters = load_registry(Path(__file__).resolve().parent.parent / "manifests")
    real_modes = {}
    for n, (m, cls) in real_adapters.items():
        a = cls(m)
        real_modes[n] = {"modes": a.login_modes(),
                         "protocols": [p["id"] for p in getattr(a, "LOGIN_PROTOCOLS", [])]}
    pwd_progs = sorted(n for n, v in real_modes.items() if "account" in v["modes"])
    for n in pwd_progs:
        print(f"  {n}: {real_modes[n]['modes']} 协议={real_modes[n]['protocols']}")
    check("每个程序都声明了 login_modes", all("modes" in v for v in real_modes.values()))
    check("login_modes 只含已知取值",
          all(set(v["modes"]) <= {"qrcode", "account"} for v in real_modes.values()))
    check("声明支持密码登录的程序都列出协议候选",
          all(real_modes[n]["protocols"] for n in pwd_progs), pwd_progs)

    # ---------- E. 密码登录：凭据真写进程序配置 ----------
    print("\n[E] 密码登录凭据落盘")
    check("存在支持密码登录的程序", bool(pwd_progs), pwd_progs or "清单里没有 account 能力")
    for P in pwd_progs:
        man, cls = real_adapters[P]
        adapter = cls(man)
        d = STATE / "pkgs" / f"pwd-{P}"
        d.mkdir(parents=True, exist_ok=True)
        (d / (man.get("config_path", "appsettings.json"))).write_text("{}", encoding="utf-8")
        from core.registry import Instance
        inst = Instance(id=f"t-{P}", dice=P, arch="standalone", dir=str(d), port=0)
        r = adapter.save_login_credentials(
            inst, {"qq": "10086", "password": "S3cret!", "protocol": "Windows"})
        cfg = adapter.read_json(adapter._config(inst))
        acc = cfg.get("Account") or cfg.get("account") or {}
        check(f"[{P}] 账号写入程序配置", str(acc.get("Uin") or acc.get("uin")) == "10086", acc)
        check(f"[{P}] ★ 密码写入程序配置（明文，程序自读）",
              (acc.get("Password") or acc.get("password")) == "S3cret!")
        check(f"[{P}] 声明需重启（凭据启动时读）", r.get("restart") is True, r)
        # 空密码不得覆盖已有凭据
        adapter.save_login_credentials(inst, {"qq": "10087", "password": ""})
        cfg2 = adapter.read_json(adapter._config(inst))
        acc2 = cfg2.get("Account") or cfg2.get("account") or {}
        check(f"[{P}] 空密码被拒（不覆盖已有凭据）",
              (acc2.get("Password") or acc2.get("password")) == "S3cret!",
              acc2.get("Password") or acc2.get("password"))
        # ★ 密码绝不能进管理器状态库（本实现只写程序配置，天然满足；此断言防回归）
        blob = str([x for x in ctx.registry.all()])
        check(f"[{P}] ★ 密码不进管理器状态库", "S3cret!" not in blob,
              "泄露！" if "S3cret!" in blob else "未泄露")

    # ---------- F. 列表接口回显 links（向导第三步靠它填勾选列表）----------
    insts = c.get("/api/instances", headers=H).json()
    ia = next(i for i in insts if i["id"] == aid)
    check("登录端反向关联后应用端 links 可见",
          any(l["login_ref"] == lid for l in ia.get("links") or []), ia.get("links"))

    # ---------- G. 删登录端级联解除 ----------
    c.delete(f"/api/instances/{lid}?confirm=true&remove_dir=false", headers=H)
    insts = c.get("/api/instances", headers=H).json()
    ia = next(i for i in insts if i["id"] == aid)
    check("删除登录端级联解除应用端 links", not ia.get("links"), ia.get("links"))

    bad = [l for l, ok in OK if not ok]
    print(f"\n{'=' * 56}\nE2E_WIZARD_{'FAIL' if bad else 'OK'}  "
          f"({sum(1 for _, ok in OK if ok)}/{len(OK)})")
    if bad:
        print("失败项: " + "; ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())