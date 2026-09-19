# DiceManager 整体代码评审（2026-09-19）

> **修复状态（2026-09-19 晚）**：下方 P1 全部修复；P2 全部修复；P3 完成密码哈希+登录限速、
> DM_STATE_DIR/DM_LOG_DIR 环境变量、manifest 可选 sha256 校验、前端两处小修。
> 验证：py_compile 零错误；新增 tests/test_process.py 4 项回归测试通过（真实子进程驱动重启链路）；
> tests/smoke_local.py 冒烟 5 项通过（含旧明文 auth.json 自动迁移）；vite build 通过。
> 未做：logging 框架替换 print、ruff/mypy/CI（建议后续迭代）。

---

## 一、总体评价：**良好偏优（7.5 / 10）**

这是一份**明显高于业余水准**的代码。分层架构（core / adapters / services / api / web）边界清晰、职责单一；并发设计（RLock + 引用计数文件锁、原子写、环形缓冲、滑动窗口重启熔断）有自己的思考且大多正确；注释大量解释「为什么」而非「是什么」，甚至记录了历史 bug 的成因——这是很好的工程习惯。前端虽小而完整：hash 路由、WS 断线退避重连、1006 三次判定鉴权失效、401 统一处理，都做对了。

失分点集中在：**一个会自我放大的并发 bug（自动重启线程翻倍）**、若干线程安全/落盘遗漏、零测试、print 式日志、安全加固空间。

### 亮点（值得保持）

1. **锁的可重入设计**（core/locks.py）：RLock + 引用计数文件锁，并在模块 docstring 里写清了「自己等自己」死锁的成因——这类注释在半年后价值千金。
2. **原子写贯穿始终**：mkstemp + fsync + os.replace，跨 JSON5/JSON/YAML/文本统一收口到 atomicio。
3. **Registry mtime 读缓存**：总览 2s 周期下避免全量重读 JSON，写穿盘 + 缓存同步的思路正确。
4. **WS 线程桥接正确**：tail 线程 → `call_soon_threadsafe` → asyncio.Queue，队列满丢最旧不阻塞 tail；持久 task 避免每轮重建 receive 的悬挂——这是很多 FastAPI 项目会写错的地方。
5. **安全基线到位**：启动命令一律后端构建、恒定时间比较、WS 查询参数鉴权、删除二次确认、监听 127.0.0.1、墓碑式删除防误分配。
6. **进程守护**：环形缓冲 + 双限滚动（50MB/7天）+ 滑动窗口重启熔断 + 指数退避，参数都有注释且与 README 口径一致。
7. **manifest 清单校验**：坏清单启动即报错而非静默容忍，fail-fast 正确。

---

## 二、发现的问题（按严重度）

### P1-1 `core/process.py`：自动重启会让 tail 线程指数级翻倍 ⚠️ 本轮最重要发现

`_tail()` 循环末尾调用 `self.start(self._last_cmd, self._last_cwd)` 后**没有 return**。
而 `start()` 内部会再起一个新 tail 线程并更新 `self._proc`。旧线程回到循环顶部后
`p = self._proc` 拿到的是**新进程**，于是新旧两个线程同时消费同一条 stdout 管道：

- 进程每崩溃重启一次，线程数翻倍：1 → 2 → 4 → 8…
- 两个线程各自 `restarts += 1` 并向 `_restart_times` 追加，**每次崩溃计数 +2**，
  导致 5 次/5 分钟的熔断阈值被提前触发（实际崩 2~3 次就停止自动重启）；
- 多线程争抢同一管道，日志行归属随机、`_append_log` 的锁竞争加剧。

**修法（建议，勿现在改）**：`self.start(...)` 之后加 `return`，让新线程接管；或把重启逻辑挪出 tail 线程，用单独的 supervisor 线程负责。

### P1-2 `adapters/napcat.py`：`self.reg_qq` 是死代码，且暴露适配器实例共享的隐患

- `configure_login` 里 `self.reg_qq = credentials["qq"]` **从未被读取**（真正生效的是
  `registry.update(qq=...)` 路径）。
- 更深的问题：`ctx.get_adapter` 会**缓存并跨请求共享适配器实例**，往 self 上写状态
  是线程不安全的（两个用户同时建 NapCat 实例会互相覆盖）。应删除该行；如果未来确需
  适配器级状态，要么不缓存、要么把状态放回 Instance 并落盘。

### P1-3 `adapters/olivadice.py`：缺件告警从未落盘

`instance.warnings = [f"缺失子模块: {missing}"]` 改的是 `registry.get()` 返回的
**临时 dataclass 副本**，不会写回 instances.json——总览页永远看不到这条告警。
应改为 `registry.update(instance.id, warnings=[...])`。

### P2-1 `api/ws_overview.py`：实例被删会让整条总览 WS 挂掉

`for rec in ctx.registry.all(): inst = ctx.registry.get(rec["id"])` 存在两件事：

1. **重复读**：`all()` 已经返回了完整记录 dict，紧接着 `get()` 又按 id 查一次并转
   Instance，纯浪费（每客户端每 2s × N 实例）。
2. **竞态崩溃**：若实例恰在两行之间被删除，`get()` 抛 KeyError，而外层只捕获
   `WebSocketDisconnect / CancelledError`，异常会直接打死这条 WS 连接（前端表现为
   总览闪断重连）。建议直接用 `all()` 的记录（补一个 `pm.probe`）+ 循环体兜底 try/except。

### P2-2 `core/registry.py`：`purge_tombstones` 全项目无人调用

墓碑「保留 30 天」的承诺只实现了一半：方法写好了，但没有任何启动/定时调用点，
tombstone 会无限累积（量小，但属于承诺未兑现）。建议在 lifespan 启动时调一次。

