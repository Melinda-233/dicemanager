# QQ 官方机器人（QQ Bot / api-v2）接入协议技术笔记

> 目的：为 DiceManager 给 Dice-Next 增加「QQ 官方机器人」接入通道提供**可直接照抄**的协议细节，并在第 8 节给出「官方事件 → OneBot v11」的翻译方案。
>
> 抓取日期：**2026-11-10**（Asia/Shanghai）。全部事实来自官方文档 + 官方 SDK 源码交叉验证，来源 URL 见每节末尾与第 10 节。
> 官方文档会持续改版，接入前请复核「上次更新」时间。

---

## 0. 一句话总览与最容易踩的三个坑

- 官方 Bot 的协议是「**HTTP OpenAPI（api.bot.qq.com）+ WebSocket（或 Webhook）事件流 + Intents 位掩码订阅**」，与 OneBot v11 的「反向 WS 上报事件」是两套完全不同的东西，必须写适配层。
- 坑 1：**域名换代**。官方 2026-08-10 起把接口域名统一为 `api.bot.qq.com`（`https://bot.q.qq.com/wiki/develop/api-v2/changelog.html`）；旧域名 `bots.qq.com`（取 token）与 `api.sgroup.qq.com`（业务 API）在 botpy 源码里仍在使用，属于「仍可用但非文档推荐」。沙箱域名为 `sandbox.api.sgroup.qq.com`。
- 坑 2：**业务错误也返回 HTTP 200**。鉴权接口错误必须以响应体 `code` 判定，HTTP 状态码只能当粗筛。
- 坑 3：**群聊默认只给「@机器人」的消息**。`GROUP_AT_MESSAGE_CREATE` 的 `content` 已自动去掉 @ 前缀；要拿全量群消息得让群主在客户端打开「接收全量消息」，此时走 `GROUP_MESSAGE_CREATE`（2026-10 新增）。

---

## 1. 鉴权：AppID + AppSecret → AccessToken

### 1.1 凭证来源

