# -*- coding: utf-8 -*-
"""voice_session.py —— 第 6 周：语音任务的会话、澄清、取消与迟到结果隔离

本周要回答的问题是：

> **系统听错了、用户改变主意或回答迟到时，怎么保持交互连续？**

第 5 周的语音链路是「一次请求 = 一句话 = 一个答案」的无状态函数：
发起之后只能等，等到的结果不管还有没有用都得接受。一旦用户中途改主意，
就会出现三种典型事故：

  A. 用户已经点了停止，3 秒后识别结果姗姗来迟，浏览器"啪"地开始念答案；
  B. 用户紧接着问了第二个问题，第一个问题的答案后到，把新答案盖掉了；
  C. 系统没听清，只能回一句"我没听明白"，用户得从头再说一遍。

本模块就是为这三件事建立的**一层薄状态**（薄到只有一个会话表 + 一张事件表），
它不碰识别、不碰任务、不碰合成 —— 它只回答四个问题：

  ① 现在这个会话处于什么状态？           → `state`（idle/listening/thinking/speaking/clarifying）
  ② 这条 turn 还是不是"被需要的那一条"？ → `interrupt_of()`
  ③ 这一轮到底能不能播？                 → `delivery_state` / `playable`
  ④ 上一轮的反问还等着回答吗？           → `clarify_json`（有限轮）

----------------------------------------------------------------------------
★ 三条硬规矩（selftest_voice6.py 逐条断言）
----------------------------------------------------------------------------
S1 **取消必须真的拦住动作**：取消发生在识别之后、任务之前时，任务段必须一行都不执行。
   "取消了但顺手把采集下发了"是本课最严重的事故 —— 那是设备上真实发生的副作用。

S2 **迟到结果不得重新播放**：取消/被顶掉的那一轮，`playable=0`，
   连 `GET /voice/reply/<id>.wav` 都返回 410。隔离必须在**服务端**生效，
   不能只靠前端"记得不要播"—— 前端是可能被绕过的那一层。

S3 **新任务不被旧结果覆盖**：turn 的先后由会话内的 `turn_seq` 单调递增钉死，
   前端拿到的响应若 request_id 不是当前在途那一条，一律丢弃渲染；
   服务端在落库时也会把它标成 `quarantined_superseded`。
   两边都判，是因为两边解决的是不同问题：前端防止"界面被盖"，服务端防止"音频能拿到"。

----------------------------------------------------------------------------
为什么是"半双工"
----------------------------------------------------------------------------
真实的全双工（边说边听、随时插话打断）需要唤醒词 + 回声消除 + 流式识别，
本周不做（课程把唤醒/回声消除/全双工列为拓展）。本周做的是**半双工**：

    同一时刻只允许一个 turn 在途；新 turn 一开始，旧 turn 立刻失去"被播报"的资格。

这不是能力退化，而是**把"谁说了算"这件事写清楚**：没有打断能力的系统，
最怕的不是"不能打断"，而是"打断了却还在播上一句"。
"""

import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone

# ===========================================================================
# 状态机（半双工）
# ===========================================================================
STATE_IDLE = "idle"              # 空闲：没有在途 turn，也没有待澄清的问题
STATE_LISTENING = "listening"    # 正在录音（前端本地事实，由前端上报）
STATE_THINKING = "thinking"      # 在途：上传/识别/任务，**可取消**
STATE_SPEAKING = "speaking"      # 答案已就绪，正在等/正在播报，**可停止播报**
STATE_CLARIFYING = "clarifying"  # 等着用户回答上一轮的反问（有限轮）

LOCAL_STATES = (STATE_LISTENING,)   # 前端可以上报的状态（只能推进"本地事实"）
SERVER_STATES = (STATE_IDLE, STATE_THINKING, STATE_SPEAKING, STATE_CLARIFYING)

STATE_LABELS = {
    STATE_IDLE: "空闲",
    STATE_LISTENING: "听你说话",
    STATE_THINKING: "处理中（可停止）",
    STATE_SPEAKING: "播报中（可停止）",
    STATE_CLARIFYING: "等待澄清",
}

# 一句话最多澄清几轮。★ 必须有限：无上限的反问会把用户困在"它一直问、我一直答"里，
# 而真实工程的出路永远是"回退到一个明确的可用说法"。
MAX_CLARIFY_ROUNDS = 2

# 会话多久没动静就当成空闲（读时惰性判断，不写库）
SESSION_STALE_S = 180

_st = {"db_path": None, "utc_now": None}
_lock = threading.Lock()


