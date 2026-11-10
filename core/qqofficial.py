"""QQ 开放平台机器人：换 access_token + 判读错误码。

为什么单独成模块：换 token 是官方通道里**唯一能在骰子程序之外离线自检的一步**。
AppID/AppSecret 填错、机器人被删/被封、服务器出口 IP 不在白名单，都会在这一步
（或紧随其后的网关握手里）暴露。把它做成向导里的「测试连通性」，用户就不必先去
Dice-Next 翻日志才知道凭据是坏的。

边界：本模块**不参与收发消息**——Dice-Next 3.x 自带 qq_official 适配器（WS 网关、
intents、富媒体都在程序内部），面板只负责把凭据写进它的 config/adapters.json。
所以这里只有 HTTP 鉴权这一条腿。

官方文档：https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html
协议细节（含 WS / 错误码全表）：docs/notes/qq-official-bot-protocol.md
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

# 官方 2026-08-10 起推荐 api.bot.qq.com；bots.qq.com 是旧域名但仍可用（botpy 还在用）。
# 按顺序试：新域名先解析，命中旧域名说明官方又在切域名，不至于直接报「网络不可达」。
TOKEN_ENDPOINTS = (
    "https://api.bot.qq.com/app/getAppAccessToken",
    "https://bots.qq.com/app/getAppAccessToken",
)

# 官方失败响应仍是 HTTP 200，只能靠 code 判读（qq-official-bot-protocol.md §1.2）
_CODE_HINT = {
    100001: "请求太频繁（code 100001），等一分钟再试；反复出现说明有别的程序在疯狂换 token。",
    100007: "AppID 无效，或该机器人已被封禁/删除（code 100007）。请到开放平台确认机器人还在。",
    100016: "AppID 与 AppSecret 不匹配（code 100016）。AppSecret 只能从开放平台「开发设置」里"
            "重新生成/复制，注意别把 QQ 互联的 AppKey 当成它。",
    10004: "机器人不存在（code 10004）。请确认用的是「机器人」页的 AppID，不是 QQ 互联应用的 ID。",
}


def _post_json(url: str, body: dict, timeout: float) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def verify_official_credentials(app_id: str, app_secret: str,
                                timeout: float = 12.0) -> dict:
    """用 AppID/AppSecret 换一次 access_token，返回可直接展示给用户的结果。

    返回 {"ok": bool, "message": str, "code": int|None, "expires_in": int|None}。
    不外泄 secret；token 本身也不返回——前端只需要「能用/不能用 + 为什么」。
    """
    app_id = str(app_id or "").strip()
    app_secret = str(app_secret or "").strip()
    if not app_id or not app_secret:
        return {"ok": False, "code": None, "expires_in": None,
                "message": "请先填 AppID 与 AppSecret（两者都在开放平台「开发设置」页）。"}

    body = {"appId": app_id, "clientSecret": app_secret}
    errors: list[str] = []
    data: Any = None
    for url in TOKEN_ENDPOINTS:
        try:
            data = _post_json(url, body, timeout)
            break
        except urllib.error.HTTPError as e:                 # 4xx/5xx：读回包，别只看状态码
            try:
                data = json.loads(e.read().decode("utf-8") or "{}")
            except Exception:
                errors.append(f"{url.split('/')[2]} 返回 HTTP {e.code}")
            if data:
                break
        except Exception as e:                              # 超时/DNS/TLS：换域名再试
            errors.append(f"{url.split('/')[2]}: {type(e).__name__} {e}")
    if data is None:
        return {"ok": False, "code": None, "expires_in": None,
                "message": "连不上官方鉴权接口（" + "；".join(errors[:2]) + "）。"
                           "先确认这台服务器能出公网，再检查开放平台是否配了 IP 白名单。"}
    if not isinstance(data, dict):                          # 回包不是对象：别让 .get 把接口打成 500
        return {"ok": False, "code": None, "expires_in": None,
                "message": "官方鉴权接口返回了看不懂的内容，稍后重试；持续如此请查官方公告。"}

    if data.get("access_token"):
        # expires_in 是字符串 "7200"，必须 int()（botpy 同款处理）
        try:
            expires = int(data.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires = 0
        mins = f"，有效期约 {expires // 60} 分钟" if expires else ""
        return {"ok": True, "code": None, "expires_in": expires or None,
                "message": f"凭据有效：官方已签发 access_token{mins}。"}

    code = data.get("code")
    try:
        code_i = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_i = None
    hint = _CODE_HINT.get(code_i) if code_i is not None else None
    detail = data.get("message") or data.get("msg") or ""
    if not hint:
        hint = f"官方拒绝了这次鉴权（code={code_i if code_i is not None else '未知'}"
        hint += f"，{detail}）。" if detail else "）。"
    return {"ok": False, "code": code_i, "expires_in": None, "message": hint}
