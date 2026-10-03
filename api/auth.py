"""鉴权：多用户（管理员 / 普通用户）+ PBKDF2 哈希 + 会话表 + 恒定时间比较 + 登录失败限速；
WS 经 Sec-WebSocket-Protocol 子协议头或 ?token= 鉴权。

模型（auth.json v2）：
    {"version": 2,
     "users":    {"admin": {"role","password_hash","salt","display_name","quota","created_at"}},
     "sessions": {"<token>": {"username","issued_at"}}}

角色：
    admin —— 不限配额，可管理账号列表（增删 / 改密 / 改配额）、可重启面板
    user  —— 受配额约束，只能管理自己名下的登录端与应用端

迁移：v1 的单条凭据 {password_hash, salt, token, issued_at} 首次加载时自动升级为
    「admin 账号 + 一条会话」，旧密码与旧 token 均继续可用（面板升级不该全员掉线）。
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import Depends, HTTPException, Request, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.atomicio import write_atomic

AUTH_FILE = Path(os.environ.get("DM_STATE_DIR", "/var/lib/dicemanager")) / "auth.json"
PBKDF2_ITER = 200_000
LOGIN_MAX_FAILS = 5                     # 60s 窗口内 ≥5 次失败 → 限速
LOGIN_WINDOW = 60
AUTH_TTL = 30 * 86400                   # 会话有效期 30 天
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{2,32}$")
ROLE_ADMIN = "admin"
ROLE_USER = "user"
RESERVED_USERNAMES = {"admin", "root", "system"}
# 配额两维：login_qq = 名下登录端已登录 QQ 号总数上限；app = 名下应用端（骰子端）实例上限。
# 值为 -1 表示不限。管理员不校验配额。
DEFAULT_QUOTA = {"login_qq": 3, "app": 5}

def _ct_eq(a: str, b: str) -> bool:
    """恒定时间比较：转 bytes，兼容任意 UTF-8 输入（非 ASCII 不再 TypeError）。"""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               salt, PBKDF2_ITER).hex()

def normalize_quota(quota: dict | None) -> dict:
    """配额输入归一化：缺失/非法值回落到默认；负数统一视为不限（-1）。"""
    q = quota or {}
    out = {}
    for k, dflt in DEFAULT_QUOTA.items():
        try:
            v = int(q.get(k, dflt))
        except (TypeError, ValueError):
            v = dflt
        out[k] = -1 if v < 0 else v
    return out

@dataclass
class CurrentUser:
    """请求上下文里的登录身份。

    username 是**登录名**——实例归属（Instance.owner）与配额计数都以它为键，
    不可用 display_name 代替（改名会导致名下实例失联）。
    """
    username: str
    role: str
    display_name: str = ""
    quota: dict = field(default_factory=lambda: dict(DEFAULT_QUOTA))

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def to_json(self) -> dict:
        return {"username": self.username, "role": self.role,
                "display_name": self.display_name, "quota": dict(self.quota)}

class Auth:
    def __init__(self, state_file: Path = AUTH_FILE):
        self._file = state_file                     # 写操作需要回写同一文件
        self._initial_password: str | None = None   # 仅首次生成时持有，供启动横幅打印一次
        self._fails: dict[str, list[float]] = {}    # "ip|username" -> 失败时间戳，滑动窗口限速
        if state_file.exists():
            d = self._migrate(json.loads(state_file.read_text("utf-8")))
        else:
            pwd = secrets.token_urlsafe(12)
            self._initial_password = pwd
            d = self._blank(pwd)
            write_atomic(state_file, json.dumps(d, ensure_ascii=False).encode("utf-8"))
        self._data = d

    # ---------- 模型构造与迁移 ----------
    @staticmethod
    def _blank(password: str) -> dict:
        salt = secrets.token_hex(16)
        token = secrets.token_urlsafe(32)
        now = time.time()
        return {"version": 2,
                "users": {ROLE_ADMIN: {
                    "role": ROLE_ADMIN, "salt": salt,
                    "password_hash": _hash_password(password, bytes.fromhex(salt)),
                    "display_name": "管理员", "quota": dict(DEFAULT_QUOTA),
                    "created_at": now}},
                "sessions": {token: {"username": ROLE_ADMIN, "issued_at": now}}}

    def _migrate(self, d: dict) -> dict:
        """旧版（无 users 键）→ v2：单条凭据升为 admin 账号。

        迁移**必须保留旧 token**：面板升级后不该全员掉线。

        两个容易写错的点（都曾导致迁移后登不进去）：
          ① salt 与 password_hash 必须成对来自旧文件。若已有 password_hash 却另起
             新 salt，校验时用新 salt 去比旧 hash，必然不匹配 → 旧密码永久失效。
          ② 旧 issued_at 若已超 TTL，必须重置为现在，否则升级即全员掉线。
        """
        if "users" in d:
            d.setdefault("version", 2)
            d.setdefault("sessions", {})
            for name, u in d["users"].items():
                u.setdefault("quota", dict(DEFAULT_QUOTA))
                u.setdefault("display_name", name)
                u.setdefault("created_at", 0)
            return d
        now = time.time()
        old_hash = d.get("password_hash")
        old_salt = d.get("salt")
        if old_hash and old_salt:
            salt, pwd_hash = old_salt, old_hash     # 原样沿用：hash 与 salt 必须同源
        else:                                       # 更早的明文版：首次哈希化
            salt = secrets.token_hex(16)
            pwd_hash = _hash_password(d.get("password") or "", bytes.fromhex(salt))
        token = d.get("token") or secrets.token_urlsafe(32)
        issued = d.get("issued_at") or now
        if now - issued > AUTH_TTL:
            # 迁移不因旧签发时间让存量token 失效（否则升级即全员掉线）→ 重置 TTL 起点。
            issued = now
        out = {"version": 2,
               "users": {ROLE_ADMIN: {
                   "role": ROLE_ADMIN, "salt": salt, "password_hash": pwd_hash,
                   "display_name": "管理员", "quota": dict(DEFAULT_QUOTA),
                   "created_at": issued}},
               "sessions": {token: {"username": ROLE_ADMIN, "issued_at": issued}}}
        write_atomic(self._file, json.dumps(out, ensure_ascii=False).encode("utf-8"))
        return out

    def _flush(self) -> None:
        write_atomic(self._file, json.dumps(self._data, ensure_ascii=False,
                                            indent=2).encode("utf-8"))

    # ---------- 单管理员语境下的兼容视图 ----------
    @property
    def admin_password(self) -> str | None:
        """启动横幅用：仅在「本次启动刚生成 auth.json」时返回明文，之后返回 None。

        明文从未落盘——既不在 auth.json 里，也不进日志文件（横幅走 console_only）。
        历史坑：哈希化改造时把明文打印一并删掉后，新装用户再也拿不到密码
        （install.sh / README 都让人去日志里找 [auth] 行），只能删 auth.json 重置。
        """
        return self._initial_password

    @property
    def _cred(self) -> dict:
        """兼容视图：把 admin 账号 + 其最新会话摊平成 v1 的单条凭据形状。

        只读视图；写入一律走专用方法。保留是因为既有测试与部分只读调用方按
        `_cred["token"]` 取值。
        """
        u = self._data["users"][ROLE_ADMIN]
        issued = max((s["issued_at"] for s in self._data["sessions"].values()
                      if s["username"] == ROLE_ADMIN), default=time.time())
        return {"password_hash": u["password_hash"], "salt": u["salt"],
                "token": self._token_of(ROLE_ADMIN), "issued_at": issued}

    def _token_of(self, username: str) -> str:
        """该用户最新签发的会话 token（sessions 是有序 dict，插入序≈时间序）。"""
        best = ("", -1.0)
        for tok, s in self._data["sessions"].items():
            if s["username"] == username and s["issued_at"] >= best[1]:
                best = (tok, s["issued_at"])
        return best[0]

    # ---------- 密码校验与限速 ----------
    def _check_rate(self, bucket: str) -> None:
        now = time.time()
        fails = [t for t in self._fails.get(bucket, []) if now - t < LOGIN_WINDOW]
        self._fails[bucket] = fails
        if len(fails) >= LOGIN_MAX_FAILS:
            raise HTTPException(429, f"尝试过于频繁，请 {int(LOGIN_WINDOW - (now - fails[0]))}s 后重试")

    def _verify(self, password: str, user: dict, bucket: str) -> None:
        """校验密码。

        bucket 按「ip|用户名」分桶：多用户后全局单桶会让一个账号的失败把所有人锁死。
        """
        self._check_rate(bucket)
        if _ct_eq(_hash_password(password, bytes.fromhex(user["salt"])),
                  user["password_hash"]):
            self._fails.pop(bucket, None)
            return
        self._fails.setdefault(bucket, []).append(time.time())
        raise HTTPException(401, "用户名或密码错误")

    def _user_or_401(self, username: str) -> dict:
        u = self._data["users"].get(username)
        if not u:
            # 统一文案：不区分「用户不存在」与「密码错误」，避免账号枚举
            raise HTTPException(401, "用户名或密码错误")
        return u

    # ---------- 会话 ----------
    def _issue(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        self._data["sessions"][token] = {"username": username, "issued_at": time.time()}
        self._prune_sessions()
        self._flush()
        return token

    def _prune_sessions(self) -> None:
        """清过期会话，避免 auth.json 无限膨胀。"""
        now = time.time()
        for tok in [t for t, s in self._data["sessions"].items()
                    if now - s["issued_at"] > AUTH_TTL]:
            del self._data["sessions"][tok]

    def _revoke_sessions(self, username: str) -> None:
        for tok in [t for t, s in self._data["sessions"].items() if s["username"] == username]:
            del self._data["sessions"][tok]

    def current(self, token: str) -> CurrentUser | None:
        """token → CurrentUser；无效或过期返回 None。"""
        s = self._data["sessions"].get(token or "")
        if not s or time.time() - s["issued_at"] > AUTH_TTL:
            return None
        u = self._data["users"].get(s["username"])
        if not u:                       # 会话指向的账号已被删除
            return None
        return CurrentUser(username=s["username"], role=u["role"],
                           display_name=u.get("display_name") or s["username"],
                           quota=normalize_quota(u.get("quota")))

    def login(self, password: str, username: str = ROLE_ADMIN,
              client: str = "") -> str:
        """按用户名 + 密码登录，返回新签发的 token。

        省略 username 时按 admin 登录（保留单管理员旧语义）。
        """
        user = self._user_or_401(username)
        self._verify(password, user, f"{client or '-'}|{username}")
        return self._issue(username)

    def logout(self, token: str) -> None:
        """登出：吊销当前会话。"""
        if self._data["sessions"].pop(token, None) is not None:
            self._flush()

    # ---------- 账号管理（管理员） ----------
    def create_user(self, username: str, password: str, role: str = ROLE_USER,
                    display_name: str = "", quota: dict | None = None) -> dict:
        if not USERNAME_RE.match(username or ""):
            raise HTTPException(400, "用户名只允许字母/数字/下划线/点/短横，长度 2-32")
        if username.lower() in RESERVED_USERNAMES:
            raise HTTPException(400, f"用户名 {username} 为保留名")
        if username in self._data["users"]:
            raise HTTPException(409, f"用户 {username} 已存在")
        if role not in (ROLE_ADMIN, ROLE_USER):
            raise HTTPException(400, f"未知角色: {role}")
        if not password or len(password) < 6:
            raise HTTPException(400, "密码至少 6 位")
        salt = secrets.token_hex(16)
        self._data["users"][username] = {
            "role": role, "salt": salt,
            "password_hash": _hash_password(password, bytes.fromhex(salt)),
            "display_name": display_name or username,
            "quota": normalize_quota(quota), "created_at": time.time()}
        self._flush()
        return self.public_user(username)

    def delete_user(self, username: str, by: str = "") -> None:
        if username == ROLE_ADMIN:
            raise HTTPException(400, "不能删除 admin 账号")
        u = self._data["users"].get(username)
        if not u:
            raise HTTPException(404, f"用户不存在: {username}")
        if u["role"] == ROLE_ADMIN and self._admin_count() <= 1:
            raise HTTPException(400, "至少需要保留一个管理员账号")
        if by and username == by:
            raise HTTPException(400, "不能删除当前登录的账号")
        del self._data["users"][username]
        self._revoke_sessions(username)
        self._flush()

    def _admin_count(self) -> int:
        return sum(1 for u in self._data["users"].values() if u["role"] == ROLE_ADMIN)

    def set_quota(self, username: str, quota: dict) -> dict:
        if username not in self._data["users"]:
            raise HTTPException(404, f"用户不存在: {username}")
        self._data["users"][username]["quota"] = normalize_quota(quota)
        self._flush()
        return self.public_user(username)

    def set_password(self, username: str, new_password: str) -> None:
        """管理员改密：改完吊销该用户全部会话——否则旧 token 仍可用，等于没改。"""
        if username not in self._data["users"]:
            raise HTTPException(404, f"用户不存在: {username}")
        if not new_password or len(new_password) < 6:
            raise HTTPException(400, "密码至少 6 位")
        salt = secrets.token_hex(16)
        u = self._data["users"][username]
        u["salt"] = salt
        u["password_hash"] = _hash_password(new_password, bytes.fromhex(salt))
        self._revoke_sessions(username)
        self._flush()

    def revoke_sessions(self, username: str) -> None:
        """强制下线：吊销该用户全部会话（踢出所有在线会话），账号本身保留。"""
        if username not in self._data["users"]:
            raise HTTPException(404, f"用户不存在: {username}")
        self._revoke_sessions(username)
        self._flush()

    def public_user(self, username: str) -> dict:
        u = self._data["users"].get(username)
        if not u:
            raise HTTPException(404, f"用户不存在: {username}")
        return {"username": username, "role": u["role"],
                "display_name": u.get("display_name") or username,
                "quota": normalize_quota(u.get("quota")),
                "created_at": u.get("created_at", 0),
                "session_count": sum(1 for s in self._data["sessions"].values()
                                     if s["username"] == username)}

    def list_users(self) -> list[dict]:
        return [self.public_user(n) for n in self._data["users"]]

    # ---------- 自身改密 ----------
    def change_password(self, old: str, new: str, username: str = ROLE_ADMIN) -> str:
        """修改自己的密码：验旧密码 → 新盐重哈希 → **吊销全部会话并重签发**。

        旧 token 立即失效；调用方（前端）拿到新 token 后必须立刻替换本地存储，
        否则自己会被 401 踢回登录页。
        """
        u = self._user_or_401(username)
        self._verify(old, u, f"-|{username}")
        if not new or len(new) < 6:
            raise HTTPException(400, "新密码至少 6 位")
        salt = secrets.token_hex(16)
        u["salt"] = salt
        u["password_hash"] = _hash_password(new, bytes.fromhex(salt))
        self._revoke_sessions(username)
        token = self._issue(username)
        self._flush()
        return token

    # ---------- 请求鉴权 ----------
    def _expired(self) -> bool:
        return time.time() - self._cred.get("issued_at", time.time()) > AUTH_TTL

    def _token_expired(self, token: str) -> bool:
        """这个 token 是「曾有效但已过期」吗？——只用于挑选提示文案。

        判据是会话表里查得到它但已超 TTL（prune 会把它删掉，所以查不到时只能报
        「未授权」）。不构成任何安全判断：真正的校验始终是 current() 的查表。
        """
        s = self._data["sessions"].get(token)
        return s is not None and time.time() - s["issued_at"] > AUTH_TTL

    def verify_http(self, cred: HTTPAuthorizationCredentials | None) -> CurrentUser:
        """校验 Bearer token，返回 CurrentUser（供 request.state 传递）。"""
        if not cred:
            raise HTTPException(401, "未授权")
        user = self.current(cred.credentials)
        if user is None:
            if self._token_expired(cred.credentials):
                raise HTTPException(401, "登录已过期，请重新登录")
            raise HTTPException(401, "未授权")
        return user

    def ws_handshake(self, ws: WebSocket) -> tuple[bool, str | None, CurrentUser | None]:
        """WS 鉴权：优先 Sec-WebSocket-Protocol 头携带 token（不会进 access log，
        替代原先的 ?token= 查询参数——后者会连同 token 一起落 nginx/uvicorn 日志）；
        ?token= 仍兼容旧前端。返回 (是否通过, 需回显的 subprotocol, 当前用户)。

        注意：调用方**必须**用返回的 user 做归属过滤，不要只判 ok——否则普通用户
        订阅 overview/logs 会看到别人的数据。
        """
        proto = (ws.headers.get("sec-websocket-protocol") or "").split(",")[0].strip()
        query = ws.query_params.get("token", "")
        for tok in (proto, query):
            if tok:
                user = self.current(tok)
                if user is not None:
                    return True, (proto if tok == proto else None), user
        return False, None, None

    def verify_ws(self, ws: WebSocket) -> bool:
        """旧入口：仅查询参数鉴权（保留兼容）。"""
        return self.current(ws.query_params.get("token", "")) is not None

auth = Auth()

security = HTTPBearer(auto_error=False)

async def require_auth(request: Request,
                       cred: HTTPAuthorizationCredentials | None = Depends(security)):
    """router 级粗校验（挂在 APIRouter.dependencies 上）。

    校验通过后把 CurrentUser 写入 request.state.user。注意 FastAPI 的 router 级依赖
    **返回值不会注入路由函数**，所以需要知道「我是谁」的路由必须再显式
    Depends(current_user)。
    """
    user = auth.verify_http(cred)
    request.state.user = user
    return user

async def current_user(request: Request) -> CurrentUser:
    """取当前登录身份，供需要它的路由显式注入。"""
    user = getattr(request.state, "user", None)
    if user is None:                     # 兜底防御：正常路径下 router 级已写入
        user = auth.verify_http(None)
    return user

def admin_user() -> CurrentUser:
    """程序内/测试直接调用端点函数时的兜底身份（无 FastAPI 依赖注入上下文）。

    仅供**进程内直接调用**（如 services 层与既有单测）使用；HTTP 路径一律走
    Depends(current_user)，绝不能拿它当鉴权依据。
    """
    return CurrentUser(username=ROLE_ADMIN, role=ROLE_ADMIN,
                       display_name="管理员", quota=dict(DEFAULT_QUOTA))

async def require_admin(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    """管理员专属端点依赖：非 admin 一律 403。"""
    if not user.is_admin:
        raise HTTPException(403, "需要管理员权限")
    return user
