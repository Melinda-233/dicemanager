# 文档《骰子管理器项目总览与联调清单》↔ 代码实现 对照核查

核查时间：2026-09-20　对象：`E:\gito_daze\dicemanager` 全量源码（core/services/adapters/api/web/manifests/deploy）
结论：**主干功能已落地 ≈ 85%**，联调清单 A（启动鉴权）、D（WS 三通道）、F（安全回归）基本齐备；
B（向导主链路）与 C（骰系专项）存在 **6 处会直接导致联调失败的实质缺口**，另有 6 处结构/细节偏差。

> 说明：`api/auth.py`、`README.md` 本次因内容审批超时未能读取；A 项的「错误密码 → 401」由
> `tests/smoke_local.py:27-39` 的断言（status_code == 401 / 429）间接确认，其余鉴权相关行为
> 由 `api/rest.py`、`ws_*.py`、`web/src/api.js` 的调用侧确认。

---

## 一、逐项对照（A–F）

### A. 启动与鉴权

| 清单项 | 状态 | 依据 |
|---|---|---|
| install.sh 一次通过；8765 起、打印随机密码 | ✅ | `deploy/install.sh`（含 --swap/--skip-build、venv、npm 构建、systemd、nginx）；`api/app.py:18` `console_only(f"[auth] 本次管理密码: ...")`；`api/app.py:38-40` `uvicorn.run(host="127.0.0.1", port=8765)` |
| 错误密码 → 401；正确密码 → token 落 localStorage | ✅ | `tests/smoke_local.py:27-39` 断言 401 + 429 限速；`web/src/api.js:4` `localStorage.setItem('dm_token', t)` |
| WS 无/错 token → close 4401 | ✅ | `ws_overview.py:73`、`ws_logs.py:21`、`ws_login.py:17` 均为 `auth.verify_ws(ws)` → `ws.close(code=4401)`；前端 `ws.js:18` 收到 4401 停止重连并回登录页 |
| GET / 返回总览页 | ✅ | `api/app.py:32-34` 挂载 `web/dist`（`html=True`）；`web/dist` 已存在 |

### B. 向导 E2E（主链路 NapCat → 海豹）

| 清单项 | 状态 | 依据 / 问题 |
|---|---|---|
| Step1 创建实例，端口 +1 起步 | ✅ | `wizard.py:26-28` `allocate_many`（webui/ob11/milky/satori）；`ports.py:22-25` 占用探测后 +1 |
| Step2 同名冲突弹窗两分支 | ✅ | `wizard.py:53-62` `use_existing` 分支 + `adapter.deploy()` 返回 `conflict`；`Wizard.vue:47-53` 两个按钮 |
| Step3 二维码 url/base64 展示 | ✅ | `base.py:124-127` `extract_qrcode` 同时支持 `data:image/png;base64` 与 http url；`Wizard.vue:59-60` |
| Step3 NapCat 日志回读**实际端口** | ✅ | `napcat.py:55-58` `[WebUI] ... :(\d{4,5})/webui`；回填在 `ws_overview.py:22-28` 周期执行（变化时写盘） |
| Step3 日志回读 **token** | ❌ **缺口 B1** | `napcat.py:19-22` 只读 `webui.json` 或 `instance.webui_token`；而 `webui_token` 字段（`registry.py:42`）**全项目无任何赋值点**（grep 仅 2 处命中：声明 + 读取） |
| Step4 预览正向 WS + **随机 token** | ⚠️ **缺口 B2** | 预览文本有（`wizard.py:90-91`），但 token 全项目无生成逻辑（`secrets` 仅出现在 auth.py）；前端 `Wizard.vue:94` 调 `doStep(4, {})` 不传 token → 后端 `payload.get("token","")` 写入**空 token** |
| Step4 写入 `config/onebot11_{qq}.json` | ⚠️ **缺口 B3** | `napcat.py:47-49` 仅当 `instance.qq` 非空才落盘；NapCat 走二维码登录时前端不传 qq（`Wizard.vue:210` 传的是空的 `cred.qq`），`wizard.py:72` `if payload.get("qq")` 为假 → qq 恒为 None → 本地落盘被跳过 |
| Step4 正向/反向可选 | ⚠️ **缺口 B4** | 后端支持 `direction`（`wizard.py:85`），但 **UI 不暴露**，默认 `forward`。主链路里海豹与 NapCat 各自跑向导都会配成"客户端"，没有一端监听 → 连线无法建立 |
| Step5 后端 build_start_cmd、状态机 RUNNING | ✅ | `wizard.py:98-106`；`rest.py:63` 同样走 `build_start_cmd`（前端不再传 cmd） |
| Step5 actual_port 回填 | ✅ | 改由 `ws_overview._backfill_actual_port` 延迟回填（比启动瞬间回读更可靠） |
| 海豹写入 `{dataDir}/serve.yaml` 的 `imSession.endPoints` | ✅ | `sealdice.py:11-19` 双路径探测；`:31` `setdefault("imSession").setdefault("endPoints")`；正向不带 `/ws`、反向带 `/ws`（`:32-39`） |
| 海豹 health_check 读 `baseInfo.state` | ✅ | `sealdice.py:57-58`：1→ok、0/3→down、2→none；`ws_overview.py:18-20` 三态映射实线绿/虚线灰/红 |

