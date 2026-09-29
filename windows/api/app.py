"""FastAPI 组装：lifespan 恢复扫描 + uvicorn 监听 127.0.0.1:8765"""
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

# 必须先于任何模块级 Auth()/registry 创建文件：auth.json（哈希+token）、instances.json
# （webui/conn token）都是敏感状态，默认 0644 会被同机其他用户读取。仅 POSIX 生效。
if os.name == "posix":
    try:
        from core.pathutil import default_state_dir
        os.umask(0o077)
        _state = Path(os.environ.get("DM_STATE_DIR", str(default_state_dir())))
        if _state.is_dir():
            _state.chmod(0o700)
            for _f in _state.glob("*.json"):
                _f.chmod(0o600)
    except OSError:
        pass                                    # 权限不足等：尽力而为，不阻断启动

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api import ws_login, ws_logs, ws_overview
from api.context import ctx
from api.rest import public, router
from core.logutil import console_only, setup_logging
from core.metrics import MetricsSampler
from services.resume import resume_running_instances

log = setup_logging(ctx.log_dir)                        # 控制台 + 滚动文件


def _resume_instances():
    """lifespan 内的后台动作：拉回面板重启前处于 RUNNING 的实例。"""
    try:
        resume_running_instances(ctx.registry, ctx.pm,
                                 lambda iid: ctx.wizard.start_instance(iid), log)
    except Exception:                                   # 兜底：后台线程异常不得拖垮面板
        log.exception("[resume] 自动恢复流程异常（面板继续正常服务）")

def _sample_metrics():
    """资源曲线采样守护：60s 一点，写 <state>/metrics/<id>.json。"""
    sampler = MetricsSampler(ctx.metrics, ctx.registry, ctx.pm)
    if not sampler.start():                             # DM_METRICS=0 可整体关闭
        log.info("[metrics] 采样已关闭（DM_METRICS=0）")
        return
    log.info("[metrics] 资源采样已启动，间隔 %ss", sampler.interval)
    sampler.loop()                                      # 常驻直到进程退出

@asynccontextmanager
async def lifespan(app: FastAPI):
    from api.auth import auth
    # 启动横幅：首次启动未设置密码 → 提示用户去 WebUI 设置；
    # 已设置 → 提示密码为哈希存储（明文从不落盘、从不进控制台），
    # 并打印凭据文件**绝对路径**——报障里最常见的困惑是「密码文件不存在却没提示设置密码」，
    # 而根因往往是看错了 auth.json（开发模式在 <项目根>/data，打包后在 <exe 同级>/data）。
    if not auth.is_initialized:
        console_only(
            f"[auth] 首次启动：尚未设置管理密码（凭据文件 {auth.path} 不存在）。"
            "请打开 http://127.0.0.1:8765 在登录页设置管理密码（≥6 位）。"
        )
    else:
        console_only(
            f"[auth] 管理密码以 PBKDF2 哈希存储（明文从不落盘），凭据文件：{auth.path}。"
            "忘记密码请删除该文件，刷新 WebUI 即可重新设置（无需重启进程）。"
        )
    ctx.registry.purge_tombstones()                     # 兑现「墓碑保留 30 天」承诺
    for inst in ctx.registry.resume_pending():          # 启动恢复：中间态扫描
        log.info("[resume] 实例 %s 停留在 %s，可经向导继续或回滚", inst.id, inst.state)
    ctx.scheduler.start()                               # 定时任务守护（重启/备份）
    # 实例是面板子进程，systemctl restart 会连带杀掉它们而 registry 仍记 RUNNING
    # → 后台线程逐个拉回（逐个 try/except，失败不阻断面板对外服务）
    threading.Thread(target=_resume_instances, name="resume-instances",
                     daemon=True).start()
    threading.Thread(target=_sample_metrics, name="metrics-sampler",
                     daemon=True).start()
    yield
    ctx.scheduler.stop()

app = FastAPI(title="DiceManager", lifespan=lifespan)
app.include_router(public)                              # 无鉴权：仅 /api/login
app.include_router(router)
app.include_router(ws_overview.router)
app.include_router(ws_logs.router)
app.include_router(ws_login.router)

# 绝对路径，摆脱对启动 CWD 的依赖；未构建前端时降级为纯 API 而不是崩溃
_dist = Path(__file__).resolve().parent.parent / "web" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="web")
else:
    log.warning("前端构建产物不存在: %s，仅提供 API", _dist)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
