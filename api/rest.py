"""REST 端点：向导 / 实例操作 / 快照查询 / 二次确认删除"""
import shutil
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from api.context import ctx
from api.auth import require_auth
from core.locks import instance_lock

# 登录入口必须无鉴权（原实现挂在带鉴权的 router 下，永远拿不到 token）
public = APIRouter(prefix="/api")
router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

VALID_OPS = ("start", "stop", "restart")

class LoginReq(BaseModel):
    password: str

class CreateReq(BaseModel):
    dice: str
    arch: str = "standalone"
    login_ref: str | None = None
    confirm_dir: bool = False            # 同名冲突「直接使用」确认

class StepReq(BaseModel):
    step: int
    payload: dict = {}

@public.post("/login")
def login(req: LoginReq):
    from api.auth import auth
    return {"token": auth.login(req.password)}

@router.post("/instances")
def create_instance(req: CreateReq):
    if req.dice not in ctx.adapters:
        raise HTTPException(400, f"未知程序: {req.dice}")
    iid = ctx.wizard.create_instance(req.dice, req.arch,
                                     login_ref=req.login_ref, confirm_dir=req.confirm_dir)
    inst = ctx.registry.get(iid)
    return {"id": iid, "dir": inst.dir, "allocated_ports": inst.allocated_ports}

@router.post("/instances/{inst_id}/wizard")
def run_wizard_step(inst_id: str, req: StepReq):
    return ctx.wizard.run_step(inst_id, req.step, req.payload)   # ok/conflict 统一

@router.get("/instances")
def list_instances():
    return [{**r, "process": ctx.pm.get(r["id"]).probe()} for r in ctx.registry.all()]

@router.post("/instances/{inst_id}/{op}")
def instance_op(inst_id: str, op: str):
    if op not in VALID_OPS:                              # 显式校验（assert 会被 -O 剥离）
        raise HTTPException(404, f"未知操作: {op}")
    with instance_lock(inst_id):
        inst = ctx.registry.get(inst_id)
        if op in ("start", "restart"):
            if op == "restart":
                ctx.pm.stop(inst_id)
            cmd = ctx.get_adapter(inst.dice).build_start_cmd(inst)   # 后端构建（安全）
            try:
                ctx.pm.launch(inst_id, cmd, inst.dir)
            except RuntimeError as e:                    # 已在运行等场景
                raise HTTPException(409, str(e))
        else:
            ctx.pm.stop(inst_id)
    return {"ok": True}

@router.delete("/instances/{inst_id}")
def delete_instance(inst_id: str, confirm: bool = False,
                    remove_dir: bool = False, keep_save: bool = True):
    """二次确认；remove_dir=true 时 keep_save 决定是否保留 config 存档目录。"""
    if not confirm:
        raise HTTPException(400, "删除需二次确认 confirm=true")
    with instance_lock(inst_id):
        inst = ctx.registry.get(inst_id)
        ctx.pm.stop(inst_id)
        ctx.ports.release_owner(inst_id)                    # 释放该实例全部端口
        ctx.registry.remove(inst_id)                        # 墓碑式删除
        if remove_dir:
            base = Path(inst.dir)
            if keep_save:
                # 保留存档：删除目录内除 config 外的全部内容
                # （原实现语义颠倒：keep_save=true 反而把存档目录删了）
                for child in base.iterdir():
                    if child.name == "config":
                        continue
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
            else:
                shutil.rmtree(base, ignore_errors=True)
    return {"ok": True}

@router.get("/manifests")
def list_manifests():
    """Step1 组合选项与兼容矩阵。"""
    return {n: {k: m.get(k) for k in ("arch", "multi_account", "login_type",
                                      "compatible_login", "recommended_protocols",
                                      "webui_default_port", "ob11_default_port",
                                      "approx_memory_mb", "auth_token_conditional",
                                      "prerequisite")}
            for n, (m, _) in ctx.adapters.items()}

@router.get("/resmon")
def resmon_snapshot():
    import psutil
    vm = psutil.virtual_memory()
    ratio = vm.used / vm.total
    return {"total_mb": vm.total // 1048576, "used_mb": vm.used // 1048576,
            "ratio": ratio,
            "per_instance": {r["id"]: ctx.pm.get(r["id"]).probe()
                             for r in ctx.registry.all()},
            "alert": ratio >= ctx.resmon_alert}

@router.get("/logs/{inst_id}/download")
def download_log(inst_id: str):
    p = ctx.log_dir / f"{inst_id}.log"
    if not p.exists():
        raise HTTPException(404, "日志不存在")
    return FileResponse(p, filename=f"{inst_id}.log")
