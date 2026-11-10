# NoneBot2 接 QQ 官方机器人（服务器实机改造记录）

> 2026-11-10 在 47.108.190.188 的 `/opt/nonebot2`（实例 `nonebot2-93106f1a`）上完成。
> 与 [dicenext-qq-official.md](dicenext-qq-official.md) 的区别：Dice-Next 原生支持官方通道，
> 面板只要写配置；NoneBot2 要先手工验证可行（本文），再把同一套链路做进面板（见文末「面板支持」）。

## 结论

NoneBot2 通过社区官方适配器 `nonebot-adapter-qq` 直连 QQ 官方 WebSocket 网关，
消息事件走 `GROUP_MESSAGE_CREATE` / `C2C_MESSAGE_CREATE`，**不再需要 LLBot / NapCat 这类登录端**。
改造后实测：`Bot 102085790 connected` → `READY` → 收到真实群消息 → 交给骰子插件 `nonedice` 处理成功。

## 改了什么（服务器侧，均不在 git 仓库内）

| 位置 | 内容 | 备注 |
| --- | --- | --- |
| `/opt/nonebot2/libs/` | 安装 `nonebot-adapter-qq==1.7.3` | `pip install --target`，与实例既有的 `libs/` 形态一致 |
| `/opt/nonebot2/plugins/qq_official_boot.py` | 新增，注册 `nonebot.adapters.qq.Adapter` | 放插件目录而非 `bot.py`：`bot.py` 由 DiceManager 生成、可能被重写，而 `load_plugins('plugins')` 一定在 `nonebot.run()` 之前执行 |
| `/opt/nonebot2/.env` | 删除 `ONEBOT_V11_WS_URLS` / `ONEBOT_V11_ACCESS_TOKEN`；新增 `QQ_BOTS`；`DRIVER` 改为 `~fastapi+~aiohttp+~websockets` | 文件权限收紧到 `600`（内含 AppSecret） |
| `/var/lib/dicemanager/instances.json` | `nonebot2-93106f1a`：`bot_mode=official`、`login_ref=null`、`links=[]`；`llbot-f88b967c`：`CONFIGURED` | 改文件必须在服务停止时做（面板启动时按 RUNNING 恢复实例） |

### .env 关键片段

```dotenv
DRIVER=~fastapi+~aiohttp+~websockets
QQ_BOTS=[{"id": "102085790", "secret": "<AppSecret>", "use_websocket": true,
          "intent": {"guilds": false, "guild_members": false, "guild_messages": false,
                     "guild_message_reactions": false, "direct_message": false,
                     "open_forum_event": false, "audio_live_member": false,
                     "group_members": false, "c2c_group_at_messages": true,
                     "interaction": false, "message_audit": false, "forum_event": false,
                     "audio_action": false, "at_messages": false}}]
```

`intent` 只开 `c2c_group_at_messages`（官方 `1<<25`，覆盖群聊 @ 与单聊）。
其余位多为频道（guild）语义，普通机器人没被授权时开启会导致 identify 被拒（`4914` 等），故全关。

## 三个必须知道的坑

1. **`DRIVER` 必须提供 HTTPClient**
   QQ 适配器在 `setup()` 里直接检查驱动：

   ```
   RuntimeError: Current driver ~fastapi+~websockets does not support http client requests!
   QQ Adapter need a HTTPClient Driver to work.
   ```

   原值 `~fastapi+~websockets` 只有 WS 客户端，没有 HTTP 客户端 → 换 token 那步就炸。
   必须加 `~aiohttp`（或 `~httpx`）。

2. **`pip install --target <libs> --upgrade` 会删掉别的包的文件**
   实测：装 `nonebot-adapter-qq --upgrade` 之后，`libs/nonebot/adapters/onebot/` 整个目录消失
   （`nonebot_adapter_onebot-2.4.6.dist-info` 还在），`bot.py` 第 9 行 `import nonebot.adapters.onebot`
   直接 `ModuleNotFoundError`，面板进入「300s 内重启达 5 次」的放弃状态。
   而且事后 `pip install --target ... nonebot-adapter-onebot` 只会打印
   `Target directory /opt/nonebot2/libs/nonebot already exists. Specify --upgrade` 而不恢复文件。
   **正确修法**：`pip download --no-deps` 拿 wheel，再 `unzip -oq *.whl -d libs` 覆盖解包。

