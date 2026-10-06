# 服务器部署与更新 SOP

> 面向**在 Linux 服务器上跑 DiceManager 的运维者**。首次部署、日常更新、排障都按本文走。
> 文中所有命令都在 `47.108.190.188`（Ubuntu 24.04）实测过，踩过的坑标了 ⚠️。
> 最后更新：2026-10-06

---

## 0. 现状速查（2026-10-06 实测）

| 项 | 值 |
|---|---|
| 代码目录 | `/opt/dicemanager`（**不是 git 仓库**，靠 tar 包覆盖更新） |
| 虚拟环境 | `/opt/dicemanager/venv`（Python 3.12.3，**在代码目录内**，不是软链） |
| 数据目录 | `/opt/dicemanager/data` → 软链到 `/var/lib/dicemanager` |
| 日志 | `/var/log/dicemanager/` |
| 服务单元 | `dicemanager.service`（nginx `:8888` → `127.0.0.1:8765`） |
| 面板地址 | `http://<服务器IP>:8888` |
| Node.js | v18.19.1 + npm 9.2.0（部署 Koishi 等 Node 程序**必须**有） |
| 在线实例 | `llbot`（QQ 975809162，端口 3080/3001）、`sealdice`（:3211） |

⚠️ **改名前先记住两件事**（都曾导致服务起不来）：
1. `api/app.py` 硬编码 `uvicorn.run(app, host="127.0.0.1", port=8765)`
   —— **面板只监听本机**，对外访问完全依赖 nginx 反代。
2. `/opt/dicemanager/venv/bin/*` 的 shebang 硬编码了创建时的绝对路径
   （`#!/opt/dicemanager/venv/bin/python3`）。venv 一旦移到别的路径，
   `pip`/`uvicorn` 等命令会失效（服务本身用 `python -m` 调用，不受影响）。

---

## 1. 首次部署

### 1.1 拉代码（⚠️ 服务器上 `git clone` 不可用）

```bash
curl -sL -o /tmp/dm.tar.gz \
  https://codeload.github.com/Melinda-233/dicemanager/tar.gz/refs/heads/main
mkdir -p /opt/dicemanager
tar xzf /tmp/dm.tar.gz -C /opt/dicemanager --strip-components=1
```

⚠️ **为什么不用 `git clone`**：服务器到 GitHub 的 **git 协议**连接会中断
（`fatal: unable to access '...': GnuTLS recv error (-110)`）。
而 **codeload 的 tarball 端点完全正常**（1.26 秒 / 462KB）。
注意这与「本机能直连 GitHub 0.9s」不矛盾 —— 能连通 ≠ git 协议可用。

### 1.2 装依赖

```bash
python3 -m venv /opt/dicemanager/venv
/opt/dicemanager/venv/bin/pip install -q fastapi 'uvicorn[standard]' pyyaml psutil httpx
```

⚠️ **不要用 `pip install -e .`**：本项目的 `pyproject.toml` 只有
`[tool.ruff]` / `[tool.mypy]` 等**工具配置**，没有 `[project]` 与
`[build-system]`，根本不是一个可安装的包，会报
`Failed to build ... when getting requirements to build editable`。

### 1.3 ⚠️ 必须构建前端（最容易漏的一步）

```bash
cd /opt/dicemanager/web
npm config set registry https://registry.npmmirror.com   # 国内加速
npm ci
npm run build          # 产物在 web/dist/
```

**为什么必须做**：`web/dist` 是**构建产物、被 .gitignore 排除**，tar 包里
只有 `web/src`。而 `api/app.py` 的静态挂载是有条件的：

```python
_dist = Path(__file__).resolve().parent.parent / "web" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="web")
else:
    log.warning("前端构建产物不存在: %s，仅提供 API", _dist)
```