在 [QQ 开放平台](https://q.qq.com/#/) 创建机器人后得到两个票据（`https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/getting-started.html`）：

| 名称 | 描述 | 备注 |
| --- | --- | --- |
| AppID | 机器人 ID | 必须使用 |
| AppSecret | 机器人密钥 | 用于请求签名的密钥（也用于换 AccessToken 与 Webhook 签名） |

官方明确：「**Token 的鉴权方式已废弃**，请使用更安全的 Access Token 鉴权方式」。

### 1.2 获取 AccessToken（HTTP）

```
POST https://api.bot.qq.com/app/getAppAccessToken
Content-Type: application/json
```

请求体（**字段名是 `appId` / `clientSecret`，不是 `appid` / `secret`**）：

```json
{
  "appId": "102012345",
  "clientSecret": "DG5g3B4j9X2KOErGxxxxxxxx"
}
```

成功响应：

```json
{
  "access_token": "Rg0v9F3v.....",
  "expires_in": "7200"
}
```

> 注意：官方示例里 `expires_in` 是**字符串** `"7200"`，解析时必须 `int()`。botpy 也是 `int(data["expires_in"])`（`botpy/robot.py`）。

失败响应仍是 HTTP 200，靠 `code` 判定：

| code | 含义 | 处理 |
| --- | --- | --- |
| 100001 | Too many requests | 退避重试（指数退避） |
| 100007 | appid invalid | AppID 错误，或机器人已被封禁/删除 → 人工介入 |
| 100016 | invalid appid or secret | AppID/AppSecret 不匹配 → 人工核对 |
| 10004 | 机器人不存在 | 人工核查 |

来源：`https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html`（页面「上次更新 9/16/2026」）。

### 1.3 后续请求头

```
Authorization: QQBot {ACCESS_TOKEN}
```

（注意是 `QQBot` + 一个空格 + token，大小写敏感；botpy 的 `Token.get_string()` 实现即 `"{} {}".format("QQBot", access_token)`）

### 1.4 刷新策略（官方原文语义）

- 有效期 7200s。
- **有效期内重复获取会返回同一个 token**（无需担心并发多请求换新）。
- 距过期 ≤ 60s 时再获取会签发新 token，旧 token 在这 60s 内仍有效 → 可以做「平滑切换」，不需要停机。
- 建议服务端定时刷新（例如每 30 分钟一次），而不是每个请求都去换。

Python（aiohttp）实现：

```python
import time
import aiohttp

class BotToken:
    BASE = "https://api.bot.qq.com"      # 旧域名：https://bots.qq.com

    def __init__(self, appid: str, secret: str):
        self.appid = appid
        self.secret = secret
        self.token: str | None = None
        self.expire_at: float = 0.0

    async def get(self) -> str:
        # 提前 120s 刷新（官方：距过期 60s 内会签新 token）
        if self.token and time.time() < self.expire_at - 120:
            return self.token
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{self.BASE}/app/getAppAccessToken",
                json={"appId": self.appid, "clientSecret": self.secret},
                timeout=aiohttp.ClientTimeout(total=20),   # botpy 用 20s
            ) as r:
                data = await r.json(content_type=None)
        if not data.get("access_token"):
            raise RuntimeError(f"getAppAccessToken failed: {data}")
        self.token = data["access_token"]
        self.expire_at = time.time() + int(data["expires_in"])  # 必须 int()
        return self.token

    async def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"QQBot {await self.get()}"}
```

---

## 2. 事件通道

### 2.1 两种接入方式对比

| 方式 | 适用 | 端口/连接 | 说明 |
| --- | --- | --- | --- |
| WebSocket（推荐给常驻进程） | 常驻服务 | 出站长连接 | 低延迟，有 `s` 序列号补发机制（Resume） |
| Webhook | 需要公网 HTTPS | 仅允许 80 / 443 / 8080 / 8443 | 平台主动 POST，必须校验 Ed25519 签名 |

DiceManager 是长驻 Python 进程，**选 WebSocket**；Webhook 只在必须过反代/CDN 时考虑。

### 2.2 获取网关地址

| 接口 | 返回 | 用途 |
| --- | --- | --- |
| `GET /gateway` | `{"url": "wss://api.bot.qq.com/websocket/"}` | 不用分片时 |
| `GET /gateway/bot` | `url` + `shards` + `session_start_limit` | 需要分片时 |

`GET /gateway/bot` 返回（`session_start_limit`）：

| 字段 | 含义 |
| --- | --- |
| `total` | 每 24 小时可创建 Session 数（示例 1000） |
| `remaining` | 目前还可创建数；**每机器人连接数不能超过 remaining** |
| `reset_after` | 重置计数剩余毫秒 |
| `max_concurrency` | 每 5s 可创建的 Session 数（示例 1） |

> 单机器人场景直接用 `/gateway`，`shard` 传 `[0, 1]`，不要被动拉分片。

### 2.3 连接时序

```
GET /gateway  ──▶ wss://api.bot.qq.com/websocket/
   │
   ├─ 连上后立刻收到  op=10 Hello  {"heartbeat_interval": 45000}
   ├─ 发 op=2 Identify {token,intents,shard,properties}
   ├─ 收 op=0 t=READY {session_id, user, shard}      ← 拿到 session_id 与机器人自身信息
   ├─ 按 heartbeat_interval 定时发 op=1 {d: 最新 s}   ← 收 op=11 ACK
   └─ 断线重连：op=6 Resume {token, session_id, seq} ← 补发后收 t=RESUMED
```

### 2.4 OpCode 全表（官方 `payload.html`）

| CODE | 名称 | 接入方式 | 客户端行为 | 描述 |
| --- | --- | --- | --- | --- |
| 0 | Dispatch | webhook/websocket | Receive | 服务端事件推送 |
| 1 | Heartbeat | websocket | Send/Receive | 心跳 |
| 2 | Identify | websocket | Send | 鉴权 |
| 6 | Resume | websocket | Send | 恢复连接 |
| 7 | Reconnect | websocket | Receive | 服务端要求重连 |
| 9 | Invalid Session | websocket | Receive | identify/resume 参数错 |
| 10 | Hello | websocket | Receive | 建连后第一条，给心跳周期 |
| 11 | Heartbeat ACK | websocket | Receive/Reply | 心跳回执 |
| 12 | HTTP Callback ACK | **webhook** | Reply | HTTP 回调模式的回包（= 已收到） |
| 13 | 回调地址验证 | **webhook** | Receive | 平台验证回调地址 |

> 关于 prompt 里的「12？」：**12 不是 WS 的 op，而是 Webhook 回执**；13 是回调地址校验请求。

### 2.5 各报文 payload

Hello（服务端 → 客户端）：

```json
{"op": 10, "d": {"heartbeat_interval": 45000}}
```

Identify（客户端 → 服务端）：

```json
{
  "op": 2,
  "d": {
    "token": "QQBot xxxxxxxx",
    "intents": 33554432,
    "shard": [0, 1],
    "properties": {"$os": "linux", "$browser": "dicemanager", "$device": "dicemanager"}
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `token` | `"QQBot {AccessToken}"` |
| `intents` | 位掩码，见第 3 节 |
| `shard` | `[当前分片, 分片总数]`，如 `[0,4]`；不分片用 `[0,1]` |
| `properties` | 官方注明「目前无实际作用」，可随意填或留空 |

READY（`op=0, t=READY`）：

```json
{
  "op": 0, "s": 1, "t": "READY",
  "d": {
    "version": 1,
    "session_id": "082ee18c-0be3-491b-9d8b-fbd95c51673a",
    "user": {"id": "6158788878435714165", "username": "群pro测试机器人", "bot": true},
    "shard": [0, 0]
  }
}
```

心跳（客户端 → 服务端），`d` = 客户端收到的**最新 `s`**，首次为 `null`：

```json
{"op": 1, "d": 251}
```

心跳回执：`{"op": 11}`

Resume（客户端 → 服务端）：

```json
{"op": 6, "d": {"token": "QQBot xxx", "session_id": "session_id_i_stored", "seq": 1337}}
```

恢复完成：`{"op": 0, "s": 2002, "t": "RESUMED", "d": ""}`（补发完遗漏事件后才下发）。

### 2.6 通用水位结构（payload）与事件 id

```json
{"id": "event_id", "op": 0, "d": {}, "s": 42, "t": "EVENT_NAME"}
```

| 字段 | 说明 |
| --- | --- |
| `id` | 事件 id（**Webhook 模式下的幂等键**；WS 下事件去重主要靠业务字段，见 4.7） |
| `op` | opcode |
| `s` | 下行消息序列号，心跳与 resume 都用它；**处理完事件后再记录 `s`** |
| `t` | 事件类型，仅 `op=0` 时有意义 |
| `d` | 事件内容，结构随 `t` 变化 |

### 2.7 WebSocket 错误码与重连策略（官方原表）

| Code | 含义 | 可 RESUME | 可 IDENTIFY |
| --- | --- | --- | --- |
| 4001 | 无效的 opcode | 否 | 否 |
| 4002 | 无效的 payload | 否 | 否 |
| 4006 | 无效 session id，需 identify | 否 | 是 |
| 4007 | seq 错误 | 否 | 是 |
| 4008 | 发送 payload 过快 | 是 | 是 |
| 4009 | 连接过期，重连并 resume | 是 | 是 |
| 4010 | 无效的 shard | 否 | 否 |
| 4011 | 单连接 guild 过多 | 否 | 否 |
| 4012 | 无效的 version | 否 | 否 |
| 4013 | 无效的 intent | 否 | 否 |
| 4014 | intent 无权限 | 否 | 否 |
| 4900~4913 | 内部错误，请重连 | 否 | 是 |
| 4914 | 机器人已下架，只允许连沙箱环境 | 否 | 否 |
| 4915 | 机器人已封禁 | 否 | 否 |

官方处理建议：4009 → 重新 resume；4914/4915 → 不可连接；其他 → 重新 identify。

工程建议（非官方）：把 `4008` 当作「发太快」，重连后先 `asyncio.sleep(1~5)`；把 `4014` 当作配置错误（intents 未申请权限），不要无脑重连。

### 2.8 Webhook 模式 + Ed25519 签名

**回调地址校验（`op=13`）**

平台请求：

```
headers: User-Agent: QQBot-Callback   X-Bot-Appid: 11111111
body: {"d":{"plain_token":"Arq0D5A61EgUu4OxUvOp","event_ts":"1725442341"},"op":13}
```

机器人需返回：

```json
{"plain_token": "Arq0D5A61EgUu4OxUvOp", "signature": "87befc99...."}
```

签名算法（官方 Go 示例的等价语义）：

1. `seed = botSecret` 反复 `repeat` 直到长度 ≥ 32，取前 32 字节；
2. 用 `seed` 作为 Ed25519 seed 生成密钥对（`ed25519.GenerateKey(reader)`）；
3. `msg = event_ts + plain_token`，用私钥签名，**hex 小写**输出。

**普通回调的验签**

| 来源 | 值 |
| --- | --- |
| `X-Signature-Ed25519` | 签名（hex，64 字节） |
| `X-Signature-Timestamp` | 时间戳字符串 |
| HTTP Body | 原始请求体 |

验签体为 `timestamp + body`（先 timestamp 再 body，**用原始 bytes**）：

```python
import hashlib
from nacl.signing import SigningKey, VerifyKey
from nacl.exceptions import BadSignatureError

def _seed(secret: str) -> bytes:
    seed = secret.encode()
    while len(seed) < 32:
        seed = seed * 2
    return seed[:32]

def sign_validation(secret: str, plain_token: str, event_ts: str) -> str:
    sk = SigningKey(_seed(secret))
    return sk.sign((event_ts + plain_token).encode()).signature.hex()

def verify_callback(secret: str, sig_hex: str, timestamp: str, raw_body: bytes) -> bool:
    try:
        sig = bytes.fromhex(sig_hex)
    except ValueError:
        return False
    if len(sig) != 64:
        return False
    vk = SigningKey(_seed(secret)).verify_key
    try:
        VerifyKey(bytes(vk)).verify(timestamp.encode() + raw_body, sig)
        return True
    except BadSignatureError:
        return False
```

（Go 示例里还有一处 sanity check：`sig[63]&224 != 0` 视为非法签名，Python 侧可省略。）

Webhook 回执：普通事件处理后返回 HTTP 200 + `{"op": 12}`（`HTTP Callback ACK`）。

来源：`https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/webhook.html`、`.../interface-framework/sign.html`。

---

## 3. Intents 位掩码

### 3.1 现行取值（`payload.html`，7/21/2026 版）

| 名称 | 位 | 值 | 事件 |
| --- | --- | --- | --- |
| `GUILDS` | 1<<0 | 1 | GUILD_CREATE/UPDATE/DELETE, CHANNEL_CREATE/UPDATE/DELETE |
| `GUILD_MEMBERS` | 1<<1 | 2 | GUILD_MEMBER_ADD/UPDATE/REMOVE |
| `GUILD_MESSAGES` | 1<<9 | 512 | MESSAGE_CREATE / MESSAGE_DELETE（**仅私域**） |
| `GUILD_MESSAGE_REACTIONS` | 1<<10 | 1024 | MESSAGE_REACTION_ADD/REMOVE |
| `DIRECT_MESSAGE` | 1<<12 | 4096 | DIRECT_MESSAGE_CREATE/DELETE（频道私信） |
| `GROUP_AND_C2C_EVENT` | 1<<25 | 33554432 | C2C_MESSAGE_CREATE, FRIEND_ADD/DEL, C2C_MSG_REJECT/RECEIVE, GROUP_AT_MESSAGE_CREATE, GROUP_ADD_ROBOT/DEL_ROBOT, GROUP_MSG_REJECT/RECEIVE |
| `INTERACTION` | 1<<26 | 67108864 | INTERACTION_CREATE |
| `MESSAGE_AUDIT` | 1<<27 | 134217728 | MESSAGE_AUDIT_PASS/REJECT |
| `FORUMS_EVENT` | 1<<28 | 268435456 | FORUM_*（**仅私域**） |
| `AUDIO_ACTION` | 1<<29 | 536870912 | AUDIO_START/FINISH/ON_MIC/OFF_MIC |
| `PUBLIC_GUILD_MESSAGES` | 1<<30 | 1073741824 | AT_MESSAGE_CREATE, PUBLIC_MESSAGE_DELETE |

**我们只需要 `GROUP_AND_C2C_EVENT = 1<<25 = 33554432`**（群 @ + 单聊 + 入群/退群事件）。要同时收频道 @ 消息再加 `1<<30`。

> 注意：prompt 里提到的 `GUILD_MESSAGES=1<<9`、`GUILD_MESSAGE_REACTIONS=1<<10`、`DIRECT_MESSAGE=1<<12`、`PUBLIC_GUILD_MESSAGES=1<<30` 均正确；但 `OPEN_FORUMS_EVENT=1<<18`、`AUDIO_ACTION=1<<20` 属于**旧版（v1）取值**，现行 v2 文档中音频事件是 `1<<29`，`1<<20` 已不再出现（botpy 的 `Intents` 类里也没有 1<<20）。botpy 另有 `open_forum_event = 1<<18`、`audio_or_live_channel_member = 1<<19`，这两个是 SDK 保留位，官方 `payload.html` 的 intents 表里**未列出**（以官方表为准，用不到就别传）。

### 3.2 权限规则（会被 ws 直接断连）

- 默认就有权限的基础事件只有三个：`GUILDS`、`PUBLIC_GUILD_MESSAGES`、`GUILD_MEMBERS`。
- 其他 intents（**包括 `GROUP_AND_C2C_EVENT` 群/单聊消息**）需要先在开放平台申请对应能力，否则 `Identify` 时 `websocket` 会报错并**直接关闭连接**（对应 4013/4014）。
- 权限被取消时：当前连接不报错，但收不到事件；**重连时才报错**。→ 适配层需要把 4013/4014 记为「配置错误」告警，而不是无限重连。
- 群场景还有一层「群主授权」：群主在添加机器人时的「设置权限」里可开关「接收全量消息」「主动推送消息」「撤回成员消息」。没开全量就只有 @ 消息，没开主动推送则主动消息发不出去。

来源：`https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/payload.html`、`https://bot.q.qq.com/wiki/bot_FAQ/`（Q：「在群里@机器人不回复，但私聊可以」）。

### 3.3 计算

```python
INTENT_GUILDS = 1 << 0
INTENT_GUILD_MEMBERS = 1 << 1
INTENT_DIRECT_MESSAGE = 1 << 12
INTENT_GROUP_AND_C2C = 1 << 25
INTENT_PUBLIC_GUILD_MESSAGES = 1 << 30

# Dice-Next 只需要群聊 + 单聊
intents = INTENT_GROUP_AND_C2C                       # 33554432
# 同时要频道 @：
intents = INTENT_GROUP_AND_C2C | INTENT_PUBLIC_GUILD_MESSAGES   # 1107296256
```

---

## 4. 事件类型与 payload

### 4.1 我们关心的消息事件

| 事件 | Intent | 场景 | 触发条件 |
| --- | --- | --- | --- |
| `C2C_MESSAGE_CREATE` | 1<<25 | 单聊 | 用户私聊机器人 |
| `GROUP_AT_MESSAGE_CREATE` | 1<<25 | 群聊 | 用户 **@机器人**（`content` 已去 @ 前缀） |
| `GROUP_MESSAGE_CREATE` | 1<<25 | 群聊 | 群主开启「接收全量消息」后的**每条**群消息（2026-10-21 文档） |
| `AT_MESSAGE_CREATE` | 1<<30 | 频道子频道 | @机器人或回复机器人消息 |
| `DIRECT_MESSAGE_CREATE` | 1<<12 | 频道私信 | 用户私信（**不支持沙箱**） |
| `FRIEND_ADD` / `FRIEND_DEL` | 1<<25 | 单聊 | 用户添加/删除机器人 |
| `GROUP_ADD_ROBOT` / `GROUP_DEL_ROBOT` | 1<<25 | 群聊 | 机器人被拉入/移出群 |
| `C2C_MSG_REJECT/RECEIVE`、`GROUP_MSG_REJECT/RECEIVE` | 1<<25 | — | 用户/群管开关「主动消息」 |

### 4.2 `C2C_MESSAGE_CREATE`（单聊）

```json
{
  "id": "ROBOT1.0_xxxxxxxxxxxxxxxxxxxxxxxx",
  "author": {
    "id": "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4",
    "user_openid": "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4",
    "union_openid": "",
    "username": "",
    "bot": false
  },
  "content": "你好，今天有什么推荐的活动吗？",
  "message_type": 0,
  "message_scene": {"source": "default", "ext": ["msg_idx=REFIDX_xxxxxxxxxxxxxxx=="]},
  "timestamp": "2026-07-21T10:00:00+08:00"
}
```

字段表：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 消息 ID，用于**被动回复**与撤回（放进发消息的 `msg_id`） |
| `author` | User | 发送者；单聊场景 `user_openid` 有值 |
| `content` | string | 文本内容（纯文本消息） |
| `timestamp` | string | RFC3339，带 `+08:00` |
| `message_type` | int | 0=普通文本，3=结构化卡片，101=并行消息，102=聊天记录，103=引用消息 |
| `message_scene.source` | string | 场景来源，`default`=默认聊天窗口 |
| `message_scene.ext` | string[] | `key=value` 列表：`msg_idx=`（消息索引）、`ref_msg_idx=`（引用的消息）、`auth_token=`（鉴权令牌） |
| `attachments` | Attachment[] | 图片/视频/语音/文件（见 4.5） |
| `ark_data` | ARKData | `message_type=3` 时的卡片数据 |
| `msg_elements` | MsgElement[] | `message_type=103` 时携带被引用内容 |

### 4.3 `GROUP_AT_MESSAGE_CREATE`（群 @ 机器人）

字段与 C2C 相同，额外多 `group_openid` 与 `mentions`：

```json
{
  "id": "ROBOT1.0_yyyy",
  "author": {
    "id": "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4",
    "member_openid": "A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4",
    "member_role": "member",
    "username": "小明",
    "bot": false
  },
  "content": " /今日天气 ",
  "group_openid": "B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5",
  "message_type": 0,
  "timestamp": "2026-07-21T10:00:00+08:00",
  "message_scene": {"source": "default", "ext": ["msg_idx=REFIDX_xxx==", "auth_token=xxx"]}
}
```

- `content`：**已自动去除 @机器人的前缀**（官方原文）。注意示例里首尾仍可能有空格。
- `mentions`：消息里 @ 的其他用户列表（**不含机器人自己**），元素是 `User` 对象。
- `author.member_openid`：群内成员身份；`author.member_role`：`member` / `admin` / `owner`。

### 4.4 `GROUP_MESSAGE_CREATE`（群全量模式）

字段与 `GROUP_AT_MESSAGE_CREATE` **完全一致**，区别只在触发条件（群里每条消息都推）。但有两个关键差异：

- 别人被 @ 时，`content` 中会替换为 `<@` + 该成员 `member_openid` + `>`，例如 `<@7A3B9C1D5E2F4A6B8C0D1E3F5A7B9C2D>`；
- @全体成员会替换为 `<@all>`；被 @ 的成员列表看 `mentions`。

→ 适配层必须把 `<@openid>` / `<@all>` 解析成 OneBot 的 `at` 段，否则骰子指令里会混进尖括号垃圾。

### 4.5 公共子结构

`User`（author / mentions / msg_elements.author 通用）：

| 字段 | 说明 |
| --- | --- |
| `id` | 用户唯一标识（OpenID 格式） |
| `username` | 昵称（可能为空串，官方示例里就是 `""`） |
| `bot` | 是否机器人 |
| `union_openid` | 跨应用统一 OpenID（**可能为空**，需邮件申请绑定） |
| `union_user_account` | 跨应用统一账号（可能为空） |
| `user_openid` | 单聊场景使用 |
| `member_openid` | 群聊场景使用 |
| `member_role` | `member` / `admin` / `owner` |

`MessageAttachment`：

| 字段 | 说明 |
| --- | --- |
| `url` | 下载 URL |
| `filename` | 文件名 |
| `width` / `height` | 图片宽高（非图片无此字段） |
| `size` | 字节数 |
| `content_type` | `voice` / `image/jpeg` / `image/png` / `image/gif` / `video/mp4` / `file` |
| `voice_wav_url` | 语音转 WAV 的 URL |
| `asr_refer_text` | 语音 ASR 参考结果 |

`MessageScene.ext`：字符串数组，元素形如 `msg_idx=REFIDX_...==`、`ref_msg_idx=...`、`auth_token=...`；解析时按第一个 `=` 切分（值里可能还有 `=`）。

### 4.6 频道消息 `AT_MESSAGE_CREATE`（intent 1<<30）

内容为 `Message` 对象（与 OneBot 最接近的一种）：

```json
{
  "author": {"avatar": "http://thirdqq.qlogo.cn/0", "bot": false, "id": "1234", "username": "abc"},
  "channel_id": "100010",
  "content": "ndnnd",
  "guild_id": "18700000000001",
  "id": "0812345677890abcdef",
  "member": {"joined_at": "2021-04-12T16:34:42+08:00", "roles": ["1"]},
  "timestamp": "2021-05-20T15:14:58+08:00",
  "seq": 101
}
```

- `id` 用于被动回复；`channel_id` 用于发送（`POST /channels/{channel_id}/messages`）；`guild_id` 是频道 ID。
- 官方声明消息**不保证严格有序**，需要顺序时自行按 `seq` 缓冲排序。
- `content` 内嵌格式见 6.1，且 `& < >` 会被转义成 `&amp; &lt; &gt;`。

### 4.7 去重与被动回复窗口（**最容易翻车的地方**）

- 官方：「为确保消息可达，相同 `msg_id` 可能重复推送」→ 同一事件可能来两次，必须业务去重。
- 去重键：C2C 文档正文说用 `message_scene.ext` 里的 `msg_idx`，同页开头又说「结合 `msg_seq`」；`message/overview.html` 说「结合 `msg_seq` 去重」。→ **建议用 `msg_idx`（每条消息唯一）做主去重键，`msg_seq` 只用于回复计数**。
- 被动回复窗口：

| 场景 | 有效期 | 每条消息最多回复次数 |
| --- | --- | --- |
| 单聊 | 60 分钟 | 4 次 |
| 群聊 | 5 分钟 | 5 次 |
| 频道 | 5 分钟 | — |

> 文档内部冲突：`v2_users_user_openid_messages.post.html` 的 `msg_id` 字段说明写「5 分钟内有效」，与 `message/overview.html` 的「单聊 60 分钟」矛盾。**实现按 5 分钟保守处理**，超时错误码是 `40034005` / `40034128`。

- 回复超过次数/超时的错误：`40034128`（被动回复时间或次数超限）。

---

## 5. 发送消息 API

### 5.1 端点

| 场景 | 方法与路径 | 频率 |
| --- | --- | --- |
| 单聊 | `POST /v2/users/{user_openid}/messages` | 100 QPS（含主被动） |
| 群聊 | `POST /v2/groups/{group_openid}/messages` | 100 QPS |
| 频道 | `POST /channels/{channel_id}/messages` | 5 条/秒/子频道 |
| 单聊上传 | `POST /v2/users/{user_openid}/files` | — |
| 群聊上传 | `POST /v2/groups/{group_openid}/files` | — |
| 撤回单聊 | `DELETE /v2/users/{user_openid}/messages/{message_id}` | 2 分钟内 |
| 撤回群聊 | `DELETE /v2/groups/{group_openid}/messages/{message_id}` | 2 分钟内 |

所有路径都拼在 `https://api.bot.qq.com` 后面。

### 5.2 请求体字段（单聊最全，群聊无 `msg_type=6`）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `msg_type` | int | 0=文本(`content`) 2=Markdown(`markdown`) 6=输入中(`input_notify`, **仅单聊**) 7=富媒体(`media`) 8=图文卡片(`card`) |
| `content` | string | 文本；**传了 markdown 后必须为空** |
| `markdown` | object | `{content, template_id(已废弃), custom_template_id(已废弃), force_verify_image_resource}` |
| `keyboard` | object | `{id}` 短形式或 `{content:{rows:[...]}}` 长形式 |
| `msg_id` | string | 被动回复的消息 ID（来自事件的 `d.id`） |
| `event_id` | string | 被动回复的事件 ID（与 `msg_id` 二选一）；单聊支持 `INTERACTION_CREATE`/`C2C_MSG_RECEIVE`/`FRIEND_ADD`，群聊支持 `INTERACTION_CREATE`/`GROUP_ADD_ROBOT`/`GROUP_MSG_RECEIVE` |
| `msg_seq` | int | 回复序号，与 `msg_id` 联用，**默认 1**；相同 `msg_id + msg_seq` 重复发送会失败 |
| `media` | object | `{file_info}`（`msg_type=7`） |
| `message_reference` | object | `{message_id}` 引用回复 |
| `is_wakeup` | bool | 互动召回消息，与 `msg_id`/`event_id` 互斥 |
| `card` | object | `msg_type=8` 必填：`{type:"tuwen", content:{title(1-50), description(1-200), url(https,≤1024), pic_url(https,≤1024)}}` |
| `input_notify` | object | `msg_type=6`：`{input_type:1, input_second:≤60}` |

响应：

```json
{
  "id": "ROBOT1.0_xxxxxxxx",
  "timestamp": "2026-07-21T10:30:00+08:00",
  "ext_info": {"ref_idx": "REFIDX_xxxxxxxxxxxx=="}
}
```

`id` 可用于后续撤回；`ext_info.ref_idx` 可用于后续 `message_reference.message_id`。

### 5.3 `msg_seq` 到底干什么

它是「**同一条用户消息的第几次回复**」的序号：

- 被动回复必须带 `msg_id`；只要再带不同 `msg_seq`，就能对同一条消息回多次（单聊最多 4 次、群聊最多 5 次）。
- 相同 `msg_id + msg_seq` 重复发送 **会直接失败**（`40054005 消息被去重`）——这既是保护（防重试重发），也是坑（重试时必须**换** `msg_seq`，或先查是否已成功）。
- 我们的适配层应按 `msg_id` 维护一个自增计数器，每次发送 `msg_seq += 1`，不要固定传 1。

```python
from collections import defaultdict

class PassiveSeq:
    def __init__(self):
        self._c: dict[str, int] = defaultdict(int)

    def next(self, msg_id: str) -> int:
        self._c[msg_id] += 1
        return self._c[msg_id]
```

### 5.4 文本回复（Python 示例）

```python
import aiohttp

API = "https://api.bot.qq.com"

async def reply_group(header: dict, group_openid: str, msg_id: str, seq: int, text: str) -> dict:
    url = f"{API}/v2/groups/{group_openid}/messages"
    body = {
        "msg_type": 0,
        "content": text,
        "msg_id": msg_id,
        "msg_seq": seq,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=body, headers=header, timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = await r.json(content_type=None)
            if r.status >= 300 or data.get("err_code"):
                raise RuntimeError(f"send failed http={r.status} body={data}")
            return data
```

### 5.5 富媒体上传

流程（二选一）：

1. **URL 上传**（文件已公网可访问）：

```http
POST /v2/users/{user_openid}/files
{"file_type": 1, "url": "https://example.com/image.png"}
```

返回 `file_info`，再 `msg_type=7` + `media.file_info` 发送。

2. **分片上传**（本地文件/大文件，推荐）：`upload_prepare`（拿 `upload_id` / `block_size` / 预签名 URL）→ 逐片 `PUT` → 每片 `part_finish` → `POST .../files {upload_id}` 合并拿 `file_info`。

| `file_type` | 类型 | 允许格式 | 软限制 | 硬限制 |
| --- | --- | --- | --- | --- |
| 1 | 图片 | png / jpg | 20 MB | 200 MB |
| 2 | 视频 | mp4 | 30 MB | 200 MB |
| 3 | 语音 | silk（另有 mp3/wav/ogg 可发） | 20 MB | 200 MB |
| 4 | 文件 | 任意 | 200 MB | 200 MB |

其它约束：

- 超过软限制会**降级成文件**上传；超过硬限制报错。
- `file_info` 有 `ttl`，过期需重传；`md5_10m`（前 10002432 字节的 MD5）用于秒传判断。
- 分片默认 5 MB，上传接口超时建议 ≥ 5s。
- **单聊与群聊的上传接口互不通用**（单聊文件不能发到群）。
- 群聊富媒体 `msg_seq` 有示例传 2，说明同一消息先发文本再发图片需要递增。

来源：`https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/rich-media.html`。

### 5.6 发送相关错误码（真实、来自官方页）

| code | 含义 | 我们的处理 |
| --- | --- | --- |
| 22006 | 消息类型与内容不匹配 | 代码 bug |
| 304061 | 消息内容无效 | 内容清洗 |
| 304103 / 40034005 | 消息 ID 已过期，不能回复 | 被动窗口已过，改主动消息或丢弃 |
| 40034128 | 被动回复时间或次数超限 | 同上 |
| 40054005 | 消息被去重 | `msg_seq` 撞了，换序号 |
| 40054007 / 40054018 | 消息长度超限 / 过长或异常 | 截断 |
| 40054010 | 不允许发送 URL | 群聊内容过滤（**群消息里带链接会被拒**） |
| 40034101 / 40054003 | 机器人不是群成员 | 已被踢，清理会话 |
| 40054002 | 机器人被禁言 | 退避 |
| 40054004 | 无好友关系 | 单聊需先加好友 |
| 40054013 | 用户拒收消息 | 停止主动推送 |
| 40054016 | 机器人已下线 | 人工 |
| 40034024 | `msg_id` 无效或越权 | 检查取的是不是本会话的消息 |
| 40034100 | 主动消息超过频控 | 退避 + 降级 |
| 50055002 / 50055001 | 发送异常，稍后重试 | 重试 |

---

## 6. 消息内容格式

### 6.1 文本与内嵌格式

| 场景 | 语法 | 说明 |
| --- | --- | --- |
| 频道 @用户 | `<@user_id>` 或 `<@!user_id>` | 解析成 @用户；频道内 ID 是频道用户 id |
| 频道 @所有人 | `@everyone` | 需机器人有「@所有人」权限 |
| 频道 #子频道 | `<#channel_id>` | 仅当前频道内的子频道 |
| 频道表情 | `<emoji:id>` | 仅 `type=1` 系统表情（如 `<emoji:4>`）；`type=2` 直接填字符串 |
| 群/单聊 @ | **没有官方语法文档** | 群全量事件里的 `<@openid>` 是**接收侧**的呈现，发送侧是否支持未验证 |
| 群 @全体 | `<qqbot-at-everyone />` | 2026-10-26 新增：机器人须为**群管理员**，每群每天**仅 1 次** |
| 引用 | `message_reference: {message_id: "REFIDX_..."}` | 引用索引来自事件 `message_scene.ext` 的 `msg_idx`，或自己发消息响应里的 `ext_info.ref_idx` |

转义（频道）：抄送时 `&`→`&amp;`、`<`→`&lt;`、`>`→`&gt;`；发送时反向还原。→ 收到频道消息要先 `html.unescape` 再解析 `<@...>`。

群/单聊的 `content` 里**没有** OneBot 的 CQ 码；图片走 `attachments`（收）与 `media.file_info`（发），不是 URL 内嵌。

### 6.2 Markdown

- `msg_type=2` + `markdown.content`（`content` 必须为空、`ark` 必须为空）。
- 支持：标题（`#`/`##`）、粗体/斜体/删除线、链接、图片、有序/无序/嵌套列表、块引用、分割线、多行（`\u200B` 占位）。
- 图片：公网 URL 会被平台转存；无公网 URL 时先用「素材管理」上传拿 `qqbot-media://` 引用。
- 2026-04-23 起，**单聊/群聊自定义 Markdown 对所有机器人开放，无需申请模板**；频道场景仍需内邀。
- 收发能力：Markdown **群聊可收可发，单聊只能发不能收，频道只能发**。
- 模板消息用 `custom_template_id` + `params[{key,values}]`（模板能力为旧路径，`template_id`/`custom_template_id` 字段已标注「已废弃」）。

### 6.3 图片/语音/视频/文件

| 类型 | 收（事件） | 发（接口） |
| --- | --- | --- |
| 图片 | `attachments[].url`，`content_type=image/jpeg` 等 | 上传 `file_type=1` → `msg_type=7` |
| 语音 | `content_type=voice`，另有 `voice_wav_url`、`asr_refer_text` | `file_type=3`（silk 最佳） |
| 视频 | `content_type=video/mp4` | `file_type=2` |
| 文件 | `content_type=file` | `file_type=4` |

`attachments[].url` 是带 `rkey` 的临时下载链接，**会过期**，要尽快转存。

### 6.4 表情

- 频道支持系统表情（`<emoji:id>`）与表情表表态（`MESSAGE_REACTION_ADD/REMOVE`，intent 1<<10）。
- 群/单聊：消息类型文档明确表情表态「仅频道支持」，文本里的 emoji 只能当普通 unicode 字符发。

来源：`https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/type/overview.html`。

---

## 7. 平台限制与风控

### 7.1 主动 / 被动 / 召回

| 类型 | 判定 | 说明 |
| --- | --- | --- |
| 主动消息 | 不带 `msg_id`/`event_id` | 用户可在客户端关闭「允许主动发送」，关了就必失败 |
| 被动消息（回复消息） | 带 `msg_id` | 受窗口与次数限制 |
| 被动消息（响应事件） | 带 `event_id` | 受事件类型白名单限制 |
| 互动召回消息 | `is_wakeup=true` | 用户主动对话后 30 天内，分 4 个周期各 1 条 |

### 7.2 频控数值（官方表）

单聊主动：

| 认证 | Bot 维度 | 单关系维度 | 每日上限 |
| --- | --- | --- | --- |
| 企业 / 个人认证 | 10/qps | 20/qpm | 1000 条/用户 |
| 未认证 | 5/qps **且** 30/qpm | 20/qpm | 1000 条/用户 |

群聊主动：

| 认证 | Bot 维度 | 单关系维度 | 每日上限 |
| --- | --- | --- | --- |
| 企业 / 个人认证 | 60/qpm | 20/qpm | 1000 条/群 |
| 未认证 | 30/qpm | 20/qpm | 1000 条/群 |

频道（子频道）：

- 主动消息默认每子频道每天 **20 条**；每频道每天最多往 **2 个**子频道推送。
- 主/被动都限 **5 条/秒/子频道**。
- 频道私信：每用户每天 2 条主动，机器人每天累计 200 条。
- 发消息接口要求机器人**处于 WebSocket 在线状态**（否则 `304018 SESSION_NOT_EXIST`）。

### 7.3 沙箱环境 vs 正式环境

- 官方 FAQ（`https://bot.q.qq.com/wiki/bot_FAQ/`）：「使用管理员账号进入新版机器人管理端后，**管理员账号可直接测试使用，无需沙箱环境**（旧版管理端已不支持添加沙箱群/频道配置）」。
- 但协议层仍保留沙箱域名：botpy 的 `SANDBOX_DOMAIN = "sandbox.api.sgroup.qq.com"`，WS 错误码 `4914` 明确写「机器人已下架，只允许连接沙箱环境」。
- 频道私信 **不支持沙箱**（官方：私信场景无法设置沙箱频道，需用用户 id 白名单调试）。
- 结论：新接入直接连正式域名；只有在机器人未上架/被下架时才可能被迫用沙箱域名（`API_BASE=https://sandbox.api.sgroup.qq.com`）。**沙箱域名与新域名 `api.bot.qq.com` 的对应关系官方未给出，标注未验证。**

### 7.4 主体与可用范围（决定能不能上线）

- 个人主体：**暂不支持公开使用**；只能把机器人拉进「自己是群主/管理员」的群（需最新版手机 QQ 操作）。
- 个人主体要给人私聊用：在管理端「开发设置」里添加**开发体验用户号码**（白名单）。
- 企业主体：可公开使用；个人 5 个机器人，企业 10 个（同主体合计上限 50）。
- AppID 是机器人 ID（不是 QQ 号）。`READY.d.user.id` 是机器人自身账号 id（示例为 19 位数字，社区普遍当作机器人 QQ 号；官方未明说，**标注未验证**）。
- `openid` 是**按 AppID 隔离**的：同一个 QQ 用户，在 A 机器人和 B 机器人下拿到的 `user_openid` 不同。→ 跨机器人/跨通道的用户身份统一必须靠 `union_openid`（需邮件申请绑定 QQ 互联主体）。

### 7.5 常见错误码速查（与消息相关）

| code | 含义 |
| --- | --- |
| 11241 / 11242 / 11243 | 缺 token / 校验 token 失败 / 校验未通过 |
| 11251 / 11261 / 11275 | 参数中 appid 错误 / 缺少 appid |
| 11253 / 11254 | 应用未获接口权限 / 接口被封禁 |
| 11265 | 机器人已被封禁 |
| 304018 | 机器人没连上 gateway（发消息要求 WS 在线） |
| 304026 / 304027 / 304028 | 回复的消息 id 错误 / 已过期 / 非 At 当前用户的消息不允许回复 |
| 304045~304050 | 各类主动消息限频 / 不允许在该处发主动消息 |
| 1100100 | 安全打击：消息被限频 |
| 1100101 | 安全打击：内容敏感 |
| 1100104 | 该群已失效或当前群不存在 |
| 1100301 | 调用方不是群成员 |
| 50046 | 非 at 机器人消息 |
| 50047 | 非机器人产生的消息或 at 消息 |

**关于 `11244` / `11298`**：在官方公共错误码表（`https://bot.q.qq.com/wiki/develop/api-v2/openapi/error/error.html`，2026-09-17 版）里**查无此码**，botpy 源码的 `HttpErrorDict` 也未收录。社区偶见把 11244 解释为「机器人未上线/权限不足」、11298 解释为「消息发送失败」，**均未验证，不要写进生产分支逻辑**（用通用 `err_code != 0` 兜底 + 日志 + 告警）。能确定的通用兜底：`11265` 封禁、`4914/4915` WS 层封禁/下架。

### 7.6 明确「未验证」清单

- 群/单聊 `content` 里 @ 别人的语法（官方只写了频道 `<@!id>`；群侧见到的 `<@openid>` 是接收侧格式）。
- 单条消息长度上限的具体数值（只有 `40054007` 这个错误码，无数字）。
- 群聊 `msg_type=6`（输入中状态）是否支持：单聊文档有、群聊文档没有 → 视为不支持。
- 沙箱域名 `sandbox.api.sgroup.qq.com` 与 `api.bot.qq.com` 的映射关系。
- `READY.d.user.id` 是否严格等于机器人 QQ 号。
- 11244 / 11298。
- 速率限制在沙箱/未认证主体的真实表现（官方只给表格，未给突发策略）。

---

## 8. 与 OneBot v11 的差异对照（适配层设计依据）

> 本节是**本笔记的工程结论**，非官方文档；OneBot v11 侧以 `post_type/message_type/action` 这套约定为准。

### 8.1 事件映射

| 官方事件 | → OneBot v11 | 关键字段 |
| --- | --- | --- |
| `C2C_MESSAGE_CREATE` | `post_type=message, message_type=private, sub_type=friend` | `user_id=author.user_openid`，`message_id=d.id`，`raw_message=content`，`time=timestamp` |
| `GROUP_AT_MESSAGE_CREATE` | `post_type=message, message_type=group, sub_type=normal` | `group_id=group_openid`，`user_id=author.member_openid`，`sender.card=author.username`，`sender.role=member/admin/owner` |
| `GROUP_MESSAGE_CREATE` | 同上（全量模式） | 额外要把 `content` 里的 `<@openid>` 转成 `at` 段 |
| `AT_MESSAGE_CREATE` | `message_type=group` + 自定义扩展 | `group_id=channel_id`，`user_id=author.id`，需额外带 `guild_id`（OneBot 无此字段，放扩展） |
| `DIRECT_MESSAGE_CREATE` | `message_type=private` + 自定义扩展 | `user_id=author.id`，`channel_id`/`guild_id` 放扩展 |
| `GROUP_ADD_ROBOT` / `GROUP_DEL_ROBOT` | `post_type=notice, notice_type=group_increase/decrease` | `group_id=group_openid` |
| `FRIEND_ADD` / `FRIEND_DEL` | `post_type=notice, notice_type=friend_add` | `user_id=openid` |

### 8.2 字段对照

| OneBot v11 | QQ 官方 Bot | 备注 |
| --- | --- | --- |
| `user_id`（int） | `author.user_openid` / `member_openid`（string） | **类型不同**：OneBot 是数字，官方是 base64 风格串 → 骰子程序若按 int 处理会崩，需用字符串或映射表 |
| `group_id`（int） | `group_openid`（string） | 同上 |
| `message_id`（int） | `d.id`（string，`ROBOT1.0_...`） | 回复时必须原样回传 |
| `raw_message` | `d.content` | 群 @ 事件已去 @ 前缀；频道消息需 `html.unescape` |
| `sender.nickname` | `author.username` | **可能为空串**（官方示例就是 `""`）→ 不能当主键，需回退到 openid |
| `sender.card` | 无 | 只能用 `username` 顶替 |
| `message` 段数组 | 无（纯文本 + `attachments`） | 需自己拼 |
| `at` 段 | 频道 `<@!id>`；群全量 `<@openid>`；群 @ 事件无内容痕迹 | 转换时要知道机器人自己的 id/openid |
| `image` 段 | `attachments[]`（收）/ `media.file_info`（发） | 不是 URL 内嵌 |
| `reply` 段 | `message_reference.message_id` | 索引是 `REFIDX_...`，不是消息 id |
| `face` 段 | 仅频道 `<emoji:id>` | 群/单聊无 |
| 撤回 `delete_msg` | `DELETE /v2/.../messages/{id}` | 2 分钟内，只能撤自己的 |

### 8.3 发送方式对照

| OneBot v11 | 官方等价 |
| --- | --- |
| `send_private_msg {user_id, message}` | `POST /v2/users/{user_openid}/messages`，**必须带 `msg_id`+递增 `msg_seq` 才是被动回复** |
| `send_group_msg {group_id, message}` | `POST /v2/groups/{group_openid}/messages` |
| `send_msg {message_type, ...}` | 按类型分发到上面两条 |
| 无 | 频道：`POST /channels/{channel_id}/messages` |
| 返回 `message_id` | 响应 `id`（可用于撤回/引用） |

### 8.4 语义上「丢不掉」的差异（必须写进适配层注释）

1. **被动窗口**：OneBot 里回消息没有时限；官方群聊 5 分钟 / 5 次、单聊 60 分钟（文档冲突，按 5 分钟）/ 4 次。骰子程序如果是「延迟很久才出结果」（比如跑团长团、异步结算），必须改成主动消息或提前占位。
2. **@ 语义**：群里只有 @ 机器人才有事件；「有人发 `.r` 但没 @」机器人根本收不到（除非开全量模式）。
3. **openid ≠ QQ 号**：骰子程序里所有「按 QQ 号建角色卡/权限」的逻辑都要改成按 openid 存，并且**换机器人就全失效**（openid 按 AppID 隔离）。
4. **富媒体不同源**：单聊文件不能发群，`file_info` 有过期时间，收到 `attachments` 的 URL 带 `rkey` 也会过期。
5. **去重**：同一条消息可能重复推送（官方明说），骰子程序必须幂等（否则一次 `.r` 会掷两次骰）。
6. **发送频率**：群 100 QPS 接口限制、主动消息 60 qpm、单关系 20 qpm，比 OneBot 直连 QQ 严格得多。

### 8.5 适配层骨架（建议）

```python
# 官方事件 -> OneBot v11 事件
def to_onebot_v11(evt_type: str, d: dict, self_openid: str) -> dict:
    if evt_type == "C2C_MESSAGE_CREATE":
        return {
            "post_type": "message", "message_type": "private", "sub_type": "friend",
            "time": _ts(d["timestamp"]),
            "self_id": self_openid,
            "user_id": d["author"]["user_openid"],
            "message_id": d["id"],
            "raw_message": d.get("content", ""),
            "message": [{"type": "text", "data": {"text": d.get("content", "")}}],
            "sender": {"nickname": d["author"].get("username") or d["author"]["user_openid"]},
            "_qqbot": {"scene": "c2c", "msg_id": d["id"], "msg_idx": _ext(d, "msg_idx")},
        }
    if evt_type in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
        return {
            "post_type": "message", "message_type": "group", "sub_type": "normal",
            "time": _ts(d["timestamp"]),
            "self_id": self_openid,
            "group_id": d["group_openid"],
            "user_id": d["author"]["member_openid"],
            "message_id": d["id"],
            "raw_message": d.get("content", ""),
            "message": _segments(d.get("content", "")),   # 解析 <@openid> / <@all>
            "sender": {
                "nickname": d["author"].get("username") or d["author"]["member_openid"],
                "role": d["author"].get("member_role", "member"),
            },
            "_qqbot": {"scene": "group", "msg_id": d["id"], "msg_seq": 0,
                       "msg_idx": _ext(d, "msg_idx"), "full_mode": evt_type == "GROUP_MESSAGE_CREATE"},
        }
    raise NotImplementedError(evt_type)


def _ext(d: dict, key: str) -> str | None:
    for kv in (d.get("message_scene") or {}).get("ext") or []:
        k, _, v = kv.partition("=")      # 值里可能有 '='，只切一次
        if k == key:
            return v
    return None
```

发送侧把 OneBot 动作翻译回去时，**必须**把 `_qqbot.msg_id` + 自增 `msg_seq` 塞进去，否则就变成主动消息（受频控与用户开关限制）。

---

## 9. 给 Dice-Next 的落地清单

1. 开通：注册 → 主体认证 → 创建机器人 → 拿 `AppID`/`AppSecret` → 在管理端申请 **`GROUP_AND_C2C_EVENT`（1<<25）** 权限。
2. 若为个人主体：只能加入自己当群主的群做测试；私聊要加「开发体验用户号码」。**上线公开使用需要企业主体**。
3. 群主端设置（最新手机 QQ，添加机器人时点「设置权限」）：打开「接收全量消息」（如需非 @ 指令）与「主动推送消息」（如需定时播报）。
4. 实现顺序建议：`BotToken` → `GET /gateway` → WS 客户端（Hello/Identify/Heartbeat/Resume/重连退避）→ `GROUP_AT_MESSAGE_CREATE` 翻译 → 回复（`msg_id` + `msg_seq`）→ 富媒体 → 主动消息。
5. 必做的幂等：按 `msg_idx` 去重；回复计数按 `msg_id` 自增。
6. 必做的降级：被动窗口过期（`40034005`/`40034128`）→ 改主动消息（若已授权）或丢弃并通知用户重发。
7. 日志：每次请求记录 `trace_id`（body 里或响应头 `X-Tps-trace-ID`），排障全靠它。

---

## 10. 来源与抓取日期

抓取日期：**2026-11-10**，抓取方式：`web_fetch` 直连官网（无需 JS 渲染），HTTP 200。

| # | 内容 | URL | 页面「上次更新」 |
| --- | --- | --- | --- |
| 1 | 获取访问凭证 | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/access-token.html | 9/16/2026 |
| 2 | 启动接入（凭证说明、SDK 链接） | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/getting-started.html | 7/21/2026 |
| 3 | API 调用指南（通用规则） | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/api-call-guide.html | 9/17/2026 |
| 4 | 通用数据结构 / OpCode / Intents | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/payload.html | 7/21/2026 |
| 5 | WebSocket 方式 | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html | 7/30/2026 |
| 6 | Webhook 方式 | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/webhook.html | 9/11/2026 |
| 7 | 安全与授权（Ed25519） | https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/sign.html | 9/12/2024 |
| 8 | 获取通用 WSS 接入点 | https://bot.q.qq.com/wiki/develop/api-v2/openapi/wss/url_get.html | 7/30/2026 |
| 9 | 获取带分片 WSS 接入点 | https://bot.q.qq.com/wiki/develop/api-v2/openapi/wss/shard_url_get.html | 7/30/2026 |
| 10 | 错误与调试（HTTP/WS/公共错误码） | https://bot.q.qq.com/wiki/develop/api-v2/openapi/error/error.html | 7/21/2026 |
| 11 | 消息收发概述（窗口、频控、去重） | https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html | 7/21/2026 |
| 12 | 发送单聊消息 | https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html | 10/28/2026 |
| 13 | 发送群聊消息 | https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html | 10/28/2026 |
| 14 | 单聊消息事件 | https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_message_create.html | 9/16/2026 |
| 15 | 群 @ 机器人消息 | https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_at_message_create.html | 9/16/2026 |
| 16 | 群消息（全量模式） | https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html | 10/21/2026 |
| 17 | 频道消息事件 | https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/message/event.html | 7/21/2026 |
| 18 | 发送子频道消息 | https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/message/send.html | 7/21/2026 |
| 19 | 内嵌格式（@、#、emoji、转义） | https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/message/format.html | 7/21/2026 |
| 20 | 消息类型（收/发类型矩阵） | https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/type/overview.html | 7/23/2026 |
| 21 | Markdown 消息 | https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/type/markdown.html | 10/21/2026 |
| 22 | 富媒体消息概述 | https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/rich-media.html | 7/21/2026 |
| 23 | 变更记录（域名统一 api.bot.qq.com 于 20260810；群 @全体 20261026；图文卡片 20261028） | https://bot.q.qq.com/wiki/develop/api-v2/changelog.html | 10/28/2026 |
| 24 | 常见问题（无沙箱、个人主体限制、群权限设置、union_openid 申请） | https://bot.q.qq.com/wiki/bot_FAQ/ | 11/3/2026 |
| 25 | botpy 源码（Intents 位、token 刷新、域名常量） | https://github.com/tencent-connect/botpy （clone 于 2026-11-10） | — |
| 26 | botgo 源码（未逐行阅读，仅作文档交叉参考） | https://github.com/tencent-connect/botgo | — |

**botpy 交叉验证的具体位置**：

- `botpy/robot.py:49` — `url="https://bots.qq.com/app/getAppAccessToken"`（旧域名仍在用）
- `botpy/robot.py` `Token.update_access_token` — 20s 超时；`self.expires_in = int(data["expires_in"]) + int(time.time())`
- `botpy/robot.py` `Token.get_string()` — `"{} {}".format("QQBot", access_token)`
- `botpy/http.py:90-91` — `DOMAIN="api.sgroup.qq.com"`、`SANDBOX_DOMAIN="sandbox.api.sgroup.qq.com"`
- `botpy/flags.py` `Intents` — `public_messages=1<<25`、`audio_action=1<<29`、`public_guild_messages=1<<30`；**无 1<<20**

> 说明：本文所有「未验证」标注项均表示官方文档未给出或文档间冲突，落地前需实测。
