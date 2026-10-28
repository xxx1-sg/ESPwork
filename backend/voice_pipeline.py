# -*- coding: utf-8 -*-
"""voice_pipeline.py —— 语音链路编排（录音 → 识别 → 任务 → 合成 → 播放）

本模块属于**主后端**，遵守全工程的「零第三方依赖」约定：只用标准库。
真正吃运行时的那部分（离线识别模型、系统语音组件）在独立进程
`backend/voice_engine.py` 里，通过 HTTP 调用 —— 这样引擎挂了也不会牵连文字入口。

一条语音 turn 的六个可观察环节
----------------------------------------------------------------------------
    recorded → vad → asr → task → tts → playback

每一段都由**服务端实测数据**产生，不是前端编出来的状态：

  recorded  音频字节数 / SHA256 / 采样率 / 位深 / 声道 / 时长（从 WAV 头解析）
  vad       由样本算出的 RMS / 峰值 / 过零率，与阈值比较
  asr       引擎名 / 模型 / 耗时 / 识别文本（或失败原因）
  task      复用第 4 周的 nl_assistant.handle_query（同一函数、同一套工具）
  tts       引擎名 / 语音名 / 耗时 / 产出音频字节（只合成，不播放）
  playback  由前端上报「播放器已开始播放」；"是否真的听到"只有现场能判

六条红线（与 docs/13 §5 一一对应，selftest_voice.py 逐条断言）
----------------------------------------------------------------------------
V1 没有识别就没有文本：ASR 失败/超时/无声时 transcript 必须为 null，
   绝不用关键字匹配、规则猜测或上一轮结果"补"出一句文本。
V2 语音与文字同源：task 段必须调用 nl_assistant.handle_query，
   同一句话在两条入口得到的 intent / claim / evidence.source 必须一致。
V3 文字入口不被语音故障连带：引擎不可用时 /api/v1/nl/query 仍正常。
V4 播放 ≠ 听到：只记录 playback_started_at，不产生 heard/acked 之类的字段。
V5 来源与位置必须记录：audio_source 必须在枚举内，run_location 每段都要有值。
V6 采样格式必须校验并回报：只收 16 kHz / 16 bit / 单声道 PCM WAV，
   其它格式 400 并报出**实际**参数（不静默转换后假装通过）。

第 6 周在此基础上加的三条红线（selftest_voice6.py 逐条断言）
----------------------------------------------------------------------------
C1 **取消必须真的拦住动作**：turn 在每一个阶段边界都要问一次
   `voice_session.interrupt_of()`。取消发生在识别之后 → 任务段一行都不执行
   （`task.status="skipped"`, `action_taken=0`）。"取消了却顺手把采集下发了"
   是本课最严重的事故：那是设备上真实发生的副作用。
C2 **迟到结果不得重新播放**：被取消/被顶掉的那一轮 `playable=0`，
   `GET /api/v1/voice/reply/<id>.wav` 直接 410。隔离写在服务端，
   因为前端是可能被绕过的那一层。
C3 **新任务不被旧任务结果覆盖**：turn 的先后由会话内 `turn_seq` 钉死；
   落库时若自己已不是会话的 active_request，一律标 `quarantined_superseded`，
   并有 `superseded_by` 指回是谁顶掉了它。

★ 为了做到"阶段之间能被取消"，本周把落库方式从"最后一次性 INSERT"改成了
  **预入库 + 阶段增量 UPDATE**：turn 一开始就有一行 `final_state='running'`，
  别的线程（取消请求）才找得到它。这也是"进行中"能在 `GET /voice/turns` 里看见的原因。
"""

import hashlib
import json
import os
import re
import sqlite3
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

try:
    import voice_session
except ImportError:                                    # 两种启动方式都在同目录
    from backend import voice_session  # type: ignore


# ===========================================================================
# 配置
# ===========================================================================
VOICE_ENGINE_URL = os.environ.get("VOICE_ENGINE_URL", "http://127.0.0.1:8013").rstrip("/")
ASR_TIMEOUT_S = float(os.environ.get("VOICE_ASR_TIMEOUT_S", "25"))
TTS_TIMEOUT_S = float(os.environ.get("VOICE_TTS_TIMEOUT_S", "20"))
# 引擎 HTTP 层比引擎内部再多留一点余量，才能区分"引擎报错"与"链路超时"
HTTP_MARGIN_S = 5.0

AUDIO_SOURCES = ("browser_mic", "uploaded_file", "device_mic",
                 # ★ 第 6 周新增：澄清轮里用户可以直接打字回答（这是"重新输入"的
                 #   备用路径，也是语音识别反复失败时的出口）。它**不是**麦克风来源，
                 #   所以必须单独一个值 —— 记成 browser_mic 就是伪造来源（红线 V5）。
                 "text_input")
SOURCE_LABELS = {
    "browser_mic": "浏览器麦克风（电脑音频）",
    "uploaded_file": "上传的授权录音文件",
    "device_mic": "开发板板载麦克风",
    "text_input": "键盘输入（澄清轮的补充回答，没有音频）",
}

MIN_DURATION_MS = 200          # 短于 200ms 不可能是有效的一句话
MAX_DURATION_MS = 40000        # 40 秒上限（超了让人重录，不静默截断）
VAD_RMS_MIN = float(os.environ.get("VOICE_VAD_RMS_MIN", "0.006"))
VAD_PEAK_MIN = float(os.environ.get("VOICE_VAD_PEAK_MIN", "0.02"))
MAX_TTS_CHARS = 600

# ---- 播报长度控制 ----
# 文字回答是给眼睛看的（可以长、可以有明细表）；语音播报是给耳朵听的，
# 一条 300 字的回答念出来要 60 秒以上，听完什么也记不住。
# 所以语音通道**只念前几句**，规则是：按整句切分 → 取前 N 句且总长不超上限。
# ★ 只截断、不改写：播报文本一定是原文的一个前缀，绝不改写、绝不补一句原文没有的话；
#   末尾只追加一句"完整回答见屏幕"的**提示语**（不是事实陈述），并如实标注 truncated。
TTS_EXCERPT_SENTENCES = int(os.environ.get("VOICE_TTS_EXCERPT_SENTENCES", "2"))
TTS_EXCERPT_CHARS = int(os.environ.get("VOICE_TTS_EXCERPT_CHARS", "160"))
TTS_FULL = os.environ.get("VOICE_TTS_FULL", "0") in ("1", "true", "yes")
EXCERPT_SUFFIX = "（完整回答见屏幕。）"
_SENT_END = "。！？!?；;"

VOICE_DIR = os.environ.get("VOICE_DATA_DIR", "")   # 由 bind() 决定

# ---- 第 6 周：测试入口（教师侧提供的"迟到响应/错误识别"注入）----
# ★ 注入能力必须**默认开启但强标注**：
#   · 关掉之后（VOICE_ALLOW_INJECT=0）所有 inject_* 参数一律 400，
#     用于"验收时确认产品链路本身是干净的"；
#   · 开着的时候，被注入的那一轮记录里 asr.engine 会写成 "injected"、
#     inject_json 会写清"注入了什么"，界面上也如实标注「注入」。
#     绝不允许把注入结果伪装成真实识别 —— 那会让所有时序证据失真。
ALLOW_INJECT = os.environ.get("VOICE_ALLOW_INJECT", "1") not in ("0", "false", "no")
MAX_INJECT_MS = 20000          # 注入延迟上限：比 wait_ms 上限还宽一点，够演示"很长"的慢响应
INJECTED_ASR_ENGINE = "injected"

_st = {"db_path": None, "nl_query": None, "utc_now": None, "web_dir": None}
_lock = threading.Lock()


def bind(db_path, nl_query, utc_now, voice_dir):
    """注入后端能力。不反向 import server，避免两种启动方式下出现两份单例。"""
    _st["db_path"] = db_path
    _st["nl_query"] = nl_query
    _st["utc_now"] = utc_now
    global VOICE_DIR
    VOICE_DIR = voice_dir
    os.makedirs(VOICE_DIR, exist_ok=True)
    # 第 6 周：会话层用同一个 DB、同一套时间函数
    voice_session.bind(db_path, utc_now)



def _now():
    return _st["utc_now"]()


def _conn():
    c = sqlite3.connect(_st["db_path"], timeout=15)
    c.row_factory = sqlite3.Row
    return c


def _error(status, code, message, retryable=False, **details):
    e = {"code": code, "message": message, "retryable": bool(retryable)}
    if details:
        e["details"] = details
    return status, {"ok": False, "error": e}


def _ok(data):
    return 200, {"ok": True, "data": data}


# ===========================================================================
# 建表
# ===========================================================================
DDL = """
CREATE TABLE IF NOT EXISTS voice_turns (
    turn_id              TEXT PRIMARY KEY,
    device_id            TEXT,
    audio_source         TEXT NOT NULL,
    capture_location     TEXT,
    run_location_json    TEXT NOT NULL,
    -- 音频事实（全部由 WAV 头/样本算出）
    audio_bytes          INTEGER,
    audio_sha256         TEXT,
    sample_rate          INTEGER,
    bits                 INTEGER,
    channels             INTEGER,
    duration_ms          INTEGER,
    rms                  REAL,
    peak                 REAL,
    zcr                  REAL,
    vad_passed           INTEGER,
    -- 识别
    asr_status           TEXT,      -- ok / empty / failed / timeout / unavailable / skipped
    asr_engine           TEXT,
    asr_model            TEXT,
    asr_ms               INTEGER,
    transcript           TEXT,      -- ★ 只有 asr_status=ok 时才非空
    asr_error            TEXT,
    -- 任务（第 4 周同一函数）
    task_status          TEXT,      -- ok / failed / skipped
    task_intent          TEXT,
    task_claim           TEXT,
    task_status_remote   TEXT,      -- nl 返回里的 status(answered/completed/clarify/...)
    task_source          TEXT,      -- nl 返回里的 evidence.source
    task_answer          TEXT,
    task_response_json   TEXT,
    task_error           TEXT,
    -- 合成
    tts_status           TEXT,      -- ok / failed / skipped
    tts_engine           TEXT,
    tts_voice            TEXT,
    tts_ms               INTEGER,
    reply_text           TEXT,
    reply_audio_bytes    INTEGER,
    reply_audio_sha256   TEXT,
    tts_error            TEXT,
    tts_truncated        INTEGER,   -- 1 = 播报是原文前几句节选（完整内容在文字回答里）
    reply_full_chars     INTEGER,   -- 完整文字回答的字数（与播报长度对照）
    -- 播放（只记录"播放器开始播放"这一事实）
    playback_started_at_utc TEXT,
    playback_reported_by TEXT,
    -- 终局
    final_state          TEXT NOT NULL,
    error_code           TEXT,
    error_message        TEXT,
    stages_json          TEXT,
    elapsed_ms           INTEGER,
    created_at_utc       TEXT NOT NULL,
    -- ==== 第 6 周：会话 / 取消 / 迟到隔离 / 澄清 / 时序 ====
    session_id           TEXT,
    request_id           TEXT,
    turn_seq             INTEGER,
    superseded_by        TEXT,      -- 顶掉自己的那条 request_id
    received_at_utc      TEXT,      -- t0：服务端收到完整音频的时刻（时序基准）
    -- 时序（全部相对 t0，单位 ms；由服务端实测，不用前端时钟）
    first_feedback_ms    INTEGER,
    task_done_ms         INTEGER,
    tts_ready_ms         INTEGER,
    client_ack_ms        INTEGER,   -- 前端上报的本地"已提交"耗时（本地事实，仅供参考）
    -- 交付资格（★ C2/C3 的落点：迟到结果不许播）
    delivery_state       TEXT,      -- deliverable / quarantined_cancelled / quarantined_superseded / no_audio
    playable             INTEGER,   -- 1 = 允许 GET reply.wav 拿到音频
    -- 取消（取消请求由另一条 HTTP 连接写入，本行随后才发现）
    cancel_requested_at_utc TEXT,
    cancel_seen_at_utc   TEXT,      -- 本行是**什么时候**发现被取消的（不是什么时候请求的）
    cancel_reason        TEXT,
    cancel_stage         TEXT,      -- before_asr / after_asr / after_task / after_tts
    action_taken         INTEGER,   -- 1 = 任务段真的跑过了（副作用可能已发生，不可撤回）
    -- 澄清（有限多轮）
    clarify_round        INTEGER,
    clarify_json         TEXT,      -- 本轮**提出**的反问（等用户回答）
    clarify_from_turn_id TEXT,      -- 本轮是回答哪一轮的反问
    clarify_resolved_with TEXT,     -- 消解后实际执行的文本
    clarify_how          TEXT,      -- intent_ordinal / intent_keyword / device_name / device_ordinal / freeform
    -- 停止播报（任务保留，只关声音）
    tts_stopped_at_utc   TEXT,
    tts_played_ms        INTEGER,
    stop_reason          TEXT,
    -- 测试注入（教师侧入口；真实链路这一列必须为 NULL）
    inject_json          TEXT
);
"""

