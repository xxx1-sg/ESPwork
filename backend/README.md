# 传感器数据接收 / 存储 / 查询 服务（后端）

零第三方依赖的 Python 标准库服务：**接收开发板上传的真实传感数据 → 原样落库 → 提供查询 API → 顺便托管网页**。

```
backend/
├── server.py                      # 全部服务代码（可直接 python server.py 运行）
├── data/sensor.db                 # SQLite 数据库（自动创建）
└── deploy/
    ├── sensor-ingest.service      # systemd 单元
    ├── nginx-sensor.conf          # nginx + HTTPS 反代配置
    └── install_vps.sh             # VPS 一键部署脚本
```

## 运行

```bash
# 最简：直接跑（默认 0.0.0.0:8000，库在 backend/data/sensor.db）
python3 server.py

# 带环境变量
SENSOR_PORT=8000 \
SENSOR_DB=/var/lib/sensor-ingest/sensor.db \
SENSOR_WEB_DIR=/opt/sensor-ingest/web \
SENSOR_TOKEN=你的设备令牌 \
python3 server.py
```

启动后会打印监听地址、数据库路径、Web 目录，然后用浏览器打开 `http://<IP>:8000/` 即可看到实时页面。

---

## 接口

### POST `/api/v1/ingest` —— 开发板上传

请求头：`Content-Type: application/json`、`X-Device-Token: <令牌>`

请求体（开发板真实发出的报文，字段含义见 `firmware/src/main.c`）。

板子现在是 **100ms 采一个点、攒 10 个点（1 秒）发一个包**，所以报文主体是
`samples[]` 数组（一次请求存 N 行，**一个事务**完成）；包顶层同时保留
`raw`/`accel_g`/`accel_ms2`/`magnitude_g`/`seq`/`ts_local` 等字段，等于**最后一个采样点**的
值（兼容早期版本）：

```json
{
  "device_id": "esp32s3eye-1C701C",
  "boot_count": 808,
  "sample_period_ms": 100,
  "sample_count": 10,
  "seq_first": 3181,
  "seq_last": 3190,
  "seq": 3190,
  "uptime_ms": 318340,
  "ts_utc_ms": 1759921400123,
  "ts_local": "2026-10-08T13:49:29.337+0800",
  "sensor": {
    "model": "QMA6100P", "type": "accelerometer", "i2c_addr": 18, "chip_id": 144,
    "range_g": 2, "lsb_per_g": 4096, "odr_hz": 100, "oversample": 8
  },
  "unit": { "raw": "LSB(14bit signed)", "accel_g": "g", "accel_ms2": "m/s^2", "magnitude_g": "g" },
  "samples": [
    { "seq": 3181, "idx": 1, "up_ms": 317440, "ts_utc_ms": 1759921399123,
      "ts_local": "2026-10-08T13:49:28.237+0800",
      "raw": {"x": -3090, "y": 1901, "z": 1352},
      "accel_g": {"x": -0.754395, "y": 0.464111, "z": 0.330078},
      "accel_ms2": {"x": -7.397, "y": 4.551, "z": 3.237},
      "magnitude_g": 0.946104 },
    { "seq": 3182, "idx": 2, "…": "… 共 10 个点，每点间隔 100ms …" },
    { "seq": 3190, "idx": 10, "raw": {"x": -3086, "y": 1904, "z": 1345},
      "accel_g": {"x": -0.753418, "y": 0.464844, "z": 0.328369}, "magnitude_g": 0.944216 }
  ],
  "raw":       { "x": -3086, "y": 1904, "z": 1345 },
  "accel_g":   { "x": -0.753418, "y": 0.464844, "z": 0.328369 },
  "accel_ms2": { "x": -7.388506, "y": 4.55856, "z": 3.220201 },
  "magnitude_g": 0.944216
}
```

成功响应 `201`（字段顺序是刻意的：开发板串口回显缓冲只有 512 字节，
所以 `ok/id/stored_count/sample_idx/seq/…/stored` 排在前面）：

