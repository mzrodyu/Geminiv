#!/usr/bin/env bash
# ============================================================
# Geminiv（Vertex/Agent Platform → OpenAI 兼容代理）一键部署脚本
# 适用：Debian 11/12、Ubuntu 20.04+（systemd）
# 用法（root）：
#   bash deploy.sh
#   自定义：REPO=... DIR=/opt/geminiv PORT=7860 bash deploy.sh
# 国内加速：PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple bash deploy.sh
# ============================================================
set -euo pipefail

REPO="${REPO:-https://github.com/mzrodyu/Geminiv.git}"
DIR="${DIR:-/opt/geminiv}"
PORT="${PORT:-7860}"
SERVICE="${SERVICE:-geminiv}"

cyan() { printf '\033[36m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
red() { printf '\033[31m%s\033[0m\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
  red "请用 root 运行：sudo bash deploy.sh"
  exit 1
fi

cyan "==> 1/6 安装系统依赖"
apt-get update -y >/dev/null
apt-get install -y git python3 python3-venv python3-pip curl >/dev/null
python3 -V

cyan "==> 2/6 拉取代码到 $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch --depth 1 origin main
  git -C "$DIR" reset --hard FETCH_HEAD
else
  mkdir -p "$DIR"
  git clone --depth 1 "$REPO" "$DIR"
fi

cyan "==> 3/6 创建虚拟环境并安装依赖（首次约 1-2 分钟）"
mkdir -p "$DIR/data"
[ -d "$DIR/venv" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -U pip >/dev/null
"$DIR/venv/bin/pip" install -r "$DIR/app/requirements.txt"

cyan "==> 4/6 写入 systemd 服务 /etc/systemd/system/$SERVICE.service"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Geminiv - Agent Platform (Gemini) to OpenAI compatible adapter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR/app
Environment=STATE_DIR=$DIR/data
Environment=PYTHONUNBUFFERED=1
ExecStart=$DIR/venv/bin/uvicorn main:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cyan "==> 5/6 启动并设置开机自启"
systemctl daemon-reload
systemctl enable --now "$SERVICE" >/dev/null
sleep 2
systemctl --no-pager --lines=0 status "$SERVICE" || true

cyan "==> 6/6 健康检查"
for i in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:$PORT/" -o /dev/null 2>/dev/null; then
    green "服务已就绪。"
    break
  fi
  [ "$i" -eq 20 ] && red "20 秒内未就绪，看日志：journalctl -u $SERVICE -n 80 --no-pager"
  sleep 1
done

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "${IP:-}" ] || IP="<服务器IP>"
cat <<EOF

============================ 部署完成 ============================
控制台地址 : http://$IP:$PORT
初始口令   : 123456
状态文件   : $DIR/data/web_state.json（删掉等于恢复出厂）

进去之后（全在浏览器里点，不用改任何环境变量）：
  1. 用 123456 登录 → 自动弹「首次启动向导」
  2. 第 1 步点「⚡ 一键随机口令并保存」把默认口令换掉
  3. 第 2 步选主通道，粘 Express API Key（一行一个）或 Cookie + Project ID
  4. 第 3 步拿到 http://$IP:$PORT/v1，客户端 Base URL 填它、Key 填新口令即可

常用命令：
  systemctl status $SERVICE         # 状态
  journalctl -u $SERVICE -f         # 实时日志
  systemctl restart $SERVICE        # 重启
  bash $DIR/deploy.sh               # 更新代码并重启（幂等）

如果外网打不开：放行防火墙 / 云厂商安全组的 TCP $PORT
=================================================================
EOF
