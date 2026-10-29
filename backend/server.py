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

# ---- 第 4 周：受限工具语言助手 ----
#   ★ 放在这里 import，是因为它以 `python backend/server.py` 方式启动时
#     sys.path[0] 就是 backend/；用 `python -m backend.server` 启动时再补一次路径。
try:
    import nl_assistant
except ImportError:
    sys.path.insert(0, BASE_DIR)
    import nl_assistant

# ---- 第 5 周：语音链路（录音 → 识别 → 任务 → 合成 → 播放）----
#   ★ 同样以补路径的方式兼容两种启动形式；本模块只用标准库，
#     真正吃运行时的是独立进程 backend/voice_engine.py（HTTP 调用）。
try:
    import voice_pipeline
except ImportError:
    sys.path.insert(0, BASE_DIR)
    import voice_pipeline

# ---- 第 6 周：语音会话状态（澄清 / 取消 / 迟到结果隔离）----
#   ★ 纯标准库、纯 SQLite，只回答"这一轮还要不要"这一个问题；
#     不碰识别、不碰任务、不碰合成。
try:
    import voice_session
except ImportError:
    sys.path.insert(0, BASE_DIR)
    import voice_session

# ---- 第 7 周：画面质量度量（从 JPEG 字节里量亮度/平坦度，纯标准库）----
#   ★ 只回答"暗不暗、平不平"，不回答"图里是什么"。用于把"镜头被挡住"从
#     "采集成功"里区分出来，且结论必须能被复算。
try:
    import img_quality
except ImportError:
    sys.path.insert(0, BASE_DIR)
    import img_quality

DB_PATH = os.environ.get("SENSOR_DB", os.path.join(BASE_DIR, "data", "sensor.db"))

# 第 5 周：录音与播报音频的落盘目录（与照片目录并列，便于一起备份/取证）
VOICE_DIR = os.environ.get("SENSOR_VOICE_DIR", os.path.join(BASE_DIR, "data", "voice"))

# Web 前端目录（同仓库的 web/ 目录）
WEB_DIR = os.environ.get("SENSOR_WEB_DIR",
                         os.path.join(os.path.dirname(BASE_DIR), "web"))

# 设备令牌：与固件 app_config.h 里的 APP_DEVICE_TOKEN 保持一致
#   ★ 默认值必须与板子里实际烧录的固件一致，否则每次上传都会被 401 拒收
#     （排查记录见 firmware/src/app_config.h 第 85 行附近）。
DEVICE_TOKEN = os.environ.get("SENSOR_TOKEN", "esp32s3eye-demo-token-2026")

# 单条上传报文大小上限（真实报文约 700 字节，给足余量）
MAX_BODY = int(os.environ.get("SENSOR_MAX_BODY", str(16 * 1024)))

# ---- 第 4 周：摄像头 ----
# 预览帧/照片是**二进制 JPEG**，不能走上面的 JSON 报文上限（16KB 连一张 VGA 都装不下）。
#   QVGA(320x240) 一帧约 8~15KB；UXGA(1600x1200) 高质量照片可达 150~300KB。
#   这里给 2MB 余量，同时仍然是一个**明确的上限**（不是无限读，避免被灌爆内存）。
CAM_MAX_BODY = int(os.environ.get("SENSOR_CAM_MAX_BODY", str(2 * 1024 * 1024)))
# 照片落盘目录（按设备分子目录，文件名含 UTC 时间与 photo_id，便于人工核对）
CAM_DIR = os.environ.get("SENSOR_CAM_DIR", os.path.join(BASE_DIR, "data", "photos"))
# 网页点「拍照」后，多久之内到达的下一帧会被附上 capture 指令（超时则丢弃，不让指令"悬空")
CAM_CAPTURE_TTL_S = float(os.environ.get("SENSOR_CAM_CAPTURE_TTL_S", "10"))

# ---- 第 7 周：按需环境快照（一次请求 = 一次采集事实）----
# ★ 这几项都是"语义参数"，不是性能参数，改它们会直接改变本周的结论：
#   超时：设备侧要重建相机拍 UXGA 需 8.7s（第 4 周实测），QVGA 约 0.3s；
#         35s 是"设备在线但一直没回"的容忍上限，超过即 timeout（终局，不再等）。
#   ★ 为什么从 25s 改成 35s：按真机 10 连测的实测分布，**下发**到设备这一步
#     最慢 17.3s（Wi-Fi 抖动时设备隔十几秒才发下一帧，请求只能搭那一趟便车），
#     上传 body 最慢 7.9s —— 25s 窗口会把"链路慢"错判成"设备超时"（实测 2/10 假超时）。
#     超时值不是越小越严格：太小只会把环境问题写成设备问题。
SNAPSHOT_TIMEOUT_S = float(os.environ.get("SENSOR_SNAPSHOT_TIMEOUT_S", "35"))
# 最少保留：每台设备至少有这么多张"带图像"的快照不会被清理删掉（不论多老）
SNAPSHOT_KEEP_MIN = int(os.environ.get("SENSOR_SNAPSHOT_KEEP_MIN", "10"))
# 保留期：超过这个天数的图像文件才会被清理（**只删文件，记录永不删**）
SNAPSHOT_RETENTION_DAYS = int(os.environ.get("SENSOR_SNAPSHOT_RETENTION_DAYS", "7"))
# 快照图像落盘目录（与 cam_photos 分开：它们的生命周期与语义都不同）
SNAPSHOT_DIR = os.environ.get("SENSOR_SNAPSHOT_DIR",
                              os.path.join(BASE_DIR, "data", "snapshots"))

# ---- 第 5 周：语音 ----
# 录音是**二进制 WAV**：16kHz/16bit/mono 约 32KB/秒，40 秒的一句话约 1.28MB。
#   给 8MB 上限（对应约 4 分钟），同时它仍是一个明确上限，不是无限读。
VOICE_MAX_BODY = int(os.environ.get("SENSOR_VOICE_MAX_BODY", str(8 * 1024 * 1024)))

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

-- ===========================================================================
-- 第 3 周新增表：按键求助与物理反馈闭环（字段级约定见 docs/06 §7）
--   ★ 同样是 CREATE TABLE IF NOT EXISTS：第 1、2 周的表一行不动，
--     历史记录不需要任何转换，verify_three_way.py / selftest_backend.py 继续可用。
-- ===========================================================================

-- 表9: 按键求助事件（佩戴者按一次键 = 行；断网时本地排队，恢复后补传仍是同一行）
--   ★ 时间字段口径（docs/06 §7.1 口径表，实现必须严格遵守）：
--       trigger_at_utc / queued_at_utc / local_feedback_at_utc = **设备侧**墙上时钟（未对时则 NULL）
--       local_feedback_received_at_utc / received_at_utc       = **服务端**接收时刻（永远非空）
--       trigger_up_ms                                          = 设备单调时钟（仅同 boot_count 内可比）
--     ★ 绝不用 trigger_up_ms 与任何 *_at_utc 相减（单调时钟 ≠ 墙上时钟）。
CREATE TABLE IF NOT EXISTS help_events (
    help_id           TEXT PRIMARY KEY,          -- UUIDv4
    device_id         TEXT NOT NULL,
    client_request_key TEXT,                     -- 幂等键：按一次键一个 key（btn-<boot>-<up_ms>）
    status            TEXT NOT NULL,             -- created|uploaded|acked|viewed|responded|cancelled|unreachable|expired
    trigger_type      TEXT NOT NULL,             -- button|touch
    trigger_at_utc    TEXT,                      -- 设备侧触发时刻（未对时=NULL，绝不用服务端时间顶替）
    trigger_up_ms     INTEGER,                   -- 设备侧单调时间（断网时唯一可靠时间）
    boot_count        INTEGER,
    queued_at_utc     TEXT,                      -- 设备首次尝试上传的时刻（算断网滞留真实时长）
    local_feedback    TEXT NOT NULL,             -- 本地反馈事实：buzzer+led|led|none
    local_feedback_at_utc TEXT,                  -- 设备侧：本地反馈**实际发生**的时刻
    local_feedback_received_at_utc TEXT NOT NULL,-- 服务端：收到该事件的时刻（未对时兜底口径）
    received_at_utc   TEXT,                      -- 服务端收到上传的时刻
    created_at_utc    TEXT NOT NULL,
    remote_acked_at_utc TEXT,                    -- 设备确认已收到「查看者回应」→ 唯一允许显示"已收到"的依据
    viewed_at_utc     TEXT,
    responded_at_utc  TEXT,
    cancelled_at_utc  TEXT,
    cancel_reason     TEXT,
    accepted_by       TEXT,                      -- 回应者（查看者）
    pending_local     INTEGER NOT NULL DEFAULT 0,-- 1 = 断网期间产生、现在补传
    message           TEXT
);
CREATE INDEX IF NOT EXISTS idx_help_device_time ON help_events(device_id, created_at_utc DESC);
CREATE INDEX IF NOT EXISTS idx_help_status      ON help_events(status);
CREATE INDEX IF NOT EXISTS idx_help_client_key  ON help_events(client_request_key);

-- 表10: 求助事件的查看者动作流水（回应/取消/查看）—— 必须留痕，取消不等于删除
CREATE TABLE IF NOT EXISTS help_actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    help_id       TEXT NOT NULL,
    action        TEXT NOT NULL,   -- viewed|responded|cancelled
    actor         TEXT,
    at_utc        TEXT NOT NULL,
    detail_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_hact_help ON help_actions(help_id, id);

