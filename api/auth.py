"""鉴权：随机密码 + Bearer token（恒定时间比较）；WS 经 ?token= 查询参数"""
import hmac, json, secrets
from pathlib import Path
from fastapi import Depends, HTTPException, WebSocket
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from core.atomicio import write_atomic

AUTH_FILE = Path("/var/lib/dicemanager/auth.json")

def _ct_eq(a: str, b: str) -> bool:
    """恒定时间比较：转 bytes，兼容任意 UTF-8 输入（非 ASCII 不再 TypeError）。"""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

class Auth:
    def __init__(self, state_file: Path = AUTH_FILE):
        if state_file.exists():
            d = json.loads(state_file.read_text("utf-8"))
        else:
            d = {"password": secrets.token_urlsafe(12), "token": secrets.token_urlsafe(32)}
            write_atomic(state_file, json.dumps(d).encode("utf-8"))   # 原子写入
        self.admin_password = d["password"]
        self._token = d["token"]

    def login(self, password: str) -> str:
        if _ct_eq(password, self.admin_password):
            return self._token
        raise HTTPException(401, "密码错误")

    def verify_http(self, cred: HTTPAuthorizationCredentials | None) -> None:
        if not cred or not _ct_eq(cred.credentials, self._token):
            raise HTTPException(401, "未授权")

    def verify_ws(self, ws: WebSocket) -> bool:
        return _ct_eq(ws.query_params.get("token", ""), self._token)

auth = Auth()

security = HTTPBearer(auto_error=False)

async def require_auth(cred: HTTPAuthorizationCredentials | None = Depends(security)):
    auth.verify_http(cred)
