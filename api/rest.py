"""REST 端点：向导 / 实例操作 / 快照查询 / 程序包管理 / 备份导入 / 扫描"""
import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel
from starlette.background import BackgroundTask

from api.auth import (
    CurrentUser,
    admin_user,
    auth,
    current_user,
    require_admin,
    require_auth,
    security,
)
from api.context import ctx
from core import backup, exports, quota, scanner
from core import packages as pkgstore
from core.edition import EDITION, is_desktop, is_server
from core.locks import instance_lock, quota_lock
from core.registry import State
from core.roles import is_login_program

# 登录入口必须无鉴权（原实现挂在带鉴权的 router 下，永远拿不到 token）
public = APIRouter(prefix="/api")
router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
# 账号/配额管理：需登录 + 管理员位。与 router 分开是因为普通用户可访问其余全部只读/自助端点。
# 两个依赖都要挂：require_auth 负责把 CurrentUser 写进 request.state.user（require_admin
# 只从state 读，不自己解析 token），少挂一个就变成恒 401。顺序即执行顺序，不可颠倒。
admin_router = APIRouter(prefix="/api/admin",
                         dependencies=[Depends(require_auth), Depends(require_admin)])

VALID_OPS = ("start", "stop", "restart")

class LoginReq(BaseModel):
    username: str = "admin"              # 省略即 admin：兼容单管理员旧语义
    password: str

class CreateReq(BaseModel):
    dice: str
    arch: str = "standalone"
    login_ref: str | None = None
    confirm_dir: bool = False            # 同名冲突「直接使用」确认
    bot_mode: str = "onebot"             # onebot=需登录端；official=官方机器人通道（无需登录端）

class StepReq(BaseModel):
    step: int
    payload: dict = {}

@public.post("/login")
def login(req: LoginReq, request: Request):
    """按用户名 + 密码登录，返回 token 与身份信息（前端据此渲染角色化菜单）。"""
    client = request.client.host if request.client else ""
    token = auth.login(req.password, req.username, client=client)
    me = auth.current(token)
    if me is None:                       # 刚签发的 token 必然有效；防御性兜底
        raise HTTPException(401, "用户名或密码错误")
    return {"token": token, "username": me.username, "role": me.role,
            "display_name": me.display_name, "quota": me.quota}

class SetupReq(BaseModel):
    new_password: str

@public.get("/edition")
def edition():
    """当前产品形态：server（Linux 服务器）/ desktop（Windows 单机本地）。

    前端据此决定渲染哪些分化功能（面板重启按钮、首启设置界面等），
    避免前端用 UA 或路径各猜各的。
    """
    return {"edition": EDITION}

@public.get("/needs-setup")
def needs_setup():
    """前端启动时探测：needs_setup=True 表示尚未设置管理密码，登录页切到「设置管理密码」。

    判定依据是**磁盘上 auth.json 的实时状态**（is_initialized 内部先 _refresh），
    而非进程启动时的快照——运行中删除 auth.json 也会立刻返回 True。

    分化 C2：仅 desktop 版有首启设置流程；server 版首启自动生成密码
    （安装脚本依赖控制台 [auth] 行），故恒返回 False。
    """
    if not is_desktop():
        return {"needs_setup": False}
    return {"needs_setup": not auth.is_initialized}

@public.post("/setup")
def setup(req: SetupReq):
    """首次设置管理密码：仅未初始化时可用，已初始化返 409。

    替代早期 launcher 弹原生密码框的方案——走 WebUI 更直观、可移植，
    不依赖 win32gui（避免精简版 Windows 缺 GUI 子系统时弹窗失败）。

    分化 C2：仅 desktop 版提供。
    """
    if not is_desktop():
        raise HTTPException(404, "首次设置流程仅 desktop 版提供")
    return {"token": auth.setup_password(req.new_password)}

@public.post("/logout")
def logout(cred: HTTPAuthorizationCredentials | None = Depends(security)):
    """登出：吊销当前会话。失败不阻塞——前端本来就会清本地 token。"""
    if cred:
        auth.logout(cred.credentials)
    return {"ok": True}

class PasswordReq(BaseModel):
    old_password: str
    new_password: str