# ★ 索引**必须**在第 6 周的补列之后才建。
#   踩过的坑：把 CREATE INDEX ... ON voice_turns(request_id) 写在上面的 DDL 里，
#   在"新库"上一切正常（CREATE TABLE 已带这些列），但在**已经跑过第 5 周的真实库**上
#   直接抛 `sqlite3.OperationalError: no such column: request_id` —— 因为
#   `CREATE TABLE IF NOT EXISTS` 遇到老表什么都不做，补列却写在 DDL 之后。
#   症状是后端在**每次启动**时崩在 db_init，守护进程每 3/6/12/24 秒重拉一次、
#   永远起不来，而日志里只有一行看起来像"库坏了"的报错。自检跑不出来（自检用新库）。
_DDL_INDEX_W6 = """
CREATE INDEX IF NOT EXISTS idx_voice_created ON voice_turns(created_at_utc DESC);
CREATE INDEX IF NOT EXISTS idx_voice_device ON voice_turns(device_id, created_at_utc DESC);
CREATE INDEX IF NOT EXISTS idx_voice_request ON voice_turns(request_id);
CREATE INDEX IF NOT EXISTS idx_voice_session ON voice_turns(session_id, turn_seq DESC);
"""

# 第 6 周新增列（老库平滑升级：只加列，不改历史值）
_NEW_COLS_W6 = (
    ("session_id", "TEXT"), ("request_id", "TEXT"), ("turn_seq", "INTEGER"),
    ("superseded_by", "TEXT"), ("received_at_utc", "TEXT"),
    ("first_feedback_ms", "INTEGER"), ("task_done_ms", "INTEGER"),
    ("tts_ready_ms", "INTEGER"), ("client_ack_ms", "INTEGER"),
    ("delivery_state", "TEXT"), ("playable", "INTEGER"),
    ("cancel_requested_at_utc", "TEXT"), ("cancel_seen_at_utc", "TEXT"),
    ("cancel_reason", "TEXT"), ("cancel_stage", "TEXT"), ("action_taken", "INTEGER"),
    ("clarify_round", "INTEGER"), ("clarify_json", "TEXT"),
    ("clarify_from_turn_id", "TEXT"), ("clarify_resolved_with", "TEXT"),
    ("clarify_how", "TEXT"),
    ("tts_stopped_at_utc", "TEXT"), ("tts_played_ms", "INTEGER"), ("stop_reason", "TEXT"),
    ("inject_json", "TEXT"),
)


def db_init(conn):
    # ① 先建表（老库上 IF NOT EXISTS 等于什么都不做）
    conn.executescript(DDL)
    # ② 再补列（老库缺哪列补哪列，只加列，不改历史值）
    # ★ 不用 try/except ALTER：SQLite 的 ALTER 一旦失败会让后续语句卡在事务里。
    have = {r[1] for r in conn.execute("PRAGMA table_info(voice_turns)")}
    for col, typ in (("tts_truncated", "INTEGER"), ("reply_full_chars", "INTEGER")) + _NEW_COLS_W6:
        if col not in have:
            conn.execute("ALTER TABLE voice_turns ADD COLUMN %s %s" % (col, typ))
    # ③ 最后建索引（索引引用的列到这一步一定存在了）
    conn.executescript(_DDL_INDEX_W6)
    conn.commit()
    voice_session.db_init(conn)



# 列 → 取值。★ 单一事实来源：DDL、INSERT、取值三者必须一致，手写占位符必然出错。
_COLS = [
    ("turn_id", lambda **k: k["turn_id"]),
    ("device_id", lambda **k: k["device_id"]),
    ("audio_source", lambda **k: k["audio_source"]),
    ("capture_location", lambda **k: k["capture_location"]),
    ("run_location_json", lambda **k: json.dumps(k["run_location"], ensure_ascii=False)),
    ("audio_bytes", lambda **k: k["audio_bytes"]),
    ("audio_sha256", lambda **k: k["sha"]),
    ("sample_rate", lambda **k: k["info"]["sample_rate"]),
    ("bits", lambda **k: k["info"]["bits"]),
    ("channels", lambda **k: k["info"]["channels"]),
    ("duration_ms", lambda **k: k["info"]["duration_ms"]),
    ("rms", lambda **k: k["st"]["rms"]),
    ("peak", lambda **k: k["st"]["peak"]),
    ("zcr", lambda **k: k["st"]["zcr"]),
    ("vad_passed", lambda **k: 1 if k["passed"] else 0),
    ("asr_status", lambda **k: k["asr_status"]),
    ("asr_engine", lambda **k: k["asr_engine"]),
    ("asr_model", lambda **k: k["asr_model"]),
    ("asr_ms", lambda **k: k["asr_ms"]),
    ("transcript", lambda **k: k["transcript"]),
    ("asr_error", lambda **k: k["asr_error"]),
    ("task_status", lambda **k: k["task_status"]),
    ("task_intent", lambda **k: k["task_intent"]),
    ("task_claim", lambda **k: k["task_claim"]),
    ("task_status_remote", lambda **k: k["task_remote_status"]),
    ("task_source", lambda **k: k["task_source"]),
    ("task_answer", lambda **k: k["task_answer"]),
    ("task_response_json", lambda **k: (json.dumps(k["task_response"], ensure_ascii=False)
                                        if k["task_response"] else None)),
    ("task_error", lambda **k: k["task_error"]),
    ("tts_status", lambda **k: k["tts_status"]),
    ("tts_engine", lambda **k: k["tts_engine"]),
    ("tts_voice", lambda **k: k["tts_voice"]),
    ("tts_ms", lambda **k: k["tts_ms"]),
    ("reply_text", lambda **k: k["reply_text"]),
    ("reply_audio_bytes", lambda **k: k["reply_bytes"]),
    ("reply_audio_sha256", lambda **k: k["reply_sha"]),
    ("tts_error", lambda **k: k["tts_error"]),
    ("tts_truncated", lambda **k: (None if k["tts_truncated"] is None
                                   else (1 if k["tts_truncated"] else 0))),
    ("reply_full_chars", lambda **k: len(k["task_answer"] or "")),
    ("final_state", lambda **k: k["final_state"]),
    ("error_code", lambda **k: k["error_code"]),
    ("error_message", lambda **k: k["error_message"]),
    ("stages_json", lambda **k: json.dumps(k["stages"], ensure_ascii=False)),
    ("elapsed_ms", lambda **k: k["elapsed"]),
    ("created_at_utc", lambda **k: k["created"]),
    # ---- 第 6 周（全部走 k.get，因为"取消"会让很多字段根本没机会产生）----
    ("session_id", lambda **k: k.get("session_id")),
    ("request_id", lambda **k: k.get("request_id")),
    ("turn_seq", lambda **k: k.get("turn_seq")),
    ("superseded_by", lambda **k: k.get("superseded_by")),
    ("received_at_utc", lambda **k: k.get("received_at")),
    ("first_feedback_ms", lambda **k: k.get("first_feedback_ms")),
    ("task_done_ms", lambda **k: k.get("task_done_ms")),
    ("tts_ready_ms", lambda **k: k.get("tts_ready_ms")),
    ("client_ack_ms", lambda **k: k.get("client_ack_ms")),
    ("delivery_state", lambda **k: k.get("delivery_state")),
    ("playable", lambda **k: (None if k.get("playable") is None
                              else (1 if k["playable"] else 0))),
    ("cancel_requested_at_utc", lambda **k: k.get("cancel_requested_at")),
    ("cancel_seen_at_utc", lambda **k: k.get("cancel_seen_at")),
    ("cancel_reason", lambda **k: k.get("cancel_reason")),
    ("cancel_stage", lambda **k: k.get("cancel_stage")),
    ("action_taken", lambda **k: (None if k.get("action_taken") is None
                                  else (1 if k["action_taken"] else 0))),
    ("clarify_round", lambda **k: k.get("clarify_round")),
    ("clarify_json", lambda **k: (json.dumps(k["clarify_ctx"], ensure_ascii=False)
                                  if k.get("clarify_ctx") else None)),
    ("clarify_from_turn_id", lambda **k: k.get("clarify_from")),
    ("clarify_resolved_with", lambda **k: k.get("clarify_resolved")),
    ("clarify_how", lambda **k: k.get("clarify_how")),
    ("tts_stopped_at_utc", lambda **k: k.get("tts_stopped_at")),
    ("tts_played_ms", lambda **k: k.get("tts_played_ms")),
    ("stop_reason", lambda **k: k.get("stop_reason")),
    ("inject_json", lambda **k: (json.dumps(k["inject"], ensure_ascii=False)
                                 if k.get("inject") else None)),
]


# ===========================================================================
# WAV 校验（红线 V6）
# ===========================================================================
def parse_wav(data, require_16k=True):
    """解析并**严格校验** WAV。返回 (info, pcm) 或抛 ValueError（带实际参数）。"""
    if len(data) < 44:
        raise ValueError("音频只有 %d 字节，不是有效的 WAV 文件" % len(data))
    if data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("不是 RIFF/WAVE 容器（文件头为 %r）" % data[0:12])
    pos, fmt, pcm = 12, None, None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4:pos + 8])
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt " and len(body) >= 16:
            afmt, ch, rate, _br, align, bits = struct.unpack("<HHIIHH", body[:16])
            fmt = {"audio_format": afmt, "channels": ch, "sample_rate": rate,
                   "block_align": align, "bits": bits}
        elif cid == b"data":
            pcm = body
            if fmt is None:
                raise ValueError("data 块出现在 fmt 之前，无法确定格式")
            break
        pos += 8 + size + (size & 1)
    if fmt is None or pcm is None:
        raise ValueError("缺少 fmt 或 data 块，不是完整的 WAV")

    actual = "实际收到 %d Hz / %d bit / %d 声道 / format=%d" % (
        fmt["sample_rate"], fmt["bits"], fmt["channels"], fmt["audio_format"])
    if fmt["audio_format"] != 1:
        raise ValueError("只接受 PCM 未压缩音频，%s（可能被浏览器压成了别的编码）" % actual)
    if fmt["bits"] != 16:
        raise ValueError("只接受 16 bit 采样，%s" % actual)
    if fmt["channels"] != 1:
        raise ValueError("只接受单声道，%s" % actual)
    if require_16k and fmt["sample_rate"] != 16000:
        raise ValueError("只接受 16000 Hz 采样（本课约定的语音输入格式），%s" % actual)

    frames = len(pcm) // max(1, fmt["block_align"])
    info = dict(fmt)
    info["data_bytes"] = len(pcm)
    info["frames"] = frames
    info["duration_ms"] = int(round(frames * 1000.0 / max(1, fmt["sample_rate"])))
    return info, pcm