### C. 其余骰系专项

| 清单项 | 状态 | 依据 / 问题 |
|---|---|---|
| LLBot JSON5 含注释可读写 | ✅ | `atomicio.py:20-27` `read_json_any`；`llbot.py:31,50` `source_json5=True`，写回纯 JSON |
| LLBot 四端口 allocate_many | ✅ | manifest 含 3080/3001/3010/5600；`wizard.py:26-28` |
| LLBot 首启 `--update` | ❌ **缺口 C1** | manifest 有 `post_start_action: "--update"`，**代码中零引用**（grep 无命中） |
| Shiki 无 AutoLogin.yml → config.txt 旧分支 | ✅ | `shiki.py:22-31` 目录存在性分派；协议非推荐值需 `confirm_protocol`（`:18-20`） |
| Shiki 删除保留 Dice 存档 | ⚠️ **缺口 C2** | `rest.py:85-94` 保留的是**硬编码 `config` 目录**，未使用 manifest 的 `save_keep_dir`；且删除确认弹窗把"保留存档"与"删除文件夹"绑成一个 confirm（`Overview.vue:62-64`），只传 `dice === 'shiki'` |
| OlivaDice 缺核阻断 / 缺件告警 | ✅ | `olivadice.py:21` 缺 OlivaDiceCore 抛错；`:23` warnings；`wizard.py:63-64` 显式落盘 |
| NapCat manual → Step4 预览区展示 | ✅ | `napcat.py:51-53` `WriteResult(ok=False, manual=...)`；`wizard.py:87-89` → 前端 `Wizard.vue:91` |

### D. WS 三通道

| 清单项 | 状态 | 依据 |
|---|---|---|
| overview 2s 推送 | ✅ | `ws_overview.py:69` `asyncio.sleep(2.0)` |
| 整合包型无连线单节点 | ✅ | `ws_overview.py:45` 仅 `login_ref` 存在才生成 edge；`arch==allinone` 前端渲染"（整合包）" |
| 三态连线渲染 | ✅ | `EDGE_STATES` + `Overview.vue:70-72` |
| 内存 >90% 水位条变红 | ✅ | `ws_overview.py:66` `alert: ratio >= ctx.resmon_alert(0.90)`；`Overview.vue:5,77` `.fill.alert` |
| logs 回放 / 不丢历史 / pause-resume / seq 连续 / filter / 错误高亮 / 复制下载 | ✅ | `process.py:45-50` 加载 `.log.1`；`ws_logs.py:42-50` 先注册 hook 再取快照 + seq 去重；`:64-73` 指令；`ERROR_RE`（:14）；`LogCenter.vue:41-42` |
| login 扫码推送 / verify 只转发 / refresh 换新码 / completed → CONFIGURED | ✅ | `ws_login.py:26-36`、`:70-73`、`:86-88` |

### E. 健壮性与恢复

| 清单项 | 状态 | 依据 / 问题 |
|---|---|---|
| kill 后重启打印中间态实例 | ✅ | `app.py:20-21` `resume_pending()` |
| 中间态「可经向导继续」 | ❌ **缺口 E1** | `resume_pending` 只在 lifespan 打印，**无 REST 接口、前端无续跑入口**，向导只能新建实例 |
| 并发互斥、端口无重复 | ✅ | `locks.py` RLock + 引用计数文件锁；`ports.py` 独立 `port_allocation_lock` |
| 删除二次确认 / 端口释放 / tombstone / 30 天 purge | ✅ | `rest.py:76-82`、`registry.py:124-144`、`app.py:19` |
| LLBot 手工加注释后仍可读写、热更新 | ✅ | 见 C 组第 1 行 |