-- ===========================================================================
-- 第 4 周新增表：摄像头照片（预览帧不落盘，只在内存里保留"最新一帧"）
--   ★ 为什么预览帧不写库：5fps × 24h ≈ 43 万帧，写库既无意义又会把磁盘写满。
--     网页要的是"此刻的画面"，内存里保存最新一帧即可（服务重启后为无画面，
--     如实显示"还没有画面"，绝不回放旧帧冒充实时）。
--   ★ 照片必须落盘 + 留痕：它是用户主动触发的**证据**，要能下载、能核对字节数。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS cam_photos (
    photo_id        TEXT PRIMARY KEY,          -- 时间戳派生，天然按时间排序
    device_id       TEXT NOT NULL,
    created_at_utc  TEXT NOT NULL,             -- 服务器接收时刻（UTC）
    width           INTEGER,                   -- 设备声明的实际宽高（来自帧元数据）
    height          INTEGER,
    byte_len        INTEGER NOT NULL,          -- 真实字节数
    sha256          TEXT NOT NULL,             -- 原始 JPEG 的 SHA256（可逐字节核对）
    seq             INTEGER,                   -- 设备端帧序号（排查乱序/重放）
    remote_addr     TEXT,
    filename        TEXT NOT NULL              -- 相对 CAM_DIR 的路径
);
CREATE INDEX IF NOT EXISTS idx_cam_photos_dev ON cam_photos(device_id, created_at_utc DESC);
"""

# ===========================================================================
# 第 7 周新增表：按需环境快照
#
# ★ 为什么不能直接用 cam_photos（第 4 周的"拍照"）：
#   拍照只记录"收到过一张照片"，它回答不了本周的问题 ——
#   「用户请求查看环境时，系统如何说明采集是否成功、采到的是哪一时刻的画面？」
#   要回答它，一行记录必须同时挂着：谁请求的、请求在哪一刻、设备什么时候按的快门、
#   画面什么时候到后端、这一张到底属于哪一次请求（而不是上一次的图）。
#   所以快照是**一次请求的账本**，照片只是一个产物。
#
# ★ counts_as_live 是本周最硬的字段：只有"真机、按这次请求、新取的、确实到了"
#   才为 1。回放（replay）与协议模拟（simulated）一律为 0 —— 它们可以是很好的演示，
#   但不是"这次真的拍到了"。
# ===========================================================================
_DDL_SNAPSHOT = """
CREATE TABLE IF NOT EXISTS cam_snapshots (
    snapshot_id       TEXT PRIMARY KEY,
    device_id         TEXT NOT NULL,
    state             TEXT NOT NULL,           -- pending/requested/capturing/ok/occluded/failed/timeout/cancelled/replayed
    trigger_kind      TEXT NOT NULL,           -- user_request / teaching_event / replay
    source            TEXT NOT NULL,           -- live / replay
    is_replay         INTEGER NOT NULL DEFAULT 0,
    simulated         INTEGER NOT NULL DEFAULT 0,
    counts_as_live    INTEGER NOT NULL DEFAULT 0,
    mode              TEXT,                    -- qvga / uxga
    requested_by      TEXT,
    reason            TEXT,
    requested_at_utc  TEXT NOT NULL,
    requested_seq     INTEGER,                 -- ★ 请求时刻设备帧序号：上传必须严格大于它
    baseline_seq      INTEGER,                 -- ★ 更严的基线：下发那一帧的序号（取两者较大）
    requested_frame_no INTEGER,                -- 请求时刻后端内存帧号（仅诊断）
    delivered_at_utc  TEXT,                    -- 请求随帧响应下发到设备的时刻
    begin_at_utc      TEXT,                    -- 设备自报"已开始取图"的时刻
    captured_at_utc   TEXT,                    -- 快门时刻（估计值，见 captured_is_est）
    captured_is_est   INTEGER NOT NULL DEFAULT 0,
    received_at_utc   TEXT,                    -- 后端收齐字节的时刻（精确）
    width             INTEGER,
    height            INTEGER,
    byte_len          INTEGER,
    sha256            TEXT,
    filename          TEXT,                    -- 相对 SNAPSHOT_DIR
    device_uptime_ms  INTEGER,                 -- 设备自报开机毫秒（不做时钟对齐）
    upload_ms         INTEGER,                 -- 设备自测"取帧→开始发请求"耗时
    rx_ms             INTEGER,                 -- 服务端量到的"开始收 body→收完"耗时
    quality_json      TEXT,
    quality_verdict   TEXT,                    -- usable / occluded / unknown
    quality_note      TEXT,
    latency_capture_ms INTEGER,                -- 请求 → 快门（估计）
    latency_total_ms  INTEGER,                 -- 请求 → 收齐字节（精确）
    error_code        TEXT,
    error_message     TEXT,
    late_at_utc       TEXT,                    -- 迟到图像：超时/取消之后才到的那张
    late_sha256       TEXT,
    late_byte_len     INTEGER,
    late_filename     TEXT,
    stale_rejects     INTEGER NOT NULL DEFAULT 0,
    replay_of_photo_id TEXT,
    image_pruned      INTEGER NOT NULL DEFAULT 0,
    version           INTEGER NOT NULL DEFAULT 0,
    note              TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_dev ON cam_snapshots(device_id, requested_at_utc DESC);
CREATE INDEX IF NOT EXISTS idx_snap_state ON cam_snapshots(state);
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

# ---- 第 3 周参数（按键求助，全部可用环境变量覆盖） ----
# 求助事件上传超时（docs/06 §5.3：help 上传默认 30000）
HELP_UPLOAD_TIMEOUT_MS = int(os.environ.get("HELP_UPLOAD_TIMEOUT_MS", "30000"))
# 求助状态机终态集合（终态后不能再回应/取消；重复取消走幂等）
HELP_FINAL_STATUSES = ("cancelled", "expired")
# 合法的求助状态取值（docs/06 §2.2）—— 越界取值一律拒绝，不允许自造状态
HELP_STATUSES = ("created", "uploaded", "acked", "viewed", "responded",
                 "cancelled", "unreachable", "expired")
# 合法触发类型与本地反馈取值（docs/06 §7.1）
HELP_TRIGGER_TYPES = ("button", "touch")
HELP_LOCAL_FEEDBACKS = ("buzzer+led", "led", "none")
# 允许的指令类型全集（docs/06 §2.2）—— 越界即契约违约
COMMAND_TYPES = ("collect_now", "pause_reports", "resume_reports",
                 "clear_alert", "help_ack")


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
    # 照片目录：提前建好，避免第一次拍照时才因权限/路径问题失败
    os.makedirs(CAM_DIR, exist_ok=True)
    # 第 7 周：按需快照表（独立 DDL，索引已包含在里面）
    conn = db_connect()
    try:
        conn.executescript(_DDL_SNAPSHOT)
        # 第 7 周补列：早期库里的 cam_snapshots 没有 rx_ms（表是刚建的，先查再加）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(cam_snapshots)")}
        if "rx_ms" not in cols:
            conn.execute("ALTER TABLE cam_snapshots ADD COLUMN rx_ms INTEGER")
            print("[db] 已为 cam_snapshots 补充字段 rx_ms INTEGER")
        conn.commit()
    finally:
        conn.close()
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    # 第 5 周：语音记录表 + 录音目录（独立建表脚本，不掺进上面的大 SCHEMA）
    conn = db_connect()
    try:
        voice_pipeline.db_init(conn)
    finally:
        conn.close()
    os.makedirs(VOICE_DIR, exist_ok=True)
    print("[db] 数据库就绪:", DB_PATH)
    print("[cam] 照片目录:", CAM_DIR)
    print("[snap] 快照目录:", SNAPSHOT_DIR,
          "（超时 %.0fs / 每设备最少保留 %d 张 / 保留 %d 天）"
          % (SNAPSHOT_TIMEOUT_S, SNAPSHOT_KEEP_MIN, SNAPSHOT_RETENTION_DAYS))
    print("[voice] 语音记录目录:", VOICE_DIR)
    print("[voice] 语音引擎地址:", voice_pipeline.VOICE_ENGINE_URL)


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


# ===========================================================================
# 第 3 周：按键求助与物理反馈闭环（docs/06 §7）
#
#   ★ 本节的**核心红线**（当堂验证第 2 项）：
#     只有 `remote_acked_at_utc` 被写入（设备真的把 help_ack 指令取走并回执）
#     才允许任何一方显示「对方已收到」。本地反馈只证明"本机知道自己按下了"，
#     后台把指令"放进队列"也不算送达 —— 这就是 §4.5 的关键推论。
# ===========================================================================
def _help_upload_response(row, deduplicated=False):
    """求助上传响应（docs/06 §7.1）。

    ★ `remote_ack_required` 恒为 true：本地反馈已经发生，但远端可见性**必须**
      等查看者回应 + 设备确认，两者缺一不可。这条字段就是"不许显示已收到"的源头。
    """
    return {
        "help_id": row["help_id"],
        "status": row["status"],
        "remote_ack_required": True,
        "notice": "本地反馈已发生；远端可见性待查看者回应",
        "deduplicated": bool(deduplicated),
    }


def create_help_event(payload, remote_addr=""):
    """设备上传一条按键求助事件（docs/06 §7.1）。

    ★ 红线一：**先本地反馈，再尝试上传**。所以本函数只负责"接收与落库"，
      设备即使上传失败（断网）也已经在本地完成了蜂鸣/LED —— 恢复后补传时带
      `pending_local=true`，且 `trigger_up_ms` 必须是**触发那一刻**的值，绝不改写。
    ★ 红线二：缺必填字段一律 400，**绝不**代为生成 `help_id` 或补时间戳。
    """
    if not isinstance(payload, dict):
        return HTTPStatus.BAD_REQUEST, _error("BAD_JSON", "报文必须是 JSON 对象")

    device_id = payload.get("device_id")
    if not device_id or not isinstance(device_id, str):
        return HTTPStatus.BAD_REQUEST, _error("MISSING_FIELD", "缺少必填字段 device_id")

    help_id = payload.get("help_id")
    if not help_id or not isinstance(help_id, str):
        return HTTPStatus.BAD_REQUEST, _error("MISSING_FIELD", "缺少必填字段 help_id")

    trigger_type = payload.get("trigger_type")
    if trigger_type not in HELP_TRIGGER_TYPES:
        return HTTPStatus.BAD_REQUEST, _error(
            "MISSING_FIELD",
            "trigger_type 非法: %r（必须是 %s）" % (trigger_type, "/".join(HELP_TRIGGER_TYPES)))

    local_feedback = payload.get("local_feedback")
    if local_feedback not in HELP_LOCAL_FEEDBACKS:
        # ★ 必须如实标注：没接蜂鸣器却报 buzzer+led 就是"谎报本地反馈"（TC-W3-04）
        return HTTPStatus.BAD_REQUEST, _error(
            "MISSING_FIELD",
            "local_feedback 非法: %r（必须是 %s）"
            % (local_feedback, "/".join(HELP_LOCAL_FEEDBACKS)))

    trigger_up_ms = payload.get("trigger_up_ms")
    boot_count = payload.get("boot_count")
    client_key = payload.get("client_request_key")
    pending_local = bool(payload.get("pending_local"))
    message = payload.get("message")

    # 设备侧墙上时钟字段：未对时就是 NULL —— 绝不用服务端时间顶替（TC-W3-05(c)）
    trigger_at_utc = payload.get("trigger_at_utc")
    queued_at_utc = payload.get("queued_at_utc")
    local_feedback_at_utc = payload.get("local_feedback_at_utc")

    now = utc_now_iso()

    with _db_lock:
        conn = db_connect()
        try:
            # ---- 幂等：长按/连按被去抖成一条（client_request_key = btn-<boot>-<up_ms>）----
            if client_key:
                dup = conn.execute(
                    "SELECT * FROM help_events WHERE client_request_key=?",
                    (client_key,)).fetchone()
                if dup is not None:
                    # 不新增行、不改触发时间、状态不倒退
                    conn.commit()
                    return HTTPStatus.OK, _ok(_help_upload_response(dup, deduplicated=True))

            dup_id = conn.execute("SELECT help_id FROM help_events WHERE help_id=?",
                                  (help_id,)).fetchone()
            if dup_id is not None:
                conn.commit()
                return HTTPStatus.OK, _ok(_help_upload_response(dup_id, deduplicated=True))

            conn.execute(
                """INSERT INTO help_events
                   (help_id, device_id, client_request_key, status, trigger_type,
                    trigger_at_utc, trigger_up_ms, boot_count, queued_at_utc,
                    local_feedback, local_feedback_at_utc, local_feedback_received_at_utc,
                    received_at_utc, created_at_utc, pending_local, message)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (help_id, device_id, client_key, "uploaded", trigger_type,
                 trigger_at_utc, trigger_up_ms, boot_count, queued_at_utc,
                 local_feedback, local_feedback_at_utc, now,
                 now, now, 1 if pending_local else 0, message))
            # 上传动作留痕（help_actions 的 action 只有 viewed/responded/cancelled 三个，
            # 上传本身记在 device_events 里，不与查看者动作混在一张表）
            add_device_event(conn, device_id, "help_uploaded", "device",
                             {"help_id": help_id, "trigger_type": trigger_type,
                              "local_feedback": local_feedback,
                              "pending_local": bool(pending_local),
                              "trigger_up_ms": trigger_up_ms,
                              "boot_count": boot_count})
            device_touch(conn, device_id, boot_count=boot_count)
            conn.commit()

            row = conn.execute("SELECT * FROM help_events WHERE help_id=?",
                               (help_id,)).fetchone()
            return HTTPStatus.CREATED, _ok(_help_upload_response(row, deduplicated=False))
        finally:
            conn.close()


def _help_age_ms(row, now_iso):
    """`age_ms` 的算法（docs/06 §7.4）—— 不得用错，否则会把"滞留 5 分钟"显示成"刚按下"。

    | 事件类型 | age_ms = | 理由 |
    |---|---|---|
    | 断网滞留补传且设备时钟已对时 | now - trigger_at_utc | 最精确 |
    | 断网滞留补传（未对时）       | now - queued_at_utc  | 设备首次尝试上传时刻 |
    | 正常在线事件                 | now - received_at_utc| 上传几乎即时 |

    ★ 禁止用 trigger_up_ms 直接减服务端时间（单调时钟与墙上时钟不可比，跨重启更不可比）。
    """
    if row["pending_local"] and row["trigger_at_utc"]:
        base = row["trigger_at_utc"]
    elif row["pending_local"] and row["queued_at_utc"]:
        base = row["queued_at_utc"]
    else:
        base = row["received_at_utc"] or row["created_at_utc"]
    # 设备侧本地串带时区（2026-10-08T13:49:31.437+0800），服务端串是 …Z：
    # 统一换算成 UTC 再相减，绝不相减两个不同口径的字符串
    d = _parse_any_iso(base)
    n = _parse_any_iso(now_iso)
    if d is None or n is None:
        return None
    return int((n - d).total_seconds() * 1000)


def _parse_any_iso(s):
    """同时吃"服务端 UTC 串（…Z）"和"设备侧带时区本地串（…+0800）"。

    ★ 为什么需要：docs/06 §7.1 的口径表里 trigger_at_utc 是设备侧本地串，
      而 received_at_utc 是服务端 Z 串。两者要在同一个 age_ms 里相减，
      必须先统一到 UTC —— 直接字符串相减或当成本地时间处理都会算错时区。
    """
    if not s:
        return None
    dt = _iso_to_dt(s)                     # 先试服务端格式（…Z）
    if dt is not None:
        return dt
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def help_to_dict(conn, row, now_iso=None):
    """求助事件的对外表示（docs/06 §7.4）。

    ★ 键集合是**契约**（TC-W3-12：少一键即失败），不要随意增删。
    """
    now_iso = now_iso or utc_now_iso()
    dev = device_effective_status(conn, row["device_id"])
    return {
        "help_id": row["help_id"],
        "device_id": row["device_id"],
        "status": row["status"],
        "trigger_type": row["trigger_type"],
        "trigger_up_ms": row["trigger_up_ms"],
        "trigger_at_utc": row["trigger_at_utc"],
        "queued_at_utc": row["queued_at_utc"],
        "received_at_utc": row["received_at_utc"],
        "local_feedback": row["local_feedback"],
        "local_feedback_at_utc": row["local_feedback_at_utc"],
        "local_feedback_received_at_utc": row["local_feedback_received_at_utc"],
        "pending_local": bool(row["pending_local"]),
        "viewed_at_utc": row["viewed_at_utc"],
        "responded_at_utc": row["responded_at_utc"],
        # ★★ 前端渲染"对方已收到"的**唯一**判据就是这个字段非空（TC-W3-20(c)）
        "remote_acked_at_utc": row["remote_acked_at_utc"],
        "accepted_by": row["accepted_by"],
        "cancel_reason": row["cancel_reason"],
        "created_at_utc": row["created_at_utc"],
        "cancelled_at_utc": row["cancelled_at_utc"],
        "message": row["message"],
        "boot_count": row["boot_count"],
        "device": dev,
        "age_ms": _help_age_ms(row, now_iso),
    }


def list_help_events(device_id=None, status=None, limit="50"):
    """查看者列表（docs/06 §7.4）。

    ★ **绝不**在这里推导/回填 remote_acked_at_utc：它只有 device_ack 里
      ack_type='help_ack' 那一条路径能写。列表里它是 NULL 就如实是 NULL。
    """
    try:
        lim = max(1, min(int(limit or 50), 500))
    except (TypeError, ValueError):
        lim = 50
    with _db_lock:
        conn = db_connect()
        try:
            sql = "SELECT * FROM help_events WHERE 1=1"
            args = []
            if device_id:
                sql += " AND device_id=?"
                args.append(device_id)
            if status:
                sql += " AND status=?"
                args.append(status)
            sql += " ORDER BY created_at_utc DESC, rowid DESC LIMIT ?"
            args.append(lim)
            rows = conn.execute(sql, args).fetchall()
            now = utc_now_iso()
            return HTTPStatus.OK, _ok({
                "count": len(rows),
                "help_events": [help_to_dict(conn, r, now) for r in rows],
            })
        finally:
            conn.close()


def get_help_event(help_id, mark_viewed=True, actor=None):
    """求助详情。

    ★ 默认**打开即记为 viewed**（docs/06 §2.2 状态机 created→uploaded→acked→viewed，
      TC-W3-13：查看者打开详情必须写 1 行 help_actions(action='viewed') 且
      viewed_at_utc 非空）。状态只前进不回退 —— 已经是 responded/cancelled 的
      事件不会被这条查看动作拉回去。

    ★ 只读不改的场景（例如测试脚本只想取值）可传 mark_viewed=False。
    """
    with _db_lock:
        conn = db_connect()
        try:
            row = conn.execute("SELECT * FROM help_events WHERE help_id=?",
                               (help_id,)).fetchone()
            if row is None:
                return HTTPStatus.NOT_FOUND, _error(
                    "REQUEST_NOT_FOUND", "help_id 不存在或已被清理")

            now = utc_now_iso()
            if mark_viewed and row["status"] not in HELP_FINAL_STATUSES:
                # 幂等：viewed_at_utc 只写第一次；重复打开不重复记动作行
                if row["viewed_at_utc"] is None:
                    conn.execute(
                        "UPDATE help_events SET viewed_at_utc=? WHERE help_id=?",
                        (now, help_id))
                    if row["status"] in ("created", "uploaded", "acked"):
                        conn.execute(
                            "UPDATE help_events SET status='viewed' WHERE help_id=?",
                            (help_id,))
                    conn.execute(
                        """INSERT INTO help_actions
                           (help_id, action, actor, at_utc, detail_json)
                           VALUES (?,?,?,?,NULL)""",
                        (help_id, "viewed", actor, now))
                    conn.commit()
                    row = conn.execute("SELECT * FROM help_events WHERE help_id=?",
                                       (help_id,)).fetchone()

            data = help_to_dict(conn, row, now)
            acts = conn.execute(
                """SELECT action, actor, at_utc FROM help_actions
                   WHERE help_id=? ORDER BY id ASC""", (help_id,)).fetchall()
            data["actions"] = [{"action": a["action"], "actor": a["actor"],
                                "at_utc": a["at_utc"]} for a in acts]
            # 已有的 help_ack / clear_alert 指令（用于核对"已下发但未确认"）
            cmds = conn.execute(
                """SELECT type, request_id, issued_at_utc, dispatched_at_utc, taken_count
                   FROM pending_commands WHERE request_id=?""",
                ("helpcmd-%s" % help_id,)).fetchall()
            data["pending_commands"] = [
                {"type": c["type"], "request_id": c["request_id"],
                 "issued_at_utc": c["issued_at_utc"],
                 "dispatched_at_utc": c["dispatched_at_utc"],
                 "taken_count": c["taken_count"]} for c in cmds]
            return HTTPStatus.OK, _ok(data)
        finally:
            conn.close()


def _queue_help_command(conn, device_id, help_id, cmd_type, nonce=None):
    """把 help_ack / clear_alert 排进设备指令队列。

    ★ 关键推论（docs/06 §4.5）：**"放进队列" ≠ "已送达"**。
      本函数只入队；`remote_acked_at_utc` 必须等设备轮询取走并
      `POST /device/ack`（ack_type=help_ack）之后才由 device_ack 写入。

    ★ request_id 用 `helpcmd-<help_id>` 前缀：pending_commands 有
      UNIQUE(device_id, request_id, type)，两种指令各占一行、互不覆盖。
    """
    rid = "helpcmd-%s" % help_id
    conn.execute(
        """INSERT OR REPLACE INTO pending_commands
           (device_id, request_id, type, nonce, sensor, timeout_ms, issued_at_utc,
            attempt, dispatched_at_utc, taken_at_utc, taken_count)
           VALUES (?,?,?,?,?,?,?,1,NULL,NULL,0)""",
        (device_id, rid, cmd_type, nonce, None, HELP_UPLOAD_TIMEOUT_MS, utc_now_iso()))
    return rid


def _latest_help_ack_ctx(conn, help_id):
    """取该 help_id 关联的 help_ack 指令的 nonce（供 ack 校验；没有则为 None）。"""
    r = conn.execute(
        """SELECT nonce FROM pending_commands
           WHERE request_id=? AND type='help_ack' LIMIT 1""",
        ("helpcmd-%s" % help_id,)).fetchone()
    return r["nonce"] if r is not None else None


def respond_help_event(help_id, payload):
    """查看者回应「我看到了」（docs/06 §7.2）。

    ★★ 本响应**不代表设备已知晓**。`remote_acked_at_utc` 在这一步**必须**是 null，
       它只能由设备 ack（ack_type=help_ack）写入 —— 这是当堂验证第 2 项的判定核心。
    """
    payload = payload or {}
    actor = payload.get("actor")
    message = payload.get("message")
    with _db_lock:
        conn = db_connect()
        try:
            row = conn.execute("SELECT * FROM help_events WHERE help_id=?",
                               (help_id,)).fetchone()
            if row is None:
                return HTTPStatus.NOT_FOUND, _error(
                    "REQUEST_NOT_FOUND", "help_id 不存在或已被清理")
            if row["status"] in HELP_FINAL_STATUSES:
                return HTTPStatus.CONFLICT, _error(
                    "REQUEST_ALREADY_FINAL",
                    "该求助已结束（%s），不能再回应" % row["status"])

            now = utc_now_iso()
            # 状态推进（不回退）：cancelled/expired 已排除，其余一律 → responded
            conn.execute(
                """UPDATE help_events
                   SET status='responded', responded_at_utc=COALESCE(responded_at_utc,?),
                       accepted_by=COALESCE(accepted_by,?)
                   WHERE help_id=?""", (now, actor, help_id))
            if message is not None:
                conn.execute("UPDATE help_events SET message=? WHERE help_id=?",
                             (message, help_id))
            conn.execute(
                """INSERT INTO help_actions (help_id, action, actor, at_utc, detail_json)
                   VALUES (?,?,?,?,?)""",
                (help_id, "responded", actor, now,
                 json.dumps({"message": message}, ensure_ascii=False) if message else None))
            # 自动生成 help_ack 指令 —— 但请注意：**入队 ≠ 送达**
            _queue_help_command(conn, row["device_id"], help_id, "help_ack")
            add_device_event(conn, row["device_id"], "help_responded", "operator",
                             {"help_id": help_id, "actor": actor})
            conn.commit()

            fresh = conn.execute("SELECT * FROM help_events WHERE help_id=?",
                                 (help_id,)).fetchone()
            return HTTPStatus.OK, _ok({
                "help_id": help_id,
                "status": "responded",
                "responded_at_utc": fresh["responded_at_utc"],
                "remote_ack_command": "help_ack",
                # ★ 恒为 null（此刻设备还没取走指令）—— 放开这个字段造成"假已收到"
                #   是本周最严重的一类缺陷
                "remote_acked_at_utc": fresh["remote_acked_at_utc"],
                "accepted_by": fresh["accepted_by"],
                "note": "已记录回应，并已向设备下发 help_ack；"
                        "设备确认后 remote_acked_at_utc 才会写入",
            })
        finally:
            conn.close()


def cancel_help_event(help_id, payload):
    """取消求助（docs/06 §7.3）—— 取消逻辑 C1–C4 的落地点。

    C1 只有佩戴者侧可发起（教学环境以操作者声明为准，记 actor）
    C2 任意非终态都可取消（created/uploaded/acked/viewed/responded → cancelled）
    C3 取消**不删除**任何记录，`cancel_reason` 必填（缺则 400，且不改写状态）
    C4 终态重复取消 → 200 + deduplicated:true（幂等，不报错）
    """
    payload = payload or {}
    actor = payload.get("actor")
    reason = payload.get("reason")
    if not reason or not isinstance(reason, str) or not reason.strip():
        # C3：cancel_reason 必填。★ 此时**绝不**改写状态（TC-W3-23(b)）
        return HTTPStatus.BAD_REQUEST, _error(
            "MISSING_FIELD", "缺少必填字段 reason（取消原因必须填写，记录全程留痕）")

    with _db_lock:
        conn = db_connect()
        try:
            row = conn.execute("SELECT * FROM help_events WHERE help_id=?",
                               (help_id,)).fetchone()
            if row is None:
                return HTTPStatus.NOT_FOUND, _error(
                    "REQUEST_NOT_FOUND", "help_id 不存在或已被清理")

            # C4：终态重复取消 → 幂等 200，不新增动作行、时间戳一字不改
            if row["status"] in HELP_FINAL_STATUSES:
                conn.commit()
                return HTTPStatus.OK, _ok({
                    "help_id": help_id,
                    "status": row["status"],
                    "cancelled_at_utc": row["cancelled_at_utc"],
                    "cancel_reason": row["cancel_reason"],
                    "device_command": None,
                    "deduplicated": True,
                    "note": "该求助已处于终态（%s），本次取消为幂等重复，未做任何改动"
                            % row["status"],
                })

            # C2：非终态 → cancelled
            now = utc_now_iso()
            conn.execute(
                """UPDATE help_events
                   SET status='cancelled', cancelled_at_utc=COALESCE(cancelled_at_utc,?),
                       cancel_reason=?
                   WHERE help_id=?""", (now, reason, help_id))
            conn.execute(
                """INSERT INTO help_actions (help_id, action, actor, at_utc, detail_json)
                   VALUES (?,?,?,?,?)""",
                (help_id, "cancelled", actor, now,
                 json.dumps({"reason": reason}, ensure_ascii=False)))
            # 取消时把可能还在队列里的 help_ack 作废：已取消的求助不该再让灯转绿
            conn.execute(
                """DELETE FROM pending_commands
                   WHERE request_id=? AND type='help_ack'""", ("helpcmd-%s" % help_id,))
            # 下发 clear_alert：设备取走后停蜂鸣、LED 回待机色
            _queue_help_command(conn, row["device_id"], help_id, "clear_alert")
            add_device_event(conn, row["device_id"], "help_cancelled", "operator",
                             {"help_id": help_id, "actor": actor, "reason": reason})
            conn.commit()

            fresh = conn.execute("SELECT * FROM help_events WHERE help_id=?",
                                 (help_id,)).fetchone()
            return HTTPStatus.OK, _ok({
                "help_id": help_id,
                "status": "cancelled",
                "cancelled_at_utc": fresh["cancelled_at_utc"],
                "cancel_reason": fresh["cancel_reason"],
                "cancelled_by": actor,
                "device_command": "clear_alert",
                "deduplicated": False,
                "note": "已下发 clear_alert；设备收到后停止蜂鸣并复位指示灯",
            })
        finally:
            conn.close()


def mark_help_viewed(help_id, actor=None):
    """查看者打开详情 → viewed（记录动作流水，状态不回退）。"""
    with _db_lock:
        conn = db_connect()
        try:
            row = conn.execute("SELECT * FROM help_events WHERE help_id=?",
                               (help_id,)).fetchone()
            if row is None:
                return HTTPStatus.NOT_FOUND, _error(
                    "REQUEST_NOT_FOUND", "help_id 不存在或已被清理")
            if row["status"] in HELP_FINAL_STATUSES:
                return HTTPStatus.OK, _ok({"help_id": help_id, "status": row["status"],
                                           "viewed": False,
                                           "note": "终态事件只读，不再记录 viewed"})
            now = utc_now_iso()
            conn.execute(
                "UPDATE help_events SET viewed_at_utc=COALESCE(viewed_at_utc,?) WHERE help_id=?",
                (now, help_id))
            # 状态机：created→uploaded→acked→viewed，只前进
            if row["status"] in ("created", "uploaded", "acked"):
                conn.execute("UPDATE help_events SET status='viewed' WHERE help_id=?",
                             (help_id,))
            conn.execute(
                """INSERT INTO help_actions (help_id, action, actor, at_utc, detail_json)
                   VALUES (?,?,?,?,NULL)""", (help_id, "viewed", actor, now))
            conn.commit()
            fresh = conn.execute("SELECT status FROM help_events WHERE help_id=?",
                                 (help_id,)).fetchone()
            return HTTPStatus.OK, _ok({"help_id": help_id, "status": fresh["status"],
                                       "viewed": True, "viewed_at_utc": now})
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
                    # pause_reports / resume_reports / help_ack / clear_alert：
                    # 取走即视为已下发，直接出队。
                    # ★ 注意：出队只说明"设备取到了指令"，**不等于**设备已执行 ——
                    #   真正的确认要走 POST /device/ack（见 device_ack 里的 help_ack 分支）。
                    is_help_cmd = r["type"] in ("help_ack", "clear_alert")
                    conn.execute("DELETE FROM pending_commands WHERE id=?", (r["id"],))
                    # 记录下发时刻，供核对"回应 → 设备取到指令"的时间线
                    if is_help_cmd:
                        hid = (r["request_id"] or "")[len("helpcmd-"):] or None
                        add_device_event(conn, device_id, "help_command_taken", "device",
                                         {"help_id": hid, "type": r["type"],
                                          "request_id": r["request_id"]})
                    commands.append({
                        "request_id": r["request_id"],
                        "type": r["type"],
                        # help 指令带上 nonce（设备 ack 时回传，后端据此绑定"同一次")
                        "nonce": r["nonce"],
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


def _device_ack_help_ack(payload, remote_addr, nonce, attempt, boot_count, up_ms, reason):
    """设备确认已收到「查看者回应」（docs/06 §4.2 的 help_ack 行）。

    ★★★ 这是全工程**唯一**允许写入 `remote_acked_at_utc` 的地方。
       写在这里 = 设备真的把 help_ack 指令取走并回了执，
       于是页面才被允许显示「佩戴者设备已收到回应」（TC-W3-17/20）。

    ★ 幂等（TC-W3-17(d)）：重复 ack → 200 + deduplicated:true，
      `remote_acked_at_utc` **逐字符不变**，绝不被第二次 ack 的时间覆盖。
    """
    payload = payload or {}
    device_id = payload.get("device_id")
    request_id = payload.get("request_id")
    if not device_id or not request_id:
        return HTTPStatus.BAD_REQUEST, _error(
            "MISSING_FIELD", "缺少必填字段 device_id/request_id")

    # request_id 形如 helpcmd-<help_id>；也接受直接传 help_id（健壮性）
    help_id = payload.get("help_id")
    if not help_id:
        if request_id.startswith("helpcmd-"):
            help_id = request_id[len("helpcmd-"):]
        else:
            help_id = request_id

    with _db_lock:
        conn = db_connect()
        try:
            row = conn.execute("SELECT * FROM help_events WHERE help_id=?",
                               (help_id,)).fetchone()
            if row is None:
                return HTTPStatus.NOT_FOUND, _error(
                    "REQUEST_NOT_FOUND", "help_id 不存在: %s" % help_id)

            # 幂等：已经确认过就直接返回原值，时间戳一字不改
            if row["remote_acked_at_utc"] is not None:
                conn.commit()
                return HTTPStatus.OK, _ok({
                    "request_id": request_id, "help_id": help_id,
                    "status": row["status"],
                    "remote_acked_at_utc": row["remote_acked_at_utc"],
                    "deduplicated": True,
                })

            now = utc_now_iso()
            conn.execute(
                "UPDATE help_events SET remote_acked_at_utc=? WHERE help_id=?",
                (now, help_id))
            # 同名回执落 request_acks，便于与 collect_now 的回执统一检索
            #   （唯一键 (request_id, ack_type, attempt)，重复即冲突 → 前面已 return）
            try:
                conn.execute(
                    """INSERT INTO request_acks
                       (request_id, device_id, ack_type, ack_at_utc, device_uptime_ms,
                        boot_count, attempt, reason, detail_json, remote_addr)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (request_id, device_id, "help_ack", now, up_ms, boot_count, attempt,
                     reason, json.dumps({"help_id": help_id}, ensure_ascii=False),
                     remote_addr))
            except sqlite3.IntegrityError:
                # 已存在同名回执行 → 属于重复，不影响本次语义
                pass
            # 队列里若还有未取走的 help_ack，清掉（已确认，无需重复下发）
            conn.execute("DELETE FROM pending_commands WHERE request_id=? AND type='help_ack'",
                         (request_id,))
            add_device_event(conn, device_id, "help_remote_acked", "device",
                             {"help_id": help_id, "request_id": request_id})
            device_touch(conn, device_id, boot_count=boot_count, uptime_ms=up_ms)
            conn.commit()
            return HTTPStatus.OK, _ok({
                "request_id": request_id, "help_id": help_id,
                "status": row["status"],
                # ★ 这是权威写入值：等于服务端接收时刻（TC-W3-17(b)）
                "remote_acked_at_utc": now,
                "deduplicated": False,
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
    if ack_type not in ("received", "started", "rejected", "superseded", "help_ack"):
        return HTTPStatus.BAD_REQUEST, _error("MISSING_FIELD", "ack_type 非法: %s" % ack_type)

    nonce = payload.get("nonce")
    try:
        attempt = int(payload.get("attempt") or 1)
    except (TypeError, ValueError):
        attempt = 1
    reason = payload.get("reason")
    boot_count = payload.get("boot_count")
    up_ms = payload.get("up_ms")

    # ---- 第 3 周：help_ack 走**独立**分支 ------------------------------------
    #   ★ 为什么不在下面 collect_requests 的查询里混着处理：help_ack 的
    #     request_id 是 `helpcmd-<help_id>` 而不是 collect_requests 的主键，
    #     混在一起会 404 REQUEST_NOT_FOUND，把"设备已确认"误报成"请求不存在"。
    if ack_type == "help_ack":
        return _device_ack_help_ack(payload, remote_addr, nonce, attempt,
                                    boot_count, up_ms, reason)

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
# 第 4 周：摄像头 —— 预览帧（内存最新一帧）+ 照片（落盘留痕）
#
# ★ 三条不可突破的原则：
#   1) 预览帧**不落库、不回放**：内存里只保留"该设备最新一帧"。服务重启后无画面，
#      网页如实显示"还没有画面"——绝不拿旧帧当实时画面。
#   2) 照片必须**落盘 + 记录 SHA256**：它是用户主动触发的证据，要能下载、能逐字节核对。
#   3) 设备上报的宽高/序号只作为**元数据**记录，不据此编造任何图像内容。
# ===========================================================================
_cam_lock = threading.Lock()
_cam_latest = {}      # device_id -> {jpeg:bytes, seq:int, w:int, h:int, received_at_utc:str,
                      #               bytes:int, remote:str, frame_no:int}
_cam_capture = {}     # device_id -> 请求拍照的时刻（time.monotonic）
_cam_state = {}       # device_id -> 设备自报的摄像头状态（ready/pid/error/at）
_cam_frame_counter = 0  # 后端累计收到的帧数（只用于诊断，不参与判新旧）


def _cam_norm_device(device_id):
    """把 device_id 规范化成一个安全、有界的键（空值不参与合并）。"""
    return (device_id or "").strip()[:64] or "unknown"


def _jpeg_looks_valid(data):
    """只做最基本的完整性检查：SOI(FFD8) 开头、EOI(FFD9) 结尾。

    ★ 这不是"图片识别"，也不判断内容；只是防止把半截 TCP 报文或任意二进制
      当成画面存下来 —— 那种"残帧"在网页上会显示成灰块/裂图，比不显示更误导。
    """
    return (len(data) > 4 and data[0] == 0xFF and data[1] == 0xD8
            and data[-2] == 0xFF and data[-1] == 0xD9)


def cam_store_frame(device_id, jpeg, seq, w, h, remote):
    """保存最新预览帧；返回 (是否要求设备拍照, 该帧是否比上一帧新, 待下发的快照请求)。"""
    global _cam_frame_counter
    dev = _cam_norm_device(device_id)
    now = utc_now_iso()
    with _cam_lock:
        _cam_frame_counter += 1
        prev = _cam_latest.get(dev)
        is_new = (prev is None) or (prev["seq"] != seq) or (prev["bytes"] != len(jpeg))
        _cam_latest[dev] = {
            "device_id": dev,
            "jpeg": jpeg,
            "seq": seq,
            "w": w,
            "h": h,
            "received_at_utc": now,
            "bytes": len(jpeg),
            "remote": remote,
            "frame_no": _cam_frame_counter,
        }
        want_photo = _cam_capture.pop(dev, None)
        # ★ 第 7 周：快照请求也搭在这一条响应里（与拍照同一个道理：设备在 NAT 后面，
        #   只有这一条 5Hz 的下行"便车"可用）。不同之处是它带 id 与 mode，
        #   设备取完图必须带着这个 id 回来，服务端才认。
        snap = snapshot_take_pending(dev)
    if snap is not None:
        # 标记"请求确实下发到了设备"——这是服务端可观测的事实，不是推断。
        snapshot_mark_delivered(snap["id"], seq)
    if want_photo is None:
        return False, is_new, snap
    # 超时的拍照请求直接丢弃：不在几秒后"补拍"一张让人误解时间点的照片
    if (time.monotonic() - want_photo) > CAM_CAPTURE_TTL_S:
        print("[cam] 设备 %s 的拍照请求已超时(%.1fs)，丢弃" % (dev, CAM_CAPTURE_TTL_S))
        return False, is_new, snap
    return True, is_new, snap


def cam_latest_frame(device_id):
    """取最新帧（返回副本，避免调用方在读的同时被新帧替换）。"""
    dev = _cam_norm_device(device_id)
    with _cam_lock:
        item = _cam_latest.get(dev)
        if item is None:
            return None
        out = dict(item)
        return out


def cam_all_devices():
    with _cam_lock:
        return sorted(_cam_latest.keys())


def cam_report_state(payload):
    """设备启动时自报摄像头状态（ready/pid/error）。

    ★ 只作为**诊断信息**保存，绝不参与"有没有画面"的判断：
      画面是否存在只由 /cam/frame 是否真的收到帧决定。
    """
    if not isinstance(payload, dict):
        return HTTPStatus.BAD_REQUEST, _error("BAD_REQUEST", "报文必须是 JSON 对象")
    device_id = payload.get("device_id")
    if not device_id:
        return HTTPStatus.BAD_REQUEST, _error("BAD_REQUEST", "缺少 device_id")
    dev = _cam_norm_device(device_id)
    rec = {
        "device_id": dev,
        "ready": bool(payload.get("ready")),
        "pid": payload.get("pid"),
        "error": payload.get("error") or "",
        "w": payload.get("w"),
        "h": payload.get("h"),
        "reported_at_utc": utc_now_iso(),
    }
    with _cam_lock:
        _cam_state[dev] = rec
    print("[cam] 设备 %s 自报摄像头状态: ready=%s pid=%s error=%s"
          % (dev, rec["ready"], rec["pid"], rec["error"] or "-"))
    return HTTPStatus.OK, {"ok": True, "device_id": dev, "ready": rec["ready"]}


def cam_request_capture(device_id):
    """网页点「拍照」：打一个待拍照标记，等设备下一次上传帧时通过响应带下去。"""
    dev = _cam_norm_device(device_id)
    with _cam_lock:
        _cam_capture[dev] = time.monotonic()
        has_frame = dev in _cam_latest
    return has_frame


def cam_save_photo(device_id, jpeg, w, h, seq, remote):
    """把照片写入磁盘并落库；返回记录 dict。"""
    dev = _cam_norm_device(device_id)
    now_dt = datetime.now(timezone.utc)
    photo_id = now_dt.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:6]
    rel_name = "%s/%s.jpg" % (dev, photo_id)
    abs_path = os.path.join(CAM_DIR, dev, photo_id + ".jpg")
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)

    # ★ 先写临时文件再原子改名：避免"网页恰好在这时来下载"读到半截 JPEG
    tmp_path = abs_path + ".part"
    with open(tmp_path, "wb") as f:
        f.write(jpeg)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, abs_path)

    digest = hashlib.sha256(jpeg).hexdigest()
    with _db_lock:
        conn = db_connect()
        try:
            conn.execute(
                """INSERT INTO cam_photos
                   (photo_id, device_id, created_at_utc, width, height,
                    byte_len, sha256, seq, remote_addr, filename)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (photo_id, dev, now_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                 w, h, len(jpeg), digest, seq, remote, rel_name))
            conn.commit()
        finally:
            conn.close()
    print("[cam] 收到照片 %s: %s %dx%d %d 字节 sha256=%s"
          % (dev, photo_id, w, h, len(jpeg), digest[:16]))
    return {
        "photo_id": photo_id,
        "device_id": dev,
        "created_at_utc": now_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "width": w, "height": h, "byte_len": len(jpeg),
        "sha256": digest, "seq": seq,
        "url": "/api/v1/cam/photo/%s.jpg" % photo_id,
        "filename": rel_name,
    }


def cam_list_photos(device_id=None, limit=20):
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    with _db_lock:
        conn = db_connect()
        try:
            if device_id:
                rows = conn.execute(
                    """SELECT * FROM cam_photos WHERE device_id=?
                       ORDER BY created_at_utc DESC, rowid DESC LIMIT ?""",
                    (_cam_norm_device(device_id), limit)).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM cam_photos
                       ORDER BY created_at_utc DESC, rowid DESC LIMIT ?""",
                    (limit,)).fetchall()
            items = [{
                "photo_id": r["photo_id"],
                "device_id": r["device_id"],
                "created_at_utc": r["created_at_utc"],
                "width": r["width"], "height": r["height"],
                "byte_len": r["byte_len"], "sha256": r["sha256"],
                "seq": r["seq"],
                "url": "/api/v1/cam/photo/%s.jpg" % r["photo_id"],
            } for r in rows]
            return HTTPStatus.OK, {"ok": True, "count": len(items), "photos": items}
        finally:
            conn.close()


