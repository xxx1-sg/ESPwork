#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py —— 传感器数据接收 / 持久化 / 查询服务（VPS 后端）

设计目标（对应任务硬性约束）
----------------------------------------------------------------------------
1. 只做「接收 + 原样落库 + 查询」，绝不在后端生成、补齐、平滑、模拟任何测量值。
   - 收到什么就存什么：原始上传报文（原始字节文本）一字不改地存进 raw_uploads 表，
     同时把报文里解析出的字段存进 sensor_records 表。
   - 报文里缺少必需字段（原始码值）→ 直接 400 拒收，而不是填默认值。
   - 所有 GET 接口都只从数据库读取，不参与任何计算性"补数据"。
2. 后端额外做一次「自洽校验」：用报文里声明的 lsb_per_g 把 raw 重新换算一遍，
   和板子算出来的 g 值比对，把结论（verify_ok / verify_note）一并存库。
   这只是校验，不替换板子的原始上报值，用于三方核对时快速定位问题。
3. 依赖：只用 Python 标准库（sqlite3 + http.server），VPS 上无需 pip 安装任何东西，
   一条命令即可跑起来，最大程度降低部署风险。

接口一览
----------------------------------------------------------------------------
  POST /api/v1/ingest          开发板上传数据（需要 X-Device-Token）
  GET  /api/v1/latest          取某设备最新一条真实记录（网页用）
  GET  /api/v1/records         取历史记录（默认最近 100 条）
  GET  /api/v1/devices         设备列表（含最后上报时间、累计条数）
  GET  /api/v1/raw/<id>        取某条记录的「原始上传报文」（证明未篡改）
  GET  /api/v1/stats           汇总统计
  GET  /healthz                健康检查
  GET  /                       返回 Web 前端页面（web/ 目录）
"""

import hashlib
import hmac
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ===========================================================================
# 配置（全部可用环境变量覆盖）
# ===========================================================================
HOST = os.environ.get("SENSOR_HOST", "0.0.0.0")
PORT = int(os.environ.get("SENSOR_PORT", "8000"))

# 数据库文件：默认放在脚本同级的 data/ 目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SENSOR_DB", os.path.join(BASE_DIR, "data", "sensor.db"))

# Web 前端目录（同仓库的 web/ 目录）
WEB_DIR = os.environ.get("SENSOR_WEB_DIR",
                         os.path.join(os.path.dirname(BASE_DIR), "web"))

# 设备令牌：与固件 app_config.h 里的 APP_DEVICE_TOKEN 保持一致
#   ★ 默认值必须与板子里实际烧录的固件一致，否则每次上传都会被 401 拒收
#     （排查记录见 firmware/src/app_config.h 第 85 行附近）。
DEVICE_TOKEN = os.environ.get("SENSOR_TOKEN", "esp32s3eye-demo-token-2026")

# 单条上传报文大小上限（真实报文约 700 字节，给足余量）
MAX_BODY = int(os.environ.get("SENSOR_MAX_BODY", str(16 * 1024)))

# 重力加速度常数（物理常数，用于自洽校验；不是测量值）
G0 = 9.80665

_db_lock = threading.Lock()


# ===========================================================================
# 数据库
# ===========================================================================
SCHEMA = """
PRAGMA journal_mode=WAL;

-- 表1: 原始上传报文，逐字节原样保存，用于证明后端没有篡改/伪造
CREATE TABLE IF NOT EXISTS raw_uploads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at_utc TEXT    NOT NULL,   -- 服务器收到的时间(UTC, ISO8601)
    device_id       TEXT,               -- 从报文解析出的设备 ID
    remote_addr     TEXT,               -- 上报方 IP:端口
    content_type    TEXT,               -- 请求头 Content-Type
    byte_len        INTEGER NOT NULL,   -- 原始字节数
    sha256          TEXT    NOT NULL,   -- 原始字节的 SHA256
    body            TEXT    NOT NULL    -- 原始报文正文（原样）
);

-- 表2: 解析后的传感记录（字段全部来自报文，缺失则为 NULL，不做任何填充）
--   ★ 一个数据包可能包含多个采样点（板端 100ms 采一个点、攒 10 点整包上报），
--     每个采样点在表里是**独立一行**：seq 相同、sample_idx 不同（1..N）。
CREATE TABLE IF NOT EXISTS sensor_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id       INTEGER NOT NULL REFERENCES raw_uploads(id),
    device_id       TEXT    NOT NULL,
    device_mac      TEXT,               -- 设备上报的芯片 MAC（真实 eFuse MAC；旧固件为 NULL）
    boot_count      INTEGER,
    seq             INTEGER,
    sample_idx      INTEGER,            -- 本采样点在该包内的序号(1..N)；旧固件为 NULL
    sample_count    INTEGER,            -- 该包里一共几个采样点(同一个包的所有行相同)
    sample_period_ms INTEGER,           -- 该包的采样间隔(ms)，来自报文声明
    uptime_ms       INTEGER,            -- 设备端单调运行时间(ms)
    ts_utc_ms       INTEGER,            -- 设备端已对时的 UTC 毫秒（未对时为 NULL）
    ts_local        TEXT,               -- 设备端本地时间字符串
    received_at_utc TEXT    NOT NULL,   -- 服务器接收时间
    sensor_model    TEXT,
    sensor_type     TEXT,
    i2c_addr        INTEGER,
    chip_id         INTEGER,
    range_g         INTEGER,
    lsb_per_g       INTEGER,
    odr_hz          INTEGER,
    oversample      INTEGER,
    raw_x           INTEGER NOT NULL,   -- 芯片原始码值（必需字段）
    raw_y           INTEGER NOT NULL,
    raw_z           INTEGER NOT NULL,
    gx              REAL, gy REAL, gz REAL,                 -- 板端换算的 g
    ms2x            REAL, ms2y REAL, ms2z REAL,             -- 板端换算的 m/s^2
    magnitude_g     REAL,                                   -- 板端合矢量
    verify_ok       INTEGER,            -- 后端自洽校验是否通过(1/0)
    verify_note     TEXT                -- 校验说明
);

CREATE INDEX IF NOT EXISTS idx_records_device_id ON sensor_records(device_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_records_received  ON sensor_records(received_at_utc);

-- ===========================================================================
-- 第 2 周新增表：远程采集指令闭环（字段级约定见 docs/06）
--   ★ 全部 CREATE TABLE IF NOT EXISTS，第 1 周的表一行不动、一个列不改名，
--     历史 1.7 万条记录不需要任何转换，verify_three_way.py 继续可用。
-- ===========================================================================

-- 表3: 采集请求（网页点一次「采集最新数据」= 一行）
CREATE TABLE IF NOT EXISTS collect_requests (
    request_id        TEXT PRIMARY KEY,          -- UUIDv4
    device_id         TEXT NOT NULL,
    sensor            TEXT NOT NULL,             -- 目标传感器，如 'qma6100p' / 'all'
    requested_by      TEXT,
    client_request_key TEXT,                     -- 幂等键（前端生成）
    status            TEXT NOT NULL,             -- submitted|acked|collecting|completed|failed|timeout|rejected|cancelled
    nonce             TEXT NOT NULL,             -- 32hex 随机数，随指令下发并在观测中回传（L1）
    timeout_ms        INTEGER NOT NULL,          -- 由后端决定，不接受前端指定
    submitted_at_utc  TEXT NOT NULL,
    dispatched_at_utc TEXT,                      -- 指令交给设备通道的时刻（L3 基准）
    acked_at_utc      TEXT,
    collecting_at_utc TEXT,
    completed_at_utc  TEXT,
    failed_at_utc     TEXT,
    deadline_utc      TEXT NOT NULL,             -- = dispatched_at_utc + timeout_ms
    attempt           INTEGER NOT NULL DEFAULT 0,-- 下发次数
    failure_reason    TEXT,
    error_code        TEXT,
    -- 下发时刻的基线快照：判新旧用（L2 基准）
    baseline_boot_count INTEGER,
    baseline_seq_last   INTEGER,
    baseline_uptime_ms  INTEGER,
    notes             TEXT
);
CREATE INDEX IF NOT EXISTS idx_creq_device_time ON collect_requests(device_id, submitted_at_utc DESC);
CREATE INDEX IF NOT EXISTS idx_creq_status      ON collect_requests(status, deadline_utc);

-- 表4: 设备回执（指令接收/开始执行/拒绝/被取代）
CREATE TABLE IF NOT EXISTS request_acks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    ack_type        TEXT NOT NULL,   -- received|started|rejected|superseded
    ack_at_utc      TEXT NOT NULL,   -- 服务端接收时刻（权威）
    device_uptime_ms INTEGER,
    boot_count      INTEGER,
    attempt         INTEGER NOT NULL DEFAULT 1,
    reason          TEXT,
    detail_json     TEXT,
    remote_addr     TEXT,
    UNIQUE(request_id, ack_type, attempt)
);

-- 表5: 绑定请求的新观测（L1∧L2∧L3 通过后才写入）
CREATE TABLE IF NOT EXISTS observations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id        TEXT NOT NULL,
    nonce             TEXT NOT NULL,
    device_id         TEXT NOT NULL,
    record_id         INTEGER,        -- → sensor_records.id
    upload_id         INTEGER,        -- → raw_uploads.id
    boot_count        INTEGER NOT NULL,
    seq_first         INTEGER NOT NULL,
    seq_last          INTEGER NOT NULL,
    sample_count      INTEGER NOT NULL,
    sensor            TEXT,
    ts_utc_ms         INTEGER,
    ts_local          TEXT,
    up_ms             INTEGER,
    value_json        TEXT NOT NULL,  -- 本次观测的结论值
    freshness         TEXT NOT NULL,  -- new_monotonic|new_after_reboot
    verify_ok         INTEGER,
    received_at_utc   TEXT NOT NULL,
    UNIQUE(request_id, boot_count, seq_first, seq_last)
);

-- 表6: 被拒收的观测（必须留痕，绝不允许静默丢弃）
CREATE TABLE IF NOT EXISTS rejected_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      TEXT,
    device_id       TEXT,
    reason_code     TEXT NOT NULL,  -- REQ_ID_MISMATCH|STALE_OBSERVATION|DUPLICATE_BATCH|REQUEST_ALREADY_FINAL|MISSING_FIELD
    reason_detail   TEXT,
    claimed_json    TEXT NOT NULL,  -- 设备声称的内容，原样保存（可追溯）
    received_at_utc TEXT NOT NULL,
    remote_addr     TEXT
);

