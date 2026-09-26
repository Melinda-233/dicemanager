#!/usr/bin/env bash
# DiceManager 一键部署脚本（阿里云 Linux 轻量 2C2G 适用）
# 用法：
#   sudo bash install.sh                  # 完整部署（含前端构建）
#   sudo bash install.sh --skip-build     # 跳过前端构建（已自带 web/dist 时用）
#   sudo bash install.sh --swap           # 额外创建 2G swap（2G 内存机器建议加）
set -euo pipefail

APP_DIR=/opt/dicemanager
VENV="$APP_DIR/venv"
REPO="${DM_REPO:-https://github.com/Melinda-233/dicemanager.git}"
BRANCH="${DM_BRANCH:-main}"
MIRROR="${DM_GITHUB_MIRROR:-}"            # 例如 https://ghfast.top
SKIP_BUILD=0
MAKE_SWAP=0
for arg in "$@"; do
  case "$arg" in
    --skip-build) SKIP_BUILD=1 ;;
    --swap)       MAKE_SWAP=1 ;;
  esac
done

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 执行：sudo bash install.sh"

# ---------- 0. swap（可选） ----------
if [ "$MAKE_SWAP" = 1 ]; then
  log "创建 2G swap（避免前端构建 / 多骰子并发时 OOM）"
  if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile && swapon /swapfile
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  else
    warn "swapfile 已存在，跳过"
  fi
  sysctl -w vm.swappiness=10 >/dev/null 2>&1 || true
fi

# ---------- 1. 系统依赖 ----------
log "安装系统依赖"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip curl git nginx unzip
  [ "$SKIP_BUILD" = 1 ] || apt-get install -y nodejs npm
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip curl git nginx unzip
  [ "$SKIP_BUILD" = 1 ] || dnf install -y nodejs npm
elif command -v yum >/dev/null 2>&1; then
  yum install -y python3 python3-pip curl git nginx unzip
  [ "$SKIP_BUILD" = 1 ] || yum install -y nodejs npm
else
  die "未识别的包管理器，请手动安装 python3/pip/nodejs/nginx 后重跑"
fi

# ---------- 2. Python 版本检查（代码用了 3.10+ 的类型语法） ----------
PY="$(command -v python3)"
PY_VER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
printf 'python3 版本：%s\n' "$PY_VER"
"$PY" - <<'EOF' || die "需要 Python 3.10+（代码使用了 X | None 语法）。请升级 python3 后重跑。"
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF

# ---------- 3. 拉代码 ----------
log "获取代码 -> $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch --depth=1 origin "$BRANCH" && git -C "$APP_DIR" reset --hard "origin/$BRANCH"
else
  REPO_URL="$REPO"
  [ -n "$MIRROR" ] && REPO_URL="$MIRROR/$REPO"
  git clone --depth=1 -b "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

# ---------- 4. Python 依赖 ----------
log "安装 Python 依赖（venv）"
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip -q
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt"

# ---------- 5. 前端构建 ----------
if [ "$SKIP_BUILD" = 1 ]; then
  log "跳过前端构建（--skip-build）"
  [ -d "$APP_DIR/web/dist" ] || warn "web/dist 不存在，页面将只有 API（后端启动会提示）"
else
  log "构建前端（2G 内存机器约 1-3 分钟，可能要 swap）"
  cd "$APP_DIR/web"
  npm config set registry https://registry.npmmirror.com      # 国内 npm 源
  npm install
  npm run build
  cd "$APP_DIR"
fi

# ---------- 6. 运行时目录 ----------
log "准备运行时目录"
mkdir -p /var/lib/dicemanager /var/log/dicemanager
chmod 750 /var/lib/dicemanager

# ---------- 7. systemd ----------
log "安装 systemd 服务"
sed -e "s#^ExecStart=.*#ExecStart=$VENV/bin/python -m api.app#" \
    "$APP_DIR/deploy/dicemanager.service" > /etc/systemd/system/dicemanager.service
if [ -n "$MIRROR" ]; then
  # 取消注释并写入镜像地址
  sed -i "s|^# Environment=\"DM_GITHUB_MIRROR=.*|Environment=\"DM_GITHUB_MIRROR=$MIRROR\"|" \
      /etc/systemd/system/dicemanager.service
fi
systemctl daemon-reload
systemctl enable --now dicemanager

# ---------- 8. Nginx ----------
# 面板对外端口一律以 nginx 配置为准（该机 80 被既有站点 default_server 占用 → 实际 8888）。
# 硬编码 80 会让「放行端口」与「访问地址」两处提示同时指错，换机部署必然踩坑。
PANEL_PORT=$(sed -n 's/^[[:space:]]*listen[[:space:]]*\([0-9]\{1,5\}\).*/\1/p' \
    "$APP_DIR/deploy/nginx-dicemanager.conf" | head -1)
PANEL_PORT=${PANEL_PORT:-8888}
log "配置 Nginx 反向代理（对外端口 $PANEL_PORT）"
cp "$APP_DIR/deploy/nginx-dicemanager.conf" /etc/nginx/conf.d/dicemanager.conf
if command -v nginx >/dev/null 2>&1; then
  nginx -t && systemctl enable --now nginx && systemctl reload nginx
fi
# 放行的是面板实际端口，不是 http(80)：两者不一致时按 80 放行等于没放行
if command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port="${PANEL_PORT}/tcp" >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
elif command -v ufw >/dev/null 2>&1; then
  ufw allow "${PANEL_PORT}/tcp" >/dev/null 2>&1 || true
fi

# ---------- 9. 输出 ----------
sleep 3
log "部署完成"
systemctl status dicemanager --no-pager | head -5 || true
echo
echo "查看本次管理密码（首次启动随机生成）："
echo "    journalctl -u dicemanager -n 50 | grep '\\[auth\\]'"
echo
PUBLIC_IP=$(curl -s --max-time 3 http://100.100.100.200/latest/meta-data/public-ipv4 2>/dev/null || echo '<你的公网IP>')
if [ "$PANEL_PORT" = "80" ]; then
  echo "浏览器访问：  http://$PUBLIC_IP"
else
  echo "浏览器访问：  http://$PUBLIC_IP:$PANEL_PORT"
fi
echo "（记得在云厂商安全组放行 ${PANEL_PORT} 端口）"