def cam_get_photo_path(photo_id):
    """按 photo_id 查磁盘路径（ID 只允许安全字符，杜绝路径穿越）。"""
    import re
    if not re.fullmatch(r"[0-9A-Za-z\-]{8,80}", photo_id or ""):
        return None, None
    with _db_lock:
        conn = db_connect()
        try:
            row = conn.execute("SELECT * FROM cam_photos WHERE photo_id=?",
                               (photo_id,)).fetchone()
        finally:
            conn.close()
    if row is None:
        return None, None
    rel = row["filename"]
    base = os.path.abspath(CAM_DIR)
    full = os.path.abspath(os.path.join(base, rel))
    if not full.startswith(base + os.sep):
        return None, None
    return row, full


# ===========================================================================
# 第 7 周：按需环境快照
#
# 本周要回答的一句话是：
#   「用户请求查看环境时，系统如何说明采集是否成功，采到的是哪一时刻的画面？」
#
# 于是"查看环境"被做成一件事，而不是一个按钮 + 一张图：
#   ① 一次请求生成一个 snapshot_id，**图像必须带着这个 id 回来**才被认账；
#   ② 状态机把"有没有拍到"拆成可核对的中间态，失败是**明确的终态**，不是空图；
#   ③ 三个时刻分开记：请求时刻（服务端精确）/ 快门时刻（设备侧，估计值）/
#      到达时刻（服务端精确）。界面说"哪一刻的画面"时引用的是快门时刻；
#   ④ counts_as_live 只对"真机 + 本次请求 + 新取的帧"为 1：
#      回放（真实历史图像）与协议模拟一律为 0 —— 演示可以很像，但不能充数。
#
# ★ 三条红线（与第 4 周的"不造假"一脉相承，但更严）：
#   R1 陈旧帧不算本次采集：上传的设备帧序号必须**严格大于**请求时刻的序号。
#      丢这一点，"上次成功图像被标成本次成功"就会悄悄发生。
#   R2 超时/取消之后到达的图像**不许改写终局**，只能另存为「迟到图像」并标注。
#   R3 取不到本次图像时，接口**不回退**到上一张成功的图 —— 宁可 409 说清楚。
# ===========================================================================
_SNAP_STATES = ("pending", "requested", "capturing", "ok", "occluded",
                "failed", "timeout", "cancelled", "replayed")
