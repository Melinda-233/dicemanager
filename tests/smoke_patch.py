"""修补项端到端冒烟：/api/pending、Step4 token 两端继承、互联配置落盘。"""
import json, os, sys, tempfile
from pathlib import Path

tmp = tempfile.mkdtemp(prefix="dm_smoke2_")
os.environ["DM_STATE_DIR"] = tmp
os.environ["DM_LOG_DIR"] = os.path.join(tmp, "logs")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 预置明文 auth.json（沿用既有迁移机制拿到已知密码）
Path(tmp, "auth.json").write_text(
    json.dumps({"password": "pwd-smoke-2", "token": "tok-smoke-2"}), encoding="utf-8")

from fastapi.testclient import TestClient
from api.app import app
from api.context import ctx
from core.registry import State

c = TestClient(app)
r = c.post("/api/login", json={"password": "pwd-smoke-2"})
assert r.status_code == 200, r.text
H = {"Authorization": "Bearer " + r.json()["token"]}

# 1) 创建登录端（napcat）与骰子端（sealdice，login_ref 指向 napcat）
r = c.post("/api/instances", json={"dice": "napcat", "arch": "standalone"}, headers=H)
assert r.status_code == 200, r.text
nap_id = r.json()["id"]
r = c.post("/api/instances", json={"dice": "sealdice", "arch": "standalone",
                                   "login_ref": nap_id}, headers=H)
assert r.status_code == 200, r.text
sea_id = r.json()["id"]
print("[1] 双实例创建 OK:", nap_id, "->", sea_id)

# 目录重定向到临时区（不真实部署，仅验证配置写入逻辑）
work = Path(tmp, "work"); work.mkdir()
for iid, sub in ((nap_id, "nap"), (sea_id, "sea")):
    ctx.registry.update(iid, dir=str(work / sub))

# 2) /api/pending：AWAIT_LOGIN 的登录端应出现，next_step=3
ctx.registry.transition(nap_id, State.AWAIT_LOGIN)
r = c.get("/api/pending", headers=H)
assert r.status_code == 200, r.text
pend = {p["id"]: p for p in r.json()}
assert nap_id in pend and pend[nap_id]["next_step"] == 3, pend
assert sea_id not in pend, "UNDEPLOYED 不应出现在续跑列表"
print("[2] /api/pending OK:", pend[nap_id]["next_step"])

# 3) Step4 token 继承：登录端先生成，骰子端经 login_ref 拿到同一个
r = c.post(f"/api/instances/{nap_id}/wizard", json={"step": 4, "payload": {}}, headers=H)
assert r.status_code == 200 and r.json()["result"] == "ok", r.text
tok_nap = r.json()["token"]
assert tok_nap, "登录端应自动生成 token"
# 登录端无 ob11 分配时用自身默认；napcat ob11 默认 3001，写进 websocketServers
nap_cfg = json.loads((work / "nap" / "config" / "onebot11.json").read_text("utf-8"))
sv = [e for e in nap_cfg["network"]["websocketServers"] if e["name"] == "dicemanager"]
assert len(sv) == 1 and sv[0]["token"] == tok_nap, nap_cfg

r = c.post(f"/api/instances/{sea_id}/wizard", json={"step": 4, "payload": {}}, headers=H)
assert r.status_code == 200 and r.json()["result"] == "ok", r.text
tok_sea = r.json()["token"]
assert tok_sea == tok_nap, f"两端 token 不一致: {tok_sea} != {tok_nap}"
assert "3001" in r.json()["preview"], r.json()   # 骰子端默认连登录端 ob11 端口
# 海豹侧落盘校验：正向 connectUrl 不带 /ws
sea_yaml = (work / "sea" / "data" / "default" / "serve.yaml").read_text("utf-8")
assert "connectUrl: ws://127.0.0.1:3001" in sea_yaml, sea_yaml
print("[3] Step4 token 继承 + 双端落盘 OK, token =", tok_nap[:6] + "…")

# 4) Step5 启动（本机无真实程序：NapCat.sh 不存在，应返回友好 error 而非 500）
r = c.post(f"/api/instances/{nap_id}/wizard", json={"step": 5, "payload": {}}, headers=H)
assert r.status_code == 200, r.text
body = r.json()
assert body["result"] == "error" and "启动失败" in body.get("message", ""), body
assert ctx.registry.get(nap_id).state == State.AWAIT_LOGIN.value   # 启动失败不迁状态
print("[4] Step5 缺失 exe 友好报错 OK:", body["message"])

# 5) 程序包端点：上传→列表→坏包拒绝→删除（部署优先解压本地包链路的入口）
import io as _io
import zipfile as _zf
from core import packages as pkgstore

def _zip(*names):
    b = _io.BytesIO()
    with _zf.ZipFile(b, "w") as z:
        for n in names:
            z.writestr(n, "x")
    return b.getvalue()

r = c.post("/api/packages/napcat", content=_zip("NapCat.sh"),
           headers={**H, "Content-Type": "application/octet-stream"})
assert r.status_code == 200 and r.json()["source"] == "upload", r.text
assert pkgstore.find_archive("napcat").exists()
r = c.post("/api/packages/napcat", content=b"garbage",
           headers={**H, "Content-Type": "application/octet-stream"})
assert r.status_code == 400, r.text                       # 坏包拒绝
assert pkgstore.find_archive("napcat").exists()           # 旧包未被顶掉
r = c.post("/api/packages/nope", content=_zip("a"), headers=H)
assert r.status_code == 400                               # 未知程序
r = c.get("/api/packages", headers=H)
assert "napcat" in [p["dice"] for p in r.json()]
assert c.delete("/api/packages/napcat", headers=H).status_code == 200
assert c.delete("/api/packages/napcat", headers=H).status_code == 404
print("[5] 程序包上传/列表/坏包拒绝/删除 OK")
print("SMOKE PATCH ALL OK")
