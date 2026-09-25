"""REST 端点：向导 / 实例操作 / 快照查询 / 程序包管理 / 备份导入 / 扫描"""
import asyncio
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api.auth import require_auth
from api.context import ctx
from core import backup, scanner
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

class PasswordReq(BaseModel):
    old_password: str
    new_password: str

@router.post("/password")
def change_password(req: PasswordReq):
    """修改管理密码：需登录；成功后旧 token 全部作废，返回新 token。"""
    from api.auth import auth
    return {"token": auth.change_password(req.old_password, req.new_password)}

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

class LinkReq(BaseModel):
    login_ref: str | None = None         # None = 解除关联

@router.post("/instances/{inst_id}/link")
def link_login(inst_id: str, req: LinkReq):
    """总览页连接管理：建立/解除 骰子端 → 登录端 的关联（login_ref 即拓扑连线数据源）。

    兼容性由 manifest 的 compatible_login 声明驱动，后端不写程序名分支。
    """
    try:
        inst = ctx.registry.get(inst_id)
    except KeyError:
        raise HTTPException(404, f"实例不存在: {inst_id}") from None
    if req.login_ref:
        if req.login_ref == inst_id:
            raise HTTPException(400, "不能关联自己")
        try:
            target = ctx.registry.get(req.login_ref)
        except KeyError:
            raise HTTPException(404, f"登录端实例不存在: {req.login_ref}") from None
        ok_set = [d for d in (ctx.adapters[inst.dice][0].get("compatible_login") or [])
                  if d != "builtin"]
        if target.dice not in ok_set:
            raise HTTPException(400, f"{inst.dice} 不兼容登录端 {target.dice}"
                                     f"（兼容：{' / '.join(ok_set) or '无'}）")
    with instance_lock(inst_id):
        ctx.registry.update(inst_id, login_ref=req.login_ref)
    return {"ok": True}

@router.get("/instances/{inst_id}/webui")
def instance_webui(inst_id: str):
    """WebUI 直连信息：实际端口优先（占用时程序可能自动 +1 换端口），令牌从启动日志回读。

    前端用 location.hostname 拼完整 URL（服务与面板同机部署，浏览器到的是同一个主机）。
    """
    try:
        rec = ctx.registry.get(inst_id)
    except KeyError:
        raise HTTPException(404, f"实例不存在: {inst_id}") from None
    port = rec.actual_port or (rec.allocated_ports or {}).get("webui")
    return {"port": port, "token": rec.webui_token}

@router.get("/pending")
def list_pending():
    """未完成的中间态实例：管理器重启 / 断网后可经向导从这里继续。"""
    return [{"id": i.id, "dice": i.dice, "state": i.state, "dir": i.dir,
             "qq": i.qq, "login_ref": i.login_ref,
             "next_step": ctx.wizard.next_step(i.id)}
            for i in ctx.registry.resume_pending()]

def _start_instance(inst_id: str) -> str | None:
    """start/restart 与备份导入恢复运行的公共启动路径（须持 instance_lock 调用）。

    首启一次性动作（LLBot --update 等）+ 后端构建启动命令 + 开放 WebUI 端口。
    """
    inst = ctx.registry.get(inst_id)
    adapter = ctx.get_adapter(inst.dice)

    def runner(cmd, cwd, label):
        ctx.pm.run_once(inst_id, cmd, cwd, label=label)

    try:
        if adapter.prepare_start(inst, runner):
            ctx.registry.update(inst_id, first_run_done=True)
    except Exception:
        pass
    cmd = adapter.build_start_cmd(inst)
    try:
        note = adapter.expose_webui(inst)
    except Exception:
        note = None
    try:
        ctx.pm.launch(inst_id, cmd, inst.dir)
    except RuntimeError as e:                        # 已在运行等场景
        raise HTTPException(409, str(e)) from e
    return note

