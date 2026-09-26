"""REST 端点：向导 / 实例操作 / 快照查询 / 程序包管理 / 备份导入 / 扫描"""
import asyncio
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from api.auth import require_auth
from api.context import ctx
from core import backup, exports, scanner
from core import packages as pkgstore
from core.locks import instance_lock
from core.registry import State

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

    全部委托 Wizard.start_instance（与向导 step5 同一实现，杜绝双入口漂移）；
    进程已在运行等 RuntimeError 映射为 409。
    """
    try:
        note, _already = ctx.wizard.start_instance(inst_id)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return note

# 注意：本端点必须注册在 `/instances/{inst_id}/{op}` 之前，否则 "backup" 会被
# 当作 op 先匹配（FastAPI 按注册顺序路由，实测踩过）。export/diagnose/upgrade 同理。


def _inst_or_404(inst_id: str):
    try:
        return ctx.registry.get(inst_id)
    except KeyError:
        raise HTTPException(404, f"实例不存在: {inst_id}") from None


def _data_paths_of(dice: str) -> list[str] | None:
    return ctx.adapters[dice][0].get("data_paths")


@router.get("/instances/{inst_id}/export")
async def export_instance(inst_id: str, scope: str = "full"):
    """导出实例备份（与「上传备份导入」对称）。

    两种口径严格区分：
    - full 整目录备份：程序+配置+存档+数据全量，恢复即得完整实例；
    - data 应用数据备份：仅 manifest data_paths 声明的数据/存档（对应应用端
      自身备份功能覆盖的局部数据），体积小、跨版本可移植，但恢复前提是
      实例已部署同版本程序。
    """
    rec = _inst_or_404(inst_id)
    if scope not in ("full", "data"):
        raise HTTPException(400, f"未知备份口径: {scope}")
    out_dir = exports.exports_dir()
    name = f"{rec.dice}-{inst_id}-{scope}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    out = out_dir / name
    try:
        info = await asyncio.to_thread(
            backup.export_dir, Path(rec.dir), out, scope, _data_paths_of(rec.dice))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        out.unlink(missing_ok=True)
        raise HTTPException(500, f"打包失败: {e}") from e
    # 发送完毕后台删除临时包（实例目录可能上 GB，不常驻磁盘）
    return FileResponse(out, filename=name, media_type="application/gzip",
                        background=BackgroundTask(os.unlink, out),
                        headers={"X-Export-Files": str(info["files"])})


@router.get("/instances/{inst_id}/diagnose")
def diagnose_instance(inst_id: str):
    """互联诊断器（拓展2）：按原因链逐层检测，返回结构化检查项。

    通用层（进程→关联→端口→token）在此统一实现；本端配置文件层由适配器
    diagnose_conn 钩子补充（如海豹读 serve.yaml 端点 state）。
    """
    rec = _inst_or_404(inst_id)
    items: list[dict] = []

    def add(ok: bool, step: str, detail: str):
        items.append({"ok": ok, "step": step, "detail": detail})

    alive = ctx.pm.is_alive(inst_id)
    add(alive, "process",
        "本端进程运行中" if alive else "本端进程未运行（先启动实例）")
    if not rec.login_ref:
        add(False, "ref", "未关联登录端（总览侧栏「关联登录端」）")
    else:
        add(True, "ref", f"已关联登录端 {rec.login_ref}")
        try:
            login = ctx.registry.get(rec.login_ref)
            lalive = ctx.pm.is_alive(rec.login_ref)
            add(lalive, "login_process",
                "登录端进程运行中" if lalive else "登录端进程未运行（登录端连不上任何人）")
            port = (login.allocated_ports or {}).get("ob11")
            if port:
                reachable = ctx.get_adapter(rec.dice).tcp_probe("127.0.0.1", port)
                add(reachable, "port",
                    f"登录端 ob11 端口 {port} {'可连通' if reachable else '未监听（登录端未启动/端口没开）'}")
            else:
                add(False, "port", "登录端未分配 ob11 端口，无法建立 OneBot 连接")
            if rec.conn_token and login.conn_token:
                same = rec.conn_token == login.conn_token
                add(same, "token",
                    "两端互联 token 一致" if same
                    else "两端互联 token 不一致（历史遗留），请「重写互联配置」对齐")
            else:
                add(False, "token",
                    "互联 token 缺失（本端或登录端尚未写互联配置），请「重写互联配置」")
        except KeyError:
            add(False, "ref", f"关联的登录端 {rec.login_ref} 已不存在，请重新关联")
    # 本端配置层（适配器专属，如海豹 serve.yaml 端点 state）
    try:
        ns = SimpleNamespace(**{k: rec.__dict__.get(k) for k in
                                ("id", "dice", "dir", "allocated_ports", "actual_port",
                                 "conn_token", "conn_addr", "conn_direction")})
        items.extend(ctx.get_adapter(rec.dice).diagnose_conn(ns))
    except Exception as e:                              # 诊断自身出错要可见，不能静默
        add(False, "adapter", f"适配器诊断异常: {e}")
    return {"ok": all(i["ok"] for i in items), "items": items}


@router.get("/instances/{inst_id}/upgrade-check")
def upgrade_check(inst_id: str):
    """升级通道：比对当前部署版本（release tag）与上游最新版本。"""
    rec = _inst_or_404(inst_id)
    adapter = ctx.get_adapter(rec.dice)
    strat = ctx.adapters[rec.dice][0].get("download_strategy")
    if strat == "manual":
        return {"supported": False,
                "message": "该程序离线分发（上游不发完整包）：请上传新程序包后用「备份导入」覆盖升级"}
    if strat == "direct":
        return {"supported": False,
                "message": "该程序为固定直链下载，无法判定版本；可直接点「升级」用最新包覆盖"}
    try:
        latest = adapter.latest_tag()
    except Exception as e:
        raise HTTPException(502, f"获取上游版本失败: {e}") from e
    return {"supported": True, "current": rec.version, "latest": latest,
            "up_to_date": rec.version is not None and rec.version == latest}


@router.post("/instances/{inst_id}/upgrade")
def upgrade_instance(inst_id: str):
    """原地升级：自动整目录备份 → 停机 → 覆盖解压最新包（数据文件保留）→ 恢复运行。"""
    rec = _inst_or_404(inst_id)
    adapter = ctx.get_adapter(rec.dice)
    strat = ctx.adapters[rec.dice][0].get("download_strategy")
    if strat == "manual":
        raise HTTPException(400, "该程序离线分发，请上传新程序包后用「备份导入」覆盖升级")
    with instance_lock(inst_id):
        was_running = ctx.pm.is_alive(inst_id)
        if was_running:
            ctx.pm.stop(inst_id)
        # 升级前强制整目录备份：升级失败可整体回退
        bak_dir = exports.exports_dir()
        bak = bak_dir / f"{rec.dice}-{inst_id}-preupgrade-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
        backup.export_dir(Path(rec.dir), bak, scope="full")
        try:
            tag = adapter.upgrade(rec)
            ctx.registry.update(inst_id, version=tag)
        except Exception as e:
            restart_error = None
            if was_running:
                try:
                    _start_instance(inst_id)
                except Exception as re:             # 拉回失败也要如实告知
                    restart_error = str(re)
            raise HTTPException(
                500, f"升级失败（已保留升级前备份 {bak.name}）: {e}"
                     + (f"；实例拉回失败: {restart_error}" if restart_error else "")) from e
        restart_error = None
        if was_running:
            try:
                _start_instance(inst_id)
            except HTTPException as e:
                restart_error = e.detail
    return {"ok": True, "version": tag, "backup": bak.name, "restarted": was_running,
            "restart_error": restart_error}
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
            # 状态落回 CONFIGURED：此前停在 RUNNING，与「进程已死」的事实矛盾（总览灰
            # 节点却标 RUNNING）。RUNNING→CONFIGURED 是状态机合法迁移；其他状态不动。
            try:
                if ctx.registry.get(inst_id).state == State.RUNNING.value:
                    ctx.registry.transition(inst_id, State.CONFIGURED)
            except Exception:
                pass
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
        # 级联解除引用：否则骰子端 login_ref 悬空——总览连线消失、向导 step4 重写时会
        # 静默生成新 token 与登录端残留配置不一致（两端连不上且难排查）
        unlinked = [r["id"] for r in ctx.registry.all() if r.get("login_ref") == inst_id]
        for other in unlinked:
            ctx.registry.update(other, login_ref=None)
        ctx.registry.remove(inst_id)                        # 全部成功后才墓碑
    # 日志文件随实例一起走 + 释放空壳 ManagedProcess：旧实现只除名不删日志，
    # 反复增删会累积一批查不到归属的孤儿日志
    removed_logs = ctx.pm.remove_logs(inst_id)
    return {"ok": True, "unlinked": unlinked, "removed_logs": removed_logs}


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


def _norm_dirs(dirs: list[str]) -> list[str]:
    """realpath 归一化 + 统一尾分隔符：防符号链接 / `..` / 尾斜杠绕过前缀比对。"""
    return [os.path.realpath(str(d)).rstrip(os.sep) + os.sep for d in dirs]


def _under(path: str, dirs: list[str]) -> bool:
    if not path:
        return False
    rp = os.path.realpath(path)
    return any(rp.startswith(d) for d in dirs)


@router.post("/scan/kill")
def kill_orphan_proc(req: KillReq):
    """结束扫描出的游离进程：exe/cwd 必须落在安装根下、不属于注册表实例、非管理器自身。"""
    import psutil
    try:
        p = psutil.Process(req.pid)
        exe, cwd = p.exe() or "", p.cwd() or ""
    except psutil.Error as e:
        raise HTTPException(404, f"进程不存在: {req.pid}") from e
    protected = _norm_dirs([r["dir"] for r in ctx.registry.all()]
                           + [str(Path(__file__).resolve().parents[1])])
    if _under(exe, protected) or _under(cwd, protected):
        raise HTTPException(400, "进程属于注册表实例或管理器自身，禁止在此结束")
    raw_roots = [m.get("install_root", "/opt") for m, _ in ctx.adapters.values()]
    if not (_under(exe, _norm_dirs(raw_roots)) or _under(cwd, _norm_dirs(raw_roots))):
        raise HTTPException(400, "进程不在安装根目录下，拒绝操作")
    # external 软件（alist 等无关目录）的进程只展示，不提供结束——与目录侧口径一致
    names = list(ctx.adapters)
    if not (scanner.match_program_dir(exe, raw_roots, names)
            or scanner.match_program_dir(cwd, raw_roots, names)):
        raise HTTPException(400, "进程不属于任何已知程序目录，拒绝在此结束")
    p.kill()
    p.wait(timeout=10)
    return {"ok": True, "pid": req.pid}


@router.get("/deploy-progress/{inst_id}")
def deploy_progress(inst_id: str):
    """部署进度快照（step2 同步部署期间前端 1s 轮询）：下载字节数 / 解压阶段。"""
    from adapters.base import deploy_progress_of
    return deploy_progress_of(inst_id)

@router.get("/manifests")
def list_manifests():
    """Step1 组合选项与兼容矩阵。

    白名单只放前端真正消费的字段：multi_account / recommended_protocols 曾在此透出
    但前端从不读取（手册 §6.2 记为「文档性字段」），透出只会误导后来者以为有用。
    """
    return {n: {k: m.get(k) for k in ("arch", "login_type",
                                      "compatible_login",
                                      "webui_default_port", "ob11_default_port",
                                      "approx_memory_mb", "auth_token_conditional",
                                      "prerequisite", "delete_keeps_save")}
            for n, (m, _) in ctx.adapters.items()}

# ---------- 程序包管理：上传/列表/删除（部署时优先解压本地包，免在线下载） ----------

@router.get("/packages")
def list_packages():
    """列出本地程序包缓存，并标注是否有实例在用。

    包下载/上传后永久驻留（无 TTL），长期会累积一批「下载过但从未部署」的死缓存；
    in_use 让前端能据此区分，避免用户误删正在使用的包。
    """
    used = {r["dice"] for r in ctx.registry.all()}
    rows = pkgstore.list_archives()
    for r in rows:
        r["in_use"] = r["dice"] in used
    return rows

# 必须注册在 @router.delete("/packages/{dice}") 之前：否则 "unused" 会被当成 dice 名
# 匹配到删除单个包的端点上（同源坑见 /instances/{id}/backup 与 /{op}）。
@router.delete("/packages/unused")
def delete_unused_packages():
    """清理没有任何实例在用的程序包缓存（回收磁盘），返回清理清单与释放量。

    删除不影响已部署实例——只在下次部署该程序时重新走在线下载（或重新上传）。
    """
    used = {r["dice"] for r in ctx.registry.all()}
    removed, freed = [], 0
    for row in pkgstore.list_archives():
        if not row.get("exists") or row["dice"] in used:
            continue
        # 用真实字节数而非 info_of 的 size_mb（后者保留一位小数，小包会被记成 0.0）
        try:
            p = pkgstore.find_archive(row["dice"])
            size = p.stat().st_size if p else 0
        except OSError:
            size = 0
        if pkgstore.remove_archive(row["dice"]):
            removed.append(row["dice"]); freed += size
    return {"ok": True, "removed": removed, "freed_mb": round(freed / 1048576, 2)}

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

# ---------- 备份产物回收：手动导出 / 升级前快照 / 定时备份都落在 exports/ ----------
# 与程序包缓存同源问题：产物只增不减，此前没有任何可见性与回收入口，
# 长期会在小内存机器上堆出几百 MB（升级一次一份整目录快照）。

@router.get("/exports")
def list_exports():
    """备份产物清单（名称 / 大小 / 修改时间 / 已存放天数）。"""
    return exports.list_exports()


# 必须注册在 /exports/{name} 之前：否则 "prune" 会被当成文件名匹配走
# （同源坑见 /packages/unused 与 /packages/{dice}）。
@router.delete("/exports/prune")
def prune_exports(days: int = exports.DEFAULT_KEEP_DAYS):
    """清理超过 days 天未修改的备份，返回删除清单与释放量。"""
    if days < 1:
        raise HTTPException(400, "保留天数需 ≥ 1")
    try:
        removed, freed = exports.prune_exports(days)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "removed": removed, "freed_mb": round(freed / 1048576, 2)}


@router.delete("/exports/{name}")
def delete_export(name: str):
    if not exports.remove_export(name):
        raise HTTPException(404, "备份文件不存在")
    return {"ok": True}


@router.get("/logs/{inst_id}/download")
def download_log(inst_id: str):
    p = ctx.log_dir / f"{inst_id}.log"
    if not p.exists():
        raise HTTPException(404, "日志不存在")
    return FileResponse(p, filename=f"{inst_id}.log")


# ---------- 定时任务（拓展7）：每日定时重启 / 定时备份 ----------

@router.get("/schedules")
def list_schedules():
    recs = {r["id"]: r for r in ctx.registry.all()}
    return [{"inst_dice": recs.get(t["inst_id"], {}).get("dice", "(已删除)"), **t}
            for t in ctx.scheduler.list_all()]


class SchedReq(BaseModel):
    inst_id: str
    kind: str                                    # restart | backup
    hh: int
    mm: int
    scope: str = "data"                          # backup 专用：full | data
    keep: int = 7                                # backup 专用：保留份数


@router.post("/schedules")
def add_schedule(req: SchedReq):
    _inst_or_404(req.inst_id)
    try:
        return ctx.scheduler.add(req.inst_id, req.kind, req.hh, req.mm,
                                 scope=req.scope, keep=req.keep)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/schedules/{task_id}")
def del_schedule(task_id: str):
    if not ctx.scheduler.remove(task_id):
        raise HTTPException(404, f"任务不存在: {task_id}")
    return {"ok": True}


@router.post("/schedules/{task_id}/run")
def run_schedule(task_id: str):
    """手动立即执行（补跑/测试）；执行后记 last_day，今晚到点不再重复跑。"""
    try:
        return ctx.scheduler.run_now(task_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


# ---------- 日志聚合检索（拓展10） ----------

SEARCH_TAIL_BYTES = 2 * 1024 * 1024           # 磁盘日志只扫尾部 2MB，防大文件拖垮


@router.get("/logs/search")
def logs_search(q: str, inst_id: str | None = None, limit: int = 100):
    """跨实例日志检索：ring（最近 2000 行）+ 磁盘日志尾部（各 2MB）。

    仅面板单管理员使用，正则元字符按字面处理（fnmatch 无关，直接 in 匹配）。
    """
    q = (q or "").strip()
    if len(q) < 2:
        raise HTTPException(400, "搜索词至少 2 个字符")
    limit = max(1, min(limit, 300))
    results: list[dict] = []
    for rec in ctx.registry.all():
        if inst_id and rec["id"] != inst_id:
            continue
        seen: set[str] = set()
        proc = ctx.pm.get(rec["id"])
        for _, line in proc.ring:
            if q.lower() in line.lower() and line not in seen:
                seen.add(line)
                results.append({"instance": rec["id"], "dice": rec["dice"],
                                "line": line, "src": "mem"})
        disk = ctx.log_dir / f"{rec['id']}.log"
        if disk.exists():
            try:
                size = disk.stat().st_size
                with open(disk, "rb") as f:
                    if size > SEARCH_TAIL_BYTES:
                        f.seek(size - SEARCH_TAIL_BYTES)
                    text = f.read().decode("utf-8", "replace")
                for line in text.splitlines()[1:]:      # 首行可能被截半，丢弃
                    if q.lower() in line.lower() and line not in seen:
                        seen.add(line)
                        results.append({"instance": rec["id"], "dice": rec["dice"],
                                        "line": line, "src": "disk"})
            except OSError:
                pass
        if len(results) >= limit * 3:                   # 收集超量即止（还要排序截断）
            break
    return {"q": q, "total": len(results),
            "results": results[:limit]}
