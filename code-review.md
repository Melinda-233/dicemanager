# DiceManager 代码检查报告

> **修复状态（2026-09-19）**：以下 P0/P1 问题已全部修复并复查通过（`python -m compileall` 零错误）。
> 复查时补修了一处遗漏：`api/auth.py` 的 `require_auth` 依赖 `Depends(security)`，但 `security = HTTPBearer(auto_error=False)` 未定义，已在修复中补上。

整体评价：架构分层清晰（core / adapters / services / api / web），安全意识不错（后端构建启动命令、恒定时间比较、原子写、墓碑删除、WS 查询参数鉴权）。审查时曾存在 **5 个"启动即崩 / 主流程走不通"的 P0 问题**，以及若干严重逻辑 bug。按严重程度列出如下。

---

## P0 — 启动即崩 / 核心流程不可用

### 1. `api/auth.py` 缺少 import，应用启动即 NameError
`Auth.__init__` 用到了 `json`、`Path`、`write_atomic`，三个都没导入；而模块底部 `auth = Auth()` 在 import 时立即执行 → 整个后端起不来。另外 `__init__` 缩进混乱（def 用 3 空格、函数体 4 空格），建议重排。
```python
import json
from pathlib import Path
from core.atomicio import write_atomic
```

### 2. `api/context.py` 没有 `ctx`，但 5 个模块都在 import 它
`app.py`、`rest.py`、`ws_overview.py`、`ws_logs.py`、`ws_login.py` 全部 `from api.context import ctx`，而 `context.py` 里根本没有定义 `ctx` → ImportError。
且 `app.py` lifespan 里的 `global ctx; ctx = build_context()` 只改 app.py 自己模块的名字空间，rest/ws 模块即使能 import 也拿到的是旧值。
**修法**：`context.py` 里 `ctx = None`，其余模块改用 `from api import context` 然后运行时访问 `context.ctx`（或提供 `get_ctx()` 访问器）。

### 3. `/api/login` 被自己的鉴权拦截 — 永远拿不到 token
```python
router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
```
router 级依赖作用于**所有**路由，包括 `/login` 本身 → 没有凭据就无法登录，鸡生蛋死锁。
**修法**：把 `/login` 挂到独立的无鉴权 router 上。

### 4. manifests 是 JSONC（带 `//` 注释），`json.loads` 解析必炸
5 个清单文件首行都是 `// manifests/xxx.json` 注释，而 `adapters/__init__.py:load_registry` 用 `json.loads` → 5 个清单全部 JSONDecodeError，适配器注册表为空，系统不可用。
**修法**：删掉注释（推荐），或改用 `json5.loads`。

### 5. `core/ports.py:release()` 会把 ports.json 整个覆写成 `false`
```python
atomic_write_json(self._path, lambda t: t.pop(str(port), None) is None and t)
```
当端口**在表里**（正常释放场景）时，`pop` 返回非 None → 表达式结果是 `False` → `atomic_write_json` 里 `result is not None` 成立 → `data = False` → **整个端口表被写成 `false`**，数据永久损坏。
**修法**：换成显式函数：
```python
def _m(t):
    t.pop(str(port), None)
    return t
```

---

## P1 — 严重逻辑 bug

### 6. Wizard.vue 所有向导调用传错了 ID（前端全挂）
```js
instance = await createInstance(...)   // 返回的是 {id, dir, allocated_ports}
doStep(2, {})  → wizardStep(instance, ...)  → URL 变成 /instances/[object Object]/wizard
```
所有 `wizardStep(instance, ...)`、`openLoginWS()` 处都应传 `instance.id`。

### 7. 同名冲突"二选一"后端没实现，且重入 step2 会 500
前端 `resolve()` 重发 step2 带 `{use_existing}`，但 `run_step` step2 完全忽略 payload；且第二次进入会再次 `transition(DEPLOYING)`，而 `DEPLOYING → DEPLOYING` 不在 TRANSITIONS 表里 → ValueError → 500。冲突弹窗这条路目前完全走不通。需要：step2 读取 `use_existing`，冲突解决路径跳过重复 transition（如先回 UNDEPLOYED 或特批同态迁移）。

### 8. 删除实例的 keep_save 逻辑是反的
`rest.py:delete_instance`：`keep_save=True`（默认值，前端选"保留存档"也是 true）时执行的是 **`rmtree(inst.dir/"config")` —— 恰好把存档目录删了**。语义完全颠倒。应为：`keep_save=True` 时**不删** config 目录（只删其他内容），`keep_save=False` 才全删。这个 bug 配合 Overview.vue 的 shiki 分支，会导致用户选"保留存档"时反而丢档，属于数据丢失级问题。

### 9. asyncio.Queue 被跨线程写入（ws_logs / ws_login）
tail 线程直接 `queue.put_nowait(line)`，但 `asyncio.Queue` **不是线程安全的** → 可能丢失唤醒、事件卡住。应在创建 queue 时记录 event loop，用 `loop.call_soon_threadsafe(queue.put_nowait, ev)` 投递。