_SNAP_OPEN = ("pending", "requested", "capturing")          # 还在等设备
_SNAP_HAVE_IMAGE = ("ok", "occluded", "replayed")           # 有图像可看
_SNAP_TRIGGERS = ("user_request", "teaching_event", "replay")
_SNAP_MODES = ("qvga", "uxga")

_snap_lock = threading.Lock()
# device_id -> {"id": snapshot_id, "mode": "qvga"/"uxga"}（等着随下一帧的响应下发到设备）
_snap_pending = {}


def _snap_parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _snap_new_id():
    return "snp_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + \
           "-" + uuid.uuid4().hex[:6]


def _snap_safe_id(sid):
    import re
    return bool(re.fullmatch(r"snp_[0-9A-Za-z\-]{6,64}", sid or ""))


def _snap_save_file(dev, sid, jpeg, suffix=""):
    """把图像写进 SNAPSHOT_DIR/<dev>/<sid><suffix>.jpg；先写 .part 再原子改名。"""
    folder = os.path.join(SNAPSHOT_DIR, dev)
    os.makedirs(folder, exist_ok=True)
    name = sid + suffix + ".jpg"
    abs_path = os.path.join(folder, name)
    tmp = abs_path + ".part"
    with open(tmp, "wb") as f:
        f.write(jpeg)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, abs_path)
    return "%s/%s" % (dev, name), abs_path


def _snap_row(row):
    """DB 行 → dict（含派生字段与一句人能读懂的说明）。"""
    if row is None:
        return None
    d = dict(row)
    d["is_replay"] = bool(d.get("is_replay"))
    d["simulated"] = bool(d.get("simulated"))
    d["counts_as_live"] = bool(d.get("counts_as_live"))
    d["captured_is_est"] = bool(d.get("captured_is_est"))
    d["image_pruned"] = bool(d.get("image_pruned"))
    q = None
    if d.get("quality_json"):
        try:
            q = json.loads(d["quality_json"])
        except (ValueError, TypeError):
            q = None
    d["quality"] = q
    d.pop("quality_json", None)
    d["has_image"] = bool(d.get("filename")) and not d["image_pruned"]
    d["image_url"] = ("/api/v1/cam/snapshot/%s/image" % d["snapshot_id"]
                      if d["has_image"] else None)
    d["late_image_url"] = ("/api/v1/cam/snapshot/%s/image?late=1" % d["snapshot_id"]
                           if d.get("late_filename") else None)
    d["explain"] = _snap_explain(d)
    return d


def _snap_explain(d):
    """一句话说清"这一次到底有没有拍到"。★ 保证前端不用自己编文案。"""
    s = d.get("state")
    t = lambda x: (x or "").replace("T", " ").replace("Z", "Z")   # noqa: E731
    if s == "pending":
        return ("已登记这次请求，正在等设备上传下一帧——请求会随那一帧的响应下发"
                "（设备在线时通常 <500ms）。此刻还没有任何画面属于这次请求。")
    if s == "requested":
        return ("请求已在 %s 下发给设备，等它取图并上传。此刻还没有任何画面属于这次请求。"
                % t(d.get("delivered_at_utc")))
    if s == "capturing":
        return "设备已在 %s 开始取图（它自报的），图像还没到后端。" % t(d.get("begin_at_utc"))
    if s == "ok":
        est = "（估计值）" if d.get("captured_is_est") else ""
        return ("采集成功：画面在 %s%s 拍下，%s 到达后端，端到端 %s ms。"
                % (t(d.get("captured_at_utc")), est, t(d.get("received_at_utc")),
                   d.get("latency_total_ms")))
    if s == "occluded":
        note = (d.get("quality_note") or "画面不可用").rstrip("。")
        return ("画面采到了，但质量判定为不可用：%s。"
                "这不算一次成功的环境查看 —— 请确认镜头没有被遮挡、对着的是要看的场景。" % note)
    if s == "failed":
        return "采集失败：%s（本次没有画面）。" % (d.get("error_message") or d.get("error_code") or "未知原因")
    if s == "timeout":
        return ("采集超时：%.0f 秒内设备没有送回图像，本次没有画面。"
                "系统不会把上一次成功的画面当作这次的结果。"
                % SNAPSHOT_TIMEOUT_S)
    if s == "cancelled":
        return ("这次请求在 %s 已取消，本次没有画面。设备之后若送回了图像，会另存为"
                "「迟到图像」并标注，不算这次的结果。" % t(d.get("requested_at_utc")))
    if s == "replayed":
        return ("回放：这是 %s 采集的历史图像，不是本次采集。它不计入"
                "「最近一次成功采集」。"
                % t(d.get("captured_at_utc") or d.get("requested_at_utc")))
    return "状态未知：%s" % s


def snapshot_get(sid):
    if not _snap_safe_id(sid):
        return None
    with _db_lock:
        conn = db_connect()
        try:
            row = conn.execute("SELECT * FROM cam_snapshots WHERE snapshot_id=?",
                               (sid,)).fetchone()
        finally:
            conn.close()
    return _snap_row(row)


def _snap_update(sid, **fields):
    """按字段增量更新一行（同时 version+1）。空 dict 直接返回。"""
    if not fields:
        return
    cols = ", ".join("%s=?" % k for k in fields)
    vals = list(fields.values()) + [sid]
    with _db_lock:
        conn = db_connect()
        try:
            conn.execute("UPDATE cam_snapshots SET %s, version=version+1 "
                         "WHERE snapshot_id=?" % cols, vals)
            conn.commit()
        finally:
            conn.close()