# 注意：本端点必须注册在 `/instances/{inst_id}/{op}` 之前，否则 "backup" 会被
# 当作 op 先匹配（FastAPI 按注册顺序路由，实测踩过）。
@router.post("/instances/{inst_id}/backup")
async def upload_backup(inst_id: str, request: Request):
    """导入实例备份（手册 §5 上传约定：原始字节流，非 multipart）。

    流程：原本在运行则先停止 → 流式接收压缩包（魔数识别 + 完整性校验）
    → 解压覆盖到实例目录 → 原本在运行的实例自动重启。
    覆盖语义：只覆盖包内出现的文件，不删除包外文件（core/backup.py 模块注释）。
    """
    try:
        ctx.registry.get(inst_id)
    except KeyError:
        raise HTTPException(404, f"实例不存在: {inst_id}") from None
    cl = request.headers.get("content-length")
    if cl and int(cl) > pkgstore.MAX_PKG_BYTES:
        raise HTTPException(413, f"压缩包超过大小上限（{pkgstore.MAX_PKG_BYTES // 1048576} MB）")
    tmp = pkgstore.pkg_dir().parent / f"restore-{inst_id}.tmp"
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
        with instance_lock(inst_id):
            was_running = ctx.pm.is_alive(inst_id)
            if was_running:
                ctx.pm.stop(inst_id)                    # 导入期间进程不得占用文件
            try:
                # 大包解压可能数十秒：放线程池，避免阻塞事件循环（WS 心跳等）
                info = await asyncio.to_thread(backup.restore_into, tmp,
                                               Path(ctx.registry.get(inst_id).dir))
            except ValueError as e:
                if was_running:
                    try: _start_instance(inst_id)       # 恢复失败也要拉回原状态
                    except HTTPException: pass
                raise HTTPException(400, str(e)) from e
            # 备份来自旧机器时，包内的互联配置指向旧登录端（如 127.0.0.1:12345），
            # 重启后必然 dial refused——重启前按本面板的 login_ref 重写互联配置。
            # instance_lock 是 RLock，wizard.run_step 内部再取同一把锁可重入。
            conn_rewrite_error = None
            inst = ctx.registry.get(inst_id)
            if inst.login_ref:
                try:
                    ctx.wizard.run_step(inst_id, 4, {})
                except Exception as e:                  # 重写失败不阻断导入
                    conn_rewrite_error = str(e)
            restart_error = None
            if was_running:
                try:
                    _start_instance(inst_id)
                except HTTPException as e:              # 启动失败不回滚导入，如实上报
                    restart_error = e.detail
            return {"ok": True, "restarted": was_running,
                    "restart_error": restart_error,
                    "conn_rewrite_error": conn_rewrite_error, **info}
    finally:
        tmp.unlink(missing_ok=True)

@router.post("/instances/{inst_id}/{op}")
def instance_op(inst_id: str, op: str):
    if op not in VALID_OPS:                              # 显式校验（assert 会被 -O 剥离）
        raise HTTPException(404, f"未知操作: {op}")
    with instance_lock(inst_id):
        webui_note = None
        if op in ("start", "restart"):
            if op == "restart":
                ctx.pm.stop(inst_id)
            webui_note = _start_instance(inst_id)
        else:
            ctx.pm.stop(inst_id)
    return {"ok": True, "webui_note": webui_note}

@router.delete("/instances/{inst_id}")
def delete_instance(inst_id: str, confirm: bool = False,
                    remove_dir: bool = False, keep_save: bool = True):
    """二次确认；remove_dir=true 时 keep_save 决定是否保留 config 存档目录。

    目录删除失败时**先于墓碑**抛 500（实例记录保留，用户可直接重试）——
    旧实现 rmtree(ignore_errors=True) 静默吞错且记录已删，失败即成无主孤儿目录。
    """
    if not confirm:
        raise HTTPException(400, "删除需二次确认 confirm=true")
    with instance_lock(inst_id):
        inst = ctx.registry.get(inst_id)
        ctx.pm.stop(inst_id)
        ctx.ports.release_owner(inst_id)                    # 释放该实例全部端口
        if remove_dir:
            base = Path(inst.dir)
            dir_errors: list[str] = []

            def _onerror(fn, p, exc):
                dir_errors.append(f"{p}: {exc}")

            if keep_save:
                # 保留存档：删除目录内除存档目录外的全部内容
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
                        shutil.rmtree(child, onerror=_onerror)
                    else:
                        child.unlink(missing_ok=True)
                # 存档内容也删光时，空壳目录一并清掉（否则留下一个看似残留的空目录）
                if not dir_errors and base.is_dir() and not any(base.iterdir()):
                    base.rmdir()
            else:
                shutil.rmtree(base, onerror=_onerror)
            if dir_errors:
                raise HTTPException(
                    500, "目录删除失败（实例未移除，可重试）："
                         + "; ".join(dir_errors[:3]))
        ctx.registry.remove(inst_id)                        # 全部成功后才墓碑
    return {"ok": True}