### 10. step3 的 needs_login 信任了前端而非适配器
`wizard.run_step` step3 判断 `payload.get("needs_login")`，但契约里适配器 `configure_login` 的返回值（如 sealdice 返回 `{"needs_login": False}`）才是权威来源。现在客户端可以任意影响状态机迁移。应改为 `r.get("needs_login", False)`。

### 11. process.py：重复定义的 start + 重启计数永不重置
- 文件里有两个 `start` 方法（34 行和 71 行），第一个是死代码，删掉一个。
- `restarts` 生命周期累计、永不重置 —— README 宣称"5 次/5 分钟退避"，实现是"一辈子只守护 5 次"。一个长期偶发崩溃的进程最终会永久失去守护。应按时间窗口计数（如 5 分钟内重启超过 5 次才停），稳定运行一段时间后清零。

### 12. `instance_op` 用 assert 校验操作名
`assert op in ("start","stop","restart")` 在 `python -O` 下被剥离，且断言失败返回 500。应显式 `raise HTTPException(404)`。顺带：`create_instance` 未校验 `dice` 是否存在于 adapters（KeyError → 500），`CreateReq.arch` 字段实际全程未被使用。

### 13. `hmac.compare_digest` 收到非 ASCII 密码会 TypeError → 500
`compare_digest(password, self.admin_password)` 要求两侧都是 ASCII str。用户密码含中文/emoji 时抛 TypeError。应 `encode()` 成 bytes 再比较（`verify_ws` 同理建议加固）。

---

## P2 — 值得修但不阻塞

- **ws.js 重连风暴**：token 失效时服务端 close(4401)，客户端仍无条件指数重连 → 死循环。收到 4401 应停止重连并跳转登录页。
- **api.js 错误处理**：`(await r.json()).detail` 在响应非 JSON（如 500 HTML）时二次抛错，掩盖原始错误。
- **Overview.vue 拓扑布局**：`pos()` 对单实例角度恒为 0，节点永远贴正右方；建议整体减 `Math.PI/2` 起始并检查画布边界（260 半径在 800×400 视窗上下会溢出，y=200±140 尚可，x=400±260 贴边）。
- **app.py 静态目录**：`StaticFiles(directory="web/dist")` 是相对路径，依赖启动时 CWD；前端未构建时直接崩。建议 `Path(__file__).resolve().parent.parent / "web/dist"` 并加存在性检查。
- **process.py 滚动语义**："mtime 距今 ≥ 7 天"实际含义是"7 天没写入才滚动"，与 README 的"日志保留 7 天"不一致；`copy2+unlink` 两步非原子，中间崩溃可能丢日志段（低概率，可接受但值得注释说明）。
- **napcat**：`json.loads(wf.read_text())` 缺 encoding；`instance.webui_token` 没有任何代码写入过该字段，token 兜底实际永远为 None。
- **llbot**：`credentials.get("version", (9,9,9))` 前端从不传 version → 恒按 v8 处理，v7 老版本用户也会被强制要求 AUTH TOKEN；且若前端传字符串版本号，元组比较直接 TypeError。
- **wizard step5**：进程已运行时 `proc.start` 抛 RuntimeError → 500；`get_actual_port` 在启动瞬间读 ring，大概率拿到 None（actual_port 常年空）。建议延迟探测或从 WS 通道异步回填。
- **ws_overview**：客户端断开时 `send_json` 抛的是 RuntimeError（send after close），不在捕获范围内 → 后台异常日志噪音；ws_logs/ws_login 的 `get` future 在 ctrl 分支未完成时会悬挂（"Task was destroyed but it is pending" 警告）。
- **端口分配**：`allocate` 的 `_in_use` 与 `str(p) in tbl` 是同一个文件的两次读（重复）；且从不探测系统真实端口占用，理论上会与机器上其他服务撞端口（可复用 `base.tcp_probe`）。
- **locks.py**：锁目录 `/tmp/dicemanager` 建议挪到 `STATE_DIR` 下，避免多套部署互相挡锁；`fcntl` 仅 Linux 可用，Windows 开发机上无法运行（README 已声明目标是 Linux 服务器，提醒一下本地调试会挂）。
- **registry IO 放大**：每次 `get/all` 都重读整个 instances.json，overview 每 2s 对每实例读 2 次。量小无碍，实例多时建议内存缓存 + 写穿。
- **性能小项**：`_append_log` 每行 stat + open/close；`_download` 整包读内存且 extractall 无 zip bomb 防护。

---

## 修复优先级建议

1. 先修 #1–#5（不修服务起不来 / 登不进去 / 端口表会坏）
2. 再修 #6–#10（向导、删除、WS 推送这三条主流程）
3. #11–#13 与 P2 按需排期

需要的话我可以直接动手把这些修掉（P0 + P1 一批改完并自查）。
