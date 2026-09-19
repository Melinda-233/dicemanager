"""本地冒烟：DM_STATE_DIR 覆盖、明文密码迁移、登录限速、清单加载、tombstone 清理"""
import json, os, sys, tempfile, time
from pathlib import Path

tmp = tempfile.mkdtemp(prefix="dm_smoke_")
os.environ["DM_STATE_DIR"] = tmp
os.environ["DM_LOG_DIR"] = os.path.join(tmp, "logs")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 本地无 fcntl：打桩跳过跨进程文件锁（真实文件锁在 Linux 服务器上验证）
if os.name != "posix":
    import types
    _stub = types.ModuleType("fcntl")
    _stub.LOCK_EX, _stub.LOCK_UN = 2, 8
    _stub.flock = lambda *a, **k: None
    sys.modules["fcntl"] = _stub

# 1) 旧版明文 auth.json 自动迁移
legacy = {"password": "test-pwd-123", "token": "tok-abc"}
Path(tmp, "auth.json").write_text(json.dumps(legacy), encoding="utf-8")

from api.auth import Auth
a = Auth(Path(tmp, "auth.json"))
d = json.loads(Path(tmp, "auth.json").read_text())
assert "password" not in d and "password_hash" in d and d["salt"], "明文未迁移为哈希"
assert a.login("test-pwd-123") == "tok-abc", "迁移后原密码应可登录"
try:
    a.login("wrong")
    raise SystemExit("错误密码竟然通过")
except Exception as e:
    assert getattr(e, "status_code", None) == 401
for _ in range(4):                       # 已 1 次失败，再 4 次触发限速
    try: a.login("wrong")
    except Exception: pass
try:
    a.login("test-pwd-123")
    raise SystemExit("限速未生效")
except Exception as e:
    assert getattr(e, "status_code", None) == 429, f"期望 429，得到 {e}"
print("[1] auth 迁移 + 登录 + 限速 OK")

# 2) context：环境变量路径生效 + 清单加载
from api.context import ctx
assert ctx.log_dir == Path(tmp, "logs"), ctx.log_dir
assert len(ctx.adapters) == 5, ctx.adapters.keys()
print("[2] context 环境变量路径 + 5 个清单加载 OK")

# 3) registry：create/transition/update/remove/purge 全链路
from core.registry import State
iid = "sealdice-test01"
ctx.registry.create(iid, dice="sealdice", arch="standalone", dir_="/tmp/x", port=3000)
ctx.registry.transition(iid, State.DEPLOYING)
ctx.registry.transition(iid, State.AWAIT_LOGIN)
ctx.registry.update(iid, warnings=["缺件告警应落盘"], actual_port=3001)
assert ctx.registry.get(iid).warnings == ["缺件告警应落盘"]
assert ctx.registry.get(iid).actual_port == 3001
ctx.registry.remove(iid)
try:
    ctx.registry.get(iid)
    raise SystemExit("删除后 get 应抛 KeyError")
except KeyError:
    pass
assert not [r for r in ctx.registry.all() if r["id"] == iid]
ctx.registry.purge_tombstones(days=0)    # 立即清空墓碑
raw = json.loads(Path(tmp, "instances.json").read_text())
assert not [k for k in raw if k.startswith("__tombstone__")], "墓碑未清理"
print("[3] registry 状态机 + 墓碑清理 OK")

# 4) ports：分配/释放/owner 批量释放
p = ctx.ports.allocate_many("sealdice", {"webui": 3080, "ob11": 3001}, owner=iid)
assert set(p) == {"webui", "ob11"}
ctx.ports.release_owner(iid)
tbl = json.loads(Path(tmp, "ports.json").read_text())
assert not [k for k, v in tbl.items() if str(v).startswith(iid)]
print("[4] ports 分配与 owner 释放 OK")

# 5) wizard step5：已运行进程 → error 结果而非 500
ctx.registry.create("sealdice-test02", dice="sealdice", arch="standalone",
                    dir_="/tmp/y", port=3000)
class FakeProc:
    def is_alive(self): return False
    def start(self, *a, **k): raise RuntimeError("进程已在运行")
orig = ctx.pm.get
ctx.pm.get = lambda _: FakeProc()
r = ctx.wizard.run_step("sealdice-test02", 5, {})
ctx.pm.get = orig
assert r == {"result": "error", "message": "进程已在运行"}, r
print("[5] wizard step5 RuntimeError 兜底 OK")

print("SMOKE_ALL_OK")