def pcm_stats(pcm):
    n = len(pcm) // 2
    if n <= 0:
        return {"samples": 0, "rms": 0.0, "peak": 0.0, "zcr": 0.0}
    vals = struct.unpack("<%dh" % n, pcm[:n * 2])
    s2, peak, zc, prev = 0, 0, 0, 0
    for v in vals:
        s2 += v * v
        av = v if v >= 0 else -v
        if av > peak:
            peak = av
        if (v >= 0) != (prev >= 0):
            zc += 1
        prev = v
    return {"samples": n,
            "rms": round((s2 / float(n)) ** 0.5 / 32768.0, 6),
            "peak": round(peak / 32768.0, 6),
            "zcr": round(zc / float(n), 6)}


# ===========================================================================
# 识别结果文本规范化
#   中文 ASR 常在词之间插空格（"查看 上次 数据"），只合并汉字之间的空格，
#   不动英文/数字间的间隔，也不做任何同义词改写（那会变成"替识别引擎改答案"）。
# ===========================================================================
def normalize_text(t):
    t = (t or "").strip()
    t = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()


def speech_excerpt(text):
    """把长回答压成适合朗读的**原文前缀**。

    返回 (播报文本, 是否节选)。规则：
      · 按句末标点与换行切句，保留标点；
      · 取前 TTS_EXCERPT_SENTENCES 句，且累计不超过 TTS_EXCERPT_CHARS；
      · 第一句本身就超长时，仍然只播这一句（绝不从中间截断，那会改变语义）；
      · 节选时末尾加一句提示语，说明完整内容在屏幕上。

    ★ 这里不做任何"改写/概括/同义替换" —— 那是模型该干的活，而且一旦概括就有
      编造的风险。本周的约束是：播报内容必须能在文字回答里逐字找到。
    """
    raw = (text or "").strip()
    if not raw:
        return "", False
    if TTS_FULL:
        return raw, False
    # 切句：句末标点后断开，换行也断开
    parts, buf = [], ""
    for ch in raw:
        buf += ch
        if ch in _SENT_END or ch == "\n":
            parts.append(buf)
            buf = ""
    if buf.strip():
        parts.append(buf)
    # ★ 不要对每一段做 strip：原文里的换行本身就是"该停顿"的地方，
    #   去掉之后 "设备：xxx" 和 "采样时间：yyy" 会粘成一句，念出来完全听不清。
    parts = [p for p in parts if p.strip()]
    if not parts:
        return raw, False

    picked, total = [], 0
    for p in parts:
        if len(picked) >= TTS_EXCERPT_SENTENCES:
            break
        if picked and total + len(p) > TTS_EXCERPT_CHARS:
            break
        picked.append(p)
        total += len(p)
    if not picked:
        picked = [parts[0]]
    body = "".join(picked).strip()
    truncated = len(body) < len(raw.strip())
    if truncated:
        body = body + EXCERPT_SUFFIX
    return body, truncated


# ===========================================================================
# 调引擎
# ===========================================================================
# ★ 语音引擎永远在本机（127.0.0.1），必须**显式绕过系统代理**。
#   现场踩坑（2026-10-23）：本机环境变量里设了 http_proxy，urllib 默认会连
#   127.0.0.1 也走代理，于是 /api/v1/voice/health 稳定返回 502 Bad Gateway，
#   而 curl 却是好的 —— 排查时极容易误判成"引擎没起"。
#   这里用空 ProxyHandler 建一个专用 opener，只影响访问本机引擎这一条路径；
#   访问外部 ASR 服务（voice_engine 内部）仍然走系统代理。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(url_or_req, timeout):
    return _LOCAL_OPENER.open(url_or_req, timeout=timeout)


def _post_json(url, payload, timeout):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with _open(req, timeout) as r:
        return r.status, r.read(), dict(r.headers)


def engine_health(timeout=3.0):
    try:
        with _open(VOICE_ENGINE_URL + "/health", timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "url": VOICE_ENGINE_URL}


def call_asr(wav_bytes, timeout_s):
    """返回 (status, dict)。status ∈ ok / timeout / unavailable / failed。"""
    req = urllib.request.Request(VOICE_ENGINE_URL + "/asr", data=wav_bytes, method="POST",
                                 headers={"Content-Type": "audio/wav"})
    t0 = time.time()
    try:
        with _open(req, timeout_s) as r:
            payload = json.loads(r.read().decode("utf-8"))
        ms = int((time.time() - t0) * 1000)
        return "ok", {"engine": payload.get("engine"), "model": payload.get("model"),
                      "ms": payload.get("ms", ms), "roundtrip_ms": ms,
                      "text": payload.get("text") or "",
                      "stats": payload.get("stats"), "audio": payload.get("audio"),
                      "empty": bool(payload.get("empty"))}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            detail = ""
        return "failed", {"engine": None, "ms": int((time.time() - t0) * 1000),
                          "error": "引擎返回 HTTP %d：%s" % (e.code, detail or e.reason)}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        kind = "timeout" if "timed out" in str(reason).lower() else "unavailable"
        return kind, {"engine": None, "ms": int((time.time() - t0) * 1000),
                      "error": "无法连接识别引擎 %s：%s" % (VOICE_ENGINE_URL, reason)}
    except TimeoutError:
        return "timeout", {"engine": None, "ms": int((time.time() - t0) * 1000),
                           "error": "识别引擎在 %.1f 秒内没有返回" % timeout_s}