@router.get("/scan")
def scan_install_roots():
    """扫描安装根：一级目录与相关进程 vs 注册表比对，找出游离目录/进程。

    managed 自身目录跳过；orphan（目录名匹配程序名但无实例记录）提供删除；
    external（无关软件如 alist/containerd）只展示不提供删除，避免误伤。
    """
    owned = {r["dir"]: {"id": r["id"], "dice": r["dice"], "state": r["state"]}
             for r in ctx.registry.all()}
    names = list(ctx.adapters)
    roots = sorted({m.get("install_root", "/opt") for m, _ in ctx.adapters.values()})
    skip = [str(Path(__file__).resolve().parents[1])]       # 管理器自身安装目录
    return {"roots": roots,
            "dirs": scanner.scan_dirs(roots, owned, names, skip=skip),
            "procs": scanner.scan_procs(roots, owned, names, skip=skip)}


@router.delete("/scan/dir")
def delete_orphan_dir(path: str):
    """删除扫描出的游离目录。三重校验：在安装根下 + 非受保护目录 + 分类为 orphan。"""
    roots = sorted({m.get("install_root", "/opt") for m, _ in ctx.adapters.values()})
    protected = [r["dir"] for r in ctx.registry.all()]
    protected.append(str(Path(__file__).resolve().parents[1]))   # 管理器自身目录
    try:
        scanner.check_deletable(path, roots, protected, list(ctx.adapters))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    errors: list[str] = []
    shutil.rmtree(Path(path), onerror=lambda fn, ip, e: errors.append(f"{ip}: {e}"))
    if errors:
        raise HTTPException(500, "删除失败: " + "; ".join(errors[:3]))
    return {"ok": True, "path": str(Path(path))}


class KillReq(BaseModel):
    pid: int

@router.post("/scan/kill")
def kill_orphan_proc(req: KillReq):
    """结束扫描出的游离进程：exe/cwd 必须落在安装根下、不属于注册表实例、非管理器自身。"""
    import psutil
    try:
        p = psutil.Process(req.pid)
        exe, cwd = p.exe() or "", p.cwd() or ""
    except psutil.Error as e:
        raise HTTPException(404, f"进程不存在: {req.pid}") from e
    self_dir = str(Path(__file__).resolve().parents[1])
    if any(exe.startswith(d.rstrip("/") + "/") or cwd.startswith(d.rstrip("/") + "/")
           for d in ([r["dir"] for r in ctx.registry.all()] + [self_dir])):
        raise HTTPException(400, "进程属于注册表实例或管理器自身，禁止在此结束")
    roots = [m.get("install_root", "/opt") for m, _ in ctx.adapters.values()]
    if not any(exe.startswith(r.rstrip("/") + "/") or cwd.startswith(r.rstrip("/") + "/")
               for r in roots):
        raise HTTPException(400, "进程不在安装根目录下，拒绝操作")
    # external 软件（alist 等无关目录）的进程只展示，不提供结束——与目录侧口径一致
    names = list(ctx.adapters)
    if not (scanner.match_program_dir(exe, roots, names)
            or scanner.match_program_dir(cwd, roots, names)):
        raise HTTPException(400, "进程不属于任何已知程序目录，拒绝在此结束")
    p.kill()
    p.wait(timeout=10)
    return {"ok": True, "pid": req.pid}

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