def bind(db_path, utc_now):
    """注入后端能力（不反向 import server，避免两份单例）。"""
    _st["db_path"] = db_path
    _st["utc_now"] = utc_now


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
CREATE TABLE IF NOT EXISTS voice_sessions (
    session_id        TEXT PRIMARY KEY,
    device_id         TEXT,
    state             TEXT NOT NULL,
    version           INTEGER NOT NULL DEFAULT 0,   -- 每次状态推进 +1（前端据此判断"过期响应"）
    active_request_id TEXT,                         -- ★ 半双工：同时只允许一个在途 turn
    active_turn_id    TEXT,
    turn_seq          INTEGER NOT NULL DEFAULT 0,   -- 会话内单调递增，钉住先后顺序
    clarify_json      TEXT,                         -- 待澄清上下文（含轮次与候选）
    clarify_round     INTEGER NOT NULL DEFAULT 0,
    last_turn_id      TEXT,
    note              TEXT,
    created_at_utc    TEXT NOT NULL,
    updated_at_utc    TEXT NOT NULL
);

-- ★ request_id 关联日志：一条 turn 从"开始"到"终局"的每一处转折都在这里留一行。
--   出问题时按 request_id 一捞，就能看到"它是在哪一步被取消/被顶掉的"。
--   这张表只追加，不改写 —— 日志一旦可改，就不叫日志了。
CREATE TABLE IF NOT EXISTS voice_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT,
    request_id   TEXT,
    turn_id      TEXT,
    event        TEXT NOT NULL,
    detail_json  TEXT,
    at_utc       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vevent_req ON voice_events(request_id, event_id);
CREATE INDEX IF NOT EXISTS idx_vevent_sess ON voice_events(session_id, event_id);

-- ★ 取消必须能"先到"：turn 还在解析音频、尚未入库时用户就点了停止，
--   这个取消要记下来，等 turn 入库后立刻生效。所以取消单独一张表，
--   不依赖 voice_turns 里那一行是否已经存在。
CREATE TABLE IF NOT EXISTS voice_cancels (
    request_id   TEXT PRIMARY KEY,
    session_id   TEXT,
    turn_id      TEXT,
    reason       TEXT,
    requested_by TEXT,
    at_utc       TEXT NOT NULL
);

