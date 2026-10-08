# 03 · VPS 后端部署

> 📌 **本次交付状态（如实说明）**：本次开发机没有可用的 VPS，所以**没有在真实 VPS 上执行过部署**。
> 本次端到端跑通用的是「**开发板 → 同网段直连本机后端**」（见第 4.1 节）：
> 板子走 Wi-Fi、后端跑在内网另一台机器上，**全程走真实网络**（不是 USB 桥接）。
> 早期还用过「本机后端 + Cloudflare 临时隧道」（第 4.2 节），协议层面与 VPS 链路完全等价
> （含真实 TLS 校验），但临时隧道地址每次重启都变、本机 DNS 还被投毒过，10Hz 高密度采集下
> 稳定性不如直连，所以当前默认用直连。**换回 https:// 域名即可回到 VPS 链路，代码一行不用改。**
> 下面的部署脚本与配置是按通用 VPS（Ubuntu/Debian + systemd + nginx）写的、已随工程提供，
> 但请在目标机器上按第 2、3 节执行并自行确认结果。

后端是**零第三方依赖**的 Python 标准库服务（`sqlite3` + `http.server`），
所以 VPS 上只要装了 `python3` 就能跑，不需要 `pip install` 任何东西 —— 部署风险最低。

它同时提供三样东西：

| 路径 | 作用 |
|---|---|
| `POST /api/v1/ingest` | 开发板上传数据（需 `X-Device-Token`） |
| `GET /api/v1/latest` 等 | 网页/脚本查询真实数据 |
| `GET /` | 直接返回 Web 前端页面（`web/` 目录） |

---

## 1. 一键部署（推荐）

```bash
# 在 VPS 上，先上传整个工程目录（scp / git 都行），然后：
cd /path/to/ESP/backend/deploy
sudo bash install_vps.sh "你的设备令牌"

# 例：
sudo bash install_vps.sh "CHANGE_ME_DEVICE_TOKEN"
```

脚本会：创建 `/opt/sensor-ingest`、数据目录 `/var/lib/sensor-ingest`、
系统用户 `sensor`、安装并启动 systemd 服务 `sensor-ingest`，最后打印自检结果。

自检：

```bash
curl -s http://127.0.0.1:8000/healthz
curl -s http://127.0.0.1:8000/api/v1/stats
systemctl status sensor-ingest
journalctl -u sensor-ingest -n 50 --no-pager
```

---

## 2. 手工部署（想自己控制时）

```bash
sudo mkdir -p /opt/sensor-ingest /var/lib/sensor-ingest
sudo cp -r backend /opt/sensor-ingest/
sudo cp -r web     /opt/sensor-ingest/
sudo useradd --system --home /opt/sensor-ingest --shell /usr/sbin/nologin sensor
sudo chown -R sensor:sensor /opt/sensor-ingest /var/lib/sensor-ingest

sudo cp /opt/sensor-ingest/backend/deploy/sensor-ingest.service /etc/systemd/system/
sudo nano /etc/systemd/system/sensor-ingest.service     # 改 SENSOR_TOKEN / 端口
sudo systemctl daemon-reload && sudo systemctl enable --now sensor-ingest
```

环境变量（都可选，有默认值）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SENSOR_HOST` | `0.0.0.0` | 监听地址（用 nginx 反代时建议 `127.0.0.1`） |
| `SENSOR_PORT` | `8000` | 监听端口 |
| `SENSOR_DB` | `backend/data/sensor.db` | SQLite 文件路径 |
| `SENSOR_WEB_DIR` | `../web` | 前端静态目录 |
| `SENSOR_TOKEN` | `CHANGE_ME_DEVICE_TOKEN` | **必须改**，与固件一致 |
| `SENSOR_MAX_BODY` | `16384` | 单条报文大小上限（字节） |

---

## 3. 加 HTTPS（强烈建议）

```bash
sudo apt install nginx certbot python3-certbot-nginx
sudo cp /opt/sensor-ingest/backend/deploy/nginx-sensor.conf /etc/nginx/sites-available/sensor-ingest
sudo ln -s /etc/nginx/sites-available/sensor-ingest /etc/nginx/sites-enabled/
sudo nano /etc/nginx/sites-available/sensor-ingest      # 把 your-domain.example.com 换成你的域名
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d your-domain.example.com         # 自动申请证书并改写配置
sudo ufw allow 80,443/tcp
```

之后：

```bash
curl -s https://你的域名/healthz
curl -s https://你的域名/api/v1/latest
```

固件端把 `APP_SERVER_URL` 改成 `https://你的域名/api/v1/ingest` 即可，
HTTPS 时会自动启用证书校验。校验方式由 `APP_TLS_TRUST_MODE` 决定：

| 取值 | 含义 | 什么时候用 |
|---|---|---|
| `0` | 使用 ESP-IDF 内置根证书包 | 域名证书由常见 CA（如 Let's Encrypt）签发 |
| `1` | 使用工程内嵌的真实 CA 链（`firmware/src/server_ca_pem.h`） | 根证书不在 IDF 内置包里（如 Cloudflare 隧道用的 Google Trust Services） |
| `2` | **不校验证书** | ★ 仅本机抓包排错，生产严禁 |

> 实机踩坑：用方式 `0` 连 Cloudflare 隧道时会报
> `esp-x509-crt-bundle: No matching trusted root certificate found` +
> `mbedtls_ssl_handshake returned -0x3000`。
> **正确做法是切到方式 `1`** —— 用 `tools/gen_ca_header.py` 把服务器真实下发的
> CA 链抓下来内嵌进固件（依然是真实校验），而不是把校验关掉。