### F. 安全项回归

| 清单项 | 状态 | 依据 |
|---|---|---|
| Step5 请求体不含 cmd | ✅ | `rest.py:28-30` `StepReq(step, payload)`；`wizard.py:98` 只用 `build_start_cmd(inst)` |
| WS 指令仅白名单字面量 | ✅ | `ws_logs.py:65-68`（pause/resume/`filter:`）、`ws_login.py:70-76`（refresh/skip_login），其余一律忽略 |
| 外网直连 8765 不可达 | ✅ | `app.py:40` + `deploy/dicemanager.service:9` 均 127.0.0.1；对外只走 nginx 80（`deploy/nginx-dicemanager.conf`） |

---

## 二、缺口清单（按联调影响排序）

| # | 缺口 | 位置 | 影响 | 建议修法 |
|---|---|---|---|---|
| **B1** | NapCat **token 未从启动日志回读**，`webui_token` 字段从未赋值 | `adapters/napcat.py:19-22,55-59`；`core/registry.py:42` | WebUI API 调用 `Bearer None` 必然失败 → Step4 恒走 manual 兜底，C 组"manual 场景"反而变成常态 | 在 `get_actual_port` 同批增加 `extract_webui_token(ring)`，回读后 `registry.update(webui_token=...)` |
| **B2** | Step4 **无随机 token** | `services/wizard.py:86`；`Wizard.vue:94` | 写入空 token，海豹 `accessToken: ""`、NapCat `token: ""`，互联鉴形同虚设 | 后端 `token = payload.get("token") or secrets.token_urlsafe(16)`，并把生成的 token 回显到预览 |
| **B4** | Step4 **UI 不暴露 direction/mode/addr** | `Wizard.vue:90-96` | 主链路两端都按 forward 配成客户端，没有监听方，拓扑永远虚线灰 | Step4 增加「正向/反向 + 端口」选择，或按骰系给默认值（骰子端 forward、登录端 reverse） |
| **B3** | NapCat 本地 `onebot11_{qq}.json` 落盘依赖 qq，二维码登录时 qq 为 None | `adapters/napcat.py:47-49`；`services/wizard.py:72` | 文档 B「确认后写入 config/onebot11_{qq}.json」不会发生 | 从登录日志回读 QQ 号（或登录完成事件回写），再落盘 |
| **C1** | LLBot 首启 `--update` 未实现 | `manifests/llbot.json:post_start_action` | 仓库版本滞后时首启可能报旧版行为 | `build_start_cmd` 首次启动时追加该参数，或新增 `post_start` 钩子 |
| **E1** | 中间态实例只能被打印，无法续跑 | `api/app.py:20` | 清单 E「可经向导继续」无法执行 | 新增 `GET /api/pending` 返回 `resume_pending()`，前端向导支持选中续跑 |

### 次要偏差（不影响主链路，但建议收敛）

1. **`core/resmon.py` 不存在**——文档目录树列出该模块，实际资源监控内联在 `api/rest.py:109-118` 与 `ws_overview.py:58-66`。
2. **`ctx.resmon_warn = 0.80`（资源黄牌）定义后未使用**（仅 0.90 告警生效）。
3. **`services/login.py` 的 `LoginService` 无调用点**（孤儿模块；`ws_login.py` 自行启动进程）。
4. **部署脚本路径不符**：文档为 `scripts/install.sh` + `scripts/run.sh`，实际是 `deploy/install.sh`（无 run.sh，改用 `python -m api.app`）；systemd `User=root` 而非文档要求的 `dicemgr`。
5. **密码恢复说明不一致**：文档说"丢失须删除 instances.json 重建"，实际密码哈希在 `auth.json`（`tests/smoke_local.py:22-26`）。
6. **`napcat.json` 的 `webui_port_bump_limit: 100` 未使用**（依赖 NapCat 自身 +1 行为）。

---

## 三、可直接执行的结论

- **能先跑**：A、D、F 三组基本可整组通过；海豹侧（serve.yaml 双路径 + baseInfo.state 三态）、日志三通道、墓碑删除与端口回收均已落地。
- **会卡住**：B 组 Step3→Step4——NapCat token 拿不到（B1）、token 为空（B2）、本地配置不落盘（B3）、两端都当客户端（B4）。这四项建议**在联调前先修**，否则 B/C 两组大部分条目会以"不符合预期"回溯定位。
- **建议补**：C1（LLBot `--update`）、E1（续跑入口）。