-- 表7: 设备状态（timeout ≠ 硬件故障 的事实来源）
CREATE TABLE IF NOT EXISTS devices (
    device_id        TEXT PRIMARY KEY,
    last_seen_utc    TEXT,
    last_status      TEXT,      -- online|offline|sensor_error|paused
    last_boot_count  INTEGER,
    last_seq         INTEGER,
    last_uptime_ms   INTEGER,
    periodic_enabled INTEGER NOT NULL DEFAULT 1,
    paused_at_utc    TEXT,
    notes            TEXT
);

-- 表8: 设备事件流水（请求维度 vs 设备维度 分家的落点）
CREATE TABLE IF NOT EXISTS device_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       TEXT NOT NULL,
    event_type      TEXT NOT NULL, -- boot|pause|resume|sensor_error|sensor_recovered|command_timeout|power_off
    event_at_utc    TEXT NOT NULL,
    detail_json     TEXT,
    source          TEXT           -- device|server|operator
);

-- 待下发的指令队列（操作者侧产生，设备通过 commands/poll 取走）
--   ★ 这是「下行只有一条通道」的实现落点：没有任何"直接通知设备"的端点，
--     所有指令都必须先入队，再由设备主动轮询取走。因此"放进队列"≠"已送达"。
CREATE TABLE IF NOT EXISTS pending_commands (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id           TEXT NOT NULL,
    request_id          TEXT NOT NULL,   -- collect_now 时为采集请求 id
    type                TEXT NOT NULL,   -- collect_now|pause_reports|resume_reports
    nonce               TEXT,
    sensor              TEXT,
    timeout_ms          INTEGER,
    issued_at_utc       TEXT NOT NULL,
    baseline_boot_count INTEGER,
    baseline_seq_last   INTEGER,
    baseline_uptime_ms  INTEGER,
    attempt             INTEGER NOT NULL DEFAULT 1,
    dispatched_at_utc   TEXT,            -- 第一次被设备取走的时刻
    taken_at_utc        TEXT,            -- 最后一次被取走
    taken_count         INTEGER NOT NULL DEFAULT 0,
    UNIQUE(device_id, request_id, type)
);
CREATE INDEX IF NOT EXISTS idx_pcmd_device ON pending_commands(device_id, id);
"""

# 旧库升级用的补列（老库没有这些列；ALTER TABLE 是幂等的，缺了才加）
MIGRATIONS = [
    ("sensor_records", "sample_idx", "INTEGER"),
    ("sensor_records", "sample_count", "INTEGER"),
    ("sensor_records", "sample_period_ms", "INTEGER"),
    ("sensor_records", "device_mac", "TEXT"),
    # 第 2 周：便于直接在原始点表里按 request_id 追溯本次观测
    ("sensor_records", "request_id", "TEXT"),
    ("sensor_records", "report_mode", "TEXT"),
]

# ---- 第 2 周参数（全部可用环境变量覆盖） ----
# 采集请求超时：由后端决定，前端传 timeout_ms 一律忽略（防止前端把超时改大掩盖失败）
COLLECT_TIMEOUT_MS = int(os.environ.get("COLLECT_TIMEOUT_MS", "15000"))
# 设备在线判定窗口：近 N 秒内有任何请求即视为 online
DEVICE_ONLINE_WINDOW_S = int(os.environ.get("DEVICE_ONLINE_WINDOW_S", "15"))
# 超时扫描周期（秒）
TIMEOUT_SCAN_PERIOD_S = 1.0
# 请求状态机终态集合
FINAL_STATUSES = ("completed", "failed", "timeout", "rejected", "cancelled")


def db_connect():
    """打开数据库连接（每次请求一个连接，线程安全靠 SQLite 自身 + 写锁）。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def db_init():
    conn = db_connect()
    try:
        conn.executescript(SCHEMA)
        # 旧库平滑升级：缺列就补列（只加列，不动已有数据的任何一个值）
        for table, col, typ in MIGRATIONS:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
            if col not in cols:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
                print("[db] 已为 %s 补充字段 %s %s" % (table, col, typ))
        conn.commit()
    finally:
        conn.close()
    print("[db] 数据库就绪:", DB_PATH)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ===========================================================================
# 业务逻辑
# ===========================================================================
def ingest(payload: dict, raw_body: bytes, remote_addr: str, content_type: str):
    """把一条设备上报写入数据库。

    返回 (http_status, response_dict)。
    关键点：只存设备真实上报的内容；缺字段就拒收，绝不补默认值。
    """
    if not isinstance(payload, dict):
        return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "报文必须是 JSON 对象"}

    device_id = payload.get("device_id")
    if not device_id or not isinstance(device_id, str):
        return HTTPStatus.BAD_REQUEST, {"ok": False, "error": "缺少 device_id"}

    # 设备上报的芯片 MAC（真实 eFuse MAC，由固件 esp_read_mac 读出）。
    # 旧固件没有这个字段 —— 那就如实存 NULL，绝不根据 device_id 反推编造一个。
    device_mac = payload.get("mac")
    if device_mac is not None and not isinstance(device_mac, str):
        device_mac = None

    sensor = payload.get("sensor") or {}
    unit_declared = payload.get("unit") or {}

    def fnum(d, k):
        v = d.get(k)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    lsb_per_g = sensor.get("lsb_per_g")
    try:
        lsb_per_g = int(lsb_per_g) if lsb_per_g is not None else None
    except (TypeError, ValueError):
        lsb_per_g = None

    sample_period_ms = payload.get("sample_period_ms")
    try:
        sample_period_ms = int(sample_period_ms) if sample_period_ms is not None else None
    except (TypeError, ValueError):
        sample_period_ms = None

    # ---- 本次要入库的采样点列表 ----
    #   新固件: samples[] 是一整包的多个点（板端 100ms 采一个点、攒够整包上报一次）
    #   旧固件: 顶层 raw 就是单个点 —— 完全兼容，无需改动
    samples = payload.get("samples")
    if samples is None:
        pending = [payload]
    elif not isinstance(samples, list) or not samples:
        return HTTPStatus.BAD_REQUEST, {
            "ok": False, "error": "samples 必须是非空数组（后端不会代为生成数据）"
        }
    else:
        pending = samples

    # ---- 逐个采样点校验并组装入库行；任何一个点缺原始码值就整包拒收 ----
    rows = []
    for i, s in enumerate(pending):
        if not isinstance(s, dict):
            return HTTPStatus.BAD_REQUEST, {
                "ok": False, "error": "samples[%d] 必须是对象" % i}

        raw = s.get("raw")
        if not isinstance(raw, dict) or not all(k in raw for k in ("x", "y", "z")):
            # 缺少原始测量值 = 无法核实来源，直接拒收（不做任何默认值填充）
            return HTTPStatus.BAD_REQUEST, {
                "ok": False,
                "error": "第 %d 个采样点缺少原始测量值 raw.x/raw.y/raw.z，整包拒收"
                         "（后端不会代为生成数据）" % (i + 1)
            }
        try:
            raw_x, raw_y, raw_z = int(raw["x"]), int(raw["y"]), int(raw["z"])
        except (TypeError, ValueError):
            return HTTPStatus.BAD_REQUEST, {
                "ok": False, "error": "第 %d 个采样点的 raw 分量必须是整数码值" % (i + 1)}

        acc_g  = s.get("accel_g") or {}
        acc_si = s.get("accel_ms2") or {}

        # ---- 后端自洽校验：用声明的灵敏度重算，与板端上报值比对（每个点各算一次）----
        verify_ok, verify_note = None, "报文未提供 lsb_per_g，跳过换算校验"
        if lsb_per_g and lsb_per_g > 0:
            diffs = []
            for expect, got in ((raw_x / lsb_per_g, fnum(acc_g, "x")),
                                (raw_y / lsb_per_g, fnum(acc_g, "y")),
                                (raw_z / lsb_per_g, fnum(acc_g, "z"))):
                if got is not None:
                    diffs.append(abs(expect - got))
            if diffs and max(diffs) <= 0.001:
                verify_ok = 1
                verify_note = "后端按 raw/lsb_per_g 重算结果与板端上报一致(误差<=0.001g)"
            elif diffs:
                verify_ok = 0
                verify_note = "重算与板端上报存在差异(最大 %.4fg)，请检查换算参数" % max(diffs)
            else:
                verify_note = "报文未提供 accel_g，无法比对"

        in_batch = samples is not None
        rows.append({
            "sample_idx":  s.get("idx") if in_batch else None,
            "uptime_ms":   s.get("up_ms") if in_batch else payload.get("uptime_ms"),
            "ts_utc_ms":   s.get("ts_utc_ms") if in_batch else payload.get("ts_utc_ms"),
            "ts_local":    s.get("ts_local") if in_batch else payload.get("ts_local"),
            "raw_x": raw_x, "raw_y": raw_y, "raw_z": raw_z,
            "gx": fnum(acc_g, "x"), "gy": fnum(acc_g, "y"), "gz": fnum(acc_g, "z"),
            "ms2x": fnum(acc_si, "x"), "ms2y": fnum(acc_si, "y"), "ms2z": fnum(acc_si, "z"),
            "magnitude_g": fnum(s, "magnitude_g"),
            "verify_ok": verify_ok, "verify_note": verify_note,
        })

    body_text = raw_body.decode("utf-8", errors="replace")
    sha = hashlib.sha256(raw_body).hexdigest()
    received = utc_now_iso()

    with _db_lock:
        conn = db_connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO raw_uploads
                   (received_at_utc, device_id, remote_addr, content_type, byte_len, sha256, body)
                   VALUES (?,?,?,?,?,?,?)""",
                (received, device_id, remote_addr, content_type,
                 len(raw_body), sha, body_text))
            upload_id = cur.lastrowid

            # 一个数据包里的每个采样点各存一行（seq 相同、sample_idx 不同）
            ids = []
            for r in rows:
                cur.execute(
                    """INSERT INTO sensor_records
                       (upload_id, device_id, device_mac, boot_count, seq, sample_idx, sample_count,
                        sample_period_ms, uptime_ms, ts_utc_ms, ts_local,
                        received_at_utc, sensor_model, sensor_type, i2c_addr, chip_id, range_g,
                        lsb_per_g, odr_hz, oversample, raw_x, raw_y, raw_z,
                        gx, gy, gz, ms2x, ms2y, ms2z, magnitude_g, verify_ok, verify_note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (upload_id, device_id, device_mac,
                     payload.get("boot_count"), payload.get("seq"),
                     r["sample_idx"], len(rows),
                     sample_period_ms,
                     r["uptime_ms"], r["ts_utc_ms"], r["ts_local"], received,
                     sensor.get("model"), sensor.get("type"), sensor.get("i2c_addr"),
                     sensor.get("chip_id"), sensor.get("range_g"), lsb_per_g,
                     sensor.get("odr_hz"), sensor.get("oversample"),
                     r["raw_x"], r["raw_y"], r["raw_z"],
                     r["gx"], r["gy"], r["gz"],
                     r["ms2x"], r["ms2y"], r["ms2z"],
                     r["magnitude_g"], r["verify_ok"], r["verify_note"]))
                ids.append(cur.lastrowid)
            conn.commit()

            # 回读刚落库的"最后一个采样点"作为响应内容（响应 = 数据库里存的东西，便于三方核对）
            row = conn.execute(
                "SELECT * FROM sensor_records WHERE id=?", (ids[-1],)).fetchone()
        finally:
            conn.close()

    # 响应字段顺序有意安排：最关键的字段放最前面 —— 板端回显缓冲区只有 512 字节，
    # 超出的部分会被截断（USB-CDC 缓冲有限），所以 id/stored_count/stored 必须在前面。
    resp = {
        "ok": True,
        "id": row["id"],
        "stored_count": len(ids),
        "sample_idx": row["sample_idx"],
        "seq": row["seq"],
        "device_id": row["device_id"],
        "received_at_utc": row["received_at_utc"],
        "stored": {
            "raw": {"x": row["raw_x"], "y": row["raw_y"], "z": row["raw_z"]},
            "accel_g": {"x": row["gx"], "y": row["gy"], "z": row["gz"]},
            "accel_ms2": {"x": row["ms2x"], "y": row["ms2y"], "z": row["ms2z"]},
            "magnitude_g": row["magnitude_g"],
            "unit": unit_declared,
        },
        "ids": ids,
        "upload_id": upload_id,
        "raw_sha256": sha,
        "verify_ok": row["verify_ok"],
        "verify_note": row["verify_note"],
    }
    return HTTPStatus.CREATED, resp