换域名/换隧道地址时，一条命令完成「改 URL + 重新生成 CA + 重新编译烧录」：

```powershell
python D:\ESP\tools\switch_endpoint.py --url https://你的域名/api/v1/ingest
```

---

## 4. 本机联调（三种可选链路，按"有没有公网"选）

### 4.1 同网段直连（**当前实际在用的方式**，最稳）

如果开发板与后端机器在**同一个可路由网络**里（本例：开发板走手机热点 10.1.42.183，
后端跑在开发机 10.1.41.182:8000，两个网段互通），那就根本不需要内网穿透：

```powershell
# ① 起后端：必须监听 0.0.0.0，否则板子连不进来
$env:SENSOR_HOST='0.0.0.0'; $env:SENSOR_PORT='8000'
$env:SENSOR_DB='D:\ESP\backend\data\sensor.db'
$env:SENSOR_WEB_DIR='D:\ESP\web'
$env:SENSOR_TOKEN='CHANGE_ME_DEVICE_TOKEN'
python D:\ESP\backend\server.py

# ② 固件里把地址填成后端的内网地址（注意是 http://，不是 https://）
#    #define APP_SERVER_URL "http://10.1.41.182:8000/api/v1/ingest"
# ③ 重新编译烧录开发板
```

这条路**全程走真实网络**（不是 USB 桥接），单次上传耗时中位 **0.31 秒**（走隧道要约 0.88 秒），
10Hz 高密度采集下最合适。后端记录来源 IP 就是开发板自己的 IP，可直接核对：

```sql
select remote_addr, count(*) from raw_uploads group by remote_addr order by max(id) desc limit 3;
-- 10.1.42.183:62446 | 2453
```

### 4.2 内网穿透（本机没有公网 IP 时）

链路与 VPS 完全等价（同样是「板子 → 公网 HTTPS → 后端」）：

```powershell
# ① 起后端（同上）
# ② 起隧道（会打印一个 https://xxxx.trycloudflare.com 临时地址）
D:\ESP\tools\bin\cloudflared.exe tunnel --url http://localhost:8000 --no-autoupdate
# ③ 把打印出来的地址填进 firmware/src/app_config.h 的 APP_SERVER_URL（后缀 /api/v1/ingest）
# ④ 重新编译烧录开发板（可以用一条命令：tools/switch_endpoint.py --url https://xxx/api/v1/ingest）
```

> ⚠️ 注意：快速隧道的地址**每次重启都会变**，变了就要改固件重新烧录。
> 正式生产请用 VPS 域名（固定不变）。
>
> 排错经验（本轮真实踩过）：如果 `cloudflared` 报
> `failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": context deadline exceeded`，
> 先别怀疑 Cloudflare —— 多半是**本机 DNS 被投毒**：
>
> ```powershell
> Resolve-DnsName api.trycloudflare.com                    # 本机 DNS：返回 103.73.220.178（错）
> Resolve-DnsName api.trycloudflare.com -Server 223.5.5.5  # 公共 DNS：返回 104.16.230.132（对）
> ```
>
> 此时换公共 DNS（或直接用 4.1 的同网段直连）即可；隧道的数据通道
> （`region*.v2.argotunnel.com:7844`）当时其实是通的，卡的只是创建隧道的那个 API 域名。

### 4.3 VPS + 域名

生产环境用第 1、3 节的方案：固定域名、真实证书、开机自启。

---

## 5. 运维要点

### 看实时日志

```bash
journalctl -u sensor-ingest -f
# 每收到一条上传都会有一行：POST /api/v1/ingest HTTP/1.1" 201
```

### 备份数据库

```bash
# SQLite 在线安全备份（不要直接 cp，WAL 模式下可能不一致）
sqlite3 /var/lib/sensor-ingest/sensor.db ".backup '/root/sensor-$(date +%F).db'"
# 或定时任务
echo '0 3 * * * root sqlite3 /var/lib/sensor-ingest/sensor.db ".backup /root/sensor-$(date +\%F).db"' \
  > /etc/cron.d/sensor-backup
```

### 查原始报文（验证后端没篡改）

```bash
# 每条记录都保存了原始上传字节 + SHA256，可逐字节回溯
curl -s http://127.0.0.1:8000/api/v1/raw/1
sqlite3 /var/lib/sensor-ingest/sensor.db \
  "SELECT id, received_at_utc, device_id, byte_len, substr(sha256,1,16) FROM raw_uploads ORDER BY id DESC LIMIT 5;"
```

### 换设备令牌

```bash
sudo sed -i 's/Environment=SENSOR_TOKEN=.*/Environment=SENSOR_TOKEN=新令牌/' \
  /etc/systemd/system/sensor-ingest.service
sudo systemctl daemon-reload && sudo systemctl restart sensor-ingest
# 同步修改固件 app_config.h 的 APP_DEVICE_TOKEN 并重新烧录
```

### 数据库会不会撑爆

一条记录约 1KB（含原始报文），1 秒 1 条 ≈ 86MB/天。
长期运行建议加一条保留策略：

```sql
-- 只保留最近 30 天（原始报文与记录一起删）
DELETE FROM sensor_records WHERE received_at_utc < datetime('now','-30 day');
DELETE FROM raw_uploads   WHERE received_at_utc < datetime('now','-30 day');
VACUUM;
```