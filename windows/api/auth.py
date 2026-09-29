"""鉴权：管理密码（PBKDF2 哈希存储）+ Bearer token（恒定时间比较）+ 登录失败限速；
WS 经 ?token= 查询参数鉴权。

**首次启动流程**（替代早期"launcher 弹原生密码框"方案）：
- auth.json 不存在 → Auth() 进入「未初始化」态（_initialized=False、_cred={}）
- 前端 GET /api/needs-setup 探测到 needs_setup=True → 切到「设置管理密码」界面
  （POST /api/login 此时返回 428「首次启动，请先设置管理密码」，兜底把用户引到设置流程）
- 用户在 WebUI 输入两次新密码 → POST /api/setup → setup_password() 写入 auth.json → 自动登录
- 后续启动 auth.json 已存在，Auth() 正常初始化

**状态以磁盘为准**（2026-09-29 修复）：Auth 是 import 时构建的模块级单例，早期实现只在
__init__ 读一次磁盘、之后 _initialized 常驻内存，导致「忘记密码 → 删除 auth.json」这一
官方重置办法在**运行中**的面板上失效：/api/needs-setup 仍返回 False、前端停在普通登录页，
看不到「设置管理密码」界面，且 POST /api/setup 返 409。现由 _refresh() 在每个鉴权入口
与磁盘核对，删除/新增 auth.json 无需重启进程即生效。

旧版明文 auth.json 首次加载时自动迁移为哈希；token 30 天有效。
"""
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
from core.pathutil import default_state_dir

AUTH_FILE = Path(os.environ.get("DM_STATE_DIR", str(default_state_dir()))) / "auth.json"
PBKDF2_ITER = 200_000
LOGIN_MAX_FAILS = 5                     # 60s 窗口内 ≥5 次失败 → 限速
LOGIN_WINDOW = 60
AUTH_TTL = 30 * 86400                   # token 有效期 30 天（旧 auth.json 无 issued_at 时迁移为当前时间）
SETUP_MIN_LEN = 6                       # 首次设置密码最小长度（与 change_password 一致）


def _ct_eq(a: str, b: str) -> bool:
    """恒定时间比较：转 bytes，兼容任意 UTF-8 输入（非 ASCII 不再 TypeError）。"""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               salt, PBKDF2_ITER).hex()