def record_to_dict(row: sqlite3.Row) -> dict:
    """把数据库行转成前端友好的 JSON（只做字段映射，不改数值）。"""
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        # 芯片 MAC：新固件上报的真实值；旧固件没上报就是 NULL（页面显示 "—"，不猜）
        "mac": row["device_mac"] if "device_mac" in row.keys() else None,
        "boot_count": row["boot_count"],
        "seq": row["seq"],
        "sample_idx": row["sample_idx"],
        "sample_count": row["sample_count"],
        "sample_period_ms": row["sample_period_ms"],
        "uptime_ms": row["uptime_ms"],
        "ts_utc_ms": row["ts_utc_ms"],
        "ts_local": row["ts_local"],
        "received_at_utc": row["received_at_utc"],
        "sensor": {
            "model": row["sensor_model"],
            "type": row["sensor_type"],
            "i2c_addr": row["i2c_addr"],
            "chip_id": row["chip_id"],
            "range_g": row["range_g"],
            "lsb_per_g": row["lsb_per_g"],
            "odr_hz": row["odr_hz"],
            "oversample": row["oversample"],
        },
        "unit": {"raw": "LSB(14bit signed)", "accel_g": "g", "accel_ms2": "m/s^2"},
        "raw": {"x": row["raw_x"], "y": row["raw_y"], "z": row["raw_z"]},
        "accel_g": {"x": row["gx"], "y": row["gy"], "z": row["gz"]},
        "accel_ms2": {"x": row["ms2x"], "y": row["ms2y"], "z": row["ms2z"]},
        "magnitude_g": row["magnitude_g"],
        "verify_ok": row["verify_ok"],
        "verify_note": row["verify_note"],
    }