```json
{
  "ok": true, "id": 10719, "stored_count": 10,
  "sample_idx": 10, "seq": 3190,
  "device_id": "esp32s3eye-1C701C",
  "received_at_utc": "2026-10-08T05:49:30.462Z",
  "stored": {
    "id": 10719, "device_id": "esp32s3eye-1C701C", "boot_count": 808,
    "seq": 3190, "sample_idx": 10, "sample_count": 10, "sample_period_ms": 100,
    "raw": {"x":-3086,"y":1904,"z":1345},
    "accel_g": {"x":-0.753418,"y":0.464844,"z":0.328369},
    "accel_ms2": {"…": "…"}, "magnitude_g": 0.944216, "unit": {"…": "…"}
  },
  "ids": [10710, "…", 10719],
  "upload_id": 2453,
  "raw_sha256": "…",
  "verify_ok": 1,
  "verify_note": "后端按 raw/lsb_per_g 重算结果与板端上报一致(误差<=0.001g)"
}
```

> `stored` 里的内容是**落库后从数据库回读**的，不是把请求原样回显 ——
> 这样开发板串口日志里就同时留下了「板端实测」与「服务器已存」两方数据，可直接对照。
> 一次请求合并了多个包时，`stored` 只回显**最后一个采样点**（其余点由 `ids` 给出），
> 三方核对脚本会按 `(seq, sample_idx)` 把回显对回正确的那个点。

错误码：

| 状态 | 含义 |
|---|---|
| 400 | JSON 非法 / 缺 `device_id` / **任何一个采样点缺 `raw`（后端绝不代为生成数据）** |
| 401 | `X-Device-Token` 不匹配 |
| 404 | 路径不存在 |

### GET `/api/v1/latest?device_id=xxx`
返回最新一条真实记录；**数据库为空时返回 404**（不会编造一条）。
记录里含 `sample_idx` / `sample_count` / `sample_period_ms`，网页用它显示「包内第 K/N 点」。

### GET `/api/v1/records?device_id=xxx&limit=100`
返回最近 N 条（按时间正序，便于画曲线），`limit` 上限 2000。

### GET `/api/v1/records?after_id=<id>&limit=400` —— **增量拉取（网页轮询用这个）**
返回 `id > after_id` 的记录，**按 id 升序**。10Hz 采样下网页必须用这个接口：
以前每次只取 `/latest` 一条，800ms 内新产生的 8 个点会被直接丢掉。
`after_id` 与 `limit` 同时给出时以增量为准；`limit` 上限 2000。

### GET `/api/v1/devices`
设备清单：设备 ID、累计条数、最后上报时间。

### GET `/api/v1/raw/<upload_id>`
返回该条记录的**原始上传报文**（逐字节原样）+ SHA256 + 来源 IP，用于证明后端没有改动数据。

### GET `/api/v1/stats`
汇总：记录数、原始报文数、设备数、首末接收时间。

### GET `/healthz`
健康检查。

### GET `/`
返回 `SENSOR_WEB_DIR` 下的网页（`index.html`、`app.js`、`style.css`）。

---

## 数据表

```sql
raw_uploads(id, received_at_utc, device_id, remote_addr, content_type, byte_len, sha256, body)
sensor_records(id, upload_id, device_id, boot_count, seq, sample_idx, sample_count,
               sample_period_ms, uptime_ms, ts_utc_ms, ts_local,
               received_at_utc, sensor_model, sensor_type, i2c_addr, chip_id, range_g,
               lsb_per_g, odr_hz, oversample, raw_x, raw_y, raw_z,
               gx, gy, gz, ms2x, ms2y, ms2z, magnitude_g, verify_ok, verify_note)
```

- `raw_uploads` = 原始报文，一字不改，用于追溯与防篡改。**一次上传一行**（哪怕这次带了 10 个采样点）。
- `sensor_records` = 解析结果，**一个采样点一行**；字段全部来自报文，缺字段则为 NULL，**不填充默认值**。
- `sample_idx` / `sample_count` / `sample_period_ms` = 包内第几个点 / 本包共几个点 / 采样间隔，
  由高密度采集版本新增；老库升级时会自动 `ALTER TABLE ADD COLUMN`（幂等，见 `server.py` 的 `MIGRATIONS`）。
- `verify_ok` / `verify_note` = 后端用 `raw / lsb_per_g` 重算后与板端上报值的自洽校验结论
  （只是校验，不替换板端原始上报值）。

---

## 自检

```bash
python tools/selftest_backend.py
```

会用**独立临时数据库**验证：空库返回 404、错误令牌被拒、缺 raw 被拒、正常落库、
原始报文留存、查询接口、统计接口等 11 项，跑完自动删除临时库
（不会污染生产/证据数据库）。