class Auth:
    def __init__(self, state_file: Path = AUTH_FILE):
        self._file = state_file                    # change_password 需要回写同一文件
        self._initial_password: str | None = None  # 历史字段，保留以兼容旧调用方；新版恒为 None
        self._initialized: bool = False            # 是否已完成首次设置（auth.json 存在且合法）
        self._cred: dict = {}                      # password_hash/salt/token/issued_at
        self._fails: list[float] = []              # 登录失败时间戳（滑动窗口限速）
        self._load()

    def _load(self) -> None:
        """从磁盘读取凭据。文件不存在 → 保持「未初始化」态（首次启动）。

        读失败/格式非法一律按「未初始化」处理而不抛异常：_refresh() 位于每条鉴权
        请求路径上，任何异常都会把整个面板变成 500，比「当作没设过密码」严重得多。
        """
        if not self._file.exists():
            # 首次启动：不写文件、不生成随机密码，进入未初始化态等待 WebUI 设置
            return
        try:
            d = json.loads(self._file.read_text("utf-8"))
        except (OSError, ValueError):
            return                      # 损坏或读取竞态（写一半/并发删除）
        if not isinstance(d, dict):
            return
        if "password_hash" not in d:    # 旧版明文字段：加载即迁移为哈希
            if "password" not in d:
                return                  # 既无哈希也无明文：无效凭据，视作未初始化
            salt = secrets.token_hex(16)
            d = {"password_hash": _hash_password(d["password"], bytes.fromhex(salt)),
                 "salt": salt, "token": d.get("token", secrets.token_urlsafe(32)),
                 "issued_at": time.time()}
            write_atomic(self._file, json.dumps(d).encode("utf-8"))
        if "issued_at" not in d:        # TTL 上线前签发的旧 token：从现在起算
            d["issued_at"] = time.time()
            write_atomic(self._file, json.dumps(d).encode("utf-8"))
        self._cred = d
        self._initialized = True

    def _refresh(self) -> None:
        """以磁盘实际状态为准同步内存态：auth.json 被删除/新增后无需重启进程即生效。

        修的就是这个现象——「密码文件不存在，但访问 WebUI 未显示设置密码」：
        单例只在 import 时读一次磁盘，之后 _initialized 常驻内存，于是运行中删除
        auth.json 后 needs-setup 仍返回 False，前端停在普通登录页。
        """
        if not self._file.exists():
            if self._initialized:               # 密码文件被移除 → 回到未初始化态
                self._initialized = False
                self._cred = {}
                self._fails.clear()
            return
        if not self._initialized:               # 文件在进程启动后才出现 → 重新加载
            self._load()

    @property
    def path(self) -> Path:
        """凭据文件绝对路径。排障用：用户常分不清该删哪个 auth.json
        （开发模式是 <项目根>/data/auth.json，打包后是 <exe 同级>/data/auth.json）。"""
        return self._file

    @property
    def is_initialized(self) -> bool:
        """首次启动未设置密码时为 False，前端据此切换到「设置管理密码」界面。

        每次都先与磁盘核对（_refresh）：密码文件被删除时必须立刻回到未设置态，
        否则前端永远看不到设置界面。
        """
        self._refresh()
        return self._initialized

    @property
    def admin_password(self) -> str | None:
        """历史兼容字段：旧版「本次启动刚生成随机密码」返回明文，新版返回 None。
        新版首次启动走 WebUI 设置流程，密码明文从不进入控制台。
        """
        return self._initial_password

    # ---------- 首次设置 ----------
    def setup_password(self, new_password: str) -> str:
        """首次设置管理密码：仅在 auth.json 不存在（未初始化）时可用。

        已初始化时调本方法返 409，提示用户走 change_password 流程（需要旧密码）。
        """
        self._refresh()                 # 文件已被删除 → 视为重新首次设置，而不是误判 409
        if self._initialized:
            raise HTTPException(409, "管理密码已设置，请走「修改密码」流程")
        if not new_password or len(new_password) < SETUP_MIN_LEN:
            raise HTTPException(400, f"管理密码至少 {SETUP_MIN_LEN} 位")
        salt = secrets.token_hex(16)
        self._cred = {"password_hash": _hash_password(new_password, bytes.fromhex(salt)),
                      "salt": salt, "token": secrets.token_urlsafe(32),
                      "issued_at": time.time()}
        write_atomic(self._file, json.dumps(self._cred).encode("utf-8"))   # 原子写入
        self._initialized = True
        return self._cred["token"]

    # ---------- 登录 / 改密 ----------
    def _verify(self, password: str) -> None:
        """校验密码（含滑动窗口限速），失败抛 401/429。登录与改密共用同一套防爆破。"""
        self._refresh()
        if not self._initialized:
            # 未初始化：登录请求统一返 428 引导前端走 setup 流程，不计入失败计数
            raise HTTPException(428, "首次启动，请先设置管理密码")
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
        if not new or len(new) < SETUP_MIN_LEN:
            raise HTTPException(400, f"新管理密码至少 {SETUP_MIN_LEN} 位")
        salt = secrets.token_hex(16)
        self._cred = {"password_hash": _hash_password(new, bytes.fromhex(salt)),
                      "salt": salt, "token": secrets.token_urlsafe(32),
                      "issued_at": time.time()}
        write_atomic(self._file, json.dumps(self._cred).encode("utf-8"))   # 原子写入
        return self._cred["token"]

    def _expired(self) -> bool:
        if not self._initialized:
            return False                 # 未初始化不参与过期判断（前端不会拿 token 调鉴权）
        return time.time() - self._cred.get("issued_at", time.time()) > AUTH_TTL

    # ---------- 鉴权 ----------
    def verify_http(self, cred: HTTPAuthorizationCredentials | None) -> None:
        self._refresh()
        if not self._initialized:
            # 未初始化态：任何鉴权请求一律 401，前端切回登录页 → 登录页再切到设置界面
            raise HTTPException(401, "未授权")
        if not cred or self._expired() or not _ct_eq(cred.credentials, self._cred["token"]):
            if cred and self._expired():
                raise HTTPException(401, "登录已过期，请重新登录")
            raise HTTPException(401, "未授权")

    def ws_handshake(self, ws: WebSocket) -> tuple[bool, str | None]:
        """WS 鉴权：优先 Sec-WebSocket-Protocol 头携带 token（不会进 access log，
        替代原先的 ?token= 查询参数——后者会连同 token 一起落 nginx/uvicorn 日志）。
        返回 (是否通过, 需回显的 subprotocol)；?token= 查询参数仍兼容旧前端。"""
        self._refresh()
        if not self._initialized:
            return False, None           # 未初始化：WS 一律拒绝（前端走 setup 流程，无 WS）
        proto = (ws.headers.get("sec-websocket-protocol") or "").split(",")[0].strip()
        tok = self._cred["token"]
        if self._expired():
            return False, None
        if proto and _ct_eq(proto, tok):
            return True, proto
        if ws.query_params.get("token") and _ct_eq(ws.query_params.get("token", ""), tok):
            return True, None
        return False, None

    def verify_ws(self, ws: WebSocket) -> bool:
        """旧入口：仅查询参数鉴权（保留兼容）。"""
        self._refresh()
        if not self._initialized:
            return False
        return not self._expired() and _ct_eq(ws.query_params.get("token", ""), self._cred["token"])


auth = Auth()

security = HTTPBearer(auto_error=False)

async def require_auth(cred: HTTPAuthorizationCredentials | None = Depends(security)):
    auth.verify_http(cred)
