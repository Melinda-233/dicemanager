"""鉴权：随机密码（PBKDF2 哈希存储）+ Bearer token（恒定时间比较）+ 登录失败限速；
WS 经 ?token= 查询参数鉴权。旧版明文 auth.json 首次加载时自动迁移为哈希。"""
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from fastapi import Depends, HTTPException, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.atomicio import write_atomic

AUTH_FILE = Path(os.environ.get("DM_STATE_DIR", "/var/lib/dicemanager")) / "auth.json"
PBKDF2_ITER = 200_000
LOGIN_MAX_FAILS = 5                     # 60s 窗口内 ≥5 次失败 → 限速
LOGIN_WINDOW = 60

def _ct_eq(a: str, b: str) -> bool:
    """恒定时间比较：转 bytes，兼容任意 UTF-8 输入（非 ASCII 不再 TypeError）。"""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               salt, PBKDF2_ITER).hex()

class Auth:
    def __init__(self, state_file: Path = AUTH_FILE):
        if state_file.exists():
            d = json.loads(state_file.read_text("utf-8"))
        else:
            salt = secrets.token_hex(16)
            d = {"password_hash": _hash_password(secrets.token_urlsafe(12), bytes.fromhex(salt)),
                 "salt": salt, "token": secrets.token_urlsafe(32)}
            write_atomic(state_file, json.dumps(d).encode("utf-8"))   # 原子写入
        if "password_hash" not in d:    # 旧版明文字段：加载即迁移为哈希
            salt = secrets.token_hex(16)
            d = {"password_hash": _hash_password(d["password"], bytes.fromhex(salt)),
                 "salt": salt, "token": d.get("token", secrets.token_urlsafe(32))}
            write_atomic(state_file, json.dumps(d).encode("utf-8"))
        self._cred = d
        self._fails: list[float] = []   # 登录失败时间戳（滑动窗口限速）

    @property
    def admin_password(self) -> str:
        """兼容启动横幅：明文密码不再持有，迁移后仅提示查看方式。"""
        return "（PBKDF2 哈希存储，首次启动见控制台 / 删除 auth.json 重置）"

    def login(self, password: str) -> str:
        now = time.time()
        self._fails = [t for t in self._fails if now - t < LOGIN_WINDOW]
        if len(self._fails) >= LOGIN_MAX_FAILS:
            raise HTTPException(429, f"尝试过于频繁，请 {int(LOGIN_WINDOW - (now - self._fails[0]))}s 后重试")
        if _ct_eq(_hash_password(password, bytes.fromhex(self._cred["salt"])),
                  self._cred["password_hash"]):
            self._fails.clear()
            return self._cred["token"]
        self._fails.append(now)
        raise HTTPException(401, "密码错误")

    def verify_http(self, cred: HTTPAuthorizationCredentials | None) -> None:
        if not cred or not _ct_eq(cred.credentials, self._cred["token"]):
            raise HTTPException(401, "未授权")

    def verify_ws(self, ws: WebSocket) -> bool:
        return _ct_eq(ws.query_params.get("token", ""), self._cred["token"])

auth = Auth()

security = HTTPBearer(auto_error=False)

async def require_auth(cred: HTTPAuthorizationCredentials | None = Depends(security)):
    auth.verify_http(cred)