def query_latest(device_id=None):
    conn = db_connect()
    try:
        if device_id:
            row = conn.execute(
                "SELECT * FROM sensor_records WHERE device_id=? ORDER BY id DESC LIMIT 1",
                (device_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM sensor_records ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            # 数据库里一条真实数据都没有时，如实返回 404，而不是编造一条
            return HTTPStatus.NOT_FOUND, {
                "ok": False, "error": "数据库中还没有任何真实记录（设备尚未上传）"
            }
        return HTTPStatus.OK, {"ok": True, "record": record_to_dict(row)}
    finally:
        conn.close()


def query_records(device_id=None, limit=100, after_id=None):
    """查询记录。

    after_id 非空时改为"增量查询"：只返回 id 大于该值的记录（按 id 升序）。
    板端现在是 10Hz 采样、1Hz 整包上传，网页每 800ms 拉一次最新一条会丢样本，
    所以前端改用 after_id 增量拉取，一条都不漏。
    """
    limit = max(1, min(int(limit), 2000))
    conn = db_connect()
    try:
        if after_id is not None:
            after_id = int(after_id)
            sql = "SELECT * FROM sensor_records WHERE id > ?"
            args = [after_id]
            if device_id:
                sql += " AND device_id = ?"
                args.append(device_id)
            sql += " ORDER BY id ASC LIMIT ?"
            args.append(limit)
            rows = conn.execute(sql, args).fetchall()
            return HTTPStatus.OK, {
                "ok": True, "count": len(rows), "after_id": after_id,
                "records": [record_to_dict(r) for r in rows],
            }

        if device_id:
            rows = conn.execute(
                "SELECT * FROM sensor_records WHERE device_id=? ORDER BY id DESC LIMIT ?",
                (device_id, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sensor_records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        # 返回时按时间正序，方便画曲线
        return HTTPStatus.OK, {
            "ok": True, "count": len(rows),
            "records": [record_to_dict(r) for r in reversed(rows)],
        }
    finally:
        conn.close()


def query_devices():
    """设备列表。

    ★ 第 1 周字段（n/last_seen/model/last_seq/mac）**原样保留**，
      第 2 周只**新增** last_status / periodic_enabled / last_seen_utc / pending_commands，
      因此老页面与老脚本不受影响。
    """
    conn = db_connect()
    try:
        rows = conn.execute(
            """SELECT device_id, COUNT(*) AS n, MAX(received_at_utc) AS last_seen,
                      MAX(sensor_model) AS model, MAX(seq) AS last_seq,
                      MAX(device_mac) AS mac
               FROM sensor_records GROUP BY device_id ORDER BY last_seen DESC""").fetchall()

        # 设备维度事实（远程采集链路引入）：即使某设备还没上传过任何周期包，
        # 只要它轮询过指令，也应该出现在列表里（否则页面根本选不到它去采集）。
        dev_rows = conn.execute("SELECT * FROM devices").fetchall()
        dev_map = {r["device_id"]: r for r in dev_rows}

        out = []
        seen = set()
        for r in rows:
            d = dict(r)
            did = d["device_id"]
            seen.add(did)
            out.append(_device_extra(conn, d, dev_map.get(did), did))
        for did, drow in dev_map.items():
            if did in seen:
                continue
            out.append(_device_extra(conn, {
                "device_id": did, "n": 0, "last_seen": drow["last_seen_utc"],
                "model": None, "last_seq": drow["last_seq"], "mac": None,
            }, drow, did))
        return HTTPStatus.OK, {"ok": True, "devices": out}
    finally:
        conn.close()


def _device_extra(conn, base, drow, device_id):
    """在设备行上补第 2 周字段（缺失一律 null/默认，不编造）。"""
    pend = conn.execute(
        "SELECT COUNT(*) c FROM pending_commands WHERE device_id=?",
        (device_id,)).fetchone()["c"]
    extra = {
        "last_status": None,
        "last_seen_utc": base.get("last_seen"),
        "periodic_enabled": 1,
        "pending_commands": pend,
        "last_boot_count": None,
        "last_uptime_ms": None,
    }
    if drow is not None:
        eff = device_effective_status(conn, device_id)
        extra.update({
            "last_status": eff["last_status"],
            "last_seen_utc": drow["last_seen_utc"] or base.get("last_seen"),
            "periodic_enabled": drow["periodic_enabled"],
            "last_boot_count": drow["last_boot_count"],
            "last_uptime_ms": drow["last_uptime_ms"],
            "last_seq": drow["last_seq"] if drow["last_seq"] is not None else base.get("last_seq"),
        })
    base.update(extra)
    return base


def query_raw(upload_id):
    conn = db_connect()
    try:
        row = conn.execute("SELECT * FROM raw_uploads WHERE id=?", (upload_id,)).fetchone()
        if not row:
            return HTTPStatus.NOT_FOUND, {"ok": False, "error": "没有这条原始报文"}
        return HTTPStatus.OK, {
            "ok": True,
            "upload_id": row["id"],
            "received_at_utc": row["received_at_utc"],
            "remote_addr": row["remote_addr"],
            "byte_len": row["byte_len"],
            "sha256": row["sha256"],
            "body": row["body"],     # 原始报文，逐字节原样
        }
    finally:
        conn.close()


def query_stats():
    conn = db_connect()
    try:
        rec = conn.execute("SELECT COUNT(*) c FROM sensor_records").fetchone()["c"]
        upl = conn.execute("SELECT COUNT(*) c FROM raw_uploads").fetchone()["c"]
        dev = conn.execute("SELECT COUNT(DISTINCT device_id) c FROM sensor_records").fetchone()["c"]
        first = conn.execute("SELECT MIN(received_at_utc) m FROM sensor_records").fetchone()["m"]
        last = conn.execute("SELECT MAX(received_at_utc) m FROM sensor_records").fetchone()["m"]
        # 第 2 周新增统计（docs/06 §6.6）—— 只增字段，不改第 1 周已有键
        req_total = conn.execute("SELECT COUNT(*) c FROM collect_requests").fetchone()["c"]
        req_done = conn.execute(
            "SELECT COUNT(*) c FROM collect_requests WHERE status='completed'").fetchone()["c"]
        req_to = conn.execute(
            "SELECT COUNT(*) c FROM collect_requests WHERE status='timeout'").fetchone()["c"]
        req_failed = conn.execute(
            "SELECT COUNT(*) c FROM collect_requests WHERE status='failed'").fetchone()["c"]
        rej_total = conn.execute("SELECT COUNT(*) c FROM rejected_observations").fetchone()["c"]
        return HTTPStatus.OK, {
            "ok": True, "records": rec, "raw_uploads": upl, "devices": dev,
            "first_received_at_utc": first, "last_received_at_utc": last,
            "requests_total": req_total,
            "requests_completed": req_done,
            "requests_timeout": req_to,
            "requests_failed": req_failed,
            "rejected_observations_total": rej_total,
            "server_time_utc": utc_now_iso(),
        }
    finally:
        conn.close()


# ===========================================================================
# 第 2 周业务逻辑：远程采集指令闭环
# ===========================================================================
def _iso_to_dt(s):
    """把 UTC ISO8601（…Z，带毫秒）解析成 datetime；解析失败返回 None。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def iso_add_ms(iso, ms):
    """在 UTC ISO8601 时刻上加 ms 毫秒，返回同格式字符串。"""
    dt = _iso_to_dt(iso)
    if dt is None:
        return None
    dt = dt + timedelta(milliseconds=ms)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def iso_diff_ms(a, b):
    """a - b，单位毫秒（任一不可解析返回 None）。"""
    da, db = _iso_to_dt(a), _iso_to_dt(b)
    if da is None or db is None:
        return None
    return int((da - db).total_seconds() * 1000)


def _error(code, message, retryable=False, **extra):
    """统一失败包裹（docs/06 §2.3）。"""
    err = {"code": code, "message": message, "retryable": bool(retryable)}
    err.update(extra)
    return {"ok": False, "server_time_utc": utc_now_iso(), "error": err}


def _ok(data):
    return {"ok": True, "server_time_utc": utc_now_iso(), "data": data}


def device_touch(conn, device_id, boot_count=None, seq=None, uptime_ms=None,
                 periodic=None, status=None):
    """更新设备维度的"最后出现"事实。

    ★ 这里只写设备维度（last_seen / last_boot_count / last_seq），
      绝不因为一次请求超时就把设备标成故障 —— 那是 inv.2 的落点。

    ★ 关于 periodic：**只有** pause/resume 回执才允许改 devices.periodic_enabled。
      设备在 poll 里自报的 periodic_enabled 只作参考，不能拿它覆盖服务端的暂停决定 ——
      否则"我把设备暂停了，可它下一次轮询就自报 true，暂停立刻被它自己解除"，
      页面上的暂停状态会来回翻转（实测踩过这个坑）。
    """
    now = utc_now_iso()
    row = conn.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO devices
               (device_id, last_seen_utc, last_status, last_boot_count, last_seq,
                last_uptime_ms, periodic_enabled)
               VALUES (?,?,?,?,?,?,?)""",
            (device_id, now, status or "online", boot_count, seq, uptime_ms,
             1 if periodic is None else (1 if periodic else 0)))
        return
    sets, args = ["last_seen_utc=?"], [now]
    if boot_count is not None:
        sets.append("last_boot_count=?"); args.append(boot_count)
    if seq is not None:
        sets.append("last_seq=?"); args.append(seq)
    if uptime_ms is not None:
        sets.append("last_uptime_ms=?"); args.append(uptime_ms)
    if periodic is not None:
        sets.append("periodic_enabled=?"); args.append(1 if periodic else 0)
    if status:
        sets.append("last_status=?"); args.append(status)
    args.append(device_id)
    conn.execute("UPDATE devices SET %s WHERE device_id=?" % ",".join(sets), args)


def device_effective_status(conn, device_id):
    """设备维度的现状（供页面区分"在线但没回报"和"离线"）。

    ★ 只依据"最近一次出现在服务端的时间"，不掺入任何请求的超时结论。
    """
    row = conn.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
    if row is None:
        return {"last_status": None, "last_seen_utc": None,
                "periodic_enabled": 1, "online": False}
    last_seen = row["last_seen_utc"]
    dt = _iso_to_dt(last_seen)
    online = False
    if dt is not None:
        online = (datetime.now(timezone.utc) - dt).total_seconds() <= DEVICE_ONLINE_WINDOW_S
    # sensor_error / paused 是设备自报的事实，优先如实展示；
    # online/offline 则按"最近是否出现"实时推导（不写库，避免伪造设备事实）。
    st = row["last_status"]
    if st not in ("sensor_error", "paused"):
        st = "online" if online else "offline"
    return {"last_status": st, "last_seen_utc": last_seen,
            "periodic_enabled": row["periodic_enabled"], "online": online}


def add_device_event(conn, device_id, event_type, source, detail=None):
    conn.execute(
        """INSERT INTO device_events (device_id, event_type, event_at_utc, detail_json, source)
           VALUES (?,?,?,?,?)""",
        (device_id, event_type, utc_now_iso(),
         json.dumps(detail, ensure_ascii=False) if detail else None, source))


def reject_observation(conn, request_id, device_id, code, detail, claimed, remote_addr):
    """拒收留痕（绝不允许静默丢弃）—— 返回 rejected_observations.id。"""
    cur = conn.execute(
        """INSERT INTO rejected_observations
           (request_id, device_id, reason_code, reason_detail, claimed_json,
            received_at_utc, remote_addr)
           VALUES (?,?,?,?,?,?,?)""",
        (request_id, device_id, code, detail,
         json.dumps(claimed, ensure_ascii=False), utc_now_iso(), remote_addr))
    return cur.lastrowid


# ---------------------------------------------------------------- 操作者侧 ----
def create_collect_request(payload):
    """创建一次「采集最新数据」任务（docs/06 §6.1）。

    ★ 与「刷新已存数据」的本质区别：这里会往 pending_commands 里排一条
      collect_now 指令，设备下一次轮询就会取走 —— 也就是**必然产生设备侧流量**。
      而刷新只读 GET /records、/latest，零设备侧流量。
    """
    if not isinstance(payload, dict):
        return HTTPStatus.BAD_REQUEST, _error("BAD_JSON", "报文必须是 JSON 对象")

    device_id = payload.get("device_id")
    if not device_id or not isinstance(device_id, str):
        # 缺必填字段=开发缺陷，直接 400；响应体绝不带 data.request_id
        return HTTPStatus.BAD_REQUEST, _error("MISSING_FIELD", "缺少必填字段 device_id")

    sensor = payload.get("sensor") or "qma6100p"
    requested_by = payload.get("requested_by")
    client_key = payload.get("client_request_key")
    note = payload.get("note")
    # ★ 前端传的 timeout_ms 一律忽略：防止前端把超时改大来"掩盖"失败
    timeout_ms = COLLECT_TIMEOUT_MS

    with _db_lock:
        conn = db_connect()
        try:
            dev = conn.execute("SELECT * FROM devices WHERE device_id=?",
                               (device_id,)).fetchone()
            if dev is None:
                return HTTPStatus.NOT_FOUND, _error(
                    "NO_DEVICE", "设备未接入：devices 表中没有 %s" % device_id)

            # ---- 幂等：同一 client_request_key 重复点击返回同一条（双击场景）----
            if client_key:
                dup = conn.execute(
                    "SELECT * FROM collect_requests WHERE client_request_key=?",
                    (client_key,)).fetchone()
                if dup is not None:
                    data = _request_summary(conn, dup)
                    data["deduplicated"] = True
                    return HTTPStatus.OK, _ok(data)

            # ---- 基线快照：判新旧（L2）就靠它 ----
            #   设备从未轮询过 → 没有基线，无法判定新旧，如实拒绝而不是瞎猜
            if dev["last_boot_count"] is None or dev["last_seq"] is None:
                return HTTPStatus.CONFLICT, _error(
                    "NO_BASELINE",
                    "设备尚未轮询过，缺少基线快照；请等待设备首次轮询后再采集")

            base_boot = dev["last_boot_count"]
            base_seq = dev["last_seq"]
            base_up = dev["last_uptime_ms"]

            request_id = str(uuid.uuid4())
            nonce = uuid.uuid4().hex          # 32hex
            submitted = utc_now_iso()
            baseline = {"boot_count": base_boot, "seq_last": base_seq, "uptime_ms": base_up}
            # ★ deadline 在创建时按提交时刻先算一份；设备真正取走指令时（poll_commands）
            #   会改用 dispatched_at_utc 重算 —— 因为"15 秒没回报"必须从**下发之后**起算，
            #   否则设备离线期间排队的请求会在还没下发时就先超时。
            deadline = iso_add_ms(submitted, timeout_ms)

            conn.execute(
                """INSERT INTO collect_requests
                   (request_id, device_id, sensor, requested_by, client_request_key,
                    status, nonce, timeout_ms, submitted_at_utc, deadline_utc,
                    baseline_boot_count, baseline_seq_last, baseline_uptime_ms, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (request_id, device_id, sensor, requested_by, client_key,
                 "submitted", nonce, timeout_ms, submitted, deadline,
                 base_boot, base_seq, base_up, note))

            conn.execute(
                """INSERT INTO pending_commands
                   (device_id, request_id, type, nonce, sensor, timeout_ms, issued_at_utc,
                    baseline_boot_count, baseline_seq_last, baseline_uptime_ms, attempt)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (device_id, request_id, "collect_now", nonce, sensor, timeout_ms, submitted,
                 base_boot, base_seq, base_up, 1))

            add_device_event(conn, device_id, "collect_requested", "operator",
                             {"request_id": request_id, "sensor": sensor})
            conn.commit()

            return HTTPStatus.CREATED, _ok({
                "request_id": request_id,
                "status": "submitted",
                "submitted_at_utc": submitted,
                "timeout_ms": timeout_ms,
                "deadline_utc": deadline,
                "baseline": baseline,
                "poll_url": "/api/v1/collect_requests/" + request_id,
                "deduplicated": False,
            })
        finally:
            conn.close()


def _request_summary(conn, row):
    """创建/幂等响应用的请求摘要（与 docs/06 §6.1 的键一致）。"""
    deadline = row["deadline_utc"]
    if row["dispatched_at_utc"]:
        deadline = iso_add_ms(row["dispatched_at_utc"], row["timeout_ms"]) or deadline
    return {
        "request_id": row["request_id"],
        "status": row["status"],
        "submitted_at_utc": row["submitted_at_utc"],
        "timeout_ms": row["timeout_ms"],
        "deadline_utc": deadline,
        "baseline": {
            "boot_count": row["baseline_boot_count"],
            "seq_last": row["baseline_seq_last"],
            "uptime_ms": row["baseline_uptime_ms"],
        },
        "poll_url": "/api/v1/collect_requests/" + row["request_id"],
        "deduplicated": False,
    }


def get_collect_request(request_id):
    """请求详情（docs/06 §6.2）—— 页面 1s 轮询它来渲染状态徽标。"""
    with _db_lock:
        conn = db_connect()
        try:
            row = conn.execute("SELECT * FROM collect_requests WHERE request_id=?",
                               (request_id,)).fetchone()
            if row is None:
                return HTTPStatus.NOT_FOUND, _error(
                    "REQUEST_NOT_FOUND", "request_id 不存在或已被清理")

            ack = conn.execute(
                """SELECT * FROM request_acks WHERE request_id=?
                   ORDER BY id ASC LIMIT 1""", (request_id,)).fetchone()
            obs = conn.execute(
                "SELECT * FROM observations WHERE request_id=? ORDER BY id DESC LIMIT 1",
                (request_id,)).fetchone()
            rejs = conn.execute(
                """SELECT id, reason_code, reason_detail, received_at_utc
                   FROM rejected_observations WHERE request_id=? ORDER BY id ASC""",
                (request_id,)).fetchall()
            dev = device_effective_status(conn, row["device_id"])

            checks = None
            observation = None
            if obs is not None:
                try:
                    val = json.loads(obs["value_json"])
                except (TypeError, ValueError):
                    val = {}
                checks = {"l1_identity": True,
                          "l2_monotonic": True,
                          "l3_time": True}
                observation = {
                    "observation_id": obs["id"],
                    "record_id": obs["record_id"],
                    "upload_id": obs["upload_id"],
                    "boot_count": obs["boot_count"],
                    "seq_first": obs["seq_first"],
                    "seq_last": obs["seq_last"],
                    "sample_count": obs["sample_count"],
                    "up_ms": obs["up_ms"],
                    "ts_local": obs["ts_local"],
                    "magnitude_g": val.get("magnitude_g"),
                    "raw": val.get("raw"),
                    "accel_g": val.get("accel_g"),
                    "accel_ms2": val.get("accel_ms2"),
                    "freshness": obs["freshness"],
                    "verify_ok": obs["verify_ok"],
                }

            deadline = row["deadline_utc"]
            if row["dispatched_at_utc"]:
                deadline = iso_add_ms(row["dispatched_at_utc"], row["timeout_ms"]) or deadline

            open_states = ("submitted", "acked", "collecting")
            data = {
                "request_id": row["request_id"],
                "device_id": row["device_id"],
                "sensor": row["sensor"],
                "status": row["status"],
                "submitted_at_utc": row["submitted_at_utc"],
                "dispatched_at_utc": row["dispatched_at_utc"],
                "acked_at_utc": row["acked_at_utc"],
                "completed_at_utc": row["completed_at_utc"],
                "deadline_utc": deadline,
                "timeout_ms": row["timeout_ms"],
                "ack": ({"ack_type": ack["ack_type"], "ack_at_utc": ack["ack_at_utc"],
                         "attempt": ack["attempt"]} if ack else None),
                "observation": observation,
                "baseline": {
                    "boot_count": row["baseline_boot_count"],
                    "seq_last": row["baseline_seq_last"],
                    "uptime_ms": row["baseline_uptime_ms"],
                },
                "checks": checks,
                "rejections": [{"rejected_id": r["id"],
                                "reason_code": r["reason_code"],
                                "reason_detail": r["reason_detail"],
                                "received_at_utc": r["received_at_utc"]} for r in rejs],
                "elapsed_ms": {
                    "submit_to_ack": iso_diff_ms(row["acked_at_utc"], row["submitted_at_utc"]),
                    "submit_to_complete": iso_diff_ms(row["completed_at_utc"],
                                                      row["submitted_at_utc"]),
                },
                "device": dev,
                "failure_reason": row["failure_reason"],
                "actions": ["cancel"] if row["status"] in open_states else [],
            }
            return HTTPStatus.OK, _ok(data)
        finally:
            conn.close()


def list_collect_requests(device_id=None, status=None, limit=50):
    """动作执行追踪（docs/06 §6.3）——用来复盘"连点两次产生了两条记录"。"""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))

    sql = "SELECT * FROM collect_requests"
    where, args = [], []
    if device_id:
        where.append("device_id=?"); args.append(device_id)
    if status:
        where.append("status=?"); args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY submitted_at_utc DESC, rowid DESC LIMIT ?"
    args.append(limit)

    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute(sql, args).fetchall()
            out = []
            for r in rows:
                n_rej = conn.execute(
                    "SELECT COUNT(*) c FROM rejected_observations WHERE request_id=?",
                    (r["request_id"],)).fetchone()["c"]
                obs = conn.execute(
                    "SELECT * FROM observations WHERE request_id=? ORDER BY id DESC LIMIT 1",
                    (r["request_id"],)).fetchone()
                try:
                    val = json.loads(obs["value_json"]) if obs else {}
                except (TypeError, ValueError):
                    val = {}
                out.append({
                    "request_id": r["request_id"],
                    "device_id": r["device_id"],
                    "status": r["status"],
                    "submitted_at_utc": r["submitted_at_utc"],
                    "dispatched_at_utc": r["dispatched_at_utc"],
                    "acked_at_utc": r["acked_at_utc"],
                    "completed_at_utc": r["completed_at_utc"],
                    "deadline_utc": r["deadline_utc"],
                    "failure_reason": r["failure_reason"],
                    "rejections_count": n_rej,
                    "observation": ({
                        "observation_id": obs["id"],
                        "seq_first": obs["seq_first"],
                        "seq_last": obs["seq_last"],
                        "magnitude_g": val.get("magnitude_g"),
                        "freshness": obs["freshness"],
                    } if obs else None),
                })
            return HTTPStatus.OK, _ok({"count": len(out), "requests": out})
        finally:
            conn.close()


def cancel_collect_request(request_id, payload):
    """操作者撤销未完成的请求（docs/06 §6.4）。"""
    payload = payload or {}
    with _db_lock:
        conn = db_connect()
        try:
            row = conn.execute("SELECT * FROM collect_requests WHERE request_id=?",
                               (request_id,)).fetchone()
            if row is None:
                return HTTPStatus.NOT_FOUND, _error("REQUEST_NOT_FOUND", "request_id 不存在")
            if row["status"] in FINAL_STATUSES:
                # 终态后不得改写；如实拒绝
                return HTTPStatus.CONFLICT, _error(
                    "REQUEST_ALREADY_FINAL",
                    "本次请求已结束（%s），不能再撤销" % row["status"])
            now = utc_now_iso()
            conn.execute(
                """UPDATE collect_requests
                   SET status='cancelled', failure_reason='CLIENT_CANCELLED'
                   WHERE request_id=?""", (request_id,))
            # 指令作废：设备即使取到也不该再执行
            conn.execute("DELETE FROM pending_commands WHERE request_id=? AND type='collect_now'",
                         (request_id,))
            add_device_event(conn, row["device_id"], "collect_cancelled", "operator",
                             {"request_id": request_id, "reason": payload.get("reason")})
            conn.commit()
            return HTTPStatus.OK, _ok({
                "request_id": request_id, "status": "cancelled",
                "cancelled_at_utc": now,
                "failure_reason": "CLIENT_CANCELLED",
                "actions": [],
            })
        finally:
            conn.close()


def set_device_pause(payload):
    """暂停/恢复周期上报（docs/06 §6.5，当堂验证用）。

    ★ 注意语义：暂停的是**周期上报**，不是采集能力 —— 暂停后 collect_now 仍必须能完成。
    """
    payload = payload or {}
    device_id = payload.get("device_id")
    if not device_id:
        return HTTPStatus.BAD_REQUEST, _error("MISSING_FIELD", "缺少必填字段 device_id")
    enable = payload.get("enable")
    if enable is None:
        return HTTPStatus.BAD_REQUEST, _error("MISSING_FIELD", "缺少必填字段 enable")
    enable = bool(enable)
    reason = payload.get("reason")

    with _db_lock:
        conn = db_connect()
        try:
            dev = conn.execute("SELECT * FROM devices WHERE device_id=?",
                               (device_id,)).fetchone()
            if dev is None:
                return HTTPStatus.NOT_FOUND, _error("NO_DEVICE", "设备未接入")
            request_id = "pause-%s-%d" % (device_id, int(time.time() * 1000))
            cmd_type = "resume_reports" if enable else "pause_reports"
            conn.execute(
                """INSERT OR REPLACE INTO pending_commands
                   (device_id, request_id, type, nonce, sensor, timeout_ms, issued_at_utc,
                    attempt)
                   VALUES (?,?,?,?,?,?,?,1)""",
                (device_id, request_id, cmd_type, None, None, COLLECT_TIMEOUT_MS,
                 utc_now_iso()))
            conn.execute(
                "UPDATE devices SET periodic_enabled=?, paused_at_utc=? WHERE device_id=?",
                (1 if enable else 0, None if enable else utc_now_iso(), device_id))
            add_device_event(conn, device_id, "resume" if enable else "pause", "operator",
                             {"reason": reason})
            conn.commit()
            return HTTPStatus.OK, _ok({
                "device_id": device_id,
                "periodic_enabled": 1 if enable else 0,
                "command_id": cmd_type,
                "status": "pending_device",
                "note": "已排入设备指令队列，等待设备下一次组合轮询",
            })
        finally:
            conn.close()


# ---------------------------------------------------------------- 设备侧 ----
def poll_commands(payload, remote_addr):
    """设备拉取待执行指令（docs/06 §4.1）—— 下行的唯一通道。

    设备轮询时顺带把 boot_count/seq_last/up_ms 带上来，后端因此天然持有
    每台设备的当前进度，可以作为**下一条**指令的基线快照。
    """
    payload = payload or {}
    device_id = payload.get("device_id")
    if not device_id:
        return HTTPStatus.BAD_REQUEST, _error("MISSING_FIELD", "缺少必填字段 device_id")

    boot_count = payload.get("boot_count")
    seq_last = payload.get("seq_last")
    up_ms = payload.get("up_ms")
    periodic = payload.get("periodic_enabled")
    health = payload.get("health") or {}

    with _db_lock:
        conn = db_connect()
        try:
            before = conn.execute("SELECT * FROM devices WHERE device_id=?",
                                  (device_id,)).fetchone()
            # 设备自报传感器故障 → 这是**设备维度**的事实（与请求超时彻底分家）
            status = None
            if health.get("sensor_ok") is False:
                status = "sensor_error"
                prev_ok = (before is None) or (before["last_status"] != "sensor_error")
                if prev_ok:
                    add_device_event(conn, device_id, "sensor_error", "device", health)
            elif before is not None and before["last_status"] == "sensor_error":
                status = "online"
                add_device_event(conn, device_id, "sensor_recovered", "device", health)

            # ★ 注意这里**不传** periodic：设备自报的 periodic_enabled 只记录进
            #   device_events 供核对，绝不覆盖服务端的暂停决定（否则暂停会被设备
            #   下一次轮询自解除）。只有 pause_reports/resume_reports 回执才改它。
            device_touch(conn, device_id, boot_count=boot_count, seq=seq_last,
                         uptime_ms=up_ms, status=status)
            if periodic is not None and before is not None and \
                    (1 if periodic else 0) != before["periodic_enabled"]:
                add_device_event(conn, device_id, "resume" if periodic else "pause",
                                 "device",
                                 {"note": "设备自报 periodic_enabled=%s，服务端决定=%s"
                                  % (bool(periodic), before["periodic_enabled"])})

            # 设备重启（boot_count 变了）→ 记一条 boot 事件，便于解释 freshness
            if boot_count is not None and (before is None or
                                           before["last_boot_count"] != boot_count):
                add_device_event(conn, device_id, "boot", "device",
                                 {"boot_count": boot_count})
            conn.commit()

            # ---- 取出该设备所有待下发指令（可能同时有 collect_now 和 pause_reports）----
            rows = conn.execute(
                """SELECT * FROM pending_commands WHERE device_id=?
                   ORDER BY id ASC LIMIT 8""", (device_id,)).fetchall()

            commands = []
            for r in rows:
                now = utc_now_iso()
                # ★ 第一次被取走的时刻就是 dispatched_at_utc —— 这才是 L3 的基准，
                #   也是 deadline 的起算点（"下发后 15 秒内没回报"才算超时）。
                if r["type"] == "collect_now":
                    req = conn.execute(
                        "SELECT * FROM collect_requests WHERE request_id=?",
                        (r["request_id"],)).fetchone()
                    if req is None:
                        conn.execute("DELETE FROM pending_commands WHERE id=?", (r["id"],))
                        continue
                    if req["status"] in FINAL_STATUSES:
                        # 已终态（例如已 timeout 或已被撤销）→ 不能再补执行
                        conn.execute("DELETE FROM pending_commands WHERE id=?", (r["id"],))
                        continue
                    if req["dispatched_at_utc"] is None:
                        conn.execute(
                            "UPDATE collect_requests SET dispatched_at_utc=?, attempt=attempt+1, status=CASE WHEN status='submitted' THEN status ELSE status END WHERE request_id=?",
                            (now, r["request_id"]))
                        # deadline 按"下发时刻 + timeout"重算
                        conn.execute(
                            "UPDATE collect_requests SET deadline_utc=? WHERE request_id=?",
                            (iso_add_ms(now, req["timeout_ms"]), r["request_id"]))
                    conn.execute(
                        """UPDATE pending_commands
                           SET dispatched_at_utc=COALESCE(dispatched_at_utc,?),
                               taken_at_utc=?, taken_count=taken_count+1
                           WHERE id=?""", (now, now, r["id"]))
                    commands.append({
                        "request_id": r["request_id"],
                        "type": "collect_now",
                        "nonce": r["nonce"],
                        "sensor": r["sensor"],
                        "timeout_ms": r["timeout_ms"],
                        "issued_at_utc": r["issued_at_utc"],
                        "baseline": {
                            "boot_count": r["baseline_boot_count"],
                            "seq_last": r["baseline_seq_last"],
                            "uptime_ms": r["baseline_uptime_ms"],
                        },
                        "attempt": r["attempt"],
                    })
                else:
                    # pause_reports / resume_reports：取走即视为已下发，直接出队
                    conn.execute("DELETE FROM pending_commands WHERE id=?", (r["id"],))
                    commands.append({
                        "request_id": r["request_id"],
                        "type": r["type"],
                        "nonce": None,
                        "sensor": None,
                        "timeout_ms": r["timeout_ms"],
                        "issued_at_utc": r["issued_at_utc"],
                        "baseline": None,
                        "attempt": r["attempt"],
                    })
            conn.commit()

            # ---- 无指令时返回空数组（是成功，不是 404）----
            return HTTPStatus.OK, _ok({
                "commands": commands,
                "poll_after_ms": 1000,
            })
        finally:
            conn.close()


def device_ack(payload, remote_addr):
    """设备回执（docs/06 §4.2）。

    ★ 红线：回执 ≠ 完成。acked 只证明"设备收到了指令"，
      这正好就是页面上「设备接收回执」这一状态的唯一来源。
    """
    payload = payload or {}
    request_id = payload.get("request_id")
    device_id = payload.get("device_id")
    ack_type = payload.get("ack_type")
    if not request_id or not device_id or not ack_type:
        return HTTPStatus.BAD_REQUEST, _error(
            "MISSING_FIELD", "缺少必填字段 request_id/device_id/ack_type")
    if ack_type not in ("received", "started", "rejected", "superseded"):
        return HTTPStatus.BAD_REQUEST, _error("MISSING_FIELD", "ack_type 非法: %s" % ack_type)

    nonce = payload.get("nonce")
    try:
        attempt = int(payload.get("attempt") or 1)
    except (TypeError, ValueError):
        attempt = 1
    reason = payload.get("reason")
    boot_count = payload.get("boot_count")
    up_ms = payload.get("up_ms")

    with _db_lock:
        conn = db_connect()
        try:
            req = conn.execute("SELECT * FROM collect_requests WHERE request_id=?",
                               (request_id,)).fetchone()
            if req is None:
                return HTTPStatus.NOT_FOUND, _error("REQUEST_NOT_FOUND", "request_id 不存在")

            # L1：nonce 必须与下发一致
            if nonce is not None and nonce != req["nonce"]:
                return HTTPStatus.CONFLICT, _error(
                    "REQ_ID_MISMATCH", "回执 nonce 与下发值不一致", nonce_mismatch=True)

            now = utc_now_iso()
            existing = conn.execute(
                """SELECT * FROM request_acks
                   WHERE request_id=? AND ack_type=? AND attempt=?""",
                (request_id, ack_type, attempt)).fetchone()
            if existing is not None:
                # 幂等：不新增行、不改时间戳、状态不倒退
                conn.commit()
                return HTTPStatus.OK, _ok({
                    "request_id": request_id, "status": req["status"],
                    "deduplicated": True})

            conn.execute(
                """INSERT INTO request_acks
                   (request_id, device_id, ack_type, ack_at_utc, device_uptime_ms,
                    boot_count, attempt, reason, detail_json, remote_addr)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (request_id, device_id, ack_type, now, up_ms, boot_count, attempt,
                 reason, json.dumps(payload.get("detail"), ensure_ascii=False)
                 if payload.get("detail") else None, remote_addr))

            new_status = req["status"]
            # 回执只能前进不能倒退
            if ack_type == "received":
                if req["status"] == "submitted":
                    new_status = "acked"
                conn.execute(
                    """UPDATE collect_requests
                       SET status=?, acked_at_utc=COALESCE(acked_at_utc,?) WHERE request_id=?""",
                    (new_status, now, request_id))
            elif ack_type == "started":
                if req["status"] in ("submitted", "acked"):
                    new_status = "collecting"
                conn.execute(
                    """UPDATE collect_requests
                       SET status=?, collecting_at_utc=COALESCE(collecting_at_utc,?)
                       WHERE request_id=?""", (new_status, now, request_id))
            elif ack_type == "rejected":
                # ★ 这才是"硬件问题"的证据路径：设备明确说它读失败
                if req["status"] not in FINAL_STATUSES:
                    new_status = "failed"
                    conn.execute(
                        """UPDATE collect_requests
                           SET status='failed', failed_at_utc=?, failure_reason=?,
                               error_code='DEVICE_REJECTED'
                           WHERE request_id=?""", (now, reason or "DEVICE_REJECTED", request_id))
                if reason == "SENSOR_ERROR":
                    add_device_event(conn, device_id, "sensor_error", "device",
                                     {"request_id": request_id, "reason": reason})
                conn.execute("DELETE FROM pending_commands WHERE request_id=?", (request_id,))
            elif ack_type == "superseded":
                # 被更新的同类型指令取代 → 本条 cancelled，failure_reason=SUPERSEDED
                if req["status"] not in FINAL_STATUSES:
                    new_status = "cancelled"
                    conn.execute(
                        """UPDATE collect_requests
                           SET status='cancelled', failure_reason='SUPERSEDED',
                               error_code='SUPERSEDED'
                           WHERE request_id=?""", (request_id,))
                conn.execute("DELETE FROM pending_commands WHERE request_id=?", (request_id,))

            device_touch(conn, device_id, boot_count=boot_count, uptime_ms=up_ms)
            conn.commit()
            return HTTPStatus.OK, _ok({
                "request_id": request_id, "status": new_status, "deduplicated": False})
        finally:
            conn.close()


def submit_observation(payload, raw_body, remote_addr, content_type):
    """绑定请求的新观测回传（docs/06 §4.3）—— 第 2 周的核心。

    完成条件 L1∧L2∧L3 三者同时成立才算 completed；
    任意一层不成立 → 409 拒收 + rejected_observations 留痕 + **状态不改写**。
    """
    if not isinstance(payload, dict):
        return HTTPStatus.BAD_REQUEST, _error("BAD_JSON", "报文必须是 JSON 对象")

    request_id = payload.get("request_id")
    nonce = payload.get("nonce")
    device_id = payload.get("device_id")
    if not request_id or not nonce or not device_id:
        return HTTPStatus.BAD_REQUEST, _error(
            "MISSING_FIELD", "缺少必填字段 request_id/nonce/device_id")

    # ---- 先做与请求无关的报文自洽校验（缺 raw 一律拒收，后端绝不代为生成）----
    samples = payload.get("samples")
    if samples is None:
        pending = [payload]
    elif not isinstance(samples, list) or not samples:
        return HTTPStatus.BAD_REQUEST, _error(
            "MISSING_FIELD", "samples 必须是非空数组（后端不会代为生成数据）")
    else:
        pending = samples
    for i, s in enumerate(pending):
        if not isinstance(s, dict):
            return HTTPStatus.BAD_REQUEST, _error("MISSING_FIELD", "samples[%d] 必须是对象" % i)
        raw = s.get("raw")
        if not isinstance(raw, dict) or not all(k in raw for k in ("x", "y", "z")):
            with _db_lock:
                conn = db_connect()
                try:
                    reject_observation(conn, request_id, device_id, "MISSING_FIELD",
                                       "第 %d 个采样点缺少 raw.x/y/z" % (i + 1),
                                       payload, remote_addr)
                    conn.commit()
                finally:
                    conn.close()
            return HTTPStatus.BAD_REQUEST, _error(
                "MISSING_FIELD",
                "第 %d 个采样点缺少原始测量值 raw.x/raw.y/raw.z，整包拒收"
                "（后端不会代为生成数据）" % (i + 1))

    try:
        boot_count = int(payload.get("boot_count"))
    except (TypeError, ValueError):
        return HTTPStatus.BAD_REQUEST, _error("MISSING_FIELD", "boot_count 必填且为整数")
    try:
        seq_first = int(payload.get("seq_first", payload.get("seq")))
        seq_last = int(payload.get("seq_last", payload.get("seq")))
    except (TypeError, ValueError):
        return HTTPStatus.BAD_REQUEST, _error("MISSING_FIELD", "seq_first/seq_last 必填且为整数")
    try:
        up_ms = int(payload.get("up_ms")) if payload.get("up_ms") is not None else None
    except (TypeError, ValueError):
        up_ms = None
    try:
        ts_utc_ms = int(payload.get("ts_utc_ms")) if payload.get("ts_utc_ms") is not None else None
    except (TypeError, ValueError):
        ts_utc_ms = None

    with _db_lock:
        conn = db_connect()
        try:
            req = conn.execute("SELECT * FROM collect_requests WHERE request_id=?",
                               (request_id,)).fetchone()
            if req is None:
                rid = reject_observation(conn, request_id, device_id, "REQUEST_NOT_FOUND",
                                         "request_id 不存在", payload, remote_addr)
                conn.commit()
                return HTTPStatus.NOT_FOUND, _error(
                    "REQUEST_NOT_FOUND", "request_id 不存在", rejected_id=rid)

            # ---- 终态后到达的观测一律拒收，且**状态绝不改写** ----
            if req["status"] in FINAL_STATUSES:
                rid = reject_observation(
                    conn, request_id, device_id, "REQUEST_ALREADY_FINAL",
                    "请求已处于终态 %s，本条观测不作为本次完成" % req["status"],
                    payload, remote_addr)
                conn.commit()
                return HTTPStatus.CONFLICT, _error(
                    "REQUEST_ALREADY_FINAL",
                    "本次请求已结束（%s），该观测被拒收" % req["status"],
                    rejected_id=rid,
                    checks={"l1_identity": True, "l2_monotonic": None, "l3_time": None})

            # ---- 重复批：同一 (request_id,boot_count,seq_first,seq_last) ----
            dup = conn.execute(
                """SELECT id FROM observations
                   WHERE request_id=? AND boot_count=? AND seq_first=? AND seq_last=?""",
                (request_id, boot_count, seq_first, seq_last)).fetchone()
            if dup is not None:
                rid = reject_observation(conn, request_id, device_id, "DUPLICATE_BATCH",
                                         "同一批次重复提交", payload, remote_addr)
                conn.commit()
                return HTTPStatus.CONFLICT, _error(
                    "DUPLICATE_BATCH", "重复上报，已忽略", rejected_id=rid,
                    checks={"l1_identity": True, "l2_monotonic": True, "l3_time": True})

            # ---- L1 身份绑定：request_id 必须相等（已经按 request_id 查到），nonce 必须相等 ----
            l1 = (nonce == req["nonce"])
            # ---- L2 新旧性 ----
            base_boot = req["baseline_boot_count"]
            base_seq = req["baseline_seq_last"]
            base_up = req["baseline_uptime_ms"]
            if base_boot is None or base_seq is None:
                l2 = False
                freshness = None
                l2_detail = "缺少基线快照，无法判定新旧"
            elif boot_count > base_boot:
                # 跨重启：重启后 seq 从 1 重新开始也不得被判为旧值
                l2 = True
                freshness = "new_after_reboot"
                l2_detail = "boot_count %d > 基线 %d（跨重启）" % (boot_count, base_boot)
            elif boot_count == base_boot and seq_first > base_seq:
                l2 = True
                freshness = "new_monotonic"
                l2_detail = "seq_first %d > 基线 seq_last %d" % (seq_first, base_seq)
            else:
                l2 = False
                freshness = None
                l2_detail = ("观测 seq_last=%d 未超过基线 seq_last=%d（boot %d vs %d），判定为旧值"
                             % (seq_last, base_seq, boot_count, base_boot))

            # ---- L3 时间性 ----
            received = utc_now_iso()
            if boot_count > base_boot:
                l3 = True
                l3_detail = "跨重启，时间性由 boot_count 成立"
            elif up_ms is not None and base_up is not None:
                l3 = up_ms > base_up
                l3_detail = ("up_ms %s vs 基线 %s" % (up_ms, base_up))
            else:
                l3 = False
                l3_detail = "同 boot 内缺少 up_ms，无法证明采集发生在下发之后"
            # 服务端接收时刻必须不早于下发时刻
            if req["dispatched_at_utc"] and received < req["dispatched_at_utc"]:
                l3 = False
                l3_detail = "服务端接收时刻早于下发时刻（时间倒挂）"

            checks = {"l1_identity": bool(l1), "l2_monotonic": bool(l2), "l3_time": bool(l3)}

            if not l1:
                rid = reject_observation(
                    conn, request_id, device_id, "REQ_ID_MISMATCH",
                    "nonce 与下发值不一致", payload, remote_addr)
                conn.commit()
                return HTTPStatus.CONFLICT, _error(
                    "REQ_ID_MISMATCH", "观测与本次请求不匹配（nonce 不符），已拒收",
                    rejected_id=rid, checks=checks)
            if not l2 or not l3:
                rid = reject_observation(
                    conn, request_id, device_id, "STALE_OBSERVATION",
                    l2_detail if not l2 else l3_detail, payload, remote_addr)
                conn.commit()
                return HTTPStatus.CONFLICT, _error(
                    "STALE_OBSERVATION",
                    (l2_detail if not l2 else l3_detail),
                    rejected_id=rid, checks=checks)

            # ================= L1∧L2∧L3 全部成立 → 一次性写入 =================
            body_text = raw_body.decode("utf-8", errors="replace")
            sha = hashlib.sha256(raw_body).hexdigest()
            cur = conn.execute(
                """INSERT INTO raw_uploads
                   (received_at_utc, device_id, remote_addr, content_type, byte_len, sha256, body)
                   VALUES (?,?,?,?,?,?,?)""",
                (received, device_id, remote_addr, content_type,
                 len(raw_body), sha, body_text))
            upload_id = cur.lastrowid

            sensor = payload.get("sensor") or {}
            try:
                lsb_per_g = int(sensor.get("lsb_per_g")) if sensor.get("lsb_per_g") else None
            except (TypeError, ValueError):
                lsb_per_g = None

            def fnum(d, k):
                v = (d or {}).get(k)
                if v is None:
                    return None
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            record_ids = []
            for i, s in enumerate(pending):
                raw = s.get("raw") or {}
                acc_g = s.get("accel_g") or {}
                acc_si = s.get("accel_ms2") or {}
                verify_ok, verify_note = None, "报文未提供 lsb_per_g，跳过换算校验"
                if lsb_per_g and lsb_per_g > 0:
                    diffs = []
                    for expect, got in ((int(raw["x"]) / lsb_per_g, fnum(acc_g, "x")),
                                        (int(raw["y"]) / lsb_per_g, fnum(acc_g, "y")),
                                        (int(raw["z"]) / lsb_per_g, fnum(acc_g, "z"))):
                        if got is not None:
                            diffs.append(abs(expect - got))
                    if diffs and max(diffs) <= 0.001:
                        verify_ok = 1
                        verify_note = "后端按 raw/lsb_per_g 重算结果与板端上报一致(误差<=0.001g)"
                    elif diffs:
                        verify_ok = 0
                        verify_note = "重算与板端上报存在差异(最大 %.4fg)" % max(diffs)
                r2 = conn.execute(
                    """INSERT INTO sensor_records
                       (upload_id, device_id, device_mac, boot_count, seq, sample_idx,
                        sample_count, sample_period_ms, uptime_ms, ts_utc_ms, ts_local,
                        received_at_utc, sensor_model, sensor_type, i2c_addr, chip_id,
                        range_g, lsb_per_g, odr_hz, oversample, raw_x, raw_y, raw_z,
                        gx, gy, gz, ms2x, ms2y, ms2z, magnitude_g, verify_ok, verify_note,
                        request_id, report_mode)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (upload_id, device_id, payload.get("mac"), boot_count,
                     s.get("seq", seq_last), s.get("idx", i + 1), len(pending),
                     payload.get("sample_period_ms"),
                     s.get("up_ms", up_ms), s.get("ts_utc_ms", ts_utc_ms),
                     s.get("ts_local", payload.get("ts_local")), received,
                     sensor.get("model"), sensor.get("type"), sensor.get("i2c_addr"),
                     sensor.get("chip_id"), sensor.get("range_g"), lsb_per_g,
                     sensor.get("odr_hz"), sensor.get("oversample"),
                     int(raw["x"]), int(raw["y"]), int(raw["z"]),
                     fnum(acc_g, "x"), fnum(acc_g, "y"), fnum(acc_g, "z"),
                     fnum(acc_si, "x"), fnum(acc_si, "y"), fnum(acc_si, "z"),
                     fnum(s, "magnitude_g"), verify_ok, verify_note,
                     request_id, "manual"))
                record_ids.append(r2.lastrowid)

            value = {
                "magnitude_g": payload.get("magnitude_g"),
                "raw": payload.get("raw"),
                "accel_g": payload.get("accel_g"),
                "accel_ms2": payload.get("accel_ms2"),
            }
            obs = conn.execute(
                """INSERT INTO observations
                   (request_id, nonce, device_id, record_id, upload_id, boot_count,
                    seq_first, seq_last, sample_count, sensor, ts_utc_ms, ts_local, up_ms,
                    value_json, freshness, verify_ok, received_at_utc)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (request_id, nonce, device_id, record_ids[0], upload_id, boot_count,
                 seq_first, seq_last, len(pending), req["sensor"], ts_utc_ms,
                 payload.get("ts_local"), up_ms,
                 json.dumps(value, ensure_ascii=False), freshness, verify_ok, received))
            observation_id = obs.lastrowid

            # ★ 只有走到这里才允许写 completed —— 这就是"旧记录不可改写为本次完成"的落点
            conn.execute(
                """UPDATE collect_requests
                   SET status='completed', completed_at_utc=?, failure_reason=NULL,
                       error_code=NULL
                   WHERE request_id=?""", (received, request_id))
            conn.execute("DELETE FROM pending_commands WHERE request_id=?", (request_id,))
            device_touch(conn, device_id, boot_count=boot_count, seq=seq_last, uptime_ms=up_ms)
            conn.commit()

            submit_to_complete = iso_diff_ms(received, req["submitted_at_utc"])
            dispatch_to_complete = iso_diff_ms(received, req["dispatched_at_utc"])
            return HTTPStatus.CREATED, _ok({
                "request_id": request_id,
                "request_status": "completed",
                "observation_id": observation_id,
                "record_id": record_ids[0],
                "upload_id": upload_id,
                "freshness": freshness,
                "checks": checks,
                "elapsed_ms": {
                    "submit_to_complete": submit_to_complete,
                    "dispatch_to_complete": dispatch_to_complete,
                },
                "value": value,
                "verify_ok": verify_ok,
            })
        finally:
            conn.close()


def submit_reports(payload, raw_body, remote_addr, content_type):
    """周期/手动上报（docs/06 §4.4）。

    复用 /ingest 的全部字段与语义，只多两个顶层字段 report_mode / request_id。
    ★ 暂停期间设备仍发周期包时：**接受落库但标记**，并回 warning=DEVICE_NOT_PAUSED，
      这样"暂停是否生效"是可核对的，而不是靠猜。
    """
    payload = payload or {}
    device_id = payload.get("device_id")
    if not device_id:
        return HTTPStatus.BAD_REQUEST, _error("MISSING_FIELD", "缺少必填字段 device_id")

    report_mode = payload.get("report_mode") or "periodic"
    warning = None
    if report_mode == "periodic":
        with _db_lock:
            conn = db_connect()
            try:
                dev = conn.execute("SELECT * FROM devices WHERE device_id=?",
                                   (device_id,)).fetchone()
                if dev is not None and dev["periodic_enabled"] == 0:
                    warning = "DEVICE_NOT_PAUSED"
                    add_device_event(conn, device_id, "resume", "device",
                                     {"note": "暂停期间设备仍发周期包"})
                    conn.commit()
            finally:
                conn.close()

    status, resp = ingest(payload, raw_body, remote_addr, content_type)
    if warning and isinstance(resp, dict):
        resp["warning"] = warning
    if report_mode == "periodic":
        with _db_lock:
            conn = db_connect()
            try:
                device_touch(conn, device_id, boot_count=payload.get("boot_count"),
                             seq=payload.get("seq"))
                conn.commit()
            finally:
                conn.close()
    return status, resp


def scan_timeouts():
    """超时扫描（docs/06 §5.3）：每 1s 把过了 deadline 且未终态的请求置 timeout。

    ★ inv.2 的落点（超时 ≠ 硬件故障）：
      - 只写 collect_requests.status='timeout' 与 device_events(command_timeout, source='server')
      - **不**改 devices.last_status、**不**写任何 sensor_error
      一句话：超时是"没等到"，故障是"设备说了它坏了"。
    """
    now = utc_now_iso()
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute(
                """SELECT * FROM collect_requests
                   WHERE status IN ('submitted','acked','collecting')
                     AND deadline_utc IS NOT NULL AND deadline_utc < ?""",
                (now,)).fetchall()
            for r in rows:
                conn.execute(
                    """UPDATE collect_requests
                       SET status='timeout', failure_reason=COALESCE(failure_reason,'NETWORK')
                       WHERE request_id=?""", (r["request_id"],))
                add_device_event(conn, r["device_id"], "command_timeout", "server",
                                 {"request_id": r["request_id"],
                                  "deadline_utc": r["deadline_utc"]})
                conn.execute("DELETE FROM pending_commands WHERE request_id=?",
                             (r["request_id"],))
            if rows:
                conn.commit()
            return len(rows)
        finally:
            conn.close()


def _timeout_scanner():
    while True:
        try:
            n = scan_timeouts()
            if n:
                print("[timeout] 本轮将 %d 条请求置为 timeout（不写 sensor_error）" % n)
        except Exception as e:            # 扫描失败不能拖垮整个服务
            print("[timeout] 扫描异常: %r" % (e,))
        time.sleep(TIMEOUT_SCAN_PERIOD_S)


# ===========================================================================
# HTTP 层
# ===========================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "SensorIngest/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 工具 ----
    def _send_json(self, status, obj, close=False):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if close:
            # ★ 关键：在没把请求体读干净就提前回错（如 401/400）时，必须显式声明
            #   Connection: close。否则 HTTP/1.1 keep-alive 会让这条连接继续复用，
            #   而客户端已经发来的 JSON 报文还留在接收缓冲区里，会被当成下一个请求的
            #   请求行去解析 —— 服务器于是看到"请求行是一坨 JSON"，回 400 Bad request
            #   syntax，并进入 401/400 交替的错位死循环，数据一条都存不进库。
            self.close_connection = True
            self.send_header("Connection", "close")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Device-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _token_ok(self):
        got = self.headers.get("X-Device-Token", "")
        return hmac.compare_digest(got, DEVICE_TOKEN)

    def log_message(self, fmt, *args):  # 控制台日志带时间戳
        sys.stderr.write("[%s] %s - %s\n" % (utc_now_iso(), self.address_string(), fmt % args))

    # ---- 路由 ----
    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        remote = "%s:%d" % self.client_address
        ctype = self.headers.get("Content-Type", "")

        # ---- 设备侧端点：统一要求 X-Device-Token（沿用第 1 周同一套令牌）----
        DEVICE_PATHS = ("/api/v1/ingest", "/api/v1/device/reports",
                        "/api/v1/device/commands/poll", "/api/v1/device/ack",
                        "/api/v1/observations")

        if path in DEVICE_PATHS:
            if not self._token_ok():
                # 令牌不对直接拒收，避免有人往库里灌假数据
                # close=True：请求体没读就直接回错，必须断开连接，否则报文残留会
                # 让下一次复用请求错位（表现为 401/400 交替）。
                self._send_json(HTTPStatus.UNAUTHORIZED,
                                {"ok": False, "error": "X-Device-Token 校验失败"},
                                close=True)
                return
        elif not path.startswith("/api/v1/") and path not in ("", "/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "未知接口"})
            return

        body = self._read_json()
        if body is None:
            return
        raw_body, payload = body

        if path == "/api/v1/ingest":
            handler = lambda: ingest(payload, raw_body, remote, ctype)
        elif path == "/api/v1/device/reports":
            handler = lambda: submit_reports(payload, raw_body, remote, ctype)
        elif path == "/api/v1/device/commands/poll":
            handler = lambda: poll_commands(payload, remote)
        elif path == "/api/v1/device/ack":
            handler = lambda: device_ack(payload, remote)
        elif path == "/api/v1/observations":
            handler = lambda: submit_observation(payload, raw_body, remote, ctype)
        elif path == "/api/v1/collect_requests":
            handler = lambda: create_collect_request(payload)
        elif path == "/api/v1/device/pause":
            handler = lambda: set_device_pause(payload)
        elif path.startswith("/api/v1/collect_requests/") and path.endswith("/cancel"):
            rid = path[len("/api/v1/collect_requests/"):-len("/cancel")]
            handler = lambda: cancel_collect_request(rid, payload)
        else:
            handler = None

        if handler is None:
            self._send_json(HTTPStatus.NOT_FOUND, _error("REQUEST_NOT_FOUND", "未知接口"))
            return
        # ★ 任何未预料的异常都必须在**这一层**兜住并回 500，而不是让连接静默断开。
        #   否则客户端只会看到 "Remote end closed connection"，排查时看不到
        #   真正的原因（服务端 traceback 混在 stderr 里，很难与请求对上号）。
        try:
            status, resp = handler()
        except Exception as e:
            import traceback
            traceback.print_exc()
            status, resp = HTTPStatus.INTERNAL_SERVER_ERROR, _error(
                "INTERNAL", "服务端异常: %r" % (e,), retryable=True)
        self._send_json(status, resp)

    def _read_json(self):
        """读请求体并解析 JSON；失败时已自行回错并返回 None。"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "报文长度非法: %d" % length},
                            close=True)
            return None
        raw_body = self.rfile.read(length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "JSON 解析失败: %s" % e})
            return None
        return raw_body, payload

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        def q(name, default=None):
            v = qs.get(name)
            return v[0] if v else default

        if path == "/api/v1/latest":
            self._send_json(*query_latest(q("device_id")))
        elif path == "/api/v1/records":
            self._send_json(*query_records(q("device_id"), q("limit", "100"),
                                           q("after_id")))
        elif path == "/api/v1/devices":
            self._send_json(*query_devices())
        elif path == "/api/v1/stats":
            self._send_json(*query_stats())
        elif path == "/api/v1/collect_requests":
            # 动作执行追踪：连点两次必须能看到两条记录与各自终局
            self._send_json(*list_collect_requests(q("device_id"), q("status"),
                                                   q("limit", "50")))
        elif path.startswith("/api/v1/collect_requests/"):
            self._send_json(*get_collect_request(path.rsplit("/", 1)[1]))
        elif path.startswith("/api/v1/raw/"):
            try:
                uid = int(path.rsplit("/", 1)[1])
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "id 非法"})
                return
            self._send_json(*query_raw(uid))
        elif path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True, "server_time_utc": utc_now_iso()})
        else:
            self._serve_static(path)

    # ---- 静态文件（Web 前端） ----
    def _serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        # 防目录穿越
        rel = os.path.normpath(path.lstrip("/")).replace("\\", "/")
        if rel.startswith("..") or os.path.isabs(rel):
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "非法路径"})
            return
        full = os.path.join(WEB_DIR, rel)
        if not os.path.isfile(full):
            self._send_json(HTTPStatus.NOT_FOUND,
                            {"ok": False, "error": "文件不存在: %s（Web 目录: %s）" % (rel, WEB_DIR)})
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".json": "application/json; charset=utf-8",
                 ".svg": "image/svg+xml"}.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")   # 调试期不做缓存
        self._cors()
        self.end_headers()
        self.wfile.write(data)