跳过这步 → 服务正常起、API 可用，但**页面 404**（打开是一片空白）。
**判活时别用 `curl -I /`**（HEAD 对根路径回 404 是正常的，会误判成服务挂了），
用 GET 打真实端点：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/          # 应 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/api/needs-setup  # 应 200
```

### 1.4 数据目录与 systemd

```bash
mkdir -p /var/lib/dicemanager /var/log/dicemanager
ln -sfn /var/lib/dicemanager /opt/dicemanager/data
```

`/etc/systemd/system/dicemanager.service`：

```ini
[Unit]
Description=DiceManager - QQ 骰子程序统一管理面板
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/dicemanager
ExecStart=/opt/dicemanager/venv/bin/python -m api.app
Environment=PYTHONUNBUFFERED=1
MemoryMax=400M              # 2G 小内存机器：给管理器自身设上限，避免失控拖垮系统
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now dicemanager.service
systemctl status dicemanager.service
```

### 1.5 nginx 反代（**不能删**）

`/etc/nginx/conf.d/dicemanager.conf`：

```nginx
# 管理器本身只监听 127.0.0.1:8765，由 Nginx 对外提供访问。
# WebSocket 必须带 Upgrade / Connection 头，否则总览、日志、登录二维码都不会动。
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 8888;
    server_name _;                      # 有域名就改成你的域名

    # 对齐离线包上限 2GB：小于此值会被 nginx 直接 413，用户看不到应用层提示
    client_max_body_size 2048m;

    # 带内容哈希的静态资源：内容变化时文件名必变，可放心长缓存
    location /assets/ {
        proxy_pass http://127.0.0.1:8765;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;         # ← WebSocket 必需
        proxy_set_header Connection $connection_upgrade;  # ← WebSocket 必需
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # 入口页禁缓存，避免浏览器凭启发式缓存拿到旧 index.html
        add_header Cache-Control "no-cache" always;
    }
}
```

⚠️ **四项作用，缺一不可**：
1. 面板只监听 `127.0.0.1`，**删了反代外网就完全访问不到面板**
2. WebSocket 头透传 —— 总览/日志/登录二维码靠它，缺了前端不动
3. `client_max_body_size 2048m` —— 对齐离线包 2GB 上限
4. 静态资源缓存策略（`/assets/` 长缓存 + 入口禁缓存）

---

## 2. 日常更新

### 2.1 标准流程（约 2 分钟）

```bash
# 0. 备份（代码 + 单元文件，是唯一的回滚依据）
TS=$(date +%Y%m%d-%H%M%S)
mkdir -p /opt/dm_bk2_$TS
cp /etc/systemd/system/dicemanager.service /opt/dm_bk2_$TS/unit.orig
tar czf /opt/dm_bk2_$TS/old_code.tar.gz -C /opt dicemanager

# 1. 拉新代码
curl -sL -o /tmp/dm.tar.gz \
  https://codeload.github.com/Melinda-233/dicemanager/tar.gz/refs/heads/main
rm -rf /opt/dicemanager_new && mkdir -p /opt/dicemanager_new
tar xzf /tmp/dm.tar.gz -C /opt/dicemanager_new --strip-components=1

# 2. ⚠️ 数据与 venv 要接回来（新目录是干净的）
ln -sfn /opt/dicemanager/venv /opt/dicemanager_new/venv
ln -sfn /var/lib/dicemanager /opt/dicemanager_new/data

# 3. 依赖（venv 复用，只补新包）
cd /opt/dicemanager_new/web && npm ci && npm run build && cd ..
/opt/dicemanager_new/venv/bin/pip install -q fastapi 'uvicorn[standard]' pyyaml psutil httpx

# 4. 离线验证：新代码能起来吗（**不影响线上**）
cd /opt/dicemanager_new
DM_EDITION=server /opt/dicemanager_new/venv/bin/python -c "
from adapters import load_registry
reg = load_registry('manifests')
print('适配器数:', len(reg)); print(sorted(reg))
"

# 5. 切单元 + 重启
sed -i 's#/opt/dicemanager#/opt/dicemanager_new#g' /etc/systemd/system/dicemanager.service
systemctl daemon-reload && systemctl restart dicemanager.service
sleep 6