-- ★ "谁顶掉了谁"必须单独记一笔，不能靠"active 是不是我"临时推断。
--   踩过的坑（开发期实测）：A 慢、B 快，B 已经跑完回空闲了，A 才醒来；
--   这时 active 是 NULL，若按"active 为空 ⇒ 会话被 reset"去判，A 会被错记成
--   cancelled（用户取消），而它其实是 superseded（被 B 顶掉）。
--   两者的处置完全不同：cancelled 要问"用户为什么停"，superseded 只是输给了更新的一轮。
CREATE TABLE IF NOT EXISTS voice_supersedes (
    request_id    TEXT PRIMARY KEY,
    by_request_id TEXT,
    session_id    TEXT,
    at_utc        TEXT NOT NULL
);
"""


def db_init(conn):
    conn.executescript(DDL)
    conn.commit()


# ===========================================================================
# 事件日志
# ===========================================================================
def log_event(event, session_id=None, request_id=None, turn_id=None, **detail):
    at = _now()
    try:
        with _lock:
            conn = _conn()
            try:
                conn.execute(
                    "INSERT INTO voice_events (session_id, request_id, turn_id, event, "
                    "detail_json, at_utc) VALUES (?,?,?,?,?,?)",
                    (session_id, request_id, turn_id, event,
                     json.dumps(detail, ensure_ascii=False) if detail else None, at))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        # 日志失败绝不能影响主链路（日志是证据，不是业务）
        pass
    return at


def timeline(session_id=None, request_id=None, limit=200):
    conn = _conn()
    try:
        if request_id:
            rows = conn.execute(
                "SELECT * FROM voice_events WHERE request_id=? ORDER BY event_id LIMIT ?",
                (request_id, limit)).fetchall()
        elif session_id:
            rows = conn.execute(
                "SELECT * FROM voice_events WHERE session_id=? ORDER BY event_id LIMIT ?",
                (session_id, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM voice_events ORDER BY event_id DESC LIMIT ?",
                (limit,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = {"event": r["event"], "at_utc": r["at_utc"],
             "request_id": r["request_id"], "turn_id": r["turn_id"],
             "session_id": r["session_id"]}
        if r["detail_json"]:
            try:
                d["detail"] = json.loads(r["detail_json"])
            except Exception:
                d["detail"] = r["detail_json"]
        out.append(d)
    return out


# ===========================================================================
# 会话
# ===========================================================================
def _new_session_id():
    return "vs_" + hashlib.sha256(
        ("%s|%s" % (_now(), threading.get_ident())).encode()).hexdigest()[:16]


def _session_dict(row, with_clarify=True):
    if row is None:
        return None
    d = {
        "session_id": row["session_id"],
        "device_id": row["device_id"],
        "state": row["state"],
        "state_label": STATE_LABELS.get(row["state"], row["state"]),
        "version": row["version"],
        "active_request_id": row["active_request_id"],
        "active_turn_id": row["active_turn_id"],
        "turn_seq": row["turn_seq"],
        "clarify_round": row["clarify_round"],
        "max_clarify_rounds": MAX_CLARIFY_ROUNDS,
        "last_turn_id": row["last_turn_id"],
        "created_at_utc": row["created_at_utc"],
        "updated_at_utc": row["updated_at_utc"],
        "note": row["note"],
        "half_duplex": True,
        "busy": row["active_request_id"] is not None,
    }
    if with_clarify:
        d["clarify"] = _loads(row["clarify_json"])
    return d


def _loads(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def create_session(device_id=None, note=None):
    sid = _new_session_id()
    at = _now()
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO voice_sessions (session_id, device_id, state, version, turn_seq, "
                "clarify_round, note, created_at_utc, updated_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, device_id, STATE_IDLE, 0, 0, 0, note, at, at))
            conn.commit()
            row = conn.execute("SELECT * FROM voice_sessions WHERE session_id=?", (sid,)).fetchone()
        finally:
            conn.close()
    log_event("session_created", session_id=sid, device_id=device_id)
    return _ok(_session_dict(row))


def get_session(session_id):
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM voice_sessions WHERE session_id=?",
                           (session_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return _error(404, "SESSION_NOT_FOUND",
                      "没有这个语音会话：%s。会话只用于保存短期上下文（待澄清问题、"
                      "在途任务、状态）。丢了会话不影响识别与任务本身，重新建一个即可。"
                      % session_id)
    d = _session_dict(row)
    # 惰性判空：很久没动静的会话视作空闲（不写库，避免读操作产生副作用）
    try:
        t = datetime.strptime(d["updated_at_utc"].replace("Z", ""), "%Y-%m-%dT%H:%M:%S.%f")
        t = t.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - t).total_seconds()
        if age > SESSION_STALE_S and d["state"] != STATE_IDLE:
            d["stale"] = True
            d["stale_age_s"] = round(age, 1)
            d["stale_note"] = ("这个会话已经 %d 秒没有动静，界面上请按「空闲」处理；"
                               "服务端记录里的状态不会被改写（如实保留它当时的样子）。" % age)
    except Exception:
        pass
    return _ok(d)


def list_sessions(limit=20):
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM voice_sessions ORDER BY updated_at_utc DESC LIMIT ?",
                            (limit,)).fetchall()
    finally:
        conn.close()
    return _ok({"sessions": [_session_dict(r, with_clarify=False) for r in rows],
                "count": len(rows), "limit": limit})


def set_state(session_id, state, note=None, **fields):
    """直接设置状态（服务端内部用）。"""
    if state not in SERVER_STATES:
        return _error(400, "BAD_STATE", "未知会话状态：%r" % (state,),
                      allowed=list(SERVER_STATES))
    with _lock:
        conn = _conn()
        try:
            row = conn.execute("SELECT session_id FROM voice_sessions WHERE session_id=?",
                               (session_id,)).fetchone()
            if row is None:
                return _error(404, "SESSION_NOT_FOUND", "没有这个语音会话：%s" % session_id)
            at = _now()
            kv = {"updated_at_utc": at}
            if note is not None:
                kv["note"] = note
            kv.update(fields)
            sets = ", ".join("%s=?" % k for k in kv)
            conn.execute("UPDATE voice_sessions SET state=?, version=version+1, " + sets +
                         " WHERE session_id=?",
                         [state] + list(kv.values()) + [session_id])
            conn.commit()
        finally:
            conn.close()
    log_event("state", session_id=session_id, state=state, note=note)
    return _ok({"session_id": session_id, "state": state,
                "state_label": STATE_LABELS.get(state, state)})


def note_local_state(session_id, state, by=None):
    """前端上报的**本地事实**：开始录音、播放结束。

    ★ 只接受 `listening` / `playback_ended` 两种。其余状态一律拒绝：
      前端不能把服务端的"处理中"改成"空闲"——否则取消语义就靠前端"自觉"了，
      而那正是第 6 周要杜绝的东西。
    """
    if state == STATE_LISTENING:
        # 拿起麦克风这件事，前端比服务端清楚；但"在途任务"不能被它抹掉
        with _lock:
            conn = _conn()
            try:
                row = conn.execute("SELECT * FROM voice_sessions WHERE session_id=?",
                                   (session_id,)).fetchone()
                if row is None:
                    return _error(404, "SESSION_NOT_FOUND", "没有这个语音会话：%s" % session_id)
                if row["active_request_id"]:
                    return _error(409, "SESSION_BUSY",
                                  "这个会话还有一条在途任务（request_id=%s）。"
                                  "半双工下一次只允许一条在途任务：请先取消它，或等它结束。"
                                  % row["active_request_id"],
                                  active_request_id=row["active_request_id"])
                at = _now()
                conn.execute(
                    "UPDATE voice_sessions SET state=?, version=version+1, updated_at_utc=?, "
                    "note=? WHERE session_id=?",
                    (STATE_LISTENING, at, "前端上报：开始录音（%s）" % (by or "browser"),
                     session_id))
                conn.commit()
            finally:
                conn.close()
        log_event("listening", session_id=session_id, by=by)
        return _ok({"session_id": session_id, "state": STATE_LISTENING,
                    "state_label": STATE_LABELS[STATE_LISTENING]})

    if state == "playback_ended":
        # 播完就回空闲。注意：这只说明"播放器播完了"，不说明用户听到了（第 5 周红线 V4）。
        with _lock:
            conn = _conn()
            try:
                row = conn.execute("SELECT * FROM voice_sessions WHERE session_id=?",
                                   (session_id,)).fetchone()
                if row is None:
                    return _error(404, "SESSION_NOT_FOUND", "没有这个语音会话：%s" % session_id)
                at = _now()
                new_state = row["state"]
                if row["state"] == STATE_SPEAKING:
                    new_state = STATE_IDLE
                conn.execute(
                    "UPDATE voice_sessions SET state=?, version=version+1, updated_at_utc=?, "
                    "note=? WHERE session_id=?",
                    (new_state, at, "前端上报：播放结束（不等于用户听到）", session_id))
                conn.commit()
            finally:
                conn.close()
        log_event("playback_ended", session_id=session_id, by=by)
        return _ok({"session_id": session_id, "state": STATE_IDLE,
                    "state_label": STATE_LABELS[STATE_IDLE],
                    "note": "播放结束已记录。这不等于用户已经听到。"})

    return _error(400, "LOCAL_STATE_NOT_ALLOWED",
                  "前端只能上报 listening / playback_ended 两种本地事实，收到 %r。"
                  "thinking / speaking / clarifying / idle 由服务端根据实际执行结果决定，"
                  "不接受前端改写。" % (state,))


# ===========================================================================
# 半双工：开始一条 turn（会顶掉上一条在途任务）
# ===========================================================================
def begin_turn(session_id, request_id, device_id=None):
    """登记一条在途 turn。返回 (turn_seq, superseded_request_id, state_before)。

    ★ 半双工的落点就在这三行里：一个新的 request_id 成为 active，
      上一条就算还在跑，也已经失去"被播报"的资格。
    """
    if session_id is None:
        return None, None, None
    with _lock:
        conn = _conn()
        try:
            row = conn.execute("SELECT * FROM voice_sessions WHERE session_id=?",
                               (session_id,)).fetchone()
            if row is None:
                return None, None, None
            superseded = row["active_request_id"]
            seq = row["turn_seq"] + 1
            at = _now()
            conn.execute(
                "UPDATE voice_sessions SET state=?, version=version+1, active_request_id=?, "
                "turn_seq=?, updated_at_utc=? WHERE session_id=?",
                (STATE_THINKING, request_id, seq, at, session_id))
            conn.commit()
        finally:
            conn.close()
    log_event("turn_begin", session_id=session_id, request_id=request_id,
              turn_seq=seq, superseded=superseded)
    if superseded and superseded != request_id:
        with _lock:
            conn = _conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO voice_supersedes (request_id, by_request_id, "
                    "session_id, at_utc) VALUES (?,?,?,?)",
                    (superseded, request_id, session_id, _now()))
                conn.commit()
            finally:
                conn.close()
        log_event("superseded", session_id=session_id, request_id=superseded,
                  by=request_id, note="新任务开始，旧任务失去播报资格（半双工）")
    return seq, superseded, STATE_THINKING


def interrupt_of(session_id, request_id):
    """这条 turn 还是不是"被需要的那一条"？

    返回 None（继续跑）或：
      {"kind": "cancelled",  "reason":…, "at":…}          用户点了停止
      {"kind": "superseded", "by":…}                      被新任务顶掉

    ★ 这个函数要在**每个阶段边界**被调用。只在一个地方检查是不够的：
      ASR 要 1~2 秒、任务可能等设备回执 6 秒、TTS 又要 0.3 秒，
      用户完全可能在任意一段中间改主意。
    """
    if not session_id:
        return None
    conn = _conn()
    try:
        # ① 取消先到（用户点了停止）——用户意图优先于"被别人顶掉"
        c = conn.execute("SELECT * FROM voice_cancels WHERE request_id=?",
                         (request_id,)).fetchone()
        if c is not None:
            return {"kind": "cancelled", "reason": c["reason"] or "user_stop",
                    "at": c["at_utc"], "requested_by": c["requested_by"]}
        # ② 明确的"被顶掉"记录（比"看 active 是谁"可靠，见 DDL 里的注释）
        sp = conn.execute("SELECT * FROM voice_supersedes WHERE request_id=?",
                          (request_id,)).fetchone()
        if sp is not None:
            return {"kind": "superseded", "by": sp["by_request_id"], "at": sp["at_utc"]}
        # ③ 兜底：active 已经换成别人了
        s = conn.execute("SELECT state, active_request_id FROM voice_sessions "
                         "WHERE session_id=?", (session_id,)).fetchone()
        if s is None:
            return None
        if s["active_request_id"] == request_id:
            return None
        if s["active_request_id"] is None:
            # active 被清空、也没有取消/顶替记录 ⇒ 只能是 reset() 主动回到空闲
            return {"kind": "cancelled", "reason": "session_reset", "at": None}
        return {"kind": "superseded", "by": s["active_request_id"]}
    finally:
        conn.close()


def cancel(session_id=None, request_id=None, turn_id=None, reason="user_stop",
           requested_by="browser"):
    """取消一条在途 turn（或整个会话当前在途的那条）。

    ★ 这里**只登记取消**，不做任何"撤销已完成动作"的假装：
      如果任务段已经跑完（比如采集指令已经下发），那次采集是真的发生了，
      记录里会写 action_taken=1，取消只意味着"不再播报、不再继续"。
    """
    conn = _conn()
    try:
        row = None
        if request_id:
            row = conn.execute("SELECT * FROM voice_sessions WHERE active_request_id=?",
                               (request_id,)).fetchone()
            sid = row["session_id"] if row else session_id
        elif turn_id:
            t = conn.execute("SELECT request_id, session_id FROM voice_turns WHERE turn_id=?",
                             (turn_id,)).fetchone()
            if t is not None:
                request_id = t["request_id"]
                sid = t["session_id"] or session_id
            else:
                sid = session_id
        else:
            sid = session_id
            if sid:
                r = conn.execute("SELECT active_request_id FROM voice_sessions "
                                 "WHERE session_id=?", (sid,)).fetchone()
                request_id = r["active_request_id"] if r else None
    finally:
        conn.close()

    if not request_id:
        return _error(409, "NOTHING_TO_CANCEL",
                      "这个会话当前没有在途任务，没有可取消的东西。"
                      "（空闲状态下按停止什么也不会发生，这是对的。）")

    at = _now()
    was_active = False
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO voice_cancels (request_id, session_id, turn_id, "
                "reason, requested_by, at_utc) VALUES (?,?,?,?,?,?)",
                (request_id, sid, turn_id, reason, requested_by, at))
            # turn 若已入库，立刻打上"待取消"标记（它是另一个线程正在跑的）
            t = conn.execute("SELECT turn_id FROM voice_turns WHERE request_id=?",
                             (request_id,)).fetchone() if request_id else None
            real_turn = t["turn_id"] if t else turn_id
            if real_turn:
                conn.execute(
                    "UPDATE voice_turns SET cancel_requested_at_utc=?, cancel_reason=? "
                    "WHERE turn_id=? AND cancel_requested_at_utc IS NULL",
                    (at, reason, real_turn))
            if sid:
                # ★ 只有"它还是当前在途的那一条"才允许改会话状态。
                #   如果它已经被新任务顶掉、或早就跑完了，这次取消只登记一条记录 ——
                #   绝不能顺手把别人（新任务/澄清上下文）的状态清掉。
                cur = conn.execute(
                    "UPDATE voice_sessions SET active_request_id=NULL, active_turn_id=NULL, "
                    "state=?, version=version+1, updated_at_utc=?, note=? "
                    "WHERE session_id=? AND active_request_id=?",
                    (STATE_IDLE, at, "已取消（%s）" % reason, sid, request_id))
                was_active = cur.rowcount > 0
                if was_active and reason == "user_stop":
                    # 取消同时也清掉待澄清问题：用户已经不想继续这个话题了
                    conn.execute("UPDATE voice_sessions SET clarify_json=NULL, clarify_round=0 "
                                 "WHERE session_id=?", (sid,))
            conn.commit()
        finally:
            conn.close()
    log_event("cancel", session_id=sid, request_id=request_id, turn_id=real_turn,
              reason=reason, requested_by=requested_by, was_active=was_active)
    return _ok({"request_id": request_id, "turn_id": real_turn, "reason": reason,
                "at_utc": at, "session_id": sid,
                "state": STATE_IDLE if was_active else None,
                "was_active": was_active,
                "note": ("已登记取消。在途的那一步跑完后会发现它已被取消："
                         "不会再执行后续动作，也不会产出可播放的播报。"
                         if was_active else
                         "这条任务已经不在途（已完成或被新任务顶掉），本次只登记了取消记录，"
                         "没有改动会话状态。")})


def stop_playback(turn_id, played_ms=None, reason="user_stop", reported_by="browser"):
    """停止播报（这一轮任务其实已经完成了，只是用户不想听了）。

    ★ 与 cancel 的区别必须写清楚：
        cancel       = 任务不要了（后续阶段一行都不执行）
        stop_playback= 任务保留、文字回答保留，只是把声音关掉
      所以这里**不改 final_state**，只记录"播报被停止"这一个事实。
    """
    at = _now()
    with _lock:
        conn = _conn()
        try:
            row = conn.execute("SELECT * FROM voice_turns WHERE turn_id=?",
                               (turn_id,)).fetchone()
            if row is None:
                return _error(404, "TURN_NOT_FOUND", "没有这条语音记录：%s" % turn_id)
            if not row["reply_audio_bytes"]:
                return _error(409, "NO_REPLY_AUDIO",
                              "这一轮没有播报音频（final_state=%s），没有可停止的内容。"
                              % row["final_state"])
            conn.execute(
                "UPDATE voice_turns SET tts_stopped_at_utc=?, tts_played_ms=?, stop_reason=? "
                "WHERE turn_id=?", (at, int(played_ms) if played_ms is not None else None,
                                   reason, turn_id))
            if row["session_id"]:
                conn.execute(
                    "UPDATE voice_sessions SET state=?, version=version+1, updated_at_utc=?, "
                    "note=? WHERE session_id=?",
                    (STATE_IDLE, at, "已停止播报", row["session_id"]))
            conn.commit()
        finally:
            conn.close()
    log_event("stop_playback", session_id=row["session_id"], request_id=row["request_id"],
              turn_id=turn_id, played_ms=played_ms, reason=reason, reported_by=reported_by)
    return _ok({"turn_id": turn_id, "tts_stopped_at_utc": at,
                "played_ms": played_ms, "reason": reason,
                "final_state": row["final_state"],
                "note": "播报已停止；任务与文字回答都保留。已播出的那一段收不回来，"
                        "所以这里只记录「停止于第几毫秒」，不声称任何东西。"})


def finish_turn(session_id, request_id, final_state, turn_id=None, turn_seq=None):
    """turn 收尾：把会话从 thinking 推到 speaking / idle / clarifying。

    ★ 两道闸，都是为了防止"旧结果覆盖新结果"：
      ① request 必须还是 active；
      ② turn_seq 必须不小于会话当前的 turn_seq（否则说明已经有更晚的一轮推进过状态了，
         旧的这一轮连 last_turn_id 都不许改）。
    """
    if not session_id:
        return
    state = STATE_SPEAKING if final_state == "done" else (
        STATE_CLARIFYING if final_state == "clarifying" else STATE_IDLE)
    ignored_why = None
    with _lock:
        conn = _conn()
        try:
            row = conn.execute("SELECT * FROM voice_sessions WHERE session_id=?",
                               (session_id,)).fetchone()
            if row is None:
                return
            at = _now()
            stale = (turn_seq is not None and row["turn_seq"] is not None
                     and int(row["turn_seq"]) > int(turn_seq))
            if row["active_request_id"] not in (request_id, None) or stale:
                conn.execute(
                    "UPDATE voice_sessions SET updated_at_utc=? WHERE session_id=?",
                    (at, session_id))
                conn.commit()
                ignored_why = (("这条的 turn_seq=%s 小于会话的 %s" % (turn_seq, row["turn_seq"]))
                               if stale else "这条已被取消/被顶掉，不允许改写会话状态")
            else:
                conn.execute(
                    "UPDATE voice_sessions SET state=?, active_request_id=NULL, "
                    "active_turn_id=NULL, last_turn_id=?, version=version+1, updated_at_utc=? "
                    "WHERE session_id=?",
                    (state, turn_id, at, session_id))
                conn.commit()
        finally:
            conn.close()
    # ★ log_event 必须在锁外调用：它自己也要拿同一把**非重入**锁。
    #   开发期实测踩过：把日志写在 with _lock 里面，一旦走到 ignored 分支就死锁，
    #   表现为"某一条 turn 的 HTTP 请求 90 秒不返回"，而日志里一行错误都没有。
    if ignored_why:
        log_event("finish_ignored", session_id=session_id, request_id=request_id,
                  final_state=final_state, why=ignored_why)
        return
    log_event("finish", session_id=session_id, request_id=request_id, turn_id=turn_id,
              final_state=final_state, state=state)


def reset(session_id, reason="user_reset"):
    """回到空闲：清掉待澄清问题与在途登记（前端「清空」按钮）。"""
    with _lock:
        conn = _conn()
        try:
            row = conn.execute("SELECT * FROM voice_sessions WHERE session_id=?",
                               (session_id,)).fetchone()
            if row is None:
                return _error(404, "SESSION_NOT_FOUND", "没有这个语音会话：%s" % session_id)
            at = _now()
            conn.execute(
                "UPDATE voice_sessions SET state=?, active_request_id=NULL, active_turn_id=NULL, "
                "clarify_json=NULL, clarify_round=0, version=version+1, updated_at_utc=?, "
                "note=? WHERE session_id=?",
                (STATE_IDLE, at, "已回到空闲（%s）" % reason, session_id))
            conn.commit()
        finally:
            conn.close()
    log_event("reset", session_id=session_id, reason=reason)
    return _ok({"session_id": session_id, "state": STATE_IDLE,
                "state_label": STATE_LABELS[STATE_IDLE],
                "cleared": ["active_request_id", "clarify_json", "clarify_round"]})


# ===========================================================================
# 有限多轮澄清
# ===========================================================================
_INTENT_WORDS = (
    # (意图查询文本, 关键词集合)
    ("查看上次数据", ("查看", "看看", "看一下", "上次", "上一次", "数据", "记录", "最新", "读数")),
    ("重新采集一次", ("采集", "重新", "再来", "再采", "新的", "现在采")),
    ("设备在线吗", ("在线", "状态", "在不在", "连着", "连上", "活着")),
)
_RE_ORDINAL = re.compile(r"第\s*([一二三四1-4])\s*(?:个|台|条|项|种|名)?")
_RE_NUM_ONLY = re.compile(r"^\s*([一二三四1-4])\s*(?:个|台|条|项|种)?\s*[。.!！]?\s*$")
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4,
           "1": 1, "2": 2, "3": 3, "4": 4}


def _ordinal_of(text):
    m = _RE_NUM_ONLY.match(text or "")
    if m:
        return _CN_NUM.get(m.group(1))
    m = _RE_ORDINAL.search(text or "")
    if m:
        return _CN_NUM.get(m.group(1))
    return None


def set_clarify(session_id, ctx):
    """保存"等着用户回答"的反问。ctx 由 voice_pipeline 依据 nl 返回的 ambiguity 构造。"""
    if not session_id:
        return
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "UPDATE voice_sessions SET clarify_json=?, clarify_round=?, version=version+1, "
                "updated_at_utc=? WHERE session_id=?",
                (json.dumps(ctx, ensure_ascii=False), int(ctx.get("round") or 1),
                 _now(), session_id))
            conn.commit()
        finally:
            conn.close()
    log_event("clarify_ask", session_id=session_id, round=ctx.get("round"),
              kind=ctx.get("kind"), question=ctx.get("question"))


def get_clarify(session_id):
    if not session_id:
        return None
    conn = _conn()
    try:
        row = conn.execute("SELECT clarify_json, clarify_round FROM voice_sessions "
                           "WHERE session_id=?", (session_id,)).fetchone()
    finally:
        conn.close()
    return _loads(row["clarify_json"]) if row else None


def clear_clarify(session_id):
    if not session_id:
        return
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE voice_sessions SET clarify_json=NULL, clarify_round=0 "
                         "WHERE session_id=?", (session_id,))
            conn.commit()
        finally:
            conn.close()


def resolve_clarify(ctx, text):
    """拿用户这一轮的回答，去消解上一轮的反问。

    ★ 这里是**纯确定性规则**，不调模型。理由与第 4 周一致：
      消歧这一步如果交给模型，它就可能"善意地"替用户选一个 —— 而选错设备、
      选错意图的代价是真实的（一条采集指令会真的下发到板子上）。
      所以只接受三种回答，全部可核对：
        ① 说出一句完整的话（命中意图关键词）→ 用这句原文重放
        ② 说"第一个/第二台"（序号）          → 取上一轮给过的候选项
        ③ 说出设备 ID（或其后缀）             → 按设备名精确匹配
      都不是 → unresolved，让上层再问一次（最多 MAX_CLARIFY_ROUNDS 轮）。

    返回 dict：
      {"resolved": bool, "how": …, "query": …, "device_id": …, "detail": …}
    """
    t = (text or "").strip()
    if not ctx:
        return {"resolved": False, "how": "no_context", "query": t, "device_id": None,
                "detail": "没有上一轮的反问上下文"}
    kind = ctx.get("kind")
    options = ctx.get("options") or []
    candidates = ctx.get("candidates") or []
    base_query = ctx.get("base_query") or ""

    # ---- ① 设备类歧义：先看用户是不是点了一台设备 ----
    if kind == "device":
        low = t.lower()
        # 设备 ID 全名或尾部（如 1C704C）
        for c in candidates:
            did = (c.get("device_id") or "")
            if not did:
                continue
            tail = did.split("-")[-1].lower()
            if did.lower() in low or (len(tail) >= 4 and tail in low):
                return {"resolved": True, "how": "device_name", "query": base_query or t,
                        "device_id": did, "detail": "回答里点名了设备 %s" % did}
        n = _ordinal_of(t)
        if n and 1 <= n <= len(candidates):
            did = candidates[n - 1].get("device_id")
            return {"resolved": True, "how": "device_ordinal", "query": base_query or t,
                    "device_id": did,
                    "detail": "按序号取了第 %d 台设备 %s" % (n, did)}
        return {"resolved": False, "how": "unresolved", "query": t, "device_id": None,
                "detail": "这一句里没有设备名，也没有序号"}

    # ---- ② 意图类歧义 ----
    if kind == "intent":
        n = _ordinal_of(t)
        if n and 1 <= n <= len(options):
            q = options[n - 1]
            return {"resolved": True, "how": "intent_ordinal", "query": q, "device_id": None,
                    "detail": "按序号取了第 %d 个候选说法「%s」" % (n, q)}
        for q, words in _INTENT_WORDS:
            for w in words:
                if w in t:
                    return {"resolved": True, "how": "intent_keyword", "query": q,
                            "device_id": None,
                            "detail": "回答里出现了「%s」，据此选「%s」" % (w, q)}
        # 用户直接说了一句能独立成立的话（例如"看看最近十分钟的数据"）→ 整句重放
        if len(t) >= 4 and (ctx.get("allow_freeform")):
            return {"resolved": True, "how": "freeform", "query": t, "device_id": None,
                    "detail": "整句重新解析（不是从候选里挑）"}
        return {"resolved": False, "how": "unresolved", "query": t, "device_id": None,
                "detail": "既没有序号，也没有可识别的意图关键词"}

    return {"resolved": False, "how": "unknown_kind", "query": t, "device_id": None,
            "detail": "不认识的反问类型：%r" % (kind,)}


def build_clarify(session_id, ambiguity, base_query, device_id=None, turn_id=None,
                  explain=None):
    """把 nl_assistant 返回的 ambiguity 变成"有轮次、有边界"的反问上下文。"""
    prev = get_clarify(session_id) or {}
    round_no = int(prev.get("round") or 0) + 1
    ctx = {
        "round": round_no,
        "max_rounds": MAX_CLARIFY_ROUNDS,
        "kind": ambiguity.get("kind") or "intent",
        "question": ambiguity.get("question") or "你能再说清楚一点吗？",
        "options": ambiguity.get("options") or [],
        "candidates": ambiguity.get("candidates") or [],
        "base_query": base_query or "",
        "device_id": device_id,
        "source_turn_id": turn_id,
        "exhausted": round_no > MAX_CLARIFY_ROUNDS,
        "why": explain or "系统没能唯一确定你的意思，所以先问一句再动手（不猜）",
    }
    if ctx["exhausted"]:
        ctx["fallback"] = ("已经问了 %d 轮还没说清，先停下。你可以直接说"
                           "「查看上次数据」/「重新采集一次」/「设备在线吗」，"
                           "或者改用文字入口打字 —— 打字的歧义更少。" % MAX_CLARIFY_ROUNDS)
    return ctx