### P2-3 `services/wizard.py` step5：双击启动 → 500

`proc.start()` 在进程已运行时抛 `RuntimeError`，REST 层（rest.py:64）捕获了它，
向导 step5 没有捕获 → 用户重复点「启动」会拿到 500 而非友好提示。
另外 `adapter.get_actual_port(proc.ring)` 在 `start()` 返回后**立即**执行，进程还没
来得及打印端口，几乎恒为 None——实际端口回读目前只对 NapCat WebUI 日志碰运气生效。
建议：step5 捕获 RuntimeError；actual_port 改为延迟若干秒后由总览通道回填，或订阅
ring 的行回调异步提取。

### P2-4 日志回放的丢行窗口

`ws_logs` / `ws_login` 都是「先回放 ring 快照，再 `proc.on_line(hook)`」。两步之间
新产生的日志行既不在快照里、也无人推送，形成一个小缺口。低频场景可接受，追求严谨的
做法是先注册 hook、记录注册时刻、回放时跳过该时刻之后的行（或给行加序号去重）。

### P3-1 安全加固空间（局域网工具可接受，公网建议做）

- `auth.json` **明文存管理密码**——建议改存 PBKDF2/argon2 哈希 + 盐；
- `/api/login` **无失败限速**，可被暴力尝试（哪怕恒定时间比较防了时序侧信道）；
- WS token 走 URL 查询参数，会进 nginx access log——可接受但建议对 `$args` 做日志脱敏；
- 下载包无校验和：manifest 里加 `sha256`，`_download` 后校验，供应链更稳；
- `auth = Auth()` 在 **import 时**执行且路径硬编码 `/var/lib/dicemanager`，Windows 开发
  / 非 root 环境直接炸——建议路径改为 `DM_STATE_DIR` 环境变量默认值，并把 Auth 构造
  挪进 lifespan。

### P3-2 工程化短板

- **零测试**。core 层（ports/registry/locks/atomicio）和 adapters（用假 manifest +
  tmp 目录）非常适合单测，`_parse_ver`、`release_owner`、transition 表这类纯逻辑 10
  分钟就能覆盖；有 P0 前科的项目，测试是性价比最高的投入。
- `print()` 当日志：建议 logging + RotatingFileHandler，ERROR 级别进独立文件。
- 无 lint/格式化/CI 配置：ruff + mypy 一把梭，成本极低。
- `Wizard.get_adapter` 每次新建实例、`ctx.get_adapter` 缓存实例——两套语义并存，
  建议统一走 ctx 缓存（前提是先解决 P1-2）。

### P3-3 前端小项

- `App.vue` 的 `hashchange` 监听未移除（根组件不卸载，实际无害，但不对称）；
- `Overview.vue` 的 `sock` 用 `let`、`Wizard.vue` 的用 `ref`——两种约定并存，Wizard 的
  注释解释了原因，但 Overview 既然不进模板用 let 没问题；建议统一 `ref` 减少心智负担；
- LogCenter 实例列表只在 mounted 拉一次，新建实例后需刷新页面才能选到——可在页面
  可见时轮询或复用总览 WS；
- `pos()` 布局 O(n²) `findIndex`——实例规模下无所谓，仅提示。

---

## 三、优化建议清单（按投入产出排序）

| # | 建议 | 量级 | 收益 |
|---|---|---|---|
| 1 | 修 `_tail` 重启后 `return`（P1-1） | 1 行 | 消除线程翻倍 + 熔断误触发 |
| 2 | 删 `napcat.reg_qq`；olivadice 告警改 `registry.update`（P1-2/3） | ~5 行 | 消除死代码与失效功能 |
| 3 | ws_overview 去掉二次 `get()` + 循环兜底 try/except（P2-1） | ~10 行 | 总览稳定性 |
| 4 | lifespan 里调 `purge_tombstones()`（P2-2） | 1 行 | 兑现 30 天承诺 |
| 5 | wizard step5 捕获 RuntimeError；actual_port 延迟回填（P2-3） | ~15 行 | 交互健壮性 |
| 6 | core + adapters 单测（pytest + tmp_path + 假 manifest） | 半天 | 回归防线，防 P0 复发 |
| 7 | logging 框架替换 print；auth.json 存哈希；登录限速 | 半天 | 安全与可运维性 |
| 8 | ruff + mypy + GitHub Actions（可走镜像） | 2 小时 | 长期代码质量 |
| 9 | manifest 加 sha256 校验和；`DM_STATE_DIR` 环境变量 | ~20 行 | 供应链 + 可移植 |

---

## 四、评分明细

| 维度 | 评分 | 说明 |
|---|---|---|
| 架构与分层 | 9/10 | 分层清晰、契约明确（WriteResult/adapter 抽象），依赖注入容器简洁 |
| 并发正确性 | 6.5/10 | 锁设计优秀，但 tail 线程翻倍是实打实的并发 bug；napcat 实例共享状态不安全 |
| 健壮性/边界处理 | 7.5/10 | 多数异常路径有兜底；删实例竞态、双击启动、回放丢行是漏网点 |
| 安全 | 7/10 | 基线好（后端构建命令、恒时比较、原子写），密码明文与无限速拉低上限 |
| 可读性与注释 | 9/10 | 注释解释 why、记录历史 bug，几乎全部文件有模块 docstring |
| 前端质量 | 7.5/10 | 小而完整，WS 重连/鉴权细节到位；状态约定不统一、无组件拆分（规模尚不需要） |
| 工程化（测试/CI/日志） | 4/10 | 零测试、print 日志、无 lint/CI——最大的短板 |
| **综合** | **7.5/10** | 架构和编码品味在水准之上，补上测试和几个并发/落盘漏洞即可到 8.5+ |