# 6. 验证（**全部通过才算成功**）
curl -s -o /dev/null -w 'panel %{http_code}\n' http://127.0.0.1:8765/            # 200
curl -s -o /dev/null -w 'api    %{http_code}\n' http://127.0.0.1:8765/api/needs-setup  # 200
journalctl -u dicemanager --since '-3min' --no-pager | grep -iE 'error|traceback'   # 应为空
journalctl -u dicemanager --since '-3min' --no-pager | grep -i resume  # 实例是否自动拉起
ps -eo pid,etime,cmd | grep -E 'llbot|sealdice' | grep -v grep      # 实例进程在不在
```

⚠️ **第 4 步不要跳过**。曾因为没做这步就改单元，出过「单元指向不存在目录、
一重启服务就起不来」的事故。

⚠️ **第 6 步的实例检查别省**。面板重启会**自动 resume** `state=RUNNING`
的实例（日志里会打「面板重启后待恢复实例：xxx 已自动拉起」）；
如果某个实例没起来，去看它是不是被标成了别的状态。

### 2.2 收敛成正式目录（更新几轮后整理）

更新时为了安全先落在 `_new` 目录，稳定运行几天后可以收敛成正式目录：

```bash
# ⚠️ 顺序不能错 —— venv 原本是软链，直接改名会让它指向自己
rm /opt/dicemanager_new/venv                    # 先删软链
mv /opt/dicemanager/venv /opt/dicemanager_new/venv   # 再把 venv 移进去
mv /opt/dicemanager /opt/dicemanager_old        # 旧目录让出名字
mv /opt/dicemanager_new /opt/dicemanager        # 新代码就位
sed -i 's#/opt/dicemanager_new#/opt/dicemanager#g' /etc/systemd/system/dicemanager.service
systemctl daemon-reload && systemctl restart dicemanager.service
sleep 6
# 验证通过后再删旧目录
rm -rf /opt/dicemanager_old
```

⚠️ `mv` 到软链上会把文件移进**软链指向的目录**里 —— 所以必须先 `rm` 软链再 `mv`。

### 2.3 备份清理

⚠️ 备份目录名带 `2`（`/opt/dm_bk2_<时间戳>/`）—— 那是部署脚本里的命名，
别写成 `dm_bk_*`（glob 匹配不到，会让你以为备份不存在）。

```bash
# 保留最近一份回滚用的，其余删掉
KEEP=$(ls -dt /opt/dm_bk2_* | head -1)
for d in /opt/dm_bk2_*; do
  [ "$d" = "$KEEP" ] && echo "保留 $d" || { echo "删除 $d"; rm -rf "$d"; }
done
```

历史遗留的备份（换部署方式后可能堆积在别处）：

```bash
ls -ld /opt/dicemanager_backup_* /opt/dm_update_backup_* 2>/dev/null
du -sh /opt/dicemanager_backup_* 2>/dev/null
# 确认无用后删除（保留上面选出的那一份）
```

⚠️ 删之前确认**当前版本能跑**（页面 200 + 实例在跑）。别在更新失败时删备份。

### 2.4 回滚

```bash
ls -dt /opt/dm_bk2_* | head -1                    # 找到最近一份
tar xzf /opt/dm_bk2_*/old_code.tar.gz -C /tmp     # 解到临时目录
# 覆盖回去（venv 与 data 是软链/独立目录，不要动）
rsync -a --delete --exclude venv --exclude data \
  /tmp/dicemanager/ /opt/dicemanager/
# 或者只回退最近几个文件
cp /opt/dm_bk2_*/unit.orig /etc/systemd/system/dicemanager.service
systemctl daemon-reload && systemctl restart dicemanager.service
```

---

## 3. 排障

### 3.1 面板打不开

```bash
systemctl is-active dicemanager.service
journalctl -u dicemanager -n 50 --no-pager
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/api/needs-setup
```

| 现象 | 原因 | 处理 |
|---|---|---|
| 连不上（000/超时） | 服务没起或端口不对 | 看 journalctl 报错 |
| API 200 但页面 404 | **`web/dist` 没构建** | `cd web && npm ci && npm run build` 再重启 |
| 只有 8765 不通、8888 通 | 反代配置有问题 | `nginx -t && systemctl reload nginx` |
| 8888 也不通 | nginx 没起 | `systemctl status nginx` |

⚠️ **404 恰恰说明 HTTP 在应答**。别把 404 当成「服务挂了」——
用真实 API 端点（如 `/api/needs-setup`）判活。

### 3.2 面板起来了但实例没起来

```bash
# 1. 面板记录的状态
python3 -c "import json;d=json.load(open('/var/lib/dicemanager/instances.json')); \
[print(' ',k,d[k].get('dice'),d[k].get('state'),d[k].get('dir')) \
 for k in d if not k.startswith('__tombstone__')]"