def snapshot_create(payload, remote=""):
    """用户/教学请求一次按需采集。返回 (HTTP status, body)。"""
    if not isinstance(payload, dict):
        return HTTPStatus.BAD_REQUEST, _error("BAD_REQUEST", "报文必须是 JSON 对象")
    device_id = payload.get("device_id")
    if not device_id:
        return HTTPStatus.BAD_REQUEST, _error("BAD_REQUEST", "缺少 device_id")
    dev = _cam_norm_device(device_id)

    mode = (payload.get("mode") or "qvga").lower()
    if mode not in _SNAP_MODES:
        return HTTPStatus.BAD_REQUEST, _error(
            "SNAPSHOT_BAD_MODE", "mode 只能是 qvga 或 uxga（收到 %r）" % mode)
    trigger = payload.get("trigger") or "user_request"
    if trigger not in _SNAP_TRIGGERS:
        return HTTPStatus.BAD_REQUEST, _error(
            "SNAPSHOT_BAD_TRIGGER", "trigger 只能是 user_request / teaching_event")

    # 在途去重：同一台设备同一时刻只允许一次在途采集。
    #   ★ 不是性能考虑，是语义考虑：两次并发会让"哪张图属于哪次请求"变得不可判定。
    with _snap_lock:
        with _db_lock:
            conn = db_connect()
            try:
                row = conn.execute(
                    "SELECT * FROM cam_snapshots WHERE device_id=? AND state IN "
                    "('pending','requested','capturing') "
                    "ORDER BY requested_at_utc DESC LIMIT 1", (dev,)).fetchone()
            finally:
                conn.close()
        if row is not None:
            cur = _snap_row(row)
            resp = _error("SNAPSHOT_BUSY",
                          "该设备还有一次采集在路上（%s，状态 %s），不能并发再发一次"
                          % (cur["snapshot_id"], cur["state"]), retryable=True)
            resp["snapshot"] = cur
            return HTTPStatus.CONFLICT, resp

    frame = cam_latest_frame(dev)         # 不加 _snap_lock，避免与 cam 锁嵌套
    now = datetime.now(timezone.utc)
    now_iso = utc_now_iso()
    sid = _snap_new_id()
    has_stream = frame is not None
    state = "pending" if has_stream else "failed"

    rec = {
        "snapshot_id": sid,
        "device_id": dev,
        "state": state,
        "trigger_kind": trigger,
        "source": "live",
        "is_replay": 0, "simulated": 0, "counts_as_live": 0,
        "mode": mode,
        "requested_by": (payload.get("requested_by") or remote or "")[:120],
        "reason": (payload.get("reason") or "")[:500],
        "requested_at_utc": now_iso,
        "requested_seq": frame["seq"] if frame else None,
        "requested_frame_no": frame["frame_no"] if frame else None,
        "error_code": None if has_stream else "NO_FRAME_STREAM",
        "error_message": None if has_stream else
        "该设备当前没有在上传画面（从未收到过预览帧），请求无法下发给它",
        "version": 0,
    }
    with _db_lock:
        conn = db_connect()
        try:
            cols = ", ".join(rec.keys())
            qs = ", ".join("?" for _ in rec)
            conn.execute("INSERT INTO cam_snapshots (%s) VALUES (%s)" % (cols, qs),
                         list(rec.values()))
            conn.commit()
        finally:
            conn.close()

    if not has_stream:
        # ★ 失败也要有记录：本周要求"采集失败保持明确状态"，空手回 409 是不够的，
        #   历史里必须看得到"某人在某刻请求过，并且失败原因是设备没在上传画面"。
        out = snapshot_get(sid)
        resp = _error("NO_FRAME_STREAM",
                      "该设备当前没有在上传画面，这次请求不可能被送到设备上；"
                      "已记下一条失败记录（%s）。" % sid, retryable=True)
        resp["snapshot"] = out
        return HTTPStatus.CONFLICT, resp

    with _snap_lock:
        _snap_pending[dev] = {"id": sid, "mode": mode}
    print("[snap] 请求 %s: device=%s mode=%s trigger=%s（等设备下一帧的响应下发）"
          % (sid, dev, mode, trigger))
    return HTTPStatus.ACCEPTED, {
        "ok": True,
        "server_time_utc": now_iso,
        "snapshot": snapshot_get(sid),
        "note": "请求已登记。真正的快门在设备端，请求会随下一次帧上传的响应下发；"
                "图像带着 snapshot_id 回来才算这一次的（见 state 字段）。",
    }


def snapshot_take_pending(dev):
    """该设备当前要下发的快照请求（由 /cam/frame 的响应带下去）。

    ★ 不 pop —— 只要设备还没确认（状态仍是 pending/requested），**每一帧都带同一个 id**。
      第 7 周 D3：设备在 NAT 后面，下行只有这一趟便车。如果"挂上一次就摘掉"，
      那一帧的响应万一丢了（真机实测会 ESP_ERR_HTTP_CONNECT / -1），请求就凭空消失，
      前端只能显示"采集超时"，而设备其实一直好好的 —— 把链路抖动说成了设备问题。
      at-least-once 投递 + 设备按 id 去重，比"恰好一次"可信得多。
    ★ 状态一旦离开 pending/requested（设备 begin / 上传 / 失败 / 超时 / 取消），
      这里就把队列条目清掉，不会无限重发。
    """
    with _snap_lock:
        cur = _snap_pending.get(dev)
    if not cur:
        return None
    sid = cur.get("id")
    rec = snapshot_get(sid)
    if rec is None or rec["state"] not in ("pending", "requested"):
        with _snap_lock:
            cur2 = _snap_pending.get(dev)
            if cur2 and cur2.get("id") == sid:
                _snap_pending.pop(dev, None)
        return None
    return {"id": sid, "mode": rec.get("mode") or cur.get("mode")}


def snapshot_mark_delivered(sid, delivered_seq=None):
    """请求确实随帧响应发给了设备 —— 这是服务端可观测的事实，不是推断。

    ★ 顺手把 baseline_seq 抬到"下发那一帧"的序号：
      设备如果偷懒把**刚上传的那一帧**原样当成本次结果交回来，
      它的 seq 等于 baseline_seq，会被陈旧判据拦住。
      只有在下发之后**再取一帧**，seq 才会严格更大。
    """
    rec = snapshot_get(sid)
    if not rec or rec["state"] != "pending":
        return rec
    base = max(int(rec.get("requested_seq") or 0), int(delivered_seq or 0))
    _snap_update(sid, state="requested", delivered_at_utc=utc_now_iso(),
                 baseline_seq=base or None)
    return snapshot_get(sid)


def snapshot_require_open(sid, action):
    """公共前置：取记录并确认它还处于可被推进的状态。"""
    rec = snapshot_get(sid)
    if rec is None:
        return None, (HTTPStatus.NOT_FOUND, _error(
            "SNAPSHOT_NOT_FOUND", "没有这个快照请求: %s" % sid, retryable=False))
    if rec["state"] not in _SNAP_OPEN:
        return rec, None
    return rec, None


def snapshot_begin(sid, payload):
    """设备自报"我已经开始取图了"（best-effort，失败不影响主链路）。"""
    rec, err = snapshot_require_open(sid, "begin")
    if err:
        return err
    if rec["state"] in _SNAP_OPEN:
        _snap_update(sid, state="capturing", begin_at_utc=utc_now_iso())
        rec = snapshot_get(sid)
    return HTTPStatus.OK, {"ok": True, "snapshot_id": sid, "state": rec["state"]}


def snapshot_fail(sid, payload):
    """设备明确回报失败（取帧失败、相机重建失败……）。"""
    if not _snap_safe_id(sid):
        return HTTPStatus.NOT_FOUND, _error("SNAPSHOT_NOT_FOUND", "没有这个快照请求")
    rec = snapshot_get(sid)
    if rec is None:
        return HTTPStatus.NOT_FOUND, _error(
            "SNAPSHOT_NOT_FOUND", "没有这个快照请求: %s" % sid)
    if rec["state"] not in _SNAP_OPEN:
        resp = _error("SNAPSHOT_ALREADY_FINAL",
                      "这次请求已经收尾（%s），设备回报的失败不再改写终局" % rec["state"])
        resp["snapshot"] = rec
        return HTTPStatus.CONFLICT, resp
    code = ((payload or {}).get("error_code") or "CAPTURE_FAILED")[:64]
    msg = ((payload or {}).get("error_message") or "设备取图失败（未说明原因）")[:400]
    _snap_update(sid, state="failed", error_code=code, error_message=msg,
                 received_at_utc=utc_now_iso(),
                 latency_total_ms=int((datetime.now(timezone.utc)
                                       - (_snap_parse_iso(rec["requested_at_utc"])
                                          or datetime.now(timezone.utc))).total_seconds() * 1000))
    print("[snap] %s 设备回报失败: %s / %s" % (sid, code, msg))
    return HTTPStatus.OK, {"ok": True, "snapshot": snapshot_get(sid)}


def snapshot_accept_image(sid, jpeg, meta, remote=""):
    """设备把这一次的图像送回来（body = 原始 JPEG）。

    ★ 这是本周所有红线集中落地的位置：
      · 序号不比请求时刻新 → 拒绝（陈旧帧不得算本次采集）
      · 已经收尾（超时/取消） → 只存为「迟到图像」，终局一个字段都不改
      · 已经成功过 → 409，后到的图不许覆盖已确认的那一张
    """
    if not _snap_safe_id(sid):
        return HTTPStatus.NOT_FOUND, _error("SNAPSHOT_NOT_FOUND", "没有这个快照请求")
    recv_dt = datetime.now(timezone.utc)
    recv_iso = utc_now_iso()
    rec = snapshot_get(sid)
    if rec is None:
        return HTTPStatus.NOT_FOUND, _error(
            "SNAPSHOT_NOT_FOUND", "没有这个快照请求: %s（图像必须带有效的 snapshot_id）" % sid)

    tail = {"snapshot_id": sid, "state": rec["state"]}

    if rec["state"] not in _SNAP_OPEN:
        if rec["state"] in ("timeout", "cancelled"):
            # ★ R2：终局不改。图像照样存下来（证据不能丢），但明确标为迟到。
            rel, _ = _snap_save_file(rec["device_id"], sid, jpeg, suffix="-late")
            _snap_update(sid, late_at_utc=recv_iso,
                         late_sha256=hashlib.sha256(jpeg).hexdigest(),
                         late_byte_len=len(jpeg), late_filename=rel)
            print("[snap] %s 收到迟到图像(%d 字节)：终局仍是 %s，不算本次结果"
                  % (sid, len(jpeg), rec["state"]))
            out = snapshot_get(sid)
            return HTTPStatus.OK, {
                "ok": True, "accepted": False, "late": True,
                "server_time_utc": recv_iso,
                "message": "这次请求已经是 %s 终局；这张图另存为「迟到图像」，"
                           "不会改写结论，也不计入成功采集。" % rec["state"],
                "snapshot": out,
            }
        resp = _error("SNAPSHOT_ALREADY_FINAL",
                      "这次请求已经收尾（%s），后到的图像不能覆盖已确认的那一张" % rec["state"])
        resp["snapshot"] = rec
        return HTTPStatus.CONFLICT, resp

    # ---- R1：陈旧帧拦截 ----
    # ★ 基线取"请求登记时"与"下发那一帧"里更晚的那个：
    #   前者挡住"重发请求之前的旧图"，后者挡住"把下发那一帧本身当结果交回来"。
    seq = meta.get("seq")
    base = rec.get("baseline_seq")
    if base is None:
        base = rec.get("requested_seq")
    if base is not None and seq is not None and seq <= base:
        with _db_lock:
            conn = db_connect()
            try:
                conn.execute("UPDATE cam_snapshots SET stale_rejects=stale_rejects+1, "
                             "version=version+1 WHERE snapshot_id=?", (sid,))
                conn.commit()
            finally:
                conn.close()
        print("[snap] %s 拒收陈旧帧：上传 seq=%s，基线 seq=%s" % (sid, seq, base))
        resp = _error("SNAPSHOT_STALE_IMAGE",
                      "送回来的帧序号(%s)不比基准(%s)新 —— 这不是请求之后重新取到的画面，"
                      "不能算作这次采集的结果" % (seq, base), retryable=False,
                      requested_seq=base, got_seq=seq)
        resp["snapshot"] = snapshot_get(sid)
        return HTTPStatus.BAD_REQUEST, resp

    # ---- 质量度量（从字节里量，不猜）----
    q = img_quality.analyze_jpeg(jpeg)
    verdict, why = img_quality.looks_occluded(q)
    state = "occluded" if verdict == "occluded" else "ok"
    if verdict == "usable":
        why = "画面可用：" + why

    simulated = 1 if str(meta.get("sim") or "").lower() in ("1", "true", "yes") else 0
    counts = 0 if simulated else 1

    rel, _ = _snap_save_file(rec["device_id"], sid, jpeg)
    digest = hashlib.sha256(jpeg).hexdigest()

    req_dt = _snap_parse_iso(rec["requested_at_utc"]) or recv_dt
    upload_ms = meta.get("upload_ms") or 0
    try:
        upload_ms = max(0, min(int(upload_ms), 600000))
    except (TypeError, ValueError):
        upload_ms = 0
    rx_ms = meta.get("rx_ms") or 0
    try:
        rx_ms = max(0, min(int(rx_ms), 600000))
    except (TypeError, ValueError):
        rx_ms = 0
    # ★ 快门时刻 = 到达时刻 − 设备自测的"取帧→开始发请求"(upload_ms) − 服务端量到的
    #   "开始收 body→收完"(rx_ms)。两段都必须减：
    #   只减 upload_ms 会得到 0ms 级别的差，等于宣称"快门就在到达那一刻"——
    #   那是把估计当精确（真机第一次跑就露出这个马脚：cap == total == 11513ms）。
    # ★ 因此 captured_is_est 恒为 1：设备没有与服务器对齐的时钟，这终究是推算值，
    #   误差上界是"连接建立 + 首字节前的那点排队"，量级几十毫秒（QVGA）。
    back_ms = upload_ms + rx_ms
    captured_dt = recv_dt - timedelta(milliseconds=back_ms) if back_ms else recv_dt
    is_est = 1
    if captured_dt < req_dt:
        # 设备自报的耗时把快门推到了请求之前：物理上不可能，说明这个估计不可信，
        # ★ 不要"修"成一个好看的数字，直接退回"以到达时刻为准"并如实标注。
        captured_dt = req_dt
        is_est = 0

    total_ms = int((recv_dt - req_dt).total_seconds() * 1000)
    cap_ms = int((captured_dt - req_dt).total_seconds() * 1000)

    _snap_update(
        sid,
        state=state,
        captured_at_utc=captured_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        captured_is_est=is_est,
        received_at_utc=recv_iso,
        width=meta.get("w"), height=meta.get("h"),
        byte_len=len(jpeg), sha256=digest, filename=rel,
        device_uptime_ms=meta.get("uptime_ms"),
        upload_ms=upload_ms,
        rx_ms=rx_ms,
        quality_json=json.dumps(q, ensure_ascii=False),
        quality_verdict=verdict,
        quality_note=why,
        latency_total_ms=total_ms,
        latency_capture_ms=cap_ms,
        simulated=simulated,
        counts_as_live=counts,
        error_code=None, error_message=None,
    )
    print("[snap] %s 采集完成: %s %sx%s %d 字节 quality=%s mean=%.1f std=%.1f "
          "cap=%sms total=%sms live=%d sim=%d"
          % (sid, state, meta.get("w"), meta.get("h"), len(jpeg),
             verdict, (q.get("mean_luma") or 0), (q.get("luma_std") or 0),
             cap_ms, total_ms, counts, simulated))
    out = snapshot_get(sid)
    return HTTPStatus.OK, {"ok": True, "accepted": True, "server_time_utc": recv_iso,
                           "snapshot": out}


