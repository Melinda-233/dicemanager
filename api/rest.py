"""REST 端点：向导 / 实例操作 / 快照查询 / 程序包管理 / 二次确认删除"""
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api.auth import require_auth
from api.context import ctx
from core import packages as pkgstore
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

@router.get("/pending")
def list_pending():
    """未完成的中间态实例：管理器重启 / 断网后可经向导从这里继续。"""
    return [{"id": i.id, "dice": i.dice, "state": i.state, "dir": i.dir,
             "qq": i.qq, "login_ref": i.login_ref,
             "next_step": ctx.wizard.next_step(i.id)}
            for i in ctx.registry.resume_pending()]

@router.post("/instances/{inst_id}/{op}")
def instance_op(inst_id: str, op: str):
    if op not in VALID_OPS:                              # 显式校验（assert 会被 -O 剥离）
        raise HTTPException(404, f"未知操作: {op}")
    with instance_lock(inst_id):
        inst = ctx.registry.get(inst_id)
        if op in ("start", "restart"):
            if op == "restart":
                ctx.pm.stop(inst_id)
            # 首启一次性动作（LLBot --update 等）：与向导 Step5 同一条路径
            def runner(cmd, cwd, label):
                ctx.pm.run_once(inst_id, cmd, cwd, label=label)
            try:
                if ctx.get_adapter(inst.dice).prepare_start(inst, runner):
                    ctx.registry.update(inst_id, first_run_done=True)
            except Exception:
                pass
            cmd = ctx.get_adapter(inst.dice).build_start_cmd(inst)   # 后端构建（安全）
            try:
                ctx.pm.launch(inst_id, cmd, inst.dir)
            except RuntimeError as e:                    # 已在运行等场景
                raise HTTPException(409, str(e)) from e
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
                # 保留存档：删除目录内除存档目录外的全部内容
                # （原实现语义颠倒：keep_save=true 反而把存档目录删了）
                # 存档目录名由 manifest 的 save_keep_dir 指定，缺省 config
                raw = ctx.adapters[inst.dice][0].get("save_keep_dir", "config")
                # 兼容三种声明：字符串（目录名）/ 数组（多个目录）/ true（用默认 config）
                if isinstance(raw, str):
                    keep_names = [raw]
                elif isinstance(raw, (list, tuple)):
                    keep_names = list(raw) or ["config"]
                else:
                    keep_names = ["config"]
                for child in base.iterdir():
                    if child.name in keep_names:
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
                                      "prerequisite", "delete_keeps_save")}
            for n, (m, _) in ctx.adapters.items()}

# ---------- 程序包管理：上传/列表/删除（部署时优先解压本地包，免在线下载） ----------

@router.get("/packages")
def list_packages():
    return pkgstore.list_archives()

@router.post("/packages/{dice}")
async def upload_package(dice: str, request: Request):
    """原始字节流上传（Content-Type: application/octet-stream）。

    不用 multipart：免 python-multipart 依赖；浏览器端直接 fetch(file) 即可。
    流式落盘临时文件再整体校验，避免大包整体读入内存。
    """
    if dice not in ctx.adapters:
        raise HTTPException(400, f"未知程序: {dice}")
    cl = request.headers.get("content-length")
    if cl and int(cl) > pkgstore.MAX_PKG_BYTES:
        raise HTTPException(413, f"压缩包超过大小上限（{pkgstore.MAX_PKG_BYTES // 1048576} MB）")
    tmp = pkgstore.archive_path(dice).with_suffix(".zip.tmp")
    total = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                total += len(chunk)
                if total > pkgstore.MAX_PKG_BYTES:
                    raise HTTPException(413, "压缩包超过大小上限")
                f.write(chunk)
        if not total:
            raise HTTPException(400, "空请求体")
        info = pkgstore.commit_archive(dice, tmp, source="upload")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        tmp.unlink(missing_ok=True)
    return info

@router.delete("/packages/{dice}")
def delete_package(dice: str):
    if not pkgstore.remove_archive(dice):
        raise HTTPException(404, "本地没有该程序包")
    return {"ok": True}

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