def main():
    db_init()

    # 超时扫描：每 1s 把过了 deadline 且未终态的请求置 timeout。
    # ★ 只写请求维度 + device_events(command_timeout, source=server)，
    #   绝不改 devices.last_status、绝不写 sensor_error（超时 ≠ 硬件故障）。
    threading.Thread(target=_timeout_scanner, daemon=True).start()

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print("=" * 70)
    print("传感器后端服务已启动")
    print("  监听地址 : http://%s:%d" % (HOST, PORT))
    print("  数据库   : %s" % DB_PATH)
    print("  Web 目录 : %s" % WEB_DIR)
    print("  上传接口 : POST /api/v1/ingest  (需要 X-Device-Token)")
    print("  查询接口 : GET  /api/v1/latest?device_id=xxx")
    print("  ---- 第 2 周：远程采集指令闭环 ----")
    print("  设备侧   : POST /api/v1/device/commands/poll   设备 1Hz 轮询取指令")
    print("             POST /api/v1/device/ack             设备回执（received/started/rejected/superseded）")
    print("             POST /api/v1/observations            绑定 request_id 的新观测回传")
    print("             POST /api/v1/device/reports          周期/手动上报（含 report_mode）")
    print("  操作者侧 : POST /api/v1/collect_requests       点击「采集最新数据」")
    print("             GET  /api/v1/collect_requests/<id>   轮询请求状态")
    print("             GET  /api/v1/collect_requests        动作执行追踪列表")
    print("             POST /api/v1/collect_requests/<id>/cancel")
    print("             POST /api/v1/device/pause            暂停/恢复周期上报")
    print("  超时     : %d ms（COLLECT_TIMEOUT_MS，前端传 timeout_ms 一律忽略）"
          % COLLECT_TIMEOUT_MS)
    print("=" * 70)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C, 正在退出 ...")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()