def snapshot_cancel(sid, reason="user_cancel", by=""):
    if not _snap_safe_id(sid):
        return HTTPStatus.NOT_FOUND, _error("SNAPSHOT_NOT_FOUND", "没有这个快照请求")
    rec = snapshot_get(sid)
    if rec is None:
        return HTTPStatus.NOT_FOUND, _error(
            "SNAPSHOT_NOT_FOUND", "没有这个快照请求: %s" % sid)
    if rec["state"] not in _SNAP_OPEN:
        resp = _error("SNAPSHOT_ALREADY_FINAL",
                      "这次请求已经是 %s 终局，不能再取消" % rec["state"])
        resp["snapshot"] = rec
        return HTTPStatus.CONFLICT, resp
    with _snap_lock:
        for k, v in list(_snap_pending.items()):
            if (v or {}).get("id") == sid:
                _snap_pending.pop(k, None)
    _snap_update(sid, state="cancelled",
                 error_code="USER_CANCELLED",
                 error_message="用户取消（%s）" % (reason or "user_cancel")[:120],
                 note=(rec.get("note") or "") + " | 取消人: %s" % (by or "unknown")[:120])
    return HTTPStatus.OK, {"ok": True, "snapshot": snapshot_get(sid)}


def snapshot_replay(payload, remote=""):
    """回放：用**已授权的真实历史图像**做练习，明确标为回放。

    ★ 这是本周的备用路径，两条纪律不能松：
      ① captured_at_utc 必须是**那张历史图像原本的采集时刻**，不能写成"现在"，
         否则界面上会变成"刚拍的一张"——那就是伪造。
      ② counts_as_live 恒为 0，且不计入"最近一次成功采集"。
    """
    payload = payload or {}
    device_id = payload.get("device_id")
    if not device_id:
        return HTTPStatus.BAD_REQUEST, _error("BAD_REQUEST", "缺少 device_id")
    dev = _cam_norm_device(device_id)

    photo_id = payload.get("photo_id")
    with _db_lock:
        conn = db_connect()
        try:
            if photo_id:
                row = conn.execute("SELECT * FROM cam_photos WHERE photo_id=?",
                                   (photo_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM cam_photos WHERE device_id=? "
                    "ORDER BY created_at_utc DESC, rowid DESC LIMIT 1",
                    (dev,)).fetchone()
        finally:
            conn.close()
    if row is None:
        return HTTPStatus.NOT_FOUND, _error(
            "PHOTO_NOT_FOUND",
            "没有可回放的历史照片（%s）。先让设备拍一张，或指定 photo_id。"
            % ("photo_id=%s" % photo_id if photo_id else "device=%s" % dev))

    base = os.path.abspath(CAM_DIR)
    full = os.path.abspath(os.path.join(base, row["filename"]))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        return HTTPStatus.NOT_FOUND, _error("PHOTO_MISSING", "照片记录存在但文件不在磁盘上")
    with open(full, "rb") as f:
        jpeg = f.read()

    q = img_quality.analyze_jpeg(jpeg)
    verdict, why = img_quality.looks_occluded(q)
    sid = _snap_new_id()
    rel, _ = _snap_save_file(dev, sid, jpeg)
    now_iso = utc_now_iso()
    note = ("回放：这不是一次新的采集。图像来自 %s，采集于 %s（%dx%d，%d 字节），"
            "仅用于练习与演示。" % (row["photo_id"], row["created_at_utc"],
                                    row["width"] or 0, row["height"] or 0, row["byte_len"]))
    rec = {
        "snapshot_id": sid, "device_id": dev, "state": "replayed",
        "trigger_kind": "replay", "source": "replay",
        "is_replay": 1, "simulated": 0, "counts_as_live": 0,
        "mode": "replay",
        "requested_by": (payload.get("requested_by") or remote or "")[:120],
        "reason": (payload.get("reason") or "回放练习")[:500],
        "requested_at_utc": now_iso,
        "requested_seq": None, "requested_frame_no": None,
        # ★ 原始采集时刻，不是"现在"
        "captured_at_utc": row["created_at_utc"],
        "captured_is_est": 0,
        "received_at_utc": now_iso,
        "width": row["width"], "height": row["height"],
        "byte_len": len(jpeg), "sha256": hashlib.sha256(jpeg).hexdigest(),
        "filename": rel,
        "quality_json": json.dumps(q, ensure_ascii=False),
        "quality_verdict": verdict,
        "quality_note": why,
        "replay_of_photo_id": row["photo_id"],
        "note": note,
    }
    with _db_lock:
        conn = db_connect()
        try:
            conn.execute("INSERT INTO cam_snapshots (%s) VALUES (%s)"
                         % (", ".join(rec.keys()), ", ".join("?" for _ in rec)),
                         list(rec.values()))
            conn.commit()
        finally:
            conn.close()
    print("[snap] 回放 %s ← 照片 %s（%s）" % (sid, row["photo_id"], row["created_at_utc"]))
    return HTTPStatus.OK, {"ok": True, "snapshot": snapshot_get(sid)}


def snapshot_list(device_id=None, limit=20, state=None):
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 20
    sql = "SELECT * FROM cam_snapshots WHERE 1=1"
    args = []
    if device_id:
        sql += " AND device_id=?"
        args.append(_cam_norm_device(device_id))
    if state:
        sql += " AND state=?"
        args.append(state)
    sql += " ORDER BY requested_at_utc DESC, rowid DESC LIMIT ?"
    args.append(limit)
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
    items = [_snap_row(r) for r in rows]
    return HTTPStatus.OK, {"ok": True, "count": len(items), "snapshots": items}


def snapshot_summary(device_id=None):
    """汇总。★ live_ok / live_occluded / simulated / replayed 必须分开，不许合成一个"成功数"。

    为什么非要分开：
      · `live_ok`        = 真机、按这次请求、新取的帧、画面可用 —— 这才是"看到环境了"；
      · `live_occluded`  = 真机也真的拍到了，但画面不可用（疑似遮挡），不算看到环境；
      · `simulated`      = 协议模拟器（无硬件时的演练），可以跑通链路，但不是这台机器拍的；
      · `replayed`       = 回放历史图像（练习），时间戳还是当年那一刻。
    把四者合成一个"成功次数"，界面就再也没法回答本周那个问题。
    """
    where, args = "", []
    if device_id:
        where, args = " WHERE device_id=?", [_cam_norm_device(device_id)]
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute("SELECT * FROM cam_snapshots%s" % where, args).fetchall()
        finally:
            conn.close()
    by_state = {}
    live_ok, live_occluded, sim, rep = 0, 0, 0, 0
    last_live_ok, last_any = None, None
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        d = _snap_row(r)
        last_any = last_any or d
        if d["is_replay"]:
            rep += 1
        elif d["simulated"]:
            sim += 1
        elif d["counts_as_live"] and d["state"] == "ok":
            live_ok += 1
            # rows 已按时间倒序，第一条命中的就是"最近一次真正可用的真机采集"
            last_live_ok = last_live_ok or d
        elif d["counts_as_live"] and d["state"] == "occluded":
            live_occluded += 1
    return HTTPStatus.OK, {
        "ok": True,
        "device_id": _cam_norm_device(device_id) if device_id else None,
        "server_time_utc": utc_now_iso(),
        "total": len(rows),
        "live_ok": live_ok,                 # 真机 + 本次请求 + 新帧 + 画面可用
        "live_occluded": live_occluded,     # 真机拍到了，但画面不可用
        "simulated": sim,                   # 协议模拟器（演练，不充数）
        "replayed": rep,                    # 回放历史图像（练习，不充数）
        "by_state": by_state,
        "last_live_ok": last_live_ok,       # ★ 排除 replay / simulated / occluded
        "last_any": last_any,
        "in_flight": [d for d in (_snap_row(r) for r in rows)
                      if d["state"] in _SNAP_OPEN],
        "settings": {
            "timeout_s": SNAPSHOT_TIMEOUT_S,
            "keep_min": SNAPSHOT_KEEP_MIN,
            "retention_days": SNAPSHOT_RETENTION_DAYS,
        },
    }


def snapshot_image_path(sid, late=False):
    rec = snapshot_get(sid)
    if rec is None:
        return None, None
    rel = rec.get("late_filename") if late else rec.get("filename")
    if not rel:
        return rec, None
    base = os.path.abspath(SNAPSHOT_DIR)
    full = os.path.abspath(os.path.join(base, rel))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        return rec, None
    return rec, full


def snapshot_timeout_scan():
    """把过了 deadline 还在等的请求置 timeout。★ 只改等待中的行，绝不碰已收尾的。"""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=SNAPSHOT_TIMEOUT_S)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    n = 0
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT snapshot_id, device_id, state, delivered_at_utc "
                "FROM cam_snapshots WHERE state IN ('pending','requested','capturing') "
                "AND requested_at_utc < ?", (cutoff_iso,)).fetchall()
            for r in rows:
                if r["delivered_at_utc"]:
                    code, msg = ("DEVICE_SLOW",
                                 "请求已下发给设备，但 %.0f 秒内没有送回图像"
                                 % SNAPSHOT_TIMEOUT_S)
                else:
                    code, msg = ("NO_FRAME_STREAM",
                                 "设备没有上传画面，请求一直没能下发；%.0f 秒后判定失败"
                                 % SNAPSHOT_TIMEOUT_S)
                conn.execute(
                    "UPDATE cam_snapshots SET state='timeout', error_code=?, "
                    "error_message=?, version=version+1 WHERE snapshot_id=?",
                    (code, msg, r["snapshot_id"]))
                n += 1
            if n:
                conn.commit()
        finally:
            conn.close()
    if n:
        print("[snap] 本轮 %d 条请求超时（%.0fs 未回）" % (n, SNAPSHOT_TIMEOUT_S))
        with _snap_lock:
            dead = [k for k, v in _snap_pending.items()
                    if (snapshot_get((v or {}).get("id") or "") or {}).get("state")
                    not in _SNAP_OPEN]
            for k in dead:
                _snap_pending.pop(k, None)
    return n


def snapshot_prune(device_id=None, dry_run=False, keep_min=None, days=None):
    """按保留策略清理图像文件。

    ★ 三条不可动摇：
      ① **只删图像文件，记录永不删** —— "某天某人请求过一次并且失败"这个事实
         比一张过期的图重要得多；
      ② 每台设备最近 keep_min 张"带图"的快照无论如何都保留；
      ③ 只删超过保留期的。dry_run 时只报告将删哪些，不动磁盘。
    """
    keep_min = SNAPSHOT_KEEP_MIN if keep_min is None else max(0, int(keep_min))
    days = SNAPSHOT_RETENTION_DAYS if days is None else max(0, int(days))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    sql = ("SELECT snapshot_id, device_id, requested_at_utc, filename, late_filename "
           "FROM cam_snapshots WHERE (filename IS NOT NULL OR late_filename IS NOT NULL) "
           "AND image_pruned=0")
    args = []
    if device_id:
        sql += " AND device_id=?"
        args.append(_cam_norm_device(device_id))
    sql += " ORDER BY requested_at_utc DESC, rowid DESC"
    with _db_lock:
        conn = db_connect()
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    seen = {}
    victims = []
    for r in rows:
        dev = r["device_id"]
        seen[dev] = seen.get(dev, 0) + 1
        if seen[dev] <= keep_min:
            continue                      # 保留下限：最近 N 张永远不删
        if r["requested_at_utc"] >= cutoff:
            continue                      # 还在保留期内
        victims.append(r)

    removed, bytes_freed, bytes_would = [], 0, 0
    base = os.path.abspath(SNAPSHOT_DIR)
    for r in victims:
        gone = []
        for key in ("filename", "late_filename"):
            rel = r[key]
            if not rel:
                continue
            full = os.path.abspath(os.path.join(base, rel))
            if not full.startswith(base + os.sep):
                continue
            if os.path.isfile(full):
                try:
                    size = os.path.getsize(full)
                    if dry_run:
                        bytes_would += size       # ★ dry_run 不删、也不许报"已释放"
                    else:
                        os.remove(full)
                        bytes_freed += size
                    gone.append(rel)
                except OSError as e:
                    print("[snap] 清理失败 %s: %r" % (rel, e))
        if gone:
            if not dry_run:
                with _db_lock:
                    conn = db_connect()
                    try:
                        conn.execute("UPDATE cam_snapshots SET image_pruned=1, "
                                     "version=version+1 WHERE snapshot_id=?",
                                     (r["snapshot_id"],))
                        conn.commit()
                    finally:
                        conn.close()
            removed.append({"snapshot_id": r["snapshot_id"], "files": gone})

    return HTTPStatus.OK, {
        "ok": True, "dry_run": bool(dry_run),
        "keep_min": keep_min, "retention_days": days, "cutoff_utc": cutoff,
        "scanned": len(rows), "pruned": len(removed),
        "bytes_freed": bytes_freed,          # 真的删掉了多少（dry_run 恒为 0）
        "bytes_would_free": bytes_would,     # dry_run 时"如果执行会释放多少"
        "items": removed,
        "note": "只删图像文件；记录（含失败/超时/回放）一条都不删。",
    }


def _snapshot_scanner():
    while True:
        try:
            snapshot_timeout_scan()
        except Exception as e:                               # noqa: BLE001
            print("[snap] 超时扫描异常: %r" % (e,))
        time.sleep(1.0)


# ===========================================================================
# 第 4 周：把后端能力注入语言助手（助手只通过这 7 个回调接触数据与设备）
#   ★ 助手不直接 import 本文件：避免 `python server.py` 与 `python -m backend.server`
#     两种启动方式下模块名不同导致的"两份单例"问题。
# ===========================================================================
nl_assistant.bind(
    # 返回 (200, {"ok":True,"devices":[...]}) 的原始结构
    list_devices=lambda: query_devices(),
    latest_record=lambda device_id: query_latest(device_id),
    records=lambda device_id, limit: query_records(device_id, limit=limit),
    create_collect=lambda payload: create_collect_request(payload),
    get_collect=lambda request_id: get_collect_request(request_id),
    list_requests=lambda **kw: list_collect_requests(**kw),
    server_time=utc_now_iso,
)