def call_tts(text, timeout_s):
    req = urllib.request.Request(VOICE_ENGINE_URL + "/tts",
                                 data=json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"),
                                 method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    t0 = time.time()
    try:
        with _open(req, timeout_s) as r:
            data = r.read()
            h = dict(r.headers)
        return "ok", {"engine": h.get("X-Tts-Engine", "?"), "voice": h.get("X-Tts-Voice", ""),
                      "ms": int(h.get("X-Tts-Ms", "0") or 0),
                      "audio_ms": int(h.get("X-Tts-Audio-Ms", "0") or 0),
                      "data": data, "roundtrip_ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            detail = ""
        return "failed", {"ms": int((time.time() - t0) * 1000),
                          "error": "合成引擎返回 HTTP %d：%s" % (e.code, detail or e.reason)}
    except urllib.error.URLError as e:
        return "unavailable", {"ms": int((time.time() - t0) * 1000),
                               "error": "无法连接合成引擎 %s：%s" % (VOICE_ENGINE_URL,
                                                              getattr(e, "reason", e))}
    except TimeoutError:
        return "timeout", {"ms": int((time.time() - t0) * 1000),
                           "error": "合成引擎在 %.1f 秒内没有返回" % timeout_s}


# ===========================================================================
# 主流程
# ===========================================================================
class _Terminate(Exception):
    """走到一个明确的终局，停止后续阶段。

    ★ 与 _Interrupted 的区别：这是"这一轮正常结束了"（无声、识别失败、反问…），
      不是"这一轮不要了"。两者都会跳出阶段流水线，但记录的语义完全不同 ——
      混在一起会让"取消率"这类统计彻底失真。
    """


class _Interrupted(Exception):
    """发现自己已被取消 / 被新任务顶掉。"""

    def __init__(self, info, stage_key):
        super().__init__(info.get("kind"))
        self.info = info
        self.stage_key = stage_key


def parse_inject(params):
    """解析教师侧的测试注入参数。

    返回 (inject_dict | None, (status, resp) | None)。

    ★ 为什么需要这个入口：第 6 周要验证的恰恰是"慢响应 / 听错"这两类异常时序。
      靠人手动制造 3 秒延迟不可复现；靠改代码临时加 sleep 又会把证据弄脏。
      所以把"注入"做成**显式参数**，并强制在记录里留痕（inject_json + asr.engine=injected）。
    """
    given = {}
    for k in ("inject_delay_ms", "inject_task_ms", "inject_asr_text"):
        v = (params.get(k) or "").strip()
        if v:
            given[k] = v
    if not given:
        return None, None
    if not ALLOW_INJECT:
        return None, _error(400, "INJECT_DISABLED",
                            "本轮部署已关闭测试注入（VOICE_ALLOW_INJECT=0）。"
                            "注入只用于演示「慢响应 / 听错」的时序，正式验收时应当关闭。",
                            tried=list(given))
    out = {"delay_ms": 0, "task_ms": 0, "asr_text": None, "raw": dict(given)}
    for src, dst in (("inject_delay_ms", "delay_ms"), ("inject_task_ms", "task_ms")):
        if src in given:
            try:
                n = int(float(given[src]))
            except ValueError:
                return None, _error(400, "INJECT_BAD_VALUE",
                                    "%s 必须是整数毫秒，收到 %r" % (src, given[src]))
            out[dst] = max(0, min(n, MAX_INJECT_MS))
    if "inject_asr_text" in given:
        out["asr_text"] = given["inject_asr_text"][:200]
    out["note"] = ("这是**注入**的一轮：延迟/识别文本是人为设定的，"
                   "不是引擎实测结果。记录里 inject_json 会保留设定值。")
    return out, None


def _patch(turn_id, **fields):
    """阶段增量更新。只写给定的列，不碰其它列。

    ★ 这里是整条链路里**唯一**允许"写失败也不许搞崩"的地方，理由是一条实测事故：
      机器上板子每 100 ms 在写 ingest、摄像头约 10 fps 在写照片，同一时刻语音 turn
      也在写它的阶段增量。Windows 上偶尔会撞到
          sqlite3.OperationalError: attempt to write a readonly database
      （文件被瞬时独占/防病毒扫描导致的短暂不可写）。
      而这个异常原本是从 flush() 里冒出去的 —— 于是**已经算完的这一轮**被整轮判成
      internal_error，用户看到的是"语音链路内部异常"，而他真正要的识别文本、
      任务回答全都丢了。**记账失败不能毁掉结果。**
      所以：先重试一次（绝大多数是瞬时的），仍失败就记一条 stderr 警告后继续。
      注意：预入库 `_insert_running` 不这样兜底 —— 那一行写不进去，后面根本没法收尾，
      该失败就失败（而且它发生在做任何动作之前，失败是安全的）。
    """
    if not fields:
        return
    keys = list(fields)
    sql = "UPDATE voice_turns SET " + ", ".join("%s=?" % k for k in keys) + " WHERE turn_id=?"
    args = [fields[k] for k in keys] + [turn_id]
    with _lock:
        for attempt in (1, 2):
            conn = _conn()
            try:
                conn.execute(sql, args)
                conn.commit()
                return
            except sqlite3.OperationalError as e:
                if attempt == 2:
                    sys.stderr.write("[voice] 阶段增量写入失败（已重试一次，跳过）：%s | %s\n"
                                     % (e, sorted(fields)))
                    sys.stderr.flush()
                    return
                time.sleep(0.05)
            finally:
                conn.close()


class StorageBusy(RuntimeError):
    """预入库写不进去（数据库瞬时不可写）。

    为什么要单独一个异常类型：这个失败**不是调用方的错**，也不是"语音链路内部异常"，
    而是"存储层这一刻忙/被独占"。回 500 会让前端以为代码坏了，回 503 + retryable
    才是真话。同时也让日志里一眼能分清"存储问题"和"逻辑问题"。
    """


def _insert_running(kw):
    """★ 预入库：turn 一开始就写一行 final_state='running'。

    这一步是第 6 周的关键改动。原来的写法是"全部算完再一次性 INSERT"，
    那样有一个致命问题：**在算的过程中这一轮根本不存在**，取消请求找不到它、
    `GET /voice/turns` 也看不到它。而"能不能被取消"完全取决于"别人找不找得到你"。

    ★ 与 `_patch()` 的区别：这里**不能**"写失败就算了继续跑"。
      这一行是后面所有阶段的落脚点 —— 写不进去，拿不到 turn_id 的引用，
      取消/停止/隔离全都没有对象，收尾也无处可写。所以这里的选择是
      **重试到放弃后抛 StorageBusy**，让上层回 503（可重试），
      绝不在"没有账本"的情况下继续做动作。
    """
    sql = (
        "INSERT INTO voice_turns (turn_id, device_id, audio_source, capture_location, "
        "run_location_json, audio_bytes, audio_sha256, sample_rate, bits, channels, "
        "duration_ms, rms, peak, zcr, vad_passed, final_state, stages_json, "
        "created_at_utc, session_id, request_id, turn_seq, received_at_utc, "
        "inject_json, delivery_state, playable) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    args = (kw["turn_id"], kw["device_id"], kw["audio_source"], kw["capture_location"],
            json.dumps(kw["run_location"], ensure_ascii=False), kw["audio_bytes"],
            kw["sha"], kw["info"]["sample_rate"], kw["info"]["bits"],
            kw["info"]["channels"], kw["info"]["duration_ms"], kw["st"]["rms"],
            kw["st"]["peak"], kw["st"]["zcr"],
            (None if kw.get("passed") is None else (1 if kw["passed"] else 0)),
            "running", "[]", kw["created"], kw["session_id"], kw["request_id"],
            kw["turn_seq"], kw["received_at"],
            json.dumps(kw["inject"], ensure_ascii=False) if kw["inject"] else None,
            None, 0)
    last = None
    # 现场实测：板子 10 Hz ingest + 摄像头 ~10 fps 同时写同一个库里时，
    # Windows 上偶发 "attempt to write a readonly database"（瞬时被独占）。
    # 退避重试三次即可覆盖；真不肯写才报出去。
    # 注意：sleep 放在 `with _lock` **外面** —— 睡在锁里等于把所有并发轮
    # 一起拖住，本该"快速失败重试"的事情会变成"全局卡顿"。
    for attempt, backoff in enumerate((0.0, 0.05, 0.2, 0.5)):
        with _lock:
            conn = _conn()
            try:
                conn.execute(sql, args)
                conn.commit()
                return
            except sqlite3.OperationalError as e:
                last = e
            finally:
                conn.close()
        if attempt == 3:
            break
        time.sleep(backoff)
    sys.stderr.write("[voice] 预入库失败（重试 3 次后放弃，本轮不执行任何动作）：%s\n" % (last,))
    sys.stderr.flush()
    raise StorageBusy(str(last))


def handle_turn(audio_bytes, params, remote=""):
    """处理一条语音 turn。params 来自 query string。

    第 6 周新增的 query 参数（都可省略，省略时行为与第 5 周完全一致）：
        session_id      会话（用于短期上下文 / 半双工 / 澄清）
        request_id      调用方生成的幂等标识（用于关联日志与迟到结果判定）
        client_ack_ms   前端上报的"本地已提交提示"耗时（本地事实，仅供参考）
        inject_*        教师侧注入（见 parse_inject）
    """
    received_at = _now()
    t0 = time.time()

    def t_ms():
        return int((time.time() - t0) * 1000)

    audio_source = (params.get("audio_source") or "").strip()
    capture_location = (params.get("capture_location") or "").strip()
    device_id = (params.get("device_id") or "").strip() or None
    session_id = (params.get("session_id") or "").strip() or None
    request_id = (params.get("request_id") or "").strip()
    # ★ 文字补充轮（第 6 周）：澄清时用户可以直接打字回答。
    #   走的是**同一个 handle_turn**、同一张表、同一套取消/隔离/时序 ——
    #   只是"输入段"从音频换成了键盘。这样"多轮上下文"就只在一个地方实现。
    text_input = (params.get("text") or "").strip()
    try:
        wait_ms = int(params.get("wait_ms") or 6000)
    except ValueError:
        wait_ms = 6000
    wait_ms = max(0, min(wait_ms, 20000))
    try:
        client_ack_ms = int(params.get("client_ack_ms") or 0) or None
    except ValueError:
        client_ack_ms = None

    inject, inj_err = parse_inject(params)
    if inj_err:
        return inj_err

    # ---- 红线 V5：来源必须在枚举内，位置必须有值（缺失就拒绝，不替调用方编）----
    if audio_source not in AUDIO_SOURCES:
        return _error(400, "AUDIO_SOURCE_INVALID",
                      "audio_source 必须是 %s 之一，收到 %r。"
                      "本周必须如实记录音频来源，不能留空或自填。" %
                      ("/".join(AUDIO_SOURCES), audio_source),
                      allowed=list(AUDIO_SOURCES))
    if not capture_location:
        return _error(400, "CAPTURE_LOCATION_MISSING",
                      "缺少 capture_location：必须说明录音发生在哪台机器/哪个设备"
                      "（例如 \"browser:Chrome/Windows-11 本机 PC\"）。"
                      "运行位置是本周的必交内容，不能省略。")

    # ---- 红线 V6：格式校验（文字补充轮没有音频，跳过 WAV 校验）----
    if text_input:
        if len(text_input) > 200:
            return _error(400, "TEXT_TOO_LONG",
                          "澄清轮的回答最多 200 字，收到 %d 字。" % len(text_input))
        info = {"sample_rate": None, "bits": None, "channels": None,
                "duration_ms": 0, "frames": 0, "data_bytes": 0, "block_align": None,
                "audio_format": None}
        pcm = b""
        st = {"samples": 0, "rms": None, "peak": None, "zcr": None}
        sha = None
    else:
        try:
            info, pcm = parse_wav(audio_bytes, require_16k=True)
        except ValueError as e:
            return _error(400, "AUDIO_FORMAT_INVALID", str(e),
                          expected={"sample_rate": 16000, "bits": 16, "channels": 1,
                                    "container": "RIFF/WAVE PCM"})

        if info["duration_ms"] < MIN_DURATION_MS:
            return _error(400, "AUDIO_TOO_SHORT",
                          "这段音频只有 %d ms，短于 %d ms，不可能是完整的一句话。请重新录音。"
                          % (info["duration_ms"], MIN_DURATION_MS),
                          duration_ms=info["duration_ms"])
        if info["duration_ms"] > MAX_DURATION_MS:
            return _error(400, "AUDIO_TOO_LONG",
                          "这段音频 %d ms，超过上限 %d ms。为避免给出不准确的识别结果，"
                          "请只说一句话（不超过 40 秒）。" % (info["duration_ms"], MAX_DURATION_MS),
                          duration_ms=info["duration_ms"])

        sha = hashlib.sha256(audio_bytes).hexdigest()
        st = pcm_stats(pcm)

    if not request_id:
        request_id = "rq_" + hashlib.sha256(
            ("%s|%s|%d" % (time.time_ns(), remote, len(audio_bytes))).encode()).hexdigest()[:16]
    turn_id = "vt_" + hashlib.sha256(
        ("%s|%s" % (request_id, time.time_ns())).encode()).hexdigest()[:20]
    created = received_at

    # ---- 会话登记（半双工：新 turn 一开始，旧 turn 就失去播报资格）----
    turn_seq, superseded_req, _state_before = voice_session.begin_turn(
        session_id, request_id, device_id)

    run_location = {
        "capture": capture_location,
        "asr": "%s · 待执行" % VOICE_ENGINE_URL,
        "task": "server.py · nl_assistant.handle_query（第 4 周同一函数，进程内调用）",
        "tts": "%s · 待执行" % VOICE_ENGINE_URL,
        "playback": (params.get("playback_location") or "browser <audio>（本机）").strip(),
        "session": ("voice_session.py · session=%s turn_seq=%s（半双工）"
                    % (session_id, turn_seq) if session_id
                    else "无会话（无状态单次调用：无短期上下文、无法取消）"),
        "cancel": "voice_session.request_cancel ← POST /api/v1/voice/session/<id>/cancel",
    }

    # ---- 预入库（能被"看见"才可能被"取消"）----
    _insert_running({
        "turn_id": turn_id, "device_id": device_id, "audio_source": audio_source,
        "capture_location": capture_location, "run_location": run_location,
        "audio_bytes": (0 if text_input else len(audio_bytes)), "sha": sha,
        "info": info, "st": st,
        "passed": None, "created": created, "session_id": session_id,
        "request_id": request_id, "turn_seq": turn_seq, "received_at": received_at,
        "inject": inject,
    })
    voice_session.log_event("turn_pending", session_id=session_id, request_id=request_id,
                            turn_id=turn_id,
                            audio_bytes=(0 if text_input else len(audio_bytes)),
                            text_input=bool(text_input))

    stages = []

    def stage(key, label, status, ms=None, detail="", **extra):
        s = {"key": key, "label": label, "status": status, "detail": detail}
        if ms is not None:
            s["ms"] = int(ms)
        s.update(extra)
        stages.append(s)
        return s

    def flush(final="running"):
        _patch(turn_id,
               final_state=final,
               stages_json=json.dumps(stages, ensure_ascii=False),
               elapsed_ms=t_ms(),
               asr_status=ctx["asr_status"], task_status=ctx["task_status"],
               tts_status=ctx["tts_status"])

    def guard(stage_key):
        """★ C1 / C3 的检查点：每个阶段边界都问一次"我还是被需要的那一条吗"。"""
        it = voice_session.interrupt_of(session_id, request_id)
        if it:
            raise _Interrupted(it, stage_key)

    ctx = {
        "asr_status": "skipped", "asr_engine": None, "asr_model": None, "asr_ms": None,
        "transcript": None, "asr_error": None,
        "task_status": "skipped", "task_intent": None, "task_claim": None,
        "task_remote_status": None, "task_source": None, "task_answer": None,
        "task_response": None, "task_error": None,
        "tts_status": "skipped", "tts_engine": None, "tts_voice": None, "tts_ms": None,
        "reply_text": None, "reply_bytes": None, "reply_sha": None, "tts_error": None,
        "tts_truncated": None,
    }
    transcript = None
    reply_wav = None
    full_answer = ""
    final_state = "running"
    error_code = None
    error_message = None
    device_source = "request" if device_id else "none"
    effective_device = device_id
    first_feedback_ms = None
    task_done_ms = None
    tts_ready_ms = None
    clarify_ctx = None          # 本轮**提出**的反问
    clarify_from = None         # 本轮回答的是哪一轮的反问
    clarify_resolved = None
    clarify_how = None
    clarify_round = None
    action_taken = False
    interrupted_info = None
    interrupted_stage = None

    def do_tts(text, note_full_chars, outcome_state):
        """合成播报。抽成函数是因为"反问"也要念出来 —— 语音助手不能在屏幕上问问题。"""
        nonlocal reply_wav, tts_ready_ms
        reply_text, trunc = speech_excerpt(text)
        ctx["reply_text"] = reply_text
        ctx["tts_truncated"] = trunc
        if not reply_text.strip():
            ctx["tts_status"] = "skipped"
            stage("tts", "语音合成", "skipped", 0, "要播的内容为空，未合成语音")
            return "done", None, None
        if len(reply_text) > MAX_TTS_CHARS:
            ctx["tts_status"] = "failed"
            ctx["tts_error"] = ("回答 %d 字，超过播报上限 %d 字（不截断，避免改变语义）"
                                % (len(reply_text), MAX_TTS_CHARS))
            stage("tts", "语音合成", "failed", 0, ctx["tts_error"])
            return "tts_failed", "TTS_TEXT_TOO_LONG", ctx["tts_error"] + "。文字回答仍然完整可用。"
        s2, r2 = call_tts(reply_text, TTS_TIMEOUT_S)
        ctx["tts_ms"] = r2.get("ms")
        if s2 == "ok":
            ctx["tts_status"] = "ok"
            ctx["tts_engine"] = r2.get("engine")
            ctx["tts_voice"] = r2.get("voice")
            reply_wav = r2.get("data") or b""
            ctx["reply_bytes"] = len(reply_wav)
            ctx["reply_sha"] = hashlib.sha256(reply_wav).hexdigest()
            tts_ready_ms = t_ms()
            stage("tts", "语音合成", "ok", r2.get("ms"),
                  "引擎 %s / 语音 %s · 产出 %.1f 秒 WAV（%d 字节）%s" % (
                      ctx["tts_engine"], ctx["tts_voice"],
                      (r2.get("audio_ms") or 0) / 1000.0, ctx["reply_bytes"],
                      "· 播报为原文前 %d 句节选（完整回答 %d 字）" % (
                          TTS_EXCERPT_SENTENCES, note_full_chars)
                      if trunc else "· 全文播报"),
                  engine=ctx["tts_engine"], voice=ctx["tts_voice"],
                  audio_ms=r2.get("audio_ms"), bytes=ctx["reply_bytes"],
                  sha256=ctx["reply_sha"], truncated=trunc, full_chars=note_full_chars)
            return outcome_state, None, None
        ctx["tts_status"] = s2
        ctx["tts_error"] = r2.get("error")
        stage("tts", "语音合成", s2, ctx["tts_ms"], ctx["tts_error"] or "合成失败")
        return "tts_failed", ("TTS_FAILED" if s2 == "failed" else "TTS_UNAVAILABLE"), \
            ("语音合成没成功：%s。识别文本与文字回答都不受影响，可以直接阅读回答。"
             % (ctx["tts_error"] or "合成引擎报错"))

    try:
        # ---------------------------------------------------------------- ① recorded
        if text_input:
            stage("recorded", "输入", "ok", None,
                  "键盘输入 · %d 字 · 没有音频（澄清轮的补充回答）" % len(text_input),
                  audio_source=audio_source, bytes=0, text_chars=len(text_input),
                  received_at_utc=received_at,
                  t0_note="时序基准 t0 = 服务端收到这条回答的时刻")
        else:
            stage("recorded", "录音", "ok", None,
                  "%s · %.2f 秒 · %d Hz/%d bit/%d 声道 · %d 字节 · sha256=%s…" % (
                      SOURCE_LABELS.get(audio_source, audio_source),
                      info["duration_ms"] / 1000.0,
                      info["sample_rate"], info["bits"], info["channels"],
                      len(audio_bytes), sha[:12]),
                  audio_source=audio_source, bytes=len(audio_bytes), sha256=sha,
                  sample_rate=info["sample_rate"], bits=info["bits"],
                  channels=info["channels"], duration_ms=info["duration_ms"],
                  received_at_utc=received_at,
                  t0_note="时序基准 t0 = 服务端收到完整音频的时刻")

        # ---------------------------------------------------------------- ② vad
        if text_input:
            # 文字不需要静音检测：没有音频可判，也不该假装判过。
            passed = None
            _patch(turn_id, vad_passed=None)
            stage("vad", "静音检测", "skipped", 0,
                  "文字输入没有音频，不需要静音检测（不记录 RMS，也不假装判过）",
                  rms=None, peak=None, zcr=None, passed=None)
        else:
            passed = (st["rms"] >= VAD_RMS_MIN) and (st["peak"] >= VAD_PEAK_MIN)
            _patch(turn_id, vad_passed=1 if passed else 0)
            stage("vad", "静音检测", "ok" if passed else "failed", 0,
                  "RMS=%.4f（阈值 %.4f）· 峰值=%.4f（阈值 %.4f）· 过零率=%.4f → %s" % (
                      st["rms"], VAD_RMS_MIN, st["peak"], VAD_PEAK_MIN, st["zcr"],
                      "有语音能量" if passed else "判定为无有效语音，**未提交识别**"),
                  rms=st["rms"], peak=st["peak"], zcr=st["zcr"],
                  rms_min=VAD_RMS_MIN, peak_min=VAD_PEAK_MIN, passed=passed)
        flush()

        if passed is False:
            # 无声：明确告诉用户"没听到"，并且**不调 ASR**
            stage("asr", "语音识别", "skipped", 0,
                  "静音检测未通过，本次没有提交给识别引擎（不会凭空产生文本）")
            stage("task", "任务执行", "skipped", 0, "没有识别文本，未执行任何任务")
            stage("tts", "语音合成", "skipped", 0, "没有回答，未合成语音")
            final_state, error_code = "no_speech", "NO_SPEECH"
            error_message = ("没有检测到有效语音：这段录音的 RMS 为 %.4f（需要 ≥ %.4f），"
                             "峰值 %.4f（需要 ≥ %.4f）。可能麦克风被静音、输入音量太小，"
                             "或者没有对着麦克风说话。" % (st["rms"], VAD_RMS_MIN,
                                                    st["peak"], VAD_PEAK_MIN))
            first_feedback_ms = t_ms()
            raise _Terminate()

        # 录音刚结束、识别还没开始 —— 用户完全可能这时候就改主意了
        guard("before_asr")

        # ---------------------------------------------------------------- ③ asr
        if text_input:
            # 文字轮：不调识别引擎。asr_status 仍记 ok（因为"有可用文本"成立），
            # 但 engine 写成 keyboard，detail 里说清"没有调用识别引擎" ——
            # 绝不把键盘输入伪装成"识别出来的"。
            transcript = normalize_text(text_input)
            ctx["transcript"] = transcript
            ctx["asr_status"] = "ok"
            ctx["asr_engine"] = "keyboard"
            ctx["asr_model"] = None
            ctx["asr_ms"] = 0
            stage("asr", "语音识别", "ok", 0,
                  "文字输入，**没有调用识别引擎** · 原文“%s”" % transcript,
                  engine="keyboard", model=None, raw_text=text_input, text=transcript,
                  injected=False, note="这是键盘输入，不是识别结果")
            flush()
        else:
            if inject and inject.get("delay_ms"):
                # ★ 注入的"慢识别"：睡在 ASR 之前，等价于"引擎很慢"。
                #   睡在这里而不是睡在链路开头，是为了让取消窗口正好落在识别期间 ——
                #   那正是本周要验证的那个窗口。
                time.sleep(min(inject["delay_ms"], MAX_INJECT_MS) / 1000.0)

            if inject and inject.get("asr_text"):
                # 注入"听错"：跳过引擎，直接给一个指定文本，但**必须**标成 injected
                text_inj = normalize_text(inject["asr_text"])
                status, r = "ok", {"engine": INJECTED_ASR_ENGINE, "model": "manual",
                                   "ms": 0, "text": text_inj, "empty": not bool(text_inj),
                                   "roundtrip_ms": 0}
            else:
                status, r = call_asr(audio_bytes, ASR_TIMEOUT_S)
            ctx["asr_ms"] = r.get("ms")
            ctx["asr_engine"] = r.get("engine")
            ctx["asr_model"] = r.get("model")
            if status == "ok" and r.get("empty"):
                status = "empty"
            if status == "ok":
                raw_text = r.get("text") or ""
                transcript = normalize_text(raw_text)
                ctx["transcript"] = transcript
                ctx["asr_status"] = "ok"
                stage("asr", "语音识别", "ok", ctx["asr_ms"],
                      ("引擎 %s / %s · %.2f 秒 → “%s”%s" % (
                          ctx["asr_engine"], ctx["asr_model"], (ctx["asr_ms"] or 0) / 1000.0,
                          transcript,
                          "  ★注入" if ctx["asr_engine"] == INJECTED_ASR_ENGINE else "")),
                      engine=ctx["asr_engine"], model=ctx["asr_model"], raw_text=raw_text,
                      text=transcript, injected=(ctx["asr_engine"] == INJECTED_ASR_ENGINE))
            else:
                ctx["asr_status"] = status
                ctx["asr_error"] = r.get("error")
                stage("asr", "语音识别", status, ctx["asr_ms"],
                      ctx["asr_error"] or "识别失败", engine=ctx["asr_engine"])
            flush()

        if ctx["asr_status"] != "ok":
            stage("task", "任务执行", "skipped", 0, "没有可用的识别文本，未执行任务")
            stage("tts", "语音合成", "skipped", 0, "没有回答，未合成语音")
            final_state = {"empty": "no_speech", "failed": "asr_failed",
                           "timeout": "asr_timeout", "unavailable": "asr_unavailable",
                           "skipped": "asr_failed"}.get(ctx["asr_status"], "asr_failed")
            error_code = {"empty": "ASR_EMPTY", "failed": "ASR_FAILED",
                          "timeout": "ASR_TIMEOUT", "unavailable": "ASR_UNAVAILABLE"
                          }.get(ctx["asr_status"], "ASR_FAILED")
            error_message = {
                "empty": "识别引擎返回了空文本：这段录音里没有可辨识的中文语音。"
                         "请靠近麦克风、放慢语速重说一遍。",
                "failed": "识别失败：%s。" % (ctx["asr_error"] or "引擎报错"),
                "timeout": "识别超时：引擎在 %.0f 秒内没有返回结果。"
                           "本次没有得到任何文本，也没有执行任何任务。" % ASR_TIMEOUT_S,
                "unavailable": "识别服务不可用：%s。"
                               "文字入口不受影响，可以直接在「语言助手」里输入同一句话。"
                               % (ctx["asr_error"] or "无法连接引擎"),
            }.get(ctx["asr_status"], "识别未成功")
            first_feedback_ms = t_ms()
            raise _Terminate()

        # 识别出文本 = 用户第一次拿到"我听到了"这个反馈
        first_feedback_ms = t_ms()

        # ★★ C1 最关键的一行 ★★
        #   识别已经完成、任务还没执行 —— 这是整个链路里**唯一**能干净取消的窗口。
        #   放在这里检查，是为了保证"取消了就不会有设备动作"。
        guard("after_asr")

        # ---------------------------------------------------------------- 澄清消解
        query_text = transcript
        task_device = device_id
        prev_clarify = voice_session.get_clarify(session_id) if session_id else None
        if prev_clarify:
            res = voice_session.resolve_clarify(prev_clarify, transcript)
            clarify_from = prev_clarify.get("source_turn_id")
            clarify_round = int(prev_clarify.get("round") or 1) + 1
            if res["resolved"]:
                query_text = res["query"] or transcript
                clarify_resolved = query_text
                clarify_how = res["how"]
                if res.get("device_id"):
                    task_device = res["device_id"]
                    effective_device = task_device
                    device_source = "clarify_resolved"
                voice_session.log_event(
                    "clarify_resolved", session_id=session_id, request_id=request_id,
                    turn_id=turn_id, how=res["how"], resolved=query_text,
                    device_id=task_device)
            else:
                # 没消解：要么再问一次，要么（到轮数上限）给退路
                nxt = int(prev_clarify.get("round") or 1) + 1
                if nxt > voice_session.MAX_CLARIFY_ROUNDS:
                    clarify_ctx = dict(prev_clarify)
                    clarify_ctx.update({
                        "round": nxt, "exhausted": True,
                        "fallback": ("已经问了 %d 轮还没说清，先停下。你可以直接说"
                                     "「查看上次数据」/「重新采集一次」/「设备在线吗」，"
                                     "或者改用文字入口打字 —— 打字的歧义更少。"
                                     % voice_session.MAX_CLARIFY_ROUNDS)})
                    stage("task", "任务执行", "skipped", 0,
                          "澄清 %d 轮未消解，已到上限，本轮不执行任何工具"
                          % voice_session.MAX_CLARIFY_ROUNDS)
                    final_state = "clarify_exhausted"
                    error_code = "CLARIFY_EXHAUSTED"
                    error_message = clarify_ctx["fallback"]
                    full_answer = clarify_ctx["fallback"]
                    tts_out, ec, em = do_tts(full_answer, len(full_answer), "clarify_exhausted")
                    # ★ 合成失败时才把终局改写成 tts_failed；合成成功时**保留**
                    #   CLARIFY_EXHAUSTED 与那句退路说明 —— 否则"为什么停下"就说不出来了。
                    if tts_out == "tts_failed":
                        final_state, error_code, error_message = tts_out, ec, em
                    raise _Terminate()
                # 再问一遍同一个问题（轮次 +1）
                clarify_ctx = dict(prev_clarify)
                clarify_ctx["round"] = nxt
                clarify_ctx["repeated"] = True
                stage("task", "任务执行", "skipped", 0,
                      "澄清未消解（第 %d 轮），再问一次；本轮没有调用任何业务工具" % nxt)
                voice_session.set_clarify(session_id, clarify_ctx)
                full_answer = clarify_ctx.get("question") or "你能再说清楚一点吗？"
                ctx["task_status"] = "clarify"
                ctx["task_answer"] = full_answer
                final_state = "clarifying"
                tts_out, ec, em = do_tts(full_answer, len(full_answer), "clarifying")
                final_state, error_code, error_message = tts_out, ec, em
                raise _Terminate()

        # ---------------------------------------------------------------- ④ task
        t_task = time.time()
        try:
            payload = {"q": query_text, "wait_ms": wait_ms}
            if task_device:
                payload["device_id"] = task_device
            tstatus, tresp = _st["nl_query"](payload)
            task_ms = int((time.time() - t_task) * 1000)
            action_taken = True    # 任务段真的跑过了（可能有副作用）
            task_done_ms = t_ms()
            if inject and inject.get("task_ms"):
                time.sleep(min(inject["task_ms"], MAX_INJECT_MS) / 1000.0)
                task_done_ms = t_ms()
            if tstatus == 200 and tresp.get("ok"):
                d = tresp["data"]
                ctx["task_status"] = "ok"
                ctx["task_intent"] = d.get("intent")
                ctx["task_claim"] = d.get("claim")
                ctx["task_remote_status"] = d.get("status")
                ctx["task_source"] = (d.get("evidence") or {}).get("source")
                ctx["task_answer"] = d.get("answer") or ""
                ctx["task_response"] = d
                full_answer = ctx["task_answer"]
                stage("task", "任务执行", "ok", task_ms,
                      "意图=%s · claim=%s · 状态=%s · 来源=%s%s" % (
                          ctx["task_intent"], ctx["task_claim"], ctx["task_remote_status"],
                          ctx["task_source"],
                          ("（消解自澄清：" + clarify_how + "）" if clarify_how else "")),
                      intent=ctx["task_intent"], claim=ctx["task_claim"],
                      remote_status=ctx["task_remote_status"], source=ctx["task_source"],
                      action_taken=True,
                      clarify_how=clarify_how, clarify_resolved=clarify_resolved)
            else:
                ctx["task_status"] = "failed"
                err = (tresp.get("error") or {})
                ctx["task_error"] = "%s: %s" % (err.get("code", "HTTP_%d" % tstatus),
                                                err.get("message", ""))
                ctx["task_response"] = {"error": err}
                stage("task", "任务执行", "failed", task_ms, ctx["task_error"],
                      action_taken=True)
        except Exception as e:
            ctx["task_status"] = "failed"
            ctx["task_error"] = "%s: %s" % (type(e).__name__, e)
            action_taken = True
            stage("task", "任务执行", "failed", 0, ctx["task_error"], action_taken=True)
        flush()

        if ctx["task_status"] != "ok":
            stage("tts", "语音合成", "skipped", 0,
                  "任务未成功，没有可播报的回答（不会播报一句编出来的话）")
            final_state = "task_failed"
            error_code = "TASK_FAILED"
            error_message = "识别到了“%s”，但任务执行失败：%s" % (transcript, ctx["task_error"])
            raise _Terminate()

        # ---- 任务返回"我需要澄清"（第 4 周的 ambiguity 结构）----
        d = ctx["task_response"] or {}
        if d.get("status") == "clarify" and d.get("ambiguity"):
            amb = d["ambiguity"]
            new_ctx = voice_session.build_clarify(
                session_id, amb, base_query=query_text, device_id=task_device,
                turn_id=turn_id)
            clarify_round = int(new_ctx.get("round") or 1)
            if new_ctx.get("exhausted"):
                clarify_ctx = new_ctx
                final_state = "clarify_exhausted"
                error_code = "CLARIFY_EXHAUSTED"
                error_message = new_ctx["fallback"]
                full_answer = new_ctx["fallback"]
                tts_out, ec, em = do_tts(full_answer, len(full_answer), "clarify_exhausted")
                if tts_out == "tts_failed":
                    final_state, error_code, error_message = tts_out, ec, em
                raise _Terminate()
            clarify_ctx = new_ctx
            if session_id:
                voice_session.set_clarify(session_id, new_ctx)
            full_answer = new_ctx["question"]
            ctx["task_answer"] = full_answer
            stage("task", "任务执行", "ok", task_ms,
                  "需要澄清（kind=%s，第 %d/%d 轮）：%s —— 本轮**没有调用任何业务工具**"
                  % (new_ctx["kind"], new_ctx["round"], voice_session.MAX_CLARIFY_ROUNDS,
                     new_ctx["question"]),
                  intent=ctx["task_intent"], claim=ctx["task_claim"],
                  remote_status="clarify", source="无（歧义未消解，未调用业务工具）",
                  action_taken=False, clarify_kind=new_ctx["kind"],
                  clarify_round=new_ctx["round"])
            # ★ 反问也要念出来：语音助手在屏幕上问问题是没用的
            tts_out, ec, em = do_tts(full_answer, len(full_answer), "clarifying")
            final_state, error_code, error_message = tts_out, ec, em
            raise _Terminate()

        # ---- 任务跑完了，用户仍可能在这时候按停止（此时动作已发生，收不回来）----
        guard("after_task")

        # ---------------------------------------------------------------- ⑤ tts
        out_state, ec, em = do_tts(full_answer, len(full_answer), "done")
        final_state, error_code, error_message = out_state, ec, em
        flush()

        # ---- 合成完了、还没播：这是最后一个能拦"迟到结果"的窗口 ----
        guard("after_tts")

    except _Terminate:
        pass
    except _Interrupted as e:
        interrupted_info = e.info
        interrupted_stage = e.stage_key
    except Exception as e:                      # 兜底：任何意外都要留下可读的终局
        import traceback
        traceback.print_exc()
        final_state = "internal_error"
        error_code = "VOICE_INTERNAL"
        error_message = "语音链路内部异常：%r" % (e,)

    # ---- 被取消 / 被顶掉：终局改写 + 迟到结果隔离（C2/C3）----
    cancel_requested_at = None
    cancel_seen_at = None
    cancel_reason = None
    cancel_stage = None
    superseded_by = None
    if interrupted_info:
        cancel_seen_at = _now()
        cancel_stage = interrupted_stage
        # ★ 被拦下的阶段必须显式标"跳过"。面板上留空等于"不知道有没有跑"，
        #   而"确定没跑"和"不知道"是两件完全不同的事 —— 前者正是本周要给出的结论。
        _done_keys = {s["key"] for s in stages}
        for _k, _label in (("task", "任务执行"), ("tts", "语音合成")):
            if _k not in _done_keys:
                stage(_k, _label, "skipped", 0,
                      "整轮已被拦住（%s），这一段没有执行" % interrupted_info["kind"])
        if interrupted_info["kind"] == "cancelled":
            final_state = "cancelled"
            cancel_reason = interrupted_info.get("reason") or "user_stop"
            cancel_requested_at = interrupted_info.get("at") or cancel_seen_at
            # 迟到结果：即使识别/任务已经跑了，也不再往下走、不产出可播放内容
            error_code = "CANCELLED"
            if action_taken:
                error_message = (
                    "这一轮在「%s」之后被取消。★ 任务段已经跑过（action_taken=1）："
                    "如果它是「请求采集」，那条采集指令**已经真实下发**，取消收不回它。"
                    "取消的作用是：不再继续后续阶段、不产出可播放的播报。" % (cancel_stage,))
            else:
                error_message = (
                    "这一轮在「%s」被取消，任务段一行都没有执行（action_taken=0）："
                    "没有写库、没有向设备下发任何指令。识别结果如实保留，但不执行、不播报。"
                    % (cancel_stage,))
            stage("playback", "播放", "skipped", 0, "整轮已被取消，不提供播报")
        else:
            final_state = "superseded"
            cancel_reason = "superseded"
            superseded_by = interrupted_info.get("by")
            error_code = "SUPERSEDED"
            error_message = (
                "这一轮已经被新任务顶掉（新 request_id=%s）。这是半双工的必然结果："
                "同一时刻只有一条 turn 有播报资格。它的结果只作为历史记录保留，"
                "不会覆盖新任务，也不会自动播放。" % (superseded_by,))
            stage("playback", "播放", "skipped", 0, "已被新任务顶掉，不再播报")
        voice_session.log_event(
            ("cancel_seen" if interrupted_info["kind"] == "cancelled" else "superseded_seen"),
            session_id=session_id, request_id=request_id, turn_id=turn_id,
            stage=cancel_stage, action_taken=action_taken,
            superseded_by=superseded_by)
        # ★ 被拦下的阶段必须显式标"跳过"。面板上留空等于"不知道有没有跑"，
        #   而"确定没跑"和"不知道"是两件完全不同的事 —— 前者是本周要给出的结论。
        done_keys = {s["key"] for s in stages}
        for _k, _label in (("task", "任务执行"), ("tts", "语音合成")):
            if _k not in done_keys:
                stage(_k, _label, "skipped", 0,
                      "整轮已在「%s」被拦住，这一段没有执行（action_taken=%s）"
                      % (cancel_stage, 1 if action_taken else 0))
    else:
        # ---- ⑥ playback：等待浏览器上报，服务端不预写 ----
        awaitable = final_state in ("done", "clarifying", "clarify_exhausted")
        stage("playback", "播放", "pending" if awaitable else "skipped", 0,
              "等待浏览器播放器开始播放（服务端不推断“已听到”）"
              if awaitable else "本次没有可播放的音频")

    elapsed = t_ms()

    # ---- 交付资格：这是 C2 的落点 ----
    if final_state in ("cancelled",):
        delivery_state = "quarantined_cancelled"
        playable = False
    elif final_state == "superseded":
        delivery_state = "quarantined_superseded"
        playable = False
    elif reply_wav:
        delivery_state = "deliverable"
        playable = True
    else:
        delivery_state = "no_audio"
        playable = False

    if not playable and reply_wav:
        # 音频留着当证据（事后可核对"它当时到底说了什么"），但拿不到 —— 见 audio_path()
        delivery_why = ("这一轮（%s）的播报已被隔离：可以回看记录，但不能播放。"
                        % final_state)
    elif not playable:
        delivery_why = "这一轮没有产出播报音频（final_state=%s）。" % final_state
    else:
        delivery_why = None

    # ---- 运行位置：每一段都要有值（红线 V5）----
    hl = engine_health()
    asr_loc = "%s · %s" % (VOICE_ENGINE_URL,
                           ("%s/%s（离线）" % (hl.get("asr", {}).get("backend"),
                                              hl.get("asr", {}).get("model_dir", "").replace("\\", "/").split("/")[-1])
                            if hl.get("ok") else "引擎未就绪"))
    if ctx["asr_engine"] == INJECTED_ASR_ENGINE:
        asr_loc = "injected · 教师侧注入（非真实识别）"
    run_location["asr"] = asr_loc
    run_location["tts"] = "%s · %s/%s" % (VOICE_ENGINE_URL, ctx["tts_engine"] or "—",
                                         ctx["tts_voice"] or "—")

    # ---- 落盘：录音与回答都保留，可回放核对 ----
    if not text_input:
        audio_path_full = os.path.join(VOICE_DIR, turn_id + "_in.wav")
        with open(audio_path_full, "wb") as f:
            f.write(audio_bytes)
    if reply_wav:
        with open(os.path.join(VOICE_DIR, turn_id + "_out.wav"), "wb") as f:
            f.write(reply_wav)

    # ---- 终局落库 ----
    # ★ 列名与取值成对写在同一个列表里（_COLS），占位符由长度生成。
    #   手写 44 个 "?" 对着 43 个列名，第一次就撞上 "44 values for 43 columns"，
    #   这种错在运行期才炸、且只在有请求时才出现 —— 改成结构化写法避免再犯。
    kv = dict(
        turn_id=turn_id, device_id=effective_device, audio_source=audio_source,
        capture_location=capture_location, run_location=run_location,
        audio_bytes=(0 if text_input else len(audio_bytes)), sha=sha,
        info=info, st=st, passed=passed,
        asr_status=ctx["asr_status"], asr_engine=ctx["asr_engine"],
        asr_model=ctx["asr_model"], asr_ms=ctx["asr_ms"], transcript=ctx["transcript"],
        asr_error=ctx["asr_error"],
        task_status=ctx["task_status"], task_intent=ctx["task_intent"],
        task_claim=ctx["task_claim"], task_remote_status=ctx["task_remote_status"],
        task_source=ctx["task_source"], task_answer=ctx["task_answer"],
        task_response=ctx["task_response"], task_error=ctx["task_error"],
        tts_status=ctx["tts_status"], tts_engine=ctx["tts_engine"],
        tts_voice=ctx["tts_voice"], tts_ms=ctx["tts_ms"], reply_text=ctx["reply_text"],
        reply_bytes=ctx["reply_bytes"], reply_sha=ctx["reply_sha"],
        tts_error=ctx["tts_error"], tts_truncated=ctx["tts_truncated"],
        final_state=final_state, error_code=error_code, error_message=error_message,
        stages=stages, elapsed=elapsed, created=created,
        session_id=session_id, request_id=request_id, turn_seq=turn_seq,
        superseded_by=superseded_by, received_at=received_at,
        first_feedback_ms=first_feedback_ms, task_done_ms=task_done_ms,
        tts_ready_ms=tts_ready_ms, client_ack_ms=client_ack_ms,
        delivery_state=delivery_state, playable=playable,
        cancel_requested_at=cancel_requested_at, cancel_seen_at=cancel_seen_at,
        cancel_reason=cancel_reason, cancel_stage=cancel_stage,
        action_taken=action_taken,
        clarify_round=clarify_round, clarify_ctx=clarify_ctx,
        clarify_from=clarify_from, clarify_resolved=clarify_resolved,
        clarify_how=clarify_how,
        inject=inject,
    )
    cols = [c for c, _ in _COLS]
    vals = [fn(**kv) for _, fn in _COLS]
    sets = ", ".join("%s=?" % c for c in cols if c != "turn_id")
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE voice_turns SET " + sets + " WHERE turn_id=?",
                         [vals[i] for i, c in enumerate(cols) if c != "turn_id"] + [turn_id])
            conn.commit()
        finally:
            conn.close()

    # ---- 会话收尾：只有"还是 active"的那一条才允许推进状态 ----
    voice_session.finish_turn(session_id, request_id, final_state, turn_id, turn_seq)

    # 冲掉待澄清上下文：
    #   · 本轮**提出了**新的反问（clarifying）→ 必须保留，否则下一句没法消解；
    #   · 本轮"没听清/识别不出"（no_speech / asr_*）→ 保留上一轮的反问（用户只是没说完）；
    #   · 其余（done / cancelled / superseded / clarify_exhausted / task_failed）→ 话题结束，清掉。
    if session_id:
        keep = final_state in ("clarifying", "no_speech", "asr_failed",
                               "asr_timeout", "asr_unavailable")
        if not keep:
            voice_session.clear_clarify(session_id)

    data = {
        "turn_id": turn_id,
        "request_id": request_id,
        "session": {
            "session_id": session_id,
            "turn_seq": turn_seq,
            "half_duplex": True,
            "superseded_request_id": superseded_req,
            "superseded_by": superseded_by,
            "note": ("半双工：新任务开始即顶掉旧任务；旧任务的结果不会覆盖新任务"
                     if session_id else "无会话：本次是无状态单次调用，没有短期上下文"),
        },
        "final_state": final_state,
        "audio_source": audio_source,
        "audio_source_label": SOURCE_LABELS.get(audio_source, audio_source),
        # ★ 这里给的是**最终生效**的设备：如果设备是用户在澄清轮里选出来的，
        #   返回原始参数（None）会让人以为"它没落到任何设备上"。
        "device_id": effective_device,
        "device_id_source": device_source,
        "audio": {"bytes": (0 if text_input else len(audio_bytes)), "sha256": sha,
                  "sample_rate": info["sample_rate"], "bits": info["bits"],
                  "channels": info["channels"], "duration_ms": info["duration_ms"],
                  "rms": st["rms"], "peak": st["peak"], "zcr": st["zcr"],
                  "vad_passed": passed},
        "stages": stages,
        "transcript": ctx["transcript"],   # ★ 只有识别成功才非空（红线 V1）
        "asr": {"status": ctx["asr_status"], "engine": ctx["asr_engine"],
                "model": ctx["asr_model"], "ms": ctx["asr_ms"], "error": ctx["asr_error"],
                "injected": ctx["asr_engine"] == INJECTED_ASR_ENGINE},
        "task": {"status": ctx["task_status"], "intent": ctx["task_intent"],
                 "claim": ctx["task_claim"], "remote_status": ctx["task_remote_status"],
                 "source": ctx["task_source"], "answer": ctx["task_answer"],
                 "error": ctx["task_error"],
                 "response": ctx["task_response"]},
        "tts": {"status": ctx["tts_status"], "engine": ctx["tts_engine"],
                "voice": ctx["tts_voice"], "ms": ctx["tts_ms"],
                "reply_text": ctx["reply_text"], "bytes": ctx["reply_bytes"],
                "sha256": ctx["reply_sha"], "error": ctx["tts_error"],
                "truncated": bool(ctx["tts_truncated"]) if ctx["tts_status"] == "ok" else False,
                "full_chars": len(full_answer or ""),
                "note": ("语音只念回答的前几句（原文前缀，未改写），完整内容见文字回答"
                         if (ctx["tts_status"] == "ok" and ctx["tts_truncated"]) else
                         "语音播报完整回答" if ctx["tts_status"] == "ok" else None)},
        # ★ 交付资格：前端据此决定"要不要给它一个 src"，服务端据此决定"给不给文件"
        "delivery": {"state": delivery_state, "playable": bool(playable),
                     "why": delivery_why},
        "reply_audio_url": ("/api/v1/voice/reply/%s.wav" % turn_id) if (reply_wav and playable) else None,
        "reply_audio_quarantined": bool(reply_wav and not playable),
        "audio_url": (None if text_input else "/api/v1/voice/audio/%s.wav" % turn_id),
        "cancel": {
            "requested_at_utc": cancel_requested_at,
            "seen_at_utc": cancel_seen_at,
            "reason": cancel_reason,
            "stage": cancel_stage,
            "action_taken": bool(action_taken),
            "note": ("取消发生在任务之后：动作已发生，收不回来（记录 action_taken=1）"
                     if (interrupted_info and action_taken) else
                     "取消发生在任务之前：任务段一行都没有执行" if interrupted_info else None),
        },
        "clarify": {
            "asked": clarify_ctx,
            "round": clarify_round,
            "from_turn_id": clarify_from,
            "resolved_with": clarify_resolved,
            "how": clarify_how,
            "max_rounds": voice_session.MAX_CLARIFY_ROUNDS,
            "pending": bool(clarify_ctx and not clarify_ctx.get("exhausted")
                            and final_state == "clarifying"),
        },
        "timing": {
            "t0_utc": received_at,
            "t0_note": "t0 = 服务端收到完整音频的时刻（= 输入结束的可证代理量）",
            "first_feedback_ms": first_feedback_ms,
            "task_done_ms": task_done_ms,
            "tts_ready_ms": tts_ready_ms,
            "client_ack_ms": client_ack_ms,
            "total_ms": elapsed,
            "note": ("first_feedback = 识别出文本（用户第一次看到/听到实质内容）；"
                     "client_ack 是前端本地提示，属本地事实"),
        },
        "inject": inject,
        "playback": {"started_at_utc": None, "reported_by": None,
                     "note": "播放是浏览器侧事实；服务端只记录上报，不推断“已听到”"},
        "stop": {"stopped_at_utc": None, "played_ms": None, "reason": None},
        "run_location": run_location,
        "error": ({"code": error_code, "message": error_message} if error_code else None),
        "elapsed_ms": elapsed,
        "created_at_utc": created,
        "engine_health": hl,
    }
    return 200, {"ok": True, "data": data}


# ===========================================================================
# 文字补充轮（澄清时用键盘回答）
# ===========================================================================
def handle_text_turn(payload, remote=""):
    """POST /api/v1/voice/text —— 澄清轮里用户直接打字回答。

    ★ 这不是"另开一条任务路径"：它只是把 handle_turn 的**输入段**换成键盘，
      后面的澄清消解、任务执行、播报、取消、时序全部走同一套代码同一张表。
      如果为了省事另写一条"文字任务链"，两条路迟早会在"claim 分档 / 取消语义"
      这些地方分叉 —— 那正是第 4 周围绕 V2 反复强调的东西。
    """
    payload = payload or {}
    text = (payload.get("text") or payload.get("q") or "").strip()
    if not text:
        return _error(400, "MISSING_FIELD", "缺少必填字段 text（你补充的那句话）")
    params = {
        "text": text,
        "audio_source": "text_input",
        "capture_location": (payload.get("capture_location")
                             or "keyboard:浏览器输入框 · 本机 PC（页面所在的这台机器）"),
        "playback_location": payload.get("playback_location"),
        "device_id": payload.get("device_id"),
        "session_id": payload.get("session_id"),
        "request_id": payload.get("request_id"),
        "wait_ms": payload.get("wait_ms"),
        "client_ack_ms": payload.get("client_ack_ms"),
        "inject_delay_ms": payload.get("inject_delay_ms"),
        "inject_task_ms": payload.get("inject_task_ms"),
        "inject_asr_text": payload.get("inject_asr_text"),
    }
    return handle_turn(b"", params, remote)


# ===========================================================================
# 查询
# ===========================================================================
def _get(row, key, default=None):
    """老库里可能没有这一列（未跑过 db_init）——读不到就给 default，不要炸。"""
    try:
        v = row[key]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def _row_to_dict(row, with_response=True):
    stages = []
    try:
        stages = json.loads(row["stages_json"] or "[]")
    except Exception:
        stages = []
    playable = bool(_get(row, "playable", 0))
    has_reply = bool(row["reply_audio_bytes"])
    d = {
        "turn_id": row["turn_id"],
        "request_id": _get(row, "request_id"),
        "device_id": row["device_id"],
        "audio_source": row["audio_source"],
        "audio_source_label": SOURCE_LABELS.get(row["audio_source"], row["audio_source"]),
        "capture_location": row["capture_location"],
        "run_location": json.loads(row["run_location_json"] or "{}"),
        "audio": {"bytes": row["audio_bytes"], "sha256": row["audio_sha256"],
                  "sample_rate": row["sample_rate"], "bits": row["bits"],
                  "channels": row["channels"], "duration_ms": row["duration_ms"],
                  "rms": row["rms"], "peak": row["peak"], "zcr": row["zcr"],
                  "vad_passed": bool(row["vad_passed"])},
        "stages": stages,
        "transcript": row["transcript"],
        "asr": {"status": row["asr_status"], "engine": row["asr_engine"],
                "model": row["asr_model"], "ms": row["asr_ms"], "error": row["asr_error"],
                "injected": row["asr_engine"] == INJECTED_ASR_ENGINE},
        "task": {"status": row["task_status"], "intent": row["task_intent"],
                 "claim": row["task_claim"], "remote_status": row["task_status_remote"],
                 "source": row["task_source"], "answer": row["task_answer"],
                 "error": row["task_error"]},
        "tts": {"status": row["tts_status"], "engine": row["tts_engine"],
                "voice": row["tts_voice"], "ms": row["tts_ms"],
                "reply_text": row["reply_text"], "bytes": row["reply_audio_bytes"],
                "sha256": row["reply_audio_sha256"], "error": row["tts_error"],
                "truncated": bool(row["tts_truncated"]) if row["tts_truncated"] is not None else None,
                "full_chars": row["reply_full_chars"]},
        # ★ 只有"可交付"的那一轮才给出可用的 url —— 被隔离的一律 None，
        #   这样即使前端有 bug，也不会拿到一个能播的地址（C2 的第二道闸）。
        "delivery": {"state": _get(row, "delivery_state"),
                     "playable": playable,
                     "why": (None if playable else
                             ("这一轮（%s）的播报已被隔离：可以回看记录，但不能播放。"
                              % row["final_state"]) if has_reply else
                             "这一轮没有产出播报音频（final_state=%s）。" % row["final_state"])},
        "reply_audio_url": ("/api/v1/voice/reply/%s.wav" % row["turn_id"]
                            if (has_reply and playable) else None),
        "reply_audio_quarantined": bool(has_reply and not playable),
        "audio_url": "/api/v1/voice/audio/%s.wav" % row["turn_id"],
        "playback": {"started_at_utc": row["playback_started_at_utc"],
                     "reported_by": row["playback_reported_by"]},
        "session": {"session_id": _get(row, "session_id"),
                    "turn_seq": _get(row, "turn_seq"),
                    "superseded_by": _get(row, "superseded_by"),
                    "half_duplex": True},
        "cancel": {"requested_at_utc": _get(row, "cancel_requested_at_utc"),
                   "seen_at_utc": _get(row, "cancel_seen_at_utc"),
                   "reason": _get(row, "cancel_reason"),
                   "stage": _get(row, "cancel_stage"),
                   "action_taken": bool(_get(row, "action_taken", 0))},
        "clarify": {"asked": json.loads(_get(row, "clarify_json", "null") or "null"),
                    "round": _get(row, "clarify_round"),
                    "from_turn_id": _get(row, "clarify_from_turn_id"),
                    "resolved_with": _get(row, "clarify_resolved_with"),
                    "how": _get(row, "clarify_how"),
                    "max_rounds": voice_session.MAX_CLARIFY_ROUNDS},
        "timing": {"t0_utc": _get(row, "received_at_utc"),
                   "first_feedback_ms": _get(row, "first_feedback_ms"),
                   "task_done_ms": _get(row, "task_done_ms"),
                   "tts_ready_ms": _get(row, "tts_ready_ms"),
                   "client_ack_ms": _get(row, "client_ack_ms"),
                   "total_ms": row["elapsed_ms"]},
        "inject": json.loads(_get(row, "inject_json", "null") or "null"),
        "stop": {"stopped_at_utc": _get(row, "tts_stopped_at_utc"),
                 "played_ms": _get(row, "tts_played_ms"),
                 "reason": _get(row, "stop_reason")},
        "final_state": row["final_state"],
        "error": ({"code": row["error_code"], "message": row["error_message"]}
                  if row["error_code"] else None),
        "elapsed_ms": row["elapsed_ms"],
        "created_at_utc": row["created_at_utc"],
    }
    if with_response and row["task_response_json"]:
        try:
            d["task"]["response"] = json.loads(row["task_response_json"])
        except Exception:
            d["task"]["response"] = None
    return d



def get_turn(turn_id):
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM voice_turns WHERE turn_id=?", (turn_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return _error(404, "TURN_NOT_FOUND", "没有这条语音记录：%s" % turn_id)
    return _ok(_row_to_dict(row))


def list_turns(device_id=None, limit=20):
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    conn = _conn()
    try:
        if device_id:
            rows = conn.execute(
                "SELECT * FROM voice_turns WHERE device_id=? ORDER BY created_at_utc DESC LIMIT ?",
                (device_id, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM voice_turns ORDER BY created_at_utc DESC LIMIT ?",
                (limit,)).fetchall()
    finally:
        conn.close()
    return _ok({"turns": [_row_to_dict(r, with_response=False) for r in rows],
                "count": len(rows), "limit": limit})


def mark_played(turn_id, payload, remote=""):
    """前端上报「播放器已开始播放」。

    ★ 红线 V4：这里只写"播放器开始播放"这一个事实。不写 heard、不写 delivered，
      也不因为"播放开始了"就把 final_state 改成别的 —— 是否真的听到，
      只有现场的人能判断。
    ★ 第 6 周补充（C2）：被隔离的一轮（取消/被顶掉）连"播放过"都不许记 ——
      否则记录里会出现"一条被取消的任务被播放了"，自相矛盾。
    """
    by = (payload or {}).get("reported_by") or "browser"
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM voice_turns WHERE turn_id=?",
                           (turn_id,)).fetchone()
        if row is None:
            return _error(404, "TURN_NOT_FOUND", "没有这条语音记录：%s" % turn_id)
        if not row["reply_audio_bytes"]:
            return _error(409, "NO_REPLY_AUDIO",
                          "这一轮没有产出播报音频（final_state=%s），没有可播放的内容。"
                          % row["final_state"])
        if not _get(row, "playable", 1):
            return _error(409, "PLAYBACK_QUARANTINED",
                          "这一轮（%s）的播报已被隔离（%s），不能播放，也不允许上报「已播放」。"
                          "隔离的原因就是「它迟到 / 它被取消」——如果允许它播，"
                          "用户会听到一个已经被否掉的答案。"
                          % (row["final_state"], _get(row, "delivery_state", "quarantined")))
        at = _now()
        conn.execute("UPDATE voice_turns SET playback_started_at_utc=?, playback_reported_by=? "
                     "WHERE turn_id=?", (at, by, turn_id))
        conn.commit()
    finally:
        conn.close()
    return _ok({"turn_id": turn_id, "playback_started_at_utc": at, "reported_by": by,
                "note": "已记录「播放器开始播放」。这不等于用户已经听到。"})


def reply_playable(turn_id):
    """这一轮的播报能不能给出去？返回 (ok, code, why)。

    code ∈ REPLAY_OK / TURN_NOT_FOUND / NO_REPLY_AUDIO / REPLY_QUARANTINED

    ★ 这是 `GET /api/v1/voice/reply/<id>.wav` 的唯一判据。
      把它放在这里（而不是在 HTTP 层顺手判断）是因为"能不能播"属于**交付语义**，
      和"音频文件在不在"是两件事：文件在，不代表允许播。

    ★ 三种"不给"必须分开：404 是"没有"，404/NO_REPLY_AUDIO 是"这一轮本来就没产出"，
      410 才是"产出过、但按当前语义不允许给"。混成一个码，排障时会指错方向。
    """
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM voice_turns WHERE turn_id=?", (turn_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return False, "TURN_NOT_FOUND", "没有这条语音记录：%s" % turn_id
    if not row["reply_audio_bytes"]:
        return False, "NO_REPLY_AUDIO", ("这一轮没有产出播报音频（final_state=%s）"
                                        % row["final_state"])
    if not _get(row, "playable", 1):
        return False, "REPLY_QUARANTINED", (
            "这一轮（%s / %s）的播报已被隔离：迟到或被取消的结果不许重新播放。"
            "记录仍然可查（文字、阶段、时序都在），只是不给音频。"
            % (row["final_state"], _get(row, "delivery_state", "quarantined")))
    return True, "REPLAY_OK", None



def audio_path(turn_id, kind="in"):
    safe = re.sub(r"[^0-9A-Za-z_\-]", "", turn_id)
    p = os.path.join(VOICE_DIR, safe + ("_in.wav" if kind == "in" else "_out.wav"))
    return p if os.path.isfile(p) else None


def health():
    hl = engine_health()
    return _ok({"engine": hl, "engine_url": VOICE_ENGINE_URL,
                "audio_sources": [{"key": k, "label": SOURCE_LABELS[k]} for k in AUDIO_SOURCES],
                "limits": {"min_duration_ms": MIN_DURATION_MS, "max_duration_ms": MAX_DURATION_MS,
                           "vad_rms_min": VAD_RMS_MIN, "vad_peak_min": VAD_PEAK_MIN,
                           "max_tts_chars": MAX_TTS_CHARS,
                           "required_format": "16000 Hz / 16 bit / mono / PCM WAV"},
                # ---- 第 6 周：会话/取消/澄清/注入 的能力自述 ----
                "session": {
                    "supported": True,
                    "states": list(voice_session.SERVER_STATES),
                    "local_states": list(voice_session.LOCAL_STATES),
                    "half_duplex": True,
                    "max_clarify_rounds": voice_session.MAX_CLARIFY_ROUNDS,
                    "note": "同一会话同一时刻只允许一条在途 turn；新 turn 开始会顶掉旧 turn",
                },
                "cancel": {
                    "supported": True,
                    "endpoints": ["POST /api/v1/voice/session/<id>/cancel",
                                  "POST /api/v1/voice/turn/<id>/cancel",
                                  "POST /api/v1/voice/turn/<id>/stop"],
                    "semantics": ("cancel = 任务不要了（后续阶段一行都不执行）；"
                                  "stop = 任务保留、只关掉声音"),
                    "quarantine": "被取消/被顶掉的一轮 playable=0，reply.wav 返回 410",
                },
                "inject": {
                    "allowed": ALLOW_INJECT,
                    "params": ["inject_delay_ms", "inject_task_ms", "inject_asr_text"],
                    "max_delay_ms": MAX_INJECT_MS,
                    "note": "教师侧测试入口：注入的轮次会在记录里标 asr.engine=injected",
                },
                "timing": {
                    "t0": "服务端收到完整音频的时刻",
                    "metrics": ["first_feedback_ms", "task_done_ms", "tts_ready_ms",
                                "client_ack_ms", "total_ms"],
                },
                "server_time_utc": _now()})

