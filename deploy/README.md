# 服务器部署说明（阿里云 Linux 轻量 2C2G）

## 1. 前置条件

| 项目 | 要求 |
|---|---|
| 系统 | Ubuntu 20.04+ / Debian 11+ / Alibaba Cloud Linux 3 / CentOS Stream（脚本自动识别 apt、dnf、yum） |
| Python | **必须 3.10+**（代码使用 `X \| None` 语法，3.9 及以下会直接语法错误） |
| 内存 | 2G 可用，但**建议加 2G swap**（脚本 `--swap` 参数一键加） |
| 端口 | 阿里云安全组需放行 **80**（管理器自身只监听 127.0.0.1:8765，经 Nginx 对外） |
| 权限 | root |

> **2G 内存容量提醒**：管理器本体约 80–150MB。骰子程序本身更吃内存，
> 建议**同时运行 1–2 个骰子**；超过后总览页的内存水位条会转红（≥90% 告警、部署前预估 ≥80% 黄牌）。
> 若要跑多个，先加 swap 并盯住 `/api/resmon`。

## 2. 一键部署

```bash
# 推荐（2G 内存机器）：同时创建 swap
curl -fsSL https://raw.githubusercontent.com/Melinda-233/dicemanager/main/deploy/install.sh \
  | sudo bash -s -- --swap

# 或者先下载再执行
sudo bash install.sh --swap
```

脚本会依次完成：装系统依赖 → 拉代码到 `/opt/dicemanager` → 建 venv 装 Python 依赖 →
构建前端（`web/dist`，用 npmmirror 源）→ 建 `/var/lib/dicemanager`、`/var/log/dicemanager` →
装 systemd 服务并启动 → 配 Nginx 反代（含 WebSocket 头）→ 打印管理密码获取命令。

## 3. 登录

管理密码在**首次启动时随机生成并打印到控制台**：

```bash
journalctl -u dicemanager -n 50 | grep '\[auth\]'
# [auth] 本次管理密码: xxxx
```

浏览器打开 `http://你的公网IP` 输入密码即可。忘记密码：

```bash
rm -f /var/lib/dicemanager/auth.json && systemctl restart dicemanager
```

## 4. 国内服务器拉 GitHub 的两种办法

五个骰子程序的安装包**全部从 GitHub Releases 下载**，国内机器经常超时。任选其一：

**办法 A：镜像加速（推荐，改一行环境变量）**

```bash
sudo systemctl edit dicemanager
# 写入：
# [Service]
# Environment="DM_GITHUB_MIRROR=https://ghfast.top"
sudo systemctl daemon-reload && sudo systemctl restart dicemanager
```

也可在部署时直接指定：`sudo DM_GITHUB_MIRROR=https://ghfast.top bash install.sh`
（脚本会自动把它写进 systemd 单元）。镜像失效时换 `https://gh-proxy.com`、`https://mirror.ghproxy.com` 等。

**办法 B：给服务挂代理**（若服务器自己有可用代理）

```ini
[Service]
Environment="HTTPS_PROXY=http://127.0.0.1:7890"
```

## 5. 常用运维

```bash
systemctl status dicemanager          # 状态
journalctl -u dicemanager -f          # 实时日志（含管理密码、实例告警）
systemctl restart dicemanager         # 重启

cd /opt/dicemanager && git pull && systemctl restart dicemanager   # 更新代码

ls /var/log/dicemanager               # 各实例日志（50MB / 7 天滚动）
cat /var/lib/dicemanager/instances.json   # 实例注册表
cat /var/lib/dicemanager/ports.json       # 端口分配表
```

## 6. 上 HTTPS（可选）

两种方案任选其一：

**方案 A：已有 Nginx（certbot 改 nginx 配置）**

```bash
# Ubuntu/Debian
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d 你的域名          # 自动改 nginx 配置，WebSocket 头会被保留
```

**方案 B：Caddy 自动证书（推荐新装，零证书维护）**

把域名 A 记录指到服务器、安全组放行 80/443，然后：

```bash
apt-get install -y caddy             # 或 yum install caddy
cp /opt/dicemanager/deploy/caddy-dicemanager.conf /etc/caddy/Caddyfile
# 编辑 Caddyfile：把 panel.example.com 换成你的域名
systemctl reload caddy
```

模板见 `deploy/caddy-dicemanager.conf`（含 WebSocket 透传、流式响应不缓冲、
骰子 WebUI 反代示例）。Caddy 自动签发/续期证书，无需 certbot。

## 7. 目录位置

| 路径 | 内容 |
|---|---|
| `/opt/dicemanager` | 程序本体（venv 在 `/opt/dicemanager/venv`） |
| `/var/lib/dicemanager` | instances.json / ports.json / auth.json |
| `/var/log/dicemanager` | 各实例日志 |
| `/opt/{程序名}` | 骰子程序安装目录（manifests 的 install_root 决定） |

## 8. 已知环境限制

- `core/locks.py` 用 `fcntl` 文件锁，**仅 Linux 可用**（Windows 本地调试会报错，属预期行为）。
- 管理器监听 `127.0.0.1:8765`，不经 Nginx 直接暴露端口是访问不到的。
- 前端未构建时后端仍能启动（打印 warning），但打开页面会是空白 —— 需先 `npm run build`。

### 依赖版本必须锁上限（踩过的坑）

`requirements.txt` 里 fastapi / starlette / uvicorn **带上限**是必要的，不要随意 `pip install -U`：

- fastapi 0.141 + starlette 1.6 之后，前端静态资源挂载在 `/` 时会吞掉 WebSocket 路由，
  `/ws/overview`、`/ws/logs/{id}`、`/ws/login/{id}` 全部返回 **404**，页面能打开但总览空白、日志不滚动、扫码登录无响应。
- 已验证可用组合：`fastapi 0.115.6` + `starlette 0.41.3` + `uvicorn 0.32.1`。
- 排错命令：`journalctl -u dicemanager | grep -i websocket`；用 Nginx 时确认反代带 `Upgrade` / `Connection` 头。

### 端口被占用时的做法

若 80 端口已被其他站点占用（常见：宝塔默认站或其他反代），不要抢同一个 `server_name`，
另起一个端口即可（本项目实测部署在 **8888**，记得同时在阿里云安全组与 ufw 放行该端口）。
