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
from datetime import datetime, timezone
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
DEVICE_TOKEN = os.environ.get("SENSOR_TOKEN", "CHANGE_ME_DEVICE_TOKEN")

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
"""

# 旧库升级用的补列（老库没有这些列；ALTER TABLE 是幂等的，缺了才加）
MIGRATIONS = [
    ("sensor_records", "sample_idx", "INTEGER"),
    ("sensor_records", "sample_count", "INTEGER"),
    ("sensor_records", "sample_period_ms", "INTEGER"),
    ("sensor_records", "device_mac", "TEXT"),
]


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
    conn = db_connect()
    try:
        rows = conn.execute(
            """SELECT device_id, COUNT(*) AS n, MAX(received_at_utc) AS last_seen,
                      MAX(sensor_model) AS model, MAX(seq) AS last_seq,
                      MAX(device_mac) AS mac
               FROM sensor_records GROUP BY device_id ORDER BY last_seen DESC""").fetchall()
        return HTTPStatus.OK, {"ok": True,
                               "devices": [dict(r) for r in rows]}
    finally:
        conn.close()


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
        return HTTPStatus.OK, {
            "ok": True, "records": rec, "raw_uploads": upl, "devices": dev,
            "first_received_at_utc": first, "last_received_at_utc": last,
            "server_time_utc": utc_now_iso(),
        }
    finally:
        conn.close()


# ===========================================================================
# HTTP 层
# ===========================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "SensorIngest/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 工具 ----
    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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
        if path != "/api/v1/ingest":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "未知接口"})
            return

        if not self._token_ok():
            # 令牌不对直接拒收，避免有人往库里灌假数据
            self._send_json(HTTPStatus.UNAUTHORIZED,
                            {"ok": False, "error": "X-Device-Token 校验失败"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "报文长度非法: %d" % length})
            return

        raw_body = self.rfile.read(length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "JSON 解析失败: %s" % e})
            return

        status, resp = ingest(payload, raw_body,
                              "%s:%d" % self.client_address,
                              self.headers.get("Content-Type", ""))
        self._send_json(status, resp)

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
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print("=" * 70)
    print("传感器后端服务已启动")
    print("  监听地址 : http://%s:%d" % (HOST, PORT))
    print("  数据库   : %s" % DB_PATH)
    print("  Web 目录 : %s" % WEB_DIR)
    print("  上传接口 : POST /api/v1/ingest  (需要 X-Device-Token)")
    print("  查询接口 : GET  /api/v1/latest?device_id=xxx")
    print("=" * 70)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C, 正在退出 ...")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()