# 2. resume 日志
journalctl -u dicemanager --since '-10min' --no-pager | grep -i resume
# 3. 进程实际在不在
ps -eo pid,etime,cmd | grep -E 'llbot|sealdice' | grep -v grep
```

⚠️ `instances.json` 的顶层是 **dict**（键为实例 id），不是 list。
按 list 解析会读到 0 个，误判成「实例全是孤儿」。

`__tombstone__` 前缀的条目是**已删除实例的历史记录**（保留用于端口/状态追溯），
不是活跃实例。

**孤儿进程**：面板里已删但进程没被杀（`ps` 里有、`instances.json` 里是
`removed`）。确认不再需要后手工清理：

```bash
kill <pid>          # 先TERM 优雅退出
sleep 5 && kill -9 <pid>   # 不听话再强杀
```

### 3.3 部署 Koishi / 依赖 Node 的程序

需服务器有 Node.js ≥ 18：

```bash
node -v || echo '需要先装 Node.js'
```

**网络卡住时的排查顺序**（别一上来就怪网络）：

```bash
# 1. registry 通不通（curl 1 秒内返回 = 网络没问题）
curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' --max-time 30 \
  https://registry.npmmirror.com/koishi
# 2. npm 客户端能不能读
timeout 90 npm view create-koishi version --registry https://registry.npmmirror.com
# 3. 手动跑一次 + 关stdin + 计时（区分「网络慢」和「在等输入」）
cd /tmp && rm -rf kbt && mkdir kbt && cd kbt
time timeout 120 npm create koishi@latest . < /dev/null
```

⚠️ **「超时」不等于网络问题**。若 curl 与 `npm view` 都秒回，但
`npm create` 挂住 —— 那是第三方 CLI 在**等交互输入**。
`create-koishi` 的最后一问**不受 `--yes` 影响**（npm 把自己那个 `--yes`
吃掉了，不转发给脚手架脚本），子进程必须 `stdin=DEVNULL` 才不会挂死。

**国内加速**：在面板的服务单元里加
`Environment="DM_NPM_REGISTRY=https://registry.npmmirror.com"`
（适配器会优先用用户的 `npm_config_registry`，其次是这个，最后才用清单里的兜底值）。

### 3.4 磁盘

```bash
df -h /
du -sh /var/lib/dicemanager/* 2>/dev/null | sort -h | tail
```

`/var/lib/dicemanager/packages/` 是下载的程序包（llbot 91M + sealdice 83M），
面板的「删除实例」会清掉对应包。**不要手工删**，会与面板记录不一致。

---

## 4. 自动化脚本

`.workbuddy/` 下有一套已实测的脚本（需 paramiko，从本机跑）：

| 脚本 | 用途 |
|---|---|
| `probe_server.py` | 只读探活：系统/Node/服务/端口/项目路径 |
| `deploy_server2.py` | 部署 + 切单元（**每步校验，失败即停**） |
| `build_web_server.py` | 构建前端 dist（修 404） |
| `verify_deployed.py` | 部署后全面复核 |
| `cleanup_backups.py` | 清理旧备份（保留最近一份） |
| `run_e2e_remote.py` | 在服务器上跑 Koishi 等适配器的端到端真跑验证 |
| `audit_server.py` | 反代/备份/进程盘点 |

⚠️ **写这类脚本时必须内建「每步失败即停 + 结果校验」**。
本项目出过一次事故：脚本第一步 `git clone` 失败后**没有中止**，
继续 `sed` 改了 systemd 单元 → 单元指向不存在的目录，
那一刻只要有人重启服务，线上就起不来。修复用的回滚脚本也在同目录
（`rollback_server.py`）。

校验标记要选**「一定出现在成功路径上」**的内容 —— 曾因为选了
「只在异常分支才出现的 echo」而误报失败，实际操作已成功。

---

## 5. 相关文档

- `docs/使用手册.md` —— 使用者向的操作手册（面向用面板的人）
- `DiceManager-Manual.md` —— 贡献者向（架构、接口契约、新增程序）
- `.workbuddy/memory/2026-10-06.md` —— 本次部署的完整过程与踩坑记录
