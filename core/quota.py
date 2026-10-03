"""配额判定：登录端 QQ 号总数 + 应用端实例数，两个维度独立计额。

配额语义（决策：总数 + 分别设上限）
    login_qq  —— 该用户名下**所有登录端已登录 QQ 号的总数**上限
    app       —— 该用户名下**应用端（骰子端）实例数**上限
    -1 表示不限；管理员不校验配额。

关于「bot 数量 = 登录端 QQ 号数」的实现约束（重要，勿在实现中想当然）
    QQ 账号**不是面板创建的**。它是用户在登录端自带的 WebUI 里扫码登录产生的，
    面板只能通过适配器 list_accounts() **回读**已经存在的账号
    （见 adapters/napcat.py:list_accounts 扫onebot11_<qq>.json）。
    因此面板无法在「扫码那一刻」拦截，只能：
      A. 事后拦截 —— 回读发现超额则拒绝配对/建链，并把实例标为超额
      B. 事前拦截 —— 开放登录端 WebUI 之前先预检，已满则不放行
    两者结合即为最终方案（决策已定A+B）。

关于「登录端实例数」为什么不计入配额
    一个登录端实例可登多个 QQ，实例数与 QQ 数不是一回事。用户要管的是 QQ 数，
    故 login 维度只计 QQ 号数；若还要限制登录端实例数，可在此基础上扩展第三维。
"""
from core.roles import is_login_program


class QuotaExceeded(Exception):
    """配额超限。调用方转成 HTTP 400/409 并给出可操作提示。"""

    def __init__(self, message: str, limit: int, used: int, kind: str = ""):
        super().__init__(message)
        self.message = message
        self.limit = limit
        self.used = used
        self.kind = kind


def owner_of(rec: dict) -> str:
    """实例归属登录名。owner 缺失（迁移前的老实例）归admin。"""
    return rec.get("owner") or "admin"


def is_login_instance(rec: dict, adapters: dict) -> bool:
    return is_login_program(rec.get("dice", ""), adapters)


def usage(owner: str, records: list[dict], adapters: dict) -> dict:
    """统计某用户名当前的配额占用。

    login_qq 取该用户名名下**所有登录端**实例的 accounts 条数之和（去重 QQ 号——
      同一 QQ 号理论上不会同时出现在两个登录端，去重是防御性写法）。
    app 取名下应用端（骰子端）实例数。
    """
    qq: set[str] = set()
    app = 0
    for rec in records:
        if owner_of(rec) != owner:
            continue
        if is_login_instance(rec, adapters):
            for a in (rec.get("accounts") or []):
                # accounts 的规范形状是 [{qq, token, port, status}]，但它由各适配器
                # list_accounts() 回读写盘，历史数据/第三方适配器可能写成裸 QQ 号字符串
                # 或 {qq: status} 映射。宁可漏算也不能让 /api/me 500——配额是展示性拦截，
                # 真正的兜底是创建时的A 段回读校验。
                q = str(a.get("qq") or "") if isinstance(a, dict) else str(a or "")
                if q:
                    qq.add(q)
        else:
            app += 1
    return {"login_qq": len(qq), "app": app}


def check(owner: str, quota: dict, records: list[dict], adapters: dict,
          add_app: int = 0, add_login_qq: int = 0) -> dict:
    """校验配额，够则放行、不够抛 QuotaExceeded。

    add_app / add_login_qq 是「即将新增」的增量（还没落盘时先预检，避免
    先创建再回滚——实例创建会分配端口、写目录、落盘，回滚代价高）。
    管理员直接放行。
    """
    if (quota.get("login_qq", -1) < 0 and quota.get("app", -1) < 0):
        return usage(owner, records, adapters)
    cur = usage(owner, records, adapters)

    lim_qq = quota.get("login_qq", -1)
    if lim_qq >= 0 and cur["login_qq"] + add_login_qq > lim_qq:
        raise QuotaExceeded(
            f"登录端 QQ 号已达上限（{cur['login_qq']}/{lim_qq}），无法继续添加；"
            "如需更多请让管理员在「账号管理」中提额",
            lim_qq, cur["login_qq"], "login_qq")

    lim_app = quota.get("app", -1)
    if lim_app >= 0 and cur["app"] + add_app > lim_app:
        raise QuotaExceeded(
            f"应用端实例已达上限（{cur['app']}/{lim_app}），无法继续创建；"
            "如需更多请让管理员在「账号管理」中提额",
            lim_app, cur["app"], "app")

    return cur


def summarize(owners: list[str], quota_map: dict, records: list[dict],
              adapters: dict) -> dict:
    """给管理员的账号列表用：每个用户的配额 + 当前占用。"""
    out = {}
    for name in owners:
        q = quota_map.get(name) or {}
        out[name] = {**usage(name, records, adapters), "quota": dict(q)}
    return out
