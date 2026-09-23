"""FastAPI 组装：lifespan 恢复扫描 + uvicorn 监听 127.0.0.1:8765"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from api import ws_login, ws_logs, ws_overview
from api.context import ctx
from api.rest import public, router
from core.logutil import console_only, setup_logging

log = setup_logging(ctx.log_dir)                        # 控制台 + 滚动文件

@asynccontextmanager
async def lifespan(app: FastAPI):
    from api.auth import auth
    # 敏感：只进控制台，不落盘。非首次启动只有哈希，拿不到明文（重置需删 auth.json）
    pwd = auth.admin_password
    console_only(f"[auth] 本次管理密码: {pwd}" if pwd else
                 "[auth] 密码为 PBKDF2 哈希存储（明文见首次启动的控制台输出）；"
                 "忘记密码请删除 auth.json 后重启服务重置")
    ctx.registry.purge_tombstones()                     # 兑现「墓碑保留 30 天」承诺
    for inst in ctx.registry.resume_pending():          # 启动恢复：中间态扫描
        log.info("[resume] 实例 %s 停留在 %s，可经向导继续或回滚", inst.id, inst.state)
    yield

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