@router.post("/password")
def change_password(req: PasswordReq, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """修改**自己的**密码：需登录；成功后旧 token 全部作废，返回新 token。"""
    user = _u(user)
    return {"token": auth.change_password(req.old_password, req.new_password, user.username)}

class AccountReq(BaseModel):
    username: str
    password: str
    role: str = "user"
    display_name: str = ""
    quota: dict = {}

@admin_router.get("/accounts")
def list_accounts(_admin: CurrentUser = Depends(require_admin)):
    """账号列表：含角色、配额与**当前占用**（实例数 / QQ 号数）。

    密码永不回显——只在创建与改密时明文传输。
    """
    recs = ctx.registry.all()
    out = []
    for u in auth.list_users():
        cur = quota.usage(u["username"], recs, ctx.adapters)
        out.append({**u, "usage": cur})
    return out

@admin_router.post("/accounts")
def create_account(req: AccountReq):
    return auth.create_user(req.username, req.password, req.role,
                            req.display_name, req.quota)

@admin_router.delete("/accounts/{username}")
def delete_account(username: str, user: CurrentUser = Depends(require_admin)):
    auth.delete_user(username, by=user.username)   # 无返回值；失败抛 4xx
    return {"ok": True, "deleted": username,
            "note": "账号已删除，其名下实例已转为归属 admin"}

@admin_router.put("/accounts/{username}/quota")
def set_account_quota(username: str, quota: dict):
    return auth.set_quota(username, quota)

@admin_router.post("/accounts/{username}/password")
def set_account_password(username: str, body: dict):
    """管理员改密。改完该用户全部会话立即失效（对方需重新登录）。"""
    auth.set_password(username, body.get("new_password", ""))
    return {"ok": True, "note": "密码已修改，该用户需重新登录"}

@admin_router.post("/accounts/{username}/revoke")
def revoke_account_sessions(username: str):
    """强制下线：吊销该用户全部会话（踢出在线会话），账号保留。"""
    auth.revoke_sessions(username)
    return {"ok": True}

# ---------- 归属与配额辅助 ----------
# 所有按inst_id 操作实例的端点都必须先过 _own。管理员放行全部，普通用户只能操作
# 自己名下的实例。越权与不存在同样返回 404，不泄露他人实例是否存在。

def _u(user: CurrentUser | None) -> CurrentUser:
    """取当前身份，无注入上下文时回落 admin。

    HTTP 路径由 FastAPI 注入真实 CurrentUser；进程内直接调用端点函数（services 层、
    既有单测）时 user 为 None，此处回落 admin 以保持旧调用方式可用。
    """
    return user if user is not None else admin_user()

def _own(inst_id: str, user: CurrentUser | None):
    """校验实例归属并返回记录 dict。越权/不存在 → 404。"""
    u = _u(user)
    try:
        return ctx.registry.owned_by(inst_id, u.username, u.is_admin)
    except KeyError:
        raise HTTPException(404, f"实例不存在: {inst_id}") from None

def _guard_quota(user: CurrentUser, add_app: int = 0, add_login_qq: int = 0) -> dict:
    """配额预检。管理员不限；普通用户按其配额校验，越限抛 400。"""
    if user.is_admin:
        return {}
    try:
        return quota.check(user.username, user.quota, ctx.registry.all(),
                           ctx.adapters, add_app=add_app, add_login_qq=add_login_qq)
    except quota.QuotaExceeded as e:
        raise HTTPException(400, e.message) from None

@router.get("/me")
def whoami(user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """当前身份 + 配额占用。前端首屏拉一次，决定菜单与「新建」按钮是否置灰。"""
    user = _u(user)
    usage = quota.usage(user.username, ctx.registry.all(), ctx.adapters)
    return {**user.to_json(), "usage": usage,
            "unlimited": user.is_admin or (user.quota.get("login_qq", -1) < 0
                                          and user.quota.get("app", -1) < 0)}

@router.post("/instances")
def create_instance(req: CreateReq, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """创建实例。归属当前登录用户；普通用户受「应用端实例数」配额约束。

    配额校验在**创建前**：实例创建会分配端口、写目录、落盘，超限后再回滚代价高。
    """
    user = _u(user)
    if req.dice not in ctx.adapters:
        raise HTTPException(400, f"未知程序: {req.dice}")
    # 官方通道（免登录端）只占应用端名额；登录端实例本身不占 app 名额（按 QQ 号计）
    add_app = 0 if is_login_program(req.dice, ctx.adapters) else 1
    with quota_lock():
        _guard_quota(user, add_app=add_app)
        iid = ctx.wizard.create_instance(req.dice, req.arch,
                                         login_ref=req.login_ref, confirm_dir=req.confirm_dir,
                                         bot_mode=req.bot_mode, owner=user.username)
    inst = ctx.registry.get(iid)
    return {"id": iid, "dir": inst.dir, "allocated_ports": inst.allocated_ports}

@router.post("/instances/{inst_id}/wizard")
def run_wizard_step(inst_id: str, req: StepReq, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    user = _u(user)
    _own(inst_id, user)
    return ctx.wizard.run_step(inst_id, req.step, req.payload)   # ok/conflict 统一

@router.get("/instances")
def list_instances(user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """实例列表：普通用户只看得到自己名下的（数据隔离的第一道闸）。"""
    user = _u(user)
    out = []
    for r in ctx.registry.all():
        if not user.is_admin and (r.get("owner") or "admin") != user.username:
            continue
        proc = ctx.pm.get(r["id"])
        p = proc.probe()
        # 句柄失效但服务端口仍通（launcher 重启 / 面板重启后 worker reparent）→ 视为存活，
        # 与 /ws/overview 同口径，避免首屏把仍在跑的 llbot 等判为已停止
        if not p["alive"]:
            try:
                if ctx.get_adapter(r["dice"]).is_up(SimpleNamespace(**r)):
                    p = {**p, "alive": True}
            except Exception:
                pass
        out.append({**r, "process": p})
    return out

class LinkReq(BaseModel):
    login_ref: str | None = None         # 兼容旧调用：单关联（解除传 null）
    account_qq: str | None = None        # 单关联时绑定的登录端账号（多连一按账号分发）
    links: list | None = None            # 新：完整关联列表，每项 {login_ref, account_qq?}

@router.post("/instances/{inst_id}/link")
def link_login(inst_id: str, req: LinkReq, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """总览页连接管理：建立/解除 骰子端 → 登录端 的关联（links 即拓扑连线数据源）。

    支持多连一（多个骰子端各持有指向同一登录端的 links）与一连多（单个骰子端
    links 含多个登录端）。兼容性由 manifest 的 compatible_login 声明驱动，后端不写程序名分支。

    跨用户关联被拒：否则 A 的骰子端能挂上 B 的登录端，而互联 token 会自动同步
    （见wizard._login_account_info），等于把 B 的登录态泄露给 A。
    """
    user = _u(user)
    _own(inst_id, user)                           # 归属校验：本端必须归本人
    inst = ctx.registry.get(inst_id)
    # 归一化为 links 列表：优先 req.links；其次单关联（login_ref/account_qq）；否则解除全部
    if req.links is not None:
        raw = req.links or []
    elif req.login_ref is not None:
        raw = [{"login_ref": req.login_ref, "account_qq": req.account_qq}]
    else:
        raw = []
    cleaned = []
    for lk in raw:
        lr = lk.get("login_ref") if isinstance(lk, dict) else lk
        if not lr:
            continue
        if lr == inst_id:
            raise HTTPException(400, "不能关联自己")
        try:
            _own(lr, user)                  # 越权即 404：目标登录端也必须归本人
            target = ctx.registry.get(lr)
        except HTTPException:
            raise HTTPException(404, f"登录端实例不存在: {lr}") from None
        ok_set = [d for d in (ctx.adapters[inst.dice][0].get("compatible_login") or [])
                  if d != "builtin"]
        if target.dice not in ok_set:
            raise HTTPException(400, f"{inst.dice} 不兼容登录端 {target.dice}"
                                     f"（兼容：{' / '.join(ok_set) or '无'}）")
        cleaned.append({"login_ref": lr,
                        "account_qq": (lk.get("account_qq") if isinstance(lk, dict) else None)})
    with instance_lock(inst_id):
        ctx.registry.set_links(inst_id, cleaned)
    return {"ok": True, "links": cleaned}

@router.get("/instances/{inst_id}/webui")
def instance_webui(inst_id: str, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """WebUI 直连信息：实际端口优先（占用时程序可能自动 +1 换端口），令牌从启动日志回读。

    前端用 location.hostname 拼完整 URL（服务与面板同机部署，浏览器到的是同一个主机）。

    配额 B 段（事前拦截）：登录端 QQ 号配额已满时**不放行** WebUI——用户在登录端
    自带 WebUI 里扫码即可登新号，这里是唯一能拦住「继续扫码」的闸门。
    """
    user = _u(user)
    rec = _own(inst_id, user)
    if is_login_program(rec.get("dice", ""), ctx.adapters):
        usage_now = quota.usage(user.username, ctx.registry.all(), ctx.adapters)
        lim = user.quota.get("login_qq", -1)
        # 管理员不限；配额为 -1 表示不限；已有账号数已达上限时拦下
        if not user.is_admin and lim >= 0 and usage_now["login_qq"] >= lim:
            raise HTTPException(400, f"登录端 QQ 号已达上限（{usage_now['login_qq']}/{lim}），"
                                     f"无法再登录新账号；请管理员在账号管理中提额")
    port = rec.get("actual_port") or (rec.get("allocated_ports") or {}).get("webui")
    return {"port": port, "token": rec.get("webui_token")}

@router.get("/instances/{inst_id}/metrics")
def instance_metrics(inst_id: str, hours: float = 24.0,
                     user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """资源曲线：最近 hours 小时的 [时间戳, 内存MB, CPU%] 采样点（默认 60s 一点）。

    面板重启不丢历史（落盘 <state>/metrics/<id>.json），新部署实例前几分钟可能无点。
    """
    user = _u(user)
    _own(inst_id, user)
    hours = min(max(hours, 0.1), 24 * 7)
    pts = ctx.metrics.points(inst_id, hours)
    mems = [p[1] for p in pts]; cpus = [p[2] for p in pts]
    return {"interval": 60, "points": pts,
            "latest_mem_mb": mems[-1] if mems else None,
            "peak_mem_mb": round(max(mems), 1) if mems else None,
            "avg_cpu": round(sum(cpus) / len(cpus), 1) if cpus else None}

@router.get("/pending")
def list_pending(user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """未完成的中间态实例：管理器重启 / 断网后可经向导从这里继续。"""
    user = _u(user)
    return [{"id": i.id, "dice": i.dice, "state": i.state, "dir": i.dir,
             "qq": i.qq, "login_ref": i.login_ref, "bot_mode": i.bot_mode,
             "next_step": ctx.wizard.next_step(i.id)}
            for i in ctx.registry.resume_pending()
            if user.is_admin or (ctx.registry.get(i.id).owner or "admin") == user.username]

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


def _inst_or_404(inst_id: str, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """取实例；给了 user 就一并校验归属（普通用户只能碰自己名下的）。

    所有按 inst_id 操作的端点都走这里，越权与不存在同样 404——不区分
    「实例存在但不属于你」和「实例根本不存在」，否则可探测他人实例 ID。
    """
    user = _u(user)
    try:
        ctx.registry.owned_by(inst_id, user.username, user.is_admin)
        return ctx.registry.get(inst_id)
    except KeyError:
        raise HTTPException(404, f"实例不存在: {inst_id}") from None


def _data_paths_of(dice: str) -> list[str] | None:
    return ctx.adapters[dice][0].get("data_paths")


@router.get("/instances/{inst_id}/export")
async def export_instance(inst_id: str, scope: str = "full",
                 user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """导出实例备份（与「上传备份导入」对称）。

    两种口径严格区分：
    - full 整目录备份：程序+配置+存档+数据全量，恢复即得完整实例；
    - data 应用数据备份：仅 manifest data_paths 声明的数据/存档（对应应用端
      自身备份功能覆盖的局部数据），体积小、跨版本可移植，但恢复前提是
      实例已部署同版本程序。
    """
    rec = _inst_or_404(inst_id, user)
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
def diagnose_instance(inst_id: str, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """互联诊断器（拓展2）：按原因链逐层检测，返回结构化检查项。

    通用层（进程→关联→端口→token）在此统一实现；本端配置文件层由适配器
    diagnose_conn 钩子补充（如海豹读 serve.yaml 端点 state）。
    """
    user = _u(user)
    rec = _inst_or_404(inst_id, user)
    items: list[dict] = []

    def add(ok: bool, step: str, detail: str):
        items.append({"ok": ok, "step": step, "detail": detail})

    alive = ctx.pm.is_alive(inst_id)
    add(alive, "process",
        "本端进程运行中" if alive else "本端进程未运行（先启动实例）")
    links = rec.links or ([{"login_ref": rec.login_ref}] if rec.login_ref else [])
    if not links:
        add(False, "ref", "未关联任何登录端（总览侧栏「关联登录端」）")
    for link in links:
        lr = link.get("login_ref"); account_qq = link.get("account_qq")
        tag = f"（账号 {account_qq}）" if account_qq else ""
        try:
            login = ctx.registry.get(lr)
            add(True, "ref", f"已关联登录端 {lr}{tag}")
            lalive = ctx.pm.is_alive(lr)
            add(lalive, "login_process",
                f"登录端 {lr} 进程{'运行中' if lalive else '未运行（连不上任何人）'}")
            lproto = ctx.get_adapter(login.dice).m.get("protocol", "ob11")
            # 优先该账号专属端口，否则登录端默认分配端口
            port = None
            if account_qq:
                acc = next((a for a in (login.accounts or [])
                            if str(a.get("qq")) == str(account_qq)), None)
                port = acc.get("port") if acc else None
            port = port or (login.allocated_ports or {}).get(lproto) or \
                   (login.allocated_ports or {}).get("ob11")
            if port:
                reachable = ctx.get_adapter(rec.dice).tcp_probe("127.0.0.1", port)
                pname = "milky" if lproto == "milky" else "ob11"
                add(reachable, "port",
                    f"登录端 {pname} 端口 {port}{tag} {'可连通' if reachable else '未监听（登录端未启动/端口没开）'}")
            else:
                add(False, "port", f"登录端 {lr} 未分配互联端口，无法建立连接")
            ltoken = None
            if account_qq:
                acc = next((a for a in (login.accounts or [])
                            if str(a.get("qq")) == str(account_qq)), None)
                ltoken = acc.get("token") if acc else None
            ltoken = ltoken or login.conn_token
            rtoken = link.get("conn_token") or rec.conn_token
            if rtoken and ltoken:
                same = rtoken == ltoken
                add(same, "token",
                    f"两端 token{tag} {'一致' if same else '不一致（历史遗留），请「重写互联配置」对齐'}")
            else:
                add(False, "token",
                    f"互联 token{tag} 缺失（本端或登录端尚未写互联配置），请「重写互联配置」")
        except KeyError:
            add(False, "ref", f"关联的登录端 {lr} 已不存在，请重新关联")
    # 本端配置层（适配器专属，如海豹 serve.yaml 端点 state）
    try:
        ns = SimpleNamespace(**{k: rec.__dict__.get(k) for k in
                                ("id", "dice", "dir", "allocated_ports", "actual_port",
                                 "conn_token", "conn_addr", "conn_direction", "links")})
        items.extend(ctx.get_adapter(rec.dice).diagnose_conn(ns))
    except Exception as e:                              # 诊断自身出错要可见，不能静默
        add(False, "adapter", f"适配器诊断异常: {e}")
    return {"ok": all(i["ok"] for i in items), "items": items}


@router.get("/instances/{inst_id}/upgrade-check")
def upgrade_check(inst_id: str, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """升级通道：比对当前部署版本（release tag）与上游最新版本。"""
    user = _u(user)
    rec = _inst_or_404(inst_id, user)
    adapter = ctx.get_adapter(rec.dice)
    strat = ctx.adapters[rec.dice][0].get("download_strategy")
    if strat == "manual":
        return {"supported": False,
                "message": "该程序离线分发（上游不发完整包）：请上传新程序包后用「备份导入」覆盖升级"}
    if strat == "direct":
        return {"supported": False,
                "message": "该程序为固定直链下载，无法判定版本；可直接点「升级」用最新包覆盖"}
    if strat == "pip_project":
        # 上游在 PyPI 而非 GitHub Release：没有 release tag 可比对，
        # 但「升级」动作本身可用（NoneBot2Adapter.upgrade 走 pip install -U）。
        # 故此处不返回 supported=False——否则前端会连升级入口一起藏掉。
        return {"supported": True, "current": rec.version, "latest": None,
                "up_to_date": False,
                "message": "该程序为 Python 依赖项目，版本以已装依赖为准；"
                           "点「升级」将执行 pip install -U 升级全部依赖"}
    if strat == "npm_project":
        # 同理（Koishi）：上游在 npm registry，没有 release tag 可比对，
        # 升级动作本身可用（npm install -U）。返回 supported=False 会让前端
        # 把升级入口一起藏掉，用户就再也升不了了。
        return {"supported": True, "current": rec.version, "latest": None,
                "up_to_date": False,
                "message": "该程序为 Node 依赖项目，版本以已装依赖为准；"
                           "点「升级」将执行 npm install -U 升级全部依赖"}
    try:
        latest = adapter.latest_tag()
    except Exception as e:
        raise HTTPException(502, f"获取上游版本失败: {e}") from e
    return {"supported": True, "current": rec.version, "latest": latest,
            "up_to_date": rec.version is not None and rec.version == latest}


@router.post("/instances/{inst_id}/upgrade")
def upgrade_instance(inst_id: str, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """原地升级：自动整目录备份 → 停机 → 覆盖解压最新包（数据文件保留）→ 恢复运行。"""
    user = _u(user)
    rec = _inst_or_404(inst_id, user)
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
async def upload_backup(inst_id: str, request: Request,
                       user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """导入实例备份（手册 §5 上传约定：原始字节流，非 multipart）。

    流程：原本在运行则先停止 → 流式接收压缩包（魔数识别 + 完整性校验）
    → 解压覆盖到实例目录 → 原本在运行的实例自动重启。
    覆盖语义：只覆盖包内出现的文件，不删除包外文件（core/backup.py 模块注释）。
    """
    _inst_or_404(inst_id, user)
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

# ---------- 管理应用（总览页「管理应用」面板）----------

def _manage_adapter(inst):
    """取实例的管理适配器。

    能力判定在适配器里（manage_capabilities），不在这里 if dice == ... ——
    程序形态差异是清单/适配器的事，前端与 API 都不该按程序名分支。
    """
    manifest, cls = ctx.adapters[inst.dice]
    return cls(manifest)


@router.get("/instances/{inst_id}/manage")
def instance_manage(inst_id: str, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """该实例能提供哪些管理入口 + 每个入口的当前数据。

    前端「管理应用」按钮点开就是这份返回：有 WebUI 的程序给一条打开入口，
    依赖型程序（NoneBot2）给插件列表。两者可并存，故是数组不是单对象。
    """
    _own(inst_id, user)                  # 越权/不存在 → 404
    inst = ctx.registry.get(inst_id)
    ad = _manage_adapter(inst)
    caps = ad.manage_capabilities(inst)
    data: dict = {}
    for c in caps:
        if c["kind"] == "webui":
            # 实际端口优先于分配端口：占用时程序常自动 +1 换端口
            data["webui"] = {
                "port": inst.actual_port or (inst.allocated_ports or {}).get("webui"),
                "token": inst.webui_token,
            }
        elif c["kind"] == "python_deps":
            data["python_deps"] = ad.list_plugins(inst)
    return {"ok": True, "capabilities": caps, "data": data}


@router.post("/instances/{inst_id}/plugins/install")
async def instance_plugin_install(inst_id: str, body: dict,
                            user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """装一个 pip 插件（pip install --target libs + 同步 pyproject）。

    **同步 pyproject 是必须的**，否则 `nb run` 下次按 pyproject 同步依赖时
    会把刚装的插件悄悄移除（用户视角：装完重启就没了）。

    装依赖要跑网络（可能几分钟），放线程池避免阻塞事件循环。
    实例须停机：正在跑的进程 import 着 libs/ 里的 .py，替换文件会得到
    半新半旧的模块状态。
    """
    _own(inst_id, user)                  # 越权/不存在 → 404
    inst = ctx.registry.get(inst_id)
    ad = _manage_adapter(inst)
    if not hasattr(ad, "install_package"):
        raise HTTPException(400, "该程序不支持插件安装")
    if ctx.pm.is_alive(inst_id):
        raise HTTPException(400, "请先停止该实例再安装插件（运行中替换依赖文件不安全）")
    try:
        ver = await asyncio.to_thread(ad.install_package, inst, body.get("spec", ""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"插件安装失败: {e}") from e
    return {"ok": True, "version": ver, "data": ad.list_plugins(inst)}


@router.post("/instances/{inst_id}/plugins/uninstall")
async def instance_plugin_uninstall(inst_id: str, body: dict,
                              user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """卸载一个 pip 插件（按 dist-info 的 RECORD 删文件 + 从 pyproject 移除声明）。"""
    _own(inst_id, user)                  # 越权/不存在 → 404
    inst = ctx.registry.get(inst_id)
    ad = _manage_adapter(inst)
    if not hasattr(ad, "uninstall_package"):
        raise HTTPException(400, "该程序不支持插件卸载")
    if ctx.pm.is_alive(inst_id):
        raise HTTPException(400, "请先停止该实例再卸载插件")
    try:
        ver = await asyncio.to_thread(ad.uninstall_package, inst, body.get("name", ""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"插件卸载失败: {e}") from e
    return {"ok": True, "version": ver, "data": ad.list_plugins(inst)}


@router.post("/instances/{inst_id}/plugins/sync")
async def instance_plugin_sync(inst_id: str, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """把 libs/ 里现有的包全量同步进 pyproject.toml。

    给「用户自己在实例目录 pip install --target libs 装了插件」的场景兜底：
    缺这一步，`nb run` 下次同步依赖会把它们移除。
    """
    _own(inst_id, user)                  # 越权/不存在 → 404
    inst = ctx.registry.get(inst_id)
    ad = _manage_adapter(inst)
    if not hasattr(ad, "sync_pyproject"):
        raise HTTPException(400, "该程序不支持该操作")
    try:
        path = await asyncio.to_thread(ad.sync_pyproject, inst)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "path": path, "data": ad.list_plugins(inst)}


# 必须注册在所有具体的 POST 子路由（/backup、/webui、/metrics…）之后：否则 {op} 会按
# 注册顺序抢先匹配到 backup 等字面路径（同源坑见 /packages/unused、/exports/prune）。
@router.post("/instances/{inst_id}/{op}")
def instance_op(inst_id: str, op: str, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    user = _u(user)
    # 归属校验：越权与不存在同样 404。必须放在 VALID_OPS 校验**之前**——否则普通用户
    # 拿到「未知操作」400（说明实例 ID 存在）vs 真实越权的 404，可据此探测他人实例。
    # 端点直接停/启他人进程，是最直接的越权提权面（DoS），故与其余按 inst_id 操作的
    # 端点同口径走 _inst_or_404。
    _inst_or_404(inst_id, user)
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
                    remove_dir: bool = False, keep_save: bool = True,
                    user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """二次确认；remove_dir=true 时 keep_save 决定是否保留 config 存档目录。

    目录删除失败时**先于墓碑**抛 500（实例记录保留，用户可直接重试）——
    旧实现 rmtree(ignore_errors=True) 静默吞错且记录已删，失败即成无主孤儿目录。
    """
    if not confirm:
        raise HTTPException(400, "删除需二次确认 confirm=true")
    user = _u(user)
    _inst_or_404(inst_id, user)
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
        # 级联解除引用：否则骰子端 links 悬空——总览连线消失、向导 step4 重写时会
        # 静默生成新 token 与登录端残留配置不一致（两端连不上且难排查）
        #
        # 跨用户边界：普通用户删除自己的登录端时，**不得**去改别人实例的 links
        # （那既是越权写，也会把对方的连线静默清掉）。管理员不受此限。
        unlinked = []
        for r in ctx.registry.all():
            if not user.is_admin and (r.get("owner") or "admin") != user.username:
                continue
            links = r.get("links") or []
            if any(lk.get("login_ref") == inst_id for lk in links):
                new_links = [lk for lk in links if lk.get("login_ref") != inst_id]
                ctx.registry.set_links(r["id"], new_links)
                unlinked.append(r["id"])
        ctx.registry.remove(inst_id)                        # 全部成功后才墓碑
    # 日志文件随实例一起走 + 释放空壳 ManagedProcess：旧实现只除名不删日志，
    # 反复增删会累积一批查不到归属的孤儿日志
    removed_logs = ctx.pm.remove_logs(inst_id)
    ctx.metrics.drop(inst_id)               # 资源曲线随实例一起回收（与日志同口径）
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
def deploy_progress(inst_id: str, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """部署进度快照（step2 同步部署期间前端 1s 轮询）：下载字节数 / 解压阶段。"""
    user = _u(user)
    from adapters.base import deploy_progress_of
    _inst_or_404(inst_id, user)
    return deploy_progress_of(inst_id)

@router.get("/manifests")
def list_manifests():
    """Step1 组合选项与兼容矩阵。

    白名单只放前端真正消费的字段：multi_account / recommended_protocols 曾在此透出
    但前端从不读取（手册 §6.2 记为「文档性字段」），透出只会误导后来者以为有用。

    login_modes 由**适配器方法**给出而非清单字段：能不能写密码是程序自身能力
    （写错字段时程序会静默忽略并退回扫码，用户以为设了密码其实每次都在扫码），
    属于代码事实，不该由清单声明。方法缺失/异常一律降级为 ["qrcode"]。
    """
    out = {}
    for n, (m, _cls) in ctx.adapters.items():
        try:
            adapter = ctx.get_adapter(n)
            modes = [x for x in (adapter.login_modes() or []) if x in ("qrcode", "account")]
        except Exception:
            adapter, modes = None, []
        out[n] = {k: m.get(k) for k in ("arch", "login_type",
                                        "compatible_login", "bot_modes",
                                        "webui_default_port", "ob11_default_port",
                                        "approx_memory_mb", "auth_token_conditional",
                                        "prerequisite", "delete_keeps_save")}
        out[n]["login_modes"] = modes or ["qrcode"]
        protos = getattr(adapter, "LOGIN_PROTOCOLS", None) if adapter else None
        if protos:
            out[n]["login_protocols"] = protos
    return out

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
def download_log(inst_id: str, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    user = _u(user)
    _inst_or_404(inst_id, user)                    # 日志含消息内容，不容跨用户读
    p = ctx.log_dir / f"{inst_id}.log"
    if not p.exists():
        raise HTTPException(404, "日志不存在")
    return FileResponse(p, filename=f"{inst_id}.log")


# ---------- 定时任务（拓展7）：按「每 X 天 X 小时」间隔定时重启 / 定时备份 ----------

@router.get("/schedules")
def list_schedules(user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    user = _u(user)
    recs = {r["id"]: r for r in ctx.registry.all()}
    tasks = ctx.scheduler.list_all()
    if not user.is_admin:                        # 定时任务会对实例动刀，只列自己名下
        tasks = [t for t in tasks
                 if (recs.get(t["inst_id"], {}).get("owner") or "admin") == user.username]
    return [{"inst_dice": recs.get(t["inst_id"], {}).get("dice", "(已删除)"), **t}
            for t in tasks]


class SchedReq(BaseModel):
    inst_id: str
    kind: str                                    # restart | backup
    every_days: int = 0                          # 每 X 天
    every_hours: int = 0                         # 每 X 小时（合计须 ≥ 1 小时）
    scope: str = "data"                          # backup 专用：full | data
    keep: int = 7                                # backup 专用：保留份数


@router.post("/schedules")
def add_schedule(req: SchedReq, user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    user = _u(user)
    _inst_or_404(req.inst_id, user)
    try:
        return ctx.scheduler.add(req.inst_id, req.kind,
                                 req.every_days, req.every_hours,
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
    """手动立即执行（补跑/测试）；执行后记 last_run，未满间隔不再重复跑。"""
    try:
        return ctx.scheduler.run_now(task_id)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


# ---------- 日志聚合检索（拓展10） ----------

SEARCH_TAIL_BYTES = 2 * 1024 * 1024           # 磁盘日志只扫尾部 2MB，防大文件拖垮


@router.get("/logs/search")
def logs_search(q: str, inst_id: str | None = None, limit: int = 100,
                user: Annotated[CurrentUser | None, Depends(current_user)] = None):
    """跨实例日志检索：ring（最近 2000 行）+ 磁盘日志尾部（各 2MB）。

    普通用户只检索自己名下实例——日志里是机器人收到的真实消息，跨用户可读属泄露。
    正则元字符按字面处理（fnmatch 无关，直接 in 匹配）。
    """
    q = (q or "").strip()
    if len(q) < 2:
        raise HTTPException(400, "搜索词至少 2 个字符")
    user = _u(user)
    limit = max(1, min(limit, 300))
    if inst_id:
        _inst_or_404(inst_id, user)
    results: list[dict] = []
    for rec in ctx.registry.all():
        if not user.is_admin and (rec.get("owner") or "admin") != user.username:
            continue
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


# ---------- 面板自管理：整体重启 ----------

log = logging.getLogger("dicemanager.rest")

# 裸跑（非 systemd）时的接班进程：等旧进程退出释放端口后，原地 exec 成新面板进程
_RESTART_HELPER = (
    "import os, sys, time\n"
    "time.sleep(1.5)\n"
    "os.execv(sys.executable, [sys.executable, '-m', 'api.app'])\n"
)

_RESTART_DELAY = 0.8                                    # 先让响应送达浏览器，再执行自杀


def _do_restart() -> None:
    """真正的重启动作，按运行环境三选一：
    - DM_PANEL_RESTART_CMD：自定义命令（外置守护/特殊托管场景自管拉起）；
    - systemd 服务内（INVOCATION_ID 存在）：SIGTERM 自杀，Restart=always 5s 后拉起；
    - 裸跑（本地调试/手动启动）：派生脱管接班进程，再自杀退出释放端口。
    骰子实例是面板子进程，会随面板退出；新进程起来后 lifespan 的 resume 线程
    自动拉回 RUNNING 实例（services/resume.py）。
    """
    try:
        cmd = os.environ.get("DM_PANEL_RESTART_CMD")
        if cmd:
            log.warning("[panel] 面板重启：执行自定义命令 %r", cmd)
            subprocess.Popen(cmd, shell=True)
        elif "INVOCATION_ID" in os.environ:
            log.warning("[panel] 面板重启：进程退出，等待 systemd Restart=always 拉起")
        else:
            log.warning("[panel] 面板重启：非托管环境，派生接班进程后退出")
            kw: dict = {"cwd": str(Path(__file__).resolve().parent.parent)}
            if os.name == "nt":
                kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
            else:
                kw["start_new_session"] = True
            subprocess.Popen([sys.executable, "-c", _RESTART_HELPER], **kw)
        os.kill(os.getpid(), signal.SIGTERM)
    except Exception:
        log.exception("[panel] 面板重启动作执行失败（面板继续运行）")


def _schedule_restart() -> None:
    t = threading.Timer(_RESTART_DELAY, _do_restart)
    t.daemon = True
    t.start()


@router.post("/panel/restart")
def restart_panel(_admin: CurrentUser = Depends(require_admin)):
    """整体重启管理面板：先应答前端，延迟 <1s 后进程自杀换新。

    运行中的骰子实例随面板进程一起退出，新进程起来后由 resume 线程自动拉回
    （只认 RUNNING 实例，DM_AUTO_RESUME=0 可关）。

    **仅管理员**：重启会连带杀掉所有用户的实例，等于面板级高危操作；
    分权后留在普通用户手里等于 anyone can DoS。

    分化 C3：仅 server 版提供（systemd Restart=always 拉起 / 裸跑接班进程 exec）。
    desktop 版是托盘常驻的单机工具，重启面板无意义，故返 404 且前端不渲染按钮。
    """
    if not is_server():
        raise HTTPException(404, "面板重启仅 server 版提供")
    _schedule_restart()
    log.warning("[panel] 收到面板重启请求，%.1fs 后重启", _RESTART_DELAY)
    return {"ok": True, "msg": "面板正在重启，约 5-10 秒后恢复；运行中的实例将自动拉回"}