# ===========================================================================
# 第 5 周：把语音链路接到同一套能力上
#   ★ 关键：task 段注入的是 nl_assistant.handle_query —— 语音入口与文字入口
#     执行的是**同一个函数、同一套工具、同一套 claim 分档**。
#     两条入口不许各自解读同一句话（红线 V2，见 voice_pipeline.py 顶部）。
# ===========================================================================
voice_pipeline.bind(
    db_path=DB_PATH,
    nl_query=lambda payload: nl_assistant.handle_query(payload),
    utc_now=utc_now_iso,
    voice_dir=VOICE_DIR,
)


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
                        "/api/v1/observations",
                        # 第 3 周：设备上传按键求助（沿用同一套 X-Device-Token）
                        "/api/v1/help_events",
                        # 第 4 周：摄像头预览帧与照片（body 是原始 JPEG，不是 JSON）
                        "/api/v1/cam/frame", "/api/v1/cam/photo",
                        # 设备启动时自报摄像头可用性（JSON）——用于区分"没联网"与"摄像头没接"
                        "/api/v1/cam/state",
                        # 第 7 周：按需快照的设备侧三个上报口（begin / upload / fail）
                        "/api/v1/cam/snapshot/begin",
                        "/api/v1/cam/snapshot/upload",
                        "/api/v1/cam/snapshot/fail")

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

        # ★ 第 4 周：摄像头上传的 body 是**原始 JPEG 二进制**，必须先于 _read_json 分流。
        #   若走 JSON 解析，每张图都会 400（"JSON 解析失败"）——这是很容易踩的坑。
        if path in ("/api/v1/cam/frame", "/api/v1/cam/photo"):
            self._handle_cam_upload(path, remote)
            return

        # ★ 第 7 周：快照图像同样是原始 JPEG，必须走同一条二进制分支。
        if path == "/api/v1/cam/snapshot/upload":
            self._handle_snapshot_upload(remote)
            return

        # ★ 第 5 周：语音 turn 的 body 是**原始 WAV 二进制**，同理必须先分流。
        #   格式校验在 voice_pipeline.parse_wav 里做（不在这一层猜）。
        if path == "/api/v1/voice/turn":
            self._handle_voice_turn(remote)
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
        # ---- 第 3 周：按键求助闭环（docs/06 §7）----
        elif path == "/api/v1/help_events":
            # 设备上传求助事件（佩戴者按键产生）
            handler = lambda: create_help_event(payload, remote)
        elif path.startswith("/api/v1/help_events/") and path.endswith("/response"):
            hid = path[len("/api/v1/help_events/"):-len("/response")]
            # 查看者回应「我看到了」—— 注意：此响应**不代表**设备已知晓
            handler = lambda: respond_help_event(hid, payload)
        elif path.startswith("/api/v1/help_events/") and path.endswith("/cancel"):
            hid = path[len("/api/v1/help_events/"):-len("/cancel")]
            handler = lambda: cancel_help_event(hid, payload)
        elif path.startswith("/api/v1/help_events/") and path.endswith("/viewed"):
            hid = path[len("/api/v1/help_events/"):-len("/viewed")]
            handler = lambda: mark_help_viewed(hid, payload.get("actor"))
        # ---- 第 4 周：摄像头 ----
        elif path == "/api/v1/cam/state":
            handler = lambda: cam_report_state(payload)
        elif path == "/api/v1/cam/capture":
            handler = lambda: self._cam_capture_api(payload)
        # ---- 第 7 周：按需环境快照 ----
        elif path == "/api/v1/cam/snapshot":
            handler = lambda: snapshot_create(payload, remote)
        elif path == "/api/v1/cam/snapshot/replay":
            handler = lambda: snapshot_replay(payload, remote)
        elif path == "/api/v1/cam/snapshot/prune":
            handler = lambda: snapshot_prune(
                (payload or {}).get("device_id"),
                bool((payload or {}).get("dry_run")),
                (payload or {}).get("keep_min"),
                (payload or {}).get("days"))
        elif path == "/api/v1/cam/snapshot/begin":
            handler = lambda: snapshot_begin((payload or {}).get("snapshot_id"), payload)
        elif path == "/api/v1/cam/snapshot/fail":
            handler = lambda: snapshot_fail((payload or {}).get("snapshot_id"), payload)
        elif path.startswith("/api/v1/cam/snapshot/") and path.endswith("/cancel"):
            sid = path[len("/api/v1/cam/snapshot/"):-len("/cancel")]
            handler = lambda: snapshot_cancel(
                sid, (payload or {}).get("reason") or "user_cancel",
                (payload or {}).get("requested_by") or remote)
        # ---- 第 4 周：受限工具语言助手（自然语言查询 / 请求采集）----
        elif path == "/api/v1/nl/query":
            handler = lambda: nl_assistant.handle_query(payload)
        # ---- 第 5 周：前端上报「播放器已开始播放」（只记事实，不推断"已听到"）----
        elif path.startswith("/api/v1/voice/turn/") and path.endswith("/played"):
            tid = path[len("/api/v1/voice/turn/"):-len("/played")]
            handler = lambda: voice_pipeline.mark_played(tid, payload, remote)
        # ---- 第 6 周：文字补充轮（澄清时用键盘回答，走同一条 turn 链路）----
        elif path == "/api/v1/voice/text":
            handler = lambda: voice_pipeline.handle_text_turn(payload, remote)
        # ---- 第 6 周：会话（短期上下文 / 半双工状态机）----
        elif path == "/api/v1/voice/session":
            handler = lambda: voice_session.create_session(
                (payload or {}).get("device_id"), (payload or {}).get("note"))
        elif path.startswith("/api/v1/voice/session/") and path.endswith("/state"):
            sid = path[len("/api/v1/voice/session/"):-len("/state")]
            handler = lambda: voice_session.note_local_state(
                sid, (payload or {}).get("state"), (payload or {}).get("by"))
        elif path.startswith("/api/v1/voice/session/") and path.endswith("/cancel"):
            sid = path[len("/api/v1/voice/session/"):-len("/cancel")]
            handler = lambda: voice_session.cancel(
                sid, (payload or {}).get("request_id"), (payload or {}).get("turn_id"),
                (payload or {}).get("reason") or "user_stop",
                (payload or {}).get("requested_by") or remote)
        elif path.startswith("/api/v1/voice/session/") and path.endswith("/reset"):
            sid = path[len("/api/v1/voice/session/"):-len("/reset")]
            handler = lambda: voice_session.reset(
                sid, (payload or {}).get("reason") or "user_reset")
        # ---- 第 6 周：单轮取消 / 停止播报 ----
        elif path.startswith("/api/v1/voice/turn/") and path.endswith("/cancel"):
            tid = path[len("/api/v1/voice/turn/"):-len("/cancel")]
            handler = lambda: voice_session.cancel(
                (payload or {}).get("session_id"), None, tid,
                (payload or {}).get("reason") or "user_stop",
                (payload or {}).get("requested_by") or remote)
        elif path.startswith("/api/v1/voice/turn/") and path.endswith("/stop"):
            tid = path[len("/api/v1/voice/turn/"):-len("/stop")]
            handler = lambda: voice_session.stop_playback(
                tid, (payload or {}).get("played_ms"),
                (payload or {}).get("reason") or "user_stop",
                (payload or {}).get("reported_by") or remote)
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
        except voice_pipeline.StorageBusy as e:
            # ★ 存储层瞬时不可写（板子 ingest + 摄像头并发写同一个库时的实测现象）
            #   不是"逻辑坏了"，也不是调用方的错 ⇒ 503 + retryable，
            #   让前端可以原样重试一次，而不是看到"服务端异常"就以为代码有 bug。
            status, resp = HTTPStatus.SERVICE_UNAVAILABLE, _error(
                "VOICE_STORAGE_BUSY",
                "存储层这一刻写不进去（%s），本轮没有做任何动作，可以重试。" % (e,),
                retryable=True)
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

    # ---- 第 4 周：摄像头（二进制） ----
    def _read_raw(self, max_len):
        """读**原始二进制**请求体（JPEG）。失败时已自行回错并返回 None。"""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > max_len:
            # 明确报错，不静默截断：截断的 JPEG 在网页上是裂图，比没有更误导
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False,
                             "error": "图像长度非法: %d（上限 %d 字节）" % (length, max_len)},
                            close=True)
            return None
        return self.rfile.read(length)

    def _send_bytes(self, status, data, ctype, extra_headers=None):
        """发送二进制响应（JPEG）。带 no-store，避免浏览器缓存把旧帧当新帧。"""
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, str(v))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _handle_voice_turn(self, remote):
        """POST /api/v1/voice/turn —— body = 原始 WAV（16k/16bit/mono）。

        参数走 query string：
            audio_source       必填，browser_mic / uploaded_file / device_mic
            capture_location   必填，录音发生在哪台机器（红线 V5）
            device_id          可选，任务上下文设备
            wait_ms            可选，透传给语言助手的等待时间
            playback_location  可选，播放发生在哪里（默认浏览器）
        """
        qs = parse_qs(urlparse(self.path).query)

        def q(name, default=None):
            v = qs.get(name)
            return v[0] if v else default

        raw = self._read_raw(VOICE_MAX_BODY)
        if raw is None:
            return
        params = {
            "audio_source": q("audio_source"),
            "capture_location": q("capture_location"),
            "device_id": q("device_id"),
            "wait_ms": q("wait_ms"),
            "playback_location": q("playback_location"),
            # ---- 第 6 周：会话与关联日志 ----
            "session_id": q("session_id"),
            "request_id": q("request_id"),
            "client_ack_ms": q("client_ack_ms"),
            # ---- 第 6 周：教师侧测试注入（记录里会标 injected）----
            "inject_delay_ms": q("inject_delay_ms"),
            "inject_task_ms": q("inject_task_ms"),
            "inject_asr_text": q("inject_asr_text"),
        }
        try:
            status, resp = voice_pipeline.handle_turn(raw, params, remote)
        except voice_pipeline.StorageBusy as e:
            status, resp = HTTPStatus.SERVICE_UNAVAILABLE, _error(
                "VOICE_STORAGE_BUSY",
                "存储层这一刻写不进去（%s），本轮没有做任何动作，可以重试。" % (e,),
                retryable=True)
        except Exception as e:
            import traceback
            traceback.print_exc()
            status, resp = HTTPStatus.INTERNAL_SERVER_ERROR, _error(
                "VOICE_INTERNAL", "语音链路内部异常: %r" % (e,), retryable=True)
        self._send_json(status, resp)

    def _handle_cam_upload(self, path, remote):
        """POST /api/v1/cam/frame | /api/v1/cam/photo —— body = 原始 JPEG。"""
        raw = self._read_raw(CAM_MAX_BODY)
        if raw is None:
            return

        qs = parse_qs(urlparse(self.path).query)
        def q(name, default=None):
            v = qs.get(name)
            return v[0] if v else default

        device_id = q("device_id")
        if not device_id:
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "缺少 device_id"})
            return
        try:
            seq = int(q("seq", "0") or 0)
            w = int(q("w", "0") or 0)
            h = int(q("h", "0") or 0)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "seq/w/h 必须是整数"})
            return

        if not _jpeg_looks_valid(raw):
            # 半截帧/非 JPEG：明确拒收，不存、不显示
            print("[cam] 拒收非法 JPEG: device=%s len=%d" % (device_id, len(raw)))
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "不是完整的 JPEG（缺 SOI/EOI）"})
            return

        if path.endswith("/frame"):
            want_photo, is_new, snap = cam_store_frame(device_id, raw, seq, w, h, remote)
            # ★ 拍照指令就搭在这条响应里：设备收到 capture=true 会立刻切 UXGA 拍一张
            # ★ 第 7 周的按需快照同样搭这条便车，但带 id 与 mode：
            #   snapshot_id 为空字符串表示"这次没有快照请求"，设备不得自作主张取图。
            self._send_json(HTTPStatus.OK,
                            {"ok": True, "seq": seq, "bytes": len(raw),
                             "capture": bool(want_photo), "new": bool(is_new),
                             "snapshot_id": (snap or {}).get("id") or "",
                             "snapshot_mode": (snap or {}).get("mode") or ""})
        else:
            rec = cam_save_photo(device_id, raw, w, h, seq, remote)
            self._send_json(HTTPStatus.OK, {"ok": True, "photo": rec})

    def _handle_snapshot_upload(self, remote):
        """POST /api/v1/cam/snapshot/upload —— body = 属于**某一次请求**的原始 JPEG。

        参数（query string）：
            sid         必填，服务端在帧响应里下发的 snapshot_id
            device_id   必填
            seq         必填，设备端帧序号（★ 必须大于请求时刻的序号，否则视为陈旧帧）
            w / h       图像声明宽高
            upload_ms   设备自测"取帧 → 开始发请求"的毫秒数
            uptime_ms   设备开机毫秒（不做时钟对齐，只作证据）
            sim=1       协议模拟器自报（★ 这类快照 counts_as_live=0）

        ★ rx_ms 由服务端自己量（不信任客户端）：从开始读 body 到读齐的耗时。
          快门时刻要靠它往前推 —— 只减设备自报的 upload_ms 会得到 0ms 级误差，
          等于把"估计"写成"精确"。
        """
        t_rx0 = time.time()
        raw = self._read_raw(CAM_MAX_BODY)
        rx_ms = int((time.time() - t_rx0) * 1000)
        if raw is None:
            return
        qs = parse_qs(urlparse(self.path).query)

        def q(name, default=None):
            v = qs.get(name)
            return v[0] if v else default

        sid = q("sid")
        if not sid:
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "缺少 sid（图像必须带 snapshot_id 回来）"})
            return
        if not _jpeg_looks_valid(raw):
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "不是完整的 JPEG（缺 SOI/EOI）"})
            return
        try:
            meta = {
                "seq": int(q("seq", "0") or 0),
                "w": int(q("w", "0") or 0),
                "h": int(q("h", "0") or 0),
                "upload_ms": int(q("upload_ms", "0") or 0),
                "uptime_ms": int(q("uptime_ms", "0") or 0),
                "rx_ms": rx_ms,
                "sim": q("sim", ""),
            }
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "seq/w/h/upload_ms 必须是整数"})
            return
        status, resp = snapshot_accept_image(sid, raw, meta, remote)
        self._send_json(status, resp)

    def _cam_capture_api(self, payload):
        """网页点「拍照」——只登记请求，真正按快门在设备端。"""
        device_id = (payload or {}).get("device_id")
        if not device_id:
            return HTTPStatus.BAD_REQUEST, _error("BAD_REQUEST", "缺少 device_id")
        had_frame = cam_request_capture(device_id)
        if not had_frame:
            # 没有画面说明设备没在上传：不假装"已拍照"，如实告知
            return HTTPStatus.CONFLICT, _error(
                "NO_FRAME", "该设备当前没有画面（未上传过帧），无法下发拍照指令",
                retryable=True)
        return HTTPStatus.OK, {
            "ok": True,
            "device_id": _cam_norm_device(device_id),
            "capture_ttl_s": CAM_CAPTURE_TTL_S,
            "note": "拍照指令已登记，将在设备下一次帧上传的响应中下发（通常 <500ms）",
        }

    def _serve_cam_frame(self, device_id):
        item = cam_latest_frame(device_id)
        if item is None:
            # 没有帧就是没有帧：不返回占位图，前端据此显示"还没有画面"
            self._send_json(HTTPStatus.NOT_FOUND, {
                "ok": False,
                "error": "该设备还没有上传过画面",
                "devices_with_frames": cam_all_devices(),
            })
            return
        self._send_bytes(HTTPStatus.OK, item["jpeg"], "image/jpeg", {
            "X-Frame-Seq": item["seq"],
            "X-Frame-W": item["w"],
            "X-Frame-H": item["h"],
            "X-Frame-Bytes": item["bytes"],
            "X-Frame-Received-At": item["received_at_utc"],
            "X-Frame-No": item["frame_no"],
        })

    def _serve_cam_status(self, device_id):
        item = cam_latest_frame(device_id)
        with _db_lock:
            conn = db_connect()
            try:
                if device_id:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n FROM cam_photos WHERE device_id=?",
                        (_cam_norm_device(device_id),)).fetchone()
                else:
                    row = conn.execute("SELECT COUNT(*) AS n FROM cam_photos").fetchone()
                photo_count = row["n"] if row else 0
            finally:
                conn.close()
        with _cam_lock:
            capture_pending = any(
                (time.monotonic() - t) <= CAM_CAPTURE_TTL_S
                for t in _cam_capture.values())
            # ★ 不带 device_id 时也要给出摄像头自报状态，否则网页在"全部设备"模式下
            #   拿不到"摄像头探测失败"的原因，只能退化成一句含糊的"还没有画面"。
            cameras = [dict(rec) for rec in _cam_state.values()]
            cameras.sort(key=lambda r: r.get("reported_at_utc") or "", reverse=True)
            if device_id:
                camera_state = _cam_state.get(_cam_norm_device(device_id))
            else:
                camera_state = cameras[0] if cameras else None
        if item is not None:
            item.pop("jpeg", None)   # 状态接口不返回图像本体
        self._send_json(HTTPStatus.OK, {
            "ok": True,
            "server_time_utc": utc_now_iso(),
            "device_id": _cam_norm_device(device_id) if device_id else None,
            "has_frame": item is not None,
            "frame": item,
            "camera": camera_state,
            "cameras": cameras,
            "devices_with_frames": cam_all_devices(),
            "photo_count": photo_count,
            "capture_pending": capture_pending,
            "capture_ttl_s": CAM_CAPTURE_TTL_S,
            "max_body": CAM_MAX_BODY,
        })

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
        # ---- 第 3 周：查看者列表与详情（docs/06 §7.4）----
        #   ★ 必须放在 collect_requests 的 startswith 之前不影响，
        #     但一定要放在下面的通用分支之前，避免被别的前缀吞掉。
        elif path == "/api/v1/help_events":
            self._send_json(*list_help_events(q("device_id"), q("status"),
                                              q("limit", "50")))
        elif path.startswith("/api/v1/help_events/"):
            self._send_json(*get_help_event(path.rsplit("/", 1)[1]))
        elif path.startswith("/api/v1/collect_requests/"):
            self._send_json(*get_collect_request(path.rsplit("/", 1)[1]))
        elif path.startswith("/api/v1/raw/"):
            try:
                uid = int(path.rsplit("/", 1)[1])
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "id 非法"})
                return
            self._send_json(*query_raw(uid))
        # ---- 第 4 周：摄像头 ----
        elif path == "/api/v1/cam/frame":
            self._serve_cam_frame(q("device_id"))
        elif path == "/api/v1/cam/status":
            self._serve_cam_status(q("device_id"))
        elif path == "/api/v1/cam/photos":
            self._send_json(*cam_list_photos(q("device_id"), q("limit", "20")))
        elif path.startswith("/api/v1/cam/photo/"):
            pid = path[len("/api/v1/cam/photo/"):]
            if pid.endswith(".jpg"):
                pid = pid[:-4]
            row, full = cam_get_photo_path(pid)
            if row is None or not os.path.isfile(full):
                self._send_json(HTTPStatus.NOT_FOUND,
                                {"ok": False, "error": "照片不存在: %s" % pid})
                return
            with open(full, "rb") as f:
                data = f.read()
            self._send_bytes(HTTPStatus.OK, data, "image/jpeg", {
                "X-Photo-Id": row["photo_id"],
                "X-Photo-Sha256": row["sha256"],
                "X-Photo-Bytes": row["byte_len"],
                "Content-Disposition": 'inline; filename="%s.jpg"' % row["photo_id"],
            })
        # ---- 第 7 周：按需环境快照 ----
        #   ★ 顺序要紧：/snapshots（列表）与 /snapshot/summary 必须排在
        #     startswith("/api/v1/cam/snapshot/") 之前，否则会被当成某个 id。
        elif path == "/api/v1/cam/snapshots":
            self._send_json(*snapshot_list(q("device_id"), q("limit", "20"),
                                           q("state")))
        elif path == "/api/v1/cam/snapshot/summary":
            self._send_json(*snapshot_summary(q("device_id")))
        elif path.startswith("/api/v1/cam/snapshot/") and path.endswith("/image"):
            sid = path[len("/api/v1/cam/snapshot/"):-len("/image")]
            late = (q("late", "0") or "0") in ("1", "true", "yes")
            rec, full = snapshot_image_path(sid, late=late)
            if rec is None:
                self._send_json(HTTPStatus.NOT_FOUND,
                                _error("SNAPSHOT_NOT_FOUND",
                                       "没有这个快照请求: %s" % sid))
                return
            if full is None:
                # ★ R3：取不到**本次**图像时绝不回退到上一张成功的图。
                #   409 而不是 404：这条请求是存在的，只是它没有图像。
                #   用 404 会让人以为"这个 id 不存在"，从而去找错方向。
                state = rec["state"]
                self._send_json(HTTPStatus.CONFLICT, _error(
                    "SNAPSHOT_NO_IMAGE",
                    "这次请求没有图像可取（state=%s）。%s" % (state, rec["explain"]),
                    retryable=(state in ("pending", "requested", "capturing")),
                    state=state, snapshot=rec))
                return
            with open(full, "rb") as f:
                data = f.read()
            self._send_bytes(HTTPStatus.OK, data, "image/jpeg", {
                "X-Snapshot-Id": rec["snapshot_id"],
                "X-Snapshot-State": rec["state"],
                "X-Snapshot-Captured-At": rec["captured_at_utc"] or "",
                "X-Snapshot-Sha256": (rec["late_sha256"] if late else rec["sha256"]) or "",
                "X-Snapshot-Late": "1" if late else "0",
            })
        elif path.startswith("/api/v1/cam/snapshot/"):
            sid = path[len("/api/v1/cam/snapshot/"):]
            rec = snapshot_get(sid)
            if rec is None:
                self._send_json(HTTPStatus.NOT_FOUND,
                                _error("SNAPSHOT_NOT_FOUND",
                                       "没有这个快照请求: %s" % sid))
                return
            self._send_json(HTTPStatus.OK, {"ok": True, "snapshot": rec})
        # ---- 第 4 周：语言助手的工具清单与解析器状态（不含任何密钥）----
        elif path == "/api/v1/nl/tools":
            self._send_json(*nl_assistant.tools_info())        # ---- 第 5 周：语音链路 ----
        elif path == "/api/v1/voice/health":
            # 引擎是否就绪、跑在哪个端口、什么引擎/模型（不含密钥）
            self._send_json(*voice_pipeline.health())
        elif path == "/api/v1/voice/turns":
            self._send_json(*voice_pipeline.list_turns(q("device_id"), q("limit", "20")))
        # ---- 第 6 周：会话 / 时序日志 ----
        elif path == "/api/v1/voice/sessions":
            self._send_json(*voice_session.list_sessions(q("limit", "20")))
        elif path == "/api/v1/voice/timeline":
            # 全局事件流（每条 turn 的每一次转折都按 request_id 可查）
            self._send_json(HTTPStatus.OK, {
                "ok": True,
                "data": {"events": voice_session.timeline(
                    session_id=q("session_id"), request_id=q("request_id"),
                    limit=int(q("limit", "200") or 200))}})
        elif path.startswith("/api/v1/voice/session/") and path.endswith("/timeline"):
            sid = path[len("/api/v1/voice/session/"):-len("/timeline")]
            self._send_json(HTTPStatus.OK, {
                "ok": True, "data": {"session_id": sid,
                                     "events": voice_session.timeline(session_id=sid)}})
        elif path.startswith("/api/v1/voice/session/"):
            self._send_json(*voice_session.get_session(path[len("/api/v1/voice/session/"):]))
        elif path.startswith("/api/v1/voice/audio/"):
            self._serve_voice_audio(path, "in")
        elif path.startswith("/api/v1/voice/reply/"):
            self._serve_voice_audio(path, "out")
        elif path.startswith("/api/v1/voice/turn/"):
            self._send_json(*voice_pipeline.get_turn(path.rsplit("/", 1)[1]))
        elif path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True, "server_time_utc": utc_now_iso()})
        else:
            self._serve_static(path)

    # ---- 静态文件（Web 前端） ----
    def _serve_voice_audio(self, path, kind):
        """回放某一轮语音的**原始录音**（kind=in）或**播报音频**（kind=out）。

        ★ 只回放已经落盘的文件：文件不存在就如实 404，绝不现场重新合成一段，
          否则"听到的"就不是那一轮真实产生的音频了。
        ★ 第 6 周（C2）：播报音频还要过一道"交付资格"——被取消/被顶掉的那一轮
          文件可能还在（留作证据），但这里必须 410。**隔离写在服务端**，
          因为前端是可能被绕过的那一层。
        """
        prefix = "/api/v1/voice/audio/" if kind == "in" else "/api/v1/voice/reply/"
        tid = path[len(prefix):]
        if tid.endswith(".wav"):
            tid = tid[:-4]
        if kind == "out":
            ok, code, why = voice_pipeline.reply_playable(tid)
            if not ok:
                # ★ 三种"不给"必须分开报，否则排障会指错方向：
                #  404 = 压根没有这条记录
                #  404 + NO_REPLY_AUDIO = 这一轮本来就没产出音频（失败/无声）
                #  410 Gone = 产出过，但按当前语义**不允许**给（取消 / 被顶掉）
                #   用 403 会让人以为是权限问题，用 404 会掩盖"它其实生成过"这个事实。
                if code == "REPLY_QUARANTINED":
                    self._send_json(HTTPStatus.GONE,
                                    {"ok": False,
                                     "error": {"code": code, "message": why,
                                               "retryable": False,
                                               "details": {"turn_id": tid,
                                                           "hint": "要听这一轮的内容，请看记录里的"
                                                                   "文字回答；音频不会再给。"}}})
                else:
                    self._send_json(HTTPStatus.NOT_FOUND,
                                    {"ok": False,
                                     "error": {"code": code, "message": why,
                                               "retryable": False,
                                               "details": {"turn_id": tid}}})
                return
        full = voice_pipeline.audio_path(tid, kind)
        if not full:
            self._send_json(HTTPStatus.NOT_FOUND,
                            {"ok": False,
                             "error": "音频不存在: %s（kind=%s）" % (tid, kind)})
            return
        with open(full, "rb") as f:
            data = f.read()
        self._send_bytes(HTTPStatus.OK, data, "audio/wav", {
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="%s_%s.wav"' % (tid, kind),
        })


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

    # 第 7 周：快照超时扫描（1s 一次）。★ 只把"等待中"的请求置 timeout，
    #   已收尾的记录一个字段都不动（迟到图像另行标注，见 snapshot_accept_image）。
    threading.Thread(target=_snapshot_scanner, daemon=True).start()

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
    print("  ---- 第 3 周：按键求助与物理反馈闭环 ----")
    print("  设备侧   : POST /api/v1/help_events             按键求助上传（断网本地排队，恢复补传）")
    print("             POST /api/v1/device/ack  ack_type=help_ack  设备确认已收到回应")
    print("                       ★ 这是唯一写入 remote_acked_at_utc 的路径")
    print("  查看者侧 : GET  /api/v1/help_events             查看者列表")
    print("             GET  /api/v1/help_events/<id>        详情（打开即记 viewed）")
    print("             POST /api/v1/help_events/<id>/response   「我看到了」（自动下发 help_ack）")
    print("             POST /api/v1/help_events/<id>/cancel     取消（reason 必填，自动下发 clear_alert）")
    print("  下行指令 : help_ack → 设备 LED 转「已送达」色；clear_alert → 停蜂鸣、灯回待机色")
    print("  红线     : 无 remote_acked_at_utc 时，任何一方都不得显示「对方已收到」")
    print("  超时     : %d ms（COLLECT_TIMEOUT_MS，前端传 timeout_ms 一律忽略）"
          % COLLECT_TIMEOUT_MS)
    print("  ---- 第 4 周：摄像头（板载 OV2640） ----")
    print("  设备侧   : POST /api/v1/cam/frame   body=JPEG 原始字节（320x240，约 5fps）")
    print("             POST /api/v1/cam/photo   body=JPEG 原始字节（1600x1200，按需）")
    print("  网页侧   : GET  /api/v1/cam/frame?device_id=xxx   最新一帧（内存，不落库）")
    print("             GET  /api/v1/cam/status?device_id=xxx  帧元数据/照片数/待拍照标记")
    print("             POST /api/v1/cam/capture               点「拍照」（{device_id}）")
    print("             GET  /api/v1/cam/photos                照片列表（落盘 + SHA256）")
    print("             GET  /api/v1/cam/photo/<id>.jpg        下载原图")
    print("  上限     : 单张 %d 字节；照片目录 %s" % (CAM_MAX_BODY, CAM_DIR))
    print("  ---- 第 7 周：按需环境快照（一次请求 = 一次采集事实）----")
    print("  请求侧   : POST /api/v1/cam/snapshot           {device_id,mode,trigger,reason}")
    print("             GET  /api/v1/cam/snapshot/<sid>     单条状态（含三个时刻与 explain）")
    print("             GET  /api/v1/cam/snapshot/<sid>/image  本次图像（没有就是 409，不回退）")
    print("             POST /api/v1/cam/snapshot/<sid>/cancel 取消（之后到的图记为迟到）")
    print("             GET  /api/v1/cam/snapshots          历史（区分 live / simulated / replay）")
    print("             GET  /api/v1/cam/snapshot/summary   汇总（last_live_ok 排除回放与模拟）")
    print("             POST /api/v1/cam/snapshot/replay    回放历史图像（练习，counts_as_live=0）")
    print("             POST /api/v1/cam/snapshot/prune     按保留策略清理图像文件（记录不删）")
    print("  设备侧   : POST /api/v1/cam/snapshot/begin   设备自报「开始取图」")
    print("             POST /api/v1/cam/snapshot/upload  body=JPEG，必须带 sid 且 seq 比请求时新")
    print("             POST /api/v1/cam/snapshot/fail    设备明确回报失败")
    print("  红线     : 陈旧帧(seq<=请求时序号) 400 拒收；超时/取消后到的图只记迟到；"
          "回放与模拟 counts_as_live=0")
    print("  保留     : 超时 %.0fs；每设备至少保留 %d 张；%d 天后清理图像文件（记录永久保留）"
          % (SNAPSHOT_TIMEOUT_S, SNAPSHOT_KEEP_MIN, SNAPSHOT_RETENTION_DAYS))
    print("=" * 70)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C, 正在退出 ...")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()