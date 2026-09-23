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
        self._file = state_file                    # change_password 需要回写同一文件
        self._initial_password: str | None = None   # 仅首次生成时持有，供启动横幅打印一次
        if state_file.exists():
            d = json.loads(state_file.read_text("utf-8"))
        else:
            salt = secrets.token_hex(16)
            # 明文只在本次进程内存里留一份给启动横幅；此后进程内不再持有
            self._initial_password = secrets.token_urlsafe(12)
            d = {"password_hash": _hash_password(self._initial_password, bytes.fromhex(salt)),
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
    def admin_password(self) -> str | None:
        """启动横幅用：仅在「本次启动刚生成 auth.json」时返回明文，之后返回 None。

        明文从未落盘——既不在 auth.json 里，也不进日志文件（横幅走 console_only）。
        历史坑：哈希化改造时把明文打印一并删掉后，新装用户再也拿不到密码
        （install.sh / README 都让人去日志里找 [auth] 行），只能删 auth.json 重置。
        """
        return self._initial_password

    def _verify(self, password: str) -> None:
        """校验密码（含滑动窗口限速），失败抛 401/429。登录与改密共用同一套防爆破。"""
        now = time.time()
        self._fails = [t for t in self._fails if now - t < LOGIN_WINDOW]
        if len(self._fails) >= LOGIN_MAX_FAILS:
            raise HTTPException(429, f"尝试过于频繁，请 {int(LOGIN_WINDOW - (now - self._fails[0]))}s 后重试")
        if _ct_eq(_hash_password(password, bytes.fromhex(self._cred["salt"])),
                  self._cred["password_hash"]):
            self._fails.clear()
            return
        self._fails.append(now)
        raise HTTPException(401, "密码错误")

    def login(self, password: str) -> str:
        self._verify(password)
        return self._cred["token"]

    def change_password(self, old: str, new: str) -> str:
        """修改密码：验旧密码 → 新盐重哈希 → **轮换 token** → 原子落盘，返回新 token。

        轮换 token 使所有旧凭据（含其他已登录会话）立即失效；调用方（前端）拿到
        返回的新 token 后必须立刻替换本地存储，否则自己会被 401 踢回登录页。
        """
        self._verify(old)
        if not new or len(new) < 6:
            raise HTTPException(400, "新密码至少 6 位")
        salt = secrets.token_hex(16)
        self._cred = {"password_hash": _hash_password(new, bytes.fromhex(salt)),
                      "salt": salt, "token": secrets.token_urlsafe(32)}
        write_atomic(self._file, json.dumps(self._cred).encode("utf-8"))   # 原子写入
        return self._cred["token"]

    def verify_http(self, cred: HTTPAuthorizationCredentials | None) -> None:
        if not cred or not _ct_eq(cred.credentials, self._cred["token"]):
            raise HTTPException(401, "未授权")

    def verify_ws(self, ws: WebSocket) -> bool:
        return _ct_eq(ws.query_params.get("token", ""), self._cred["token"])

auth = Auth()

security = HTTPBearer(auto_error=False)

async def require_auth(cred: HTTPAuthorizationCredentials | None = Depends(security)):
    auth.verify_http(cred)