3. **面板看护会重拉进程，但只认「进程退出」**
   改完 `.env` 后 `kill <nonebot2 pid>`，面板日志出现 `[manager] 进程退出 rc=-15，第 1 次重启` 并自动拉起，
   不用整机重启面板（避免顺带踢掉海豹/LLBot 的连接）。

## 验证方式（真实证据，非推断）

```text
11-10 19:21:54 [SUCCESS] nonebot | Loaded adapters: OneBot V11, QQ
11-10 19:21:55 [INFO]    nonebot | QQ | Bot 102085790 connected
11-10 19:21:55 [SUCCESS] nonebot | QQ 102085790 | [EventType.READY]: {... 'shard': (0, 1)}
11-10 19:22:11 [SUCCESS] nonebot | QQ 102085790 | [EventType.GROUP_MESSAGE_CREATE]: ...
11-10 19:22:11 [INFO]    nonebot | Matcher(type='message', module=plugins.nonedice, lineno=78) running complete
```

日志路径：`/var/log/dicemanager/nonebot2-93106f1a.log`（面板捕获子进程输出）。

## 插件兼容性（重要）

| 插件 | 是否在官方通道工作 | 原因 |
| --- | --- | --- |
| `nonedice`（骰子核心） | ✅ | 只用 `on_message`，不 import 任何适配器专有类型（`plugins/nonedice/__init__.py:70`） |
| `personal_assistant` | ❌ | `from nonebot.adapters.onebot.v11 import PrivateMessageEvent` 且 `isinstance` 过滤（`memory.py:34/720`），QQ 事件不是该类型 |
| `hindsight_mem` | ❌ | 同上（`commands.py:11/38`） |

要让后两者工作，得改用 `on_message` + 适配器无关的 `Event.get_session_id()` / `UniMessage`，属于独立改动。

## 面板支持（2026-11-10 当晚已补齐）

首次改造是纯服务器侧手工做的，面板当时对 NoneBot2 的认知只有「Python 源码项目 + OneBot 连登录端」。
现已按 Dicenext 的同构做法补进仓库：

- `manifests/nonebot2.json` 与 `nonebot2_win.json`：`bot_modes` = `onebot` + `official`，
  `official.credentials` = `[{app_id, label 机器人 AppID, secret:false}, {app_secret, label 机器人 AppSecret, secret:true}]`；
- `adapters/nonebot2.py`：`official_mode()` / `requires_official_credentials()` /
  `configure_official()` / `official_status()`；
- `configure_official()` 写的东西与本文手工改造一一对应：`QQ_BOTS`（替换而非追加）、
  `DRIVER` 补 `~aiohttp`、删 `ONEBOT_V11_*`、生成 `plugins/qq_official_boot.py`、`chmod 0600`；
- 首次保存时若 `libs/nonebot/adapters/qq` 不存在，会 **`pip download` 拿 wheel 再按目录精确解包**
  （不能用 `pip install --target`：会静默不装或整个替换 `libs/nonebot`）；
- `services/wizard.py` 与 `web/src/pages/Wizard.vue` 无需改动——官方通道的表单与 step3 契约是清单驱动的。

仍然存在的边界（不是缺陷，是设计）：

- `bot_mode` 在创建实例时确定；已建好的 OneBot 实例不会自己变成官方通道，需要重建或手工改
  `instances.json`（改文件必须在面板停止时做）；
- 重启是必须的：NoneBot2 没有配置热加载，向导保存后要重启实例才会连上官方网关；
- 三个插件里只有 `nonedice` 与适配器无关；`personal_assistant` / `hindsight_mem` 用
  `isinstance(PrivateMessageEvent)` 过滤，官方通道下不响应（见上文「插件兼容性」）。
