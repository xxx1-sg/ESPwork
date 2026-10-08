#!/usr/bin/env bash
# ============================================================================
#  install_vps.sh —— 在 VPS 上一键部署「传感器接收/存储/查询」服务
#
#  适用: Debian 11/12、Ubuntu 20.04/22.04/24.04（其它带 systemd 的发行版同理）
#  用法:
#      sudo bash install_vps.sh [设备令牌]
#  例:
#      sudo bash install_vps.sh "my-secret-device-token-2026"
#
#  做的事:
#      1) 检查 python3（本服务只用标准库，无需 pip 安装任何依赖）
#      2) 建目录 /opt/sensor-ingest 与数据目录 /var/lib/sensor-ingest
#      3) 拷贝 backend/ 与 web/ 代码
#      4) 建系统用户 sensor 并授权数据目录
#      5) 安装并启动 systemd 服务
#      6) 打印自检命令
# ============================================================================
set -euo pipefail

TOKEN="${1:-CHANGE_ME_DEVICE_TOKEN}"
APP_DIR=/opt/sensor-ingest
DATA_DIR=/var/lib/sensor-ingest
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # 仓库根目录

echo "=== 1/6 检查运行环境 ==="
command -v python3 >/dev/null || { echo "缺少 python3，请先 apt install python3"; exit 1; }
python3 -V

echo "=== 2/6 创建目录 ==="
mkdir -p "$APP_DIR" "$DATA_DIR"

echo "=== 3/6 拷贝代码 (源: $SRC_DIR) ==="
cp -r "$SRC_DIR/backend" "$APP_DIR/"
cp -r "$SRC_DIR/web"     "$APP_DIR/"
rm -rf "$APP_DIR/backend/data"      # 不要把本地测试数据库带上生产

echo "=== 4/6 创建服务账号并授权 ==="
if ! id sensor >/dev/null 2>&1; then
    useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin sensor
fi
chown -R sensor:sensor "$DATA_DIR" "$APP_DIR"

echo "=== 5/6 安装 systemd 服务 ==="
sed "s|Environment=SENSOR_TOKEN=.*|Environment=SENSOR_TOKEN=${TOKEN}|" \
    "$APP_DIR/backend/deploy/sensor-ingest.service" > /etc/systemd/system/sensor-ingest.service
systemctl daemon-reload
systemctl enable --now sensor-ingest

echo "=== 6/6 自检 ==="
sleep 2
systemctl --no-pager --full status sensor-ingest | head -n 12 || true
echo
echo "--- 健康检查 ---"
curl -s http://127.0.0.1:8000/healthz || echo "（服务可能还没起来，看上面的 journal 日志）"
echo
echo "--- 最近日志 ---"
journalctl -u sensor-ingest -n 15 --no-pager || true
echo
cat <<EOF

部署完成。接下来：
  1) 放行防火墙端口（如果用 nginx 反代就只需 80/443）:
       ufw allow 80,443/tcp
  2) 配置 HTTPS 反代: 见 backend/deploy/nginx-sensor.conf
  3) 把固件 firmware/src/app_config.h 里的
       APP_SERVER_URL   改成 https://你的域名/api/v1/ingest
       APP_DEVICE_TOKEN 改成上面用的令牌: ${TOKEN}
     然后重新编译烧录开发板。
  4) 浏览器打开 https://你的域名/ 就能看到实时页面。
EOF