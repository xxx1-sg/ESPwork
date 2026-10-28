#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_voice6.py —— 第 6 周「让语音任务可以澄清与停止」接口层自检

覆盖的不是"能不能正常跑"，而是**三种异常时序下系统还敢不敢乱动**：

  A 会话生命周期   建会话 / 读状态 / 本地上报只允许 listening·playback_ended / 404
  B 时序测量       t0、first_feedback、task_done、tts_ready、client_ack 全部落库且单调
  C 取消·识别期间  ★ 任务段一行都不执行：collect_requests 表**一条新增都没有**
  D 取消·任务之后  ★ 动作已经发生（表里真的多了一条），取消只能拦住播报，并如实标注
  E 新任务顶掉旧任务 ★ 旧 turn 变 superseded、旧 reply 410、新 reply 200、会话状态不被旧结果改写
  F 停止播报       任务与文字回答保留，只关声音；没有音频的轮次拒绝停止
  G 澄清·有限多轮  反问不调业务工具、可播报；序号/关键词/设备名三种消解；到上限必须退出
  H 隔离的绕过测试 直接 curl：reply 410、played 409、列表里 playable=false
  I 注入开关       关闭注入后一律 400；开启时记录里必须标 asr.engine=injected
  J request_id 日志 一条 turn 的 turn_begin/cancel/cancel_seen/finish 能被串起来
  K 回归           无会话的单次调用（第 5 周行为）仍然可用；文字入口不受影响

运行（需要先起语音引擎，8013）：
    D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\selftest_voice6.py
"""

import json
import math
import os
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(BASE, "backend")
PORT = 8016
TMP = os.path.join(BASE, ".tmp")
TMP_DB = os.path.join(TMP, "selftest_voice6.db")
TMP_VOICE = os.path.join(TMP, "voice_w6")
os.makedirs(TMP, exist_ok=True)
for p in (TMP_DB,):
    if os.path.exists(p):
        os.remove(p)

sys.path.insert(0, BACKEND)

os.environ.update({
    "SENSOR_DB": TMP_DB,
    "SENSOR_HOST": "127.0.0.1",
    "SENSOR_PORT": str(PORT),
    "SENSOR_WEB_DIR": os.path.join(BASE, "web"),
    "SENSOR_VOICE_DIR": TMP_VOICE,
    "SENSOR_CAM_DIR": os.path.join(TMP, "photos_w6"),
    "NL_COLLECT_WAIT_MS": "1200",
    # 自检里连续两次 request_collect 是**故意的**（C/D 两段要对照），关掉业务节流
    "NL_COLLECT_MIN_INTERVAL_MS": "0",
})

import server            # noqa: E402
import voice_pipeline    # noqa: E402
import voice_engine      # noqa: E402
import voice_session     # noqa: E402

fails = []
checks = 0

DEV_A = "esp32s3eye-TESTA"
DEV_B = "esp32s3eye-TESTB"


def check(name, cond, detail=""):
    global checks
    checks += 1
    print(("  OK   " if cond else "  FAIL ") + name + ((" | " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)


def section(t):
    print("\n" + "-" * 78 + "\n" + t + "\n" + "-" * 78)


# ===========================================================================
# 造音频（真实 TTS 合成 → 16k 单声道）
# ===========================================================================
def wav_bytes(pcm, rate=16000):
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_speech(text):
    # 走 do_tts()（而不是直接 tts_sapi()）：这样在没有音频设备的机器上
    # 会自动降级到 tone 合成音，自检才能把状态机/取消/隔离跑完。
    # 降级只影响"这一段音频是不是真人声"，不影响任何被测语义。
    raw, _info = voice_engine.do_tts(text)
    i, pcm = voice_engine.wav_info(raw)
    return wav_bytes(voice_engine.resample_to_16k(pcm, i["sample_rate"]))


# ===========================================================================
# 环境能力探测
# ---------------------------------------------------------------------------
# 本课的自检要在两类机器上都能跑：
#   ① 有声卡：TTS 合成真人语音 → vosk 真的识别出文字（原设计路径）
#   ② 无声卡：Speak() 无法工作，只能给"合成音"→ vosk 识别为空
# 在 ② 上，如果还硬等"识别出指定文本"，测的就不是状态机而是机器的声卡，
# 所以这里退到"教师提供的注入入口"（inject_asr_text）来指定识别文本，
# 并在输出里**明确标注**本段是注入模式 —— 注入不是真实识别，不能混为一谈。
CAPS = {"tts_speaks": None, "asr_usable": True, "inject_needed": False}
INJECTED_TURNS = set()   # 因为环境限制而走了注入的 request_id（汇总里如实报告）


def probe_caps(hl):
    """从引擎自述里读出"这台机器能不能真发声"，进而决定要不要走注入。"""
    tts = (hl or {}).get("tts") or {}
    CAPS["tts_speaks"] = tts.get("speaks")
    if tts.get("speaks") is False:
        # 没声卡：合成音识别不出文字 → 必须靠注入提供识别文本
        CAPS["asr_usable"] = False
        CAPS["inject_needed"] = True
        print("  [!] 本机 SAPI 不可用（audio_outputs=%s）：改为「注入识别文本」模式。"
              % (tts.get("audio_outputs"),))
        print("      原因：%s" % (tts.get("note") or "未知"))
        print("      ★ 注入轮在记录里标 asr.engine=injected，与真实识别可区分。")
        return
    # 有声卡也不能保证慕斯克识别出这句话（小模型对合成语音偶有偏差），
    # 所以再做一次"真实识别"探针，识别不出来才降级到注入。
    try:
        probe_wav = wav_speech("设备在线吗")
        pi, ppcm = voice_engine.wav_info(probe_wav)
        r = voice_engine.do_asr(voice_engine.resample_to_16k(ppcm, pi["sample_rate"]))
        got = ((r or {}).get("text") or "").strip()
    except Exception as e:
        got = ""
        print("  [!] 识别探针失败：%s: %s" % (type(e).__name__, e))
    if not got:
        CAPS["asr_usable"] = False
        CAPS["inject_needed"] = True
        print("  [!] 识别探针没有返回文本：改为「注入识别文本」模式。")
    else:
        print("  [+] 真实识别可用：探针识别为「%s」" % got)


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 绕开 http_proxy


def http(method, path, body=None, raw=None, ctype=None, timeout=120):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = raw if raw is not None else (
        json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype or "application/json")
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            b = r.read()
            try:
                return r.status, json.loads(b.decode("utf-8"))
            except ValueError:
                return r.status, {"_bytes": len(b), "_head": b[:16]}
    except urllib.error.HTTPError as e:
        b = e.read()
        try:
            return e.code, json.loads(b.decode("utf-8"))
        except ValueError:
            return e.code, {"_bytes": len(b)}


_rid = [0]


def next_rid(tag):
    _rid[0] += 1
    return "rq6-%s-%02d" % (tag, _rid[0])


def turn(wav, sid=None, rid=None, device=DEV_A, inject=None, ack_ms=15, wait_ms=1200,
         say=None):
    """提交一轮语音。

    新增 `say=`：这一句"用户本来说的是什么"。
    ★ 必须区分两种模式，且都要如实记录（见文件头 CAPS 的说明）：
      · 真实识别可用 → 不传 inject_asr_text，让 vosk 自己听（原路径）
      · 真实识别不可用（无声卡）→ 自动带上 inject_asr_text，并在
        `INJECTED_TURNS` 里留痕，最后的汇总里会如实报告有多少轮是注入的
    """
    q = ["audio_source=uploaded_file",
         "capture_location=" + urllib.parse.quote("tts-loopback:Windows-11 本机 PC（授权录音·备用路径）"),
         "wait_ms=%d" % wait_ms,
         "client_ack_ms=%d" % ack_ms]
    if sid:
        q.append("session_id=" + sid)
    if rid:
        q.append("request_id=" + rid)
    if device:
        q.append("device_id=" + device)
    inj = dict(inject or {})
    if CAPS["inject_needed"] and "inject_asr_text" not in inj:
        # 无声卡：用注入文本代替"识别结果"。音频本身仍然是真实提交的，
        # 所以 VAD / 时长 / 字节数这些仍然测的是真实音频。
        # say 缺省时用一句"能独立成立的话"兜底 —— 目的是让链路走得下去，
        # 断言关心的是取消/隔离语义，不是这句话被解析成什么意图。
        inj["inject_asr_text"] = say if say is not None else "设备在线吗"
        if rid:
            INJECTED_TURNS.add(rid)
    for k, v in inj.items():
        q.append("%s=%s" % (k, urllib.parse.quote(str(v))))
    return http("POST", "/api/v1/voice/turn?" + "&".join(q), raw=wav, ctype="audio/wav")


def turn_async(holder, key, **kw):
    def run():
        try:
            holder[key] = turn(**kw)
        except Exception as e:                     # 线程里的异常要留痕，否则表现为"卡住"
            holder[key] = (0, {"_error": "%s: %s" % (type(e).__name__, e)})
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


def db_rows(sql, args=()):
    import sqlite3
    with sqlite3.connect(TMP_DB) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def collect_count():
    return len(db_rows("SELECT request_id FROM collect_requests"))


def wait_state(sid, want, timeout=8.0):
    """等会话进入某个状态（轮询真实接口，不 sleep 猜）。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        st, b = http("GET", "/api/v1/voice/session/" + sid)
        last = (b.get("data") or {}).get("state")
        if last == want:
            return True, last
        time.sleep(0.1)
    return False, last


def stages_of(d):
    return {s["key"]: s for s in d.get("stages") or []}


# ===========================================================================
def main():
    section("0. 启动")
    hl = voice_pipeline.engine_health()
    if not hl.get("ok"):
        print("  !! 语音引擎未就绪（%s）：%s" % (voice_pipeline.VOICE_ENGINE_URL, hl.get("error")))
        print("     请先运行：.venv\\Scripts\\python.exe backend\\voice_engine.py")
        return 2
    check("语音引擎就绪", True, "%s / model_present=%s"
          % (hl["asr"]["backend"], hl["asr"].get("model_present")))
    probe_caps(hl)

    server.db_init()
    # 两台设备：设备歧义澄清需要"多于一台"。这两台是**测试夹具**，不代表真实硬件。
    # ★ 必须给 last_boot_count / last_seq：没有基线快照，后端会（正确地）拒绝下发采集，
    #   那样 D 段"动作已发生"就测不出来 —— 夹具不真实会让断言失真。
    now = server.utc_now_iso()
    import sqlite3
    with sqlite3.connect(TMP_DB) as c:
        for d in (DEV_A, DEV_B):
            c.execute("INSERT OR REPLACE INTO devices (device_id, last_seen_utc, last_status, "
                      "last_boot_count, last_seq, last_uptime_ms, periodic_enabled) "
                      "VALUES (?,?,?,?,?,?,1)", (d, now, "online", 1, 100, 60000))
        c.commit()

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.4)
    st, _ = http("GET", "/healthz")
    check("后端已监听", st == 200, st)

    # ===================================================================
    section("A. 会话生命周期（半双工状态机）")
    # ===================================================================
    st, b = http("POST", "/api/v1/voice/session", {"device_id": DEV_A})
    d = b.get("data") or {}
    sid = d.get("session_id")
    check("建会话 → 200", st == 200 and bool(sid), (st, sid))
    check("初始 state=idle", d.get("state") == "idle", d.get("state"))
    check("状态机自述 half_duplex=True", d.get("half_duplex") is True)
    check("澄清轮数上限已声明", d.get("max_clarify_rounds") == voice_session.MAX_CLARIFY_ROUNDS,
          d.get("max_clarify_rounds"))
    check("初始无在途任务", d.get("active_request_id") is None)

    st, b = http("GET", "/api/v1/voice/session/" + sid)
    check("读会话 → 200", st == 200, st)
    check("含 clarify 字段（即使是 null）", "clarify" in (b.get("data") or {}))

    st, b = http("GET", "/api/v1/voice/session/vs_does_not_exist")
    check("不存在的会话 → 404", st == 404, st)
    check("404 说明了「会话丢了不影响识别与任务」",
          "不影响" in ((b.get("error") or {}).get("message") or "（无 message）"),
          (b.get("error") or {}).get("message"))

    st, b = http("POST", "/api/v1/voice/session/%s/state" % sid, {"state": "thinking"})
    check("★ 前端不许把状态改成 thinking → 400", st == 400, st)
    check("拒绝时列出了允许的本地状态",
          "listening" in ((b.get("error") or {}).get("message") or ""),
          (b.get("error") or {}).get("message"))

    st, b = http("POST", "/api/v1/voice/session/%s/state" % sid,
                 {"state": "listening", "by": "selftest"})
    check("前端上报 listening → 200", st == 200, st)
    check("state 变成 listening", (b.get("data") or {}).get("state") == "listening")

    st, b = http("POST", "/api/v1/voice/session/%s/state" % sid, {"state": "playback_ended"})
    check("前端上报 playback_ended → 回 idle", (b.get("data") or {}).get("state") == "idle")
    check("playback_ended 明确「不等于用户听到」",
          "听到" in ((b.get("data") or {}).get("note") or ""))

    # ===================================================================
    section("B. 时序测量：输入结束 → 首次反馈 → 任务完成 → 播报就绪")
    # ===================================================================
    rid = next_rid("normal")
    t_send = time.time()
    st, b = turn(wav_speech("设备在线吗"), sid=sid, rid=rid, say='设备在线吗')
    wall_ms = int((time.time() - t_send) * 1000)
    d = b.get("data") or {}
    tm = d.get("timing") or {}
    check("正常链路 final_state=done", d.get("final_state") == "done", d.get("final_state"))
    check("t0 有值（服务端收到音频的时刻）", bool(tm.get("t0_utc")), tm.get("t0_utc"))
    check("first_feedback_ms 有值", isinstance(tm.get("first_feedback_ms"), int),
          tm.get("first_feedback_ms"))
    check("task_done_ms 有值", isinstance(tm.get("task_done_ms"), int), tm.get("task_done_ms"))
    check("tts_ready_ms 有值", isinstance(tm.get("tts_ready_ms"), int), tm.get("tts_ready_ms"))
    check("★ 时序单调：首次反馈 ≤ 任务完成 ≤ 播报就绪",
          tm["first_feedback_ms"] <= tm["task_done_ms"] <= tm["tts_ready_ms"], tm)
    check("total_ms ≥ tts_ready_ms", tm["total_ms"] >= tm["tts_ready_ms"], tm["total_ms"])
    check("client_ack_ms 原样记录了前端上报值", tm.get("client_ack_ms") == 15,
          tm.get("client_ack_ms"))
    check("总耗时与服务端实测量级一致（墙上时钟 ±1500ms）",
          abs(wall_ms - tm["total_ms"]) < 1500, (wall_ms, tm["total_ms"]))
    check("timing 里写明了 t0 的定义", "服务端收到完整音频" in (tm.get("t0_note") or ""))

    check("turn_seq=1（会话内第一轮）", (d.get("session") or {}).get("turn_seq") == 1,
          (d.get("session") or {}).get("turn_seq"))
    check("request_id 原样回传（便于关联日志）", d.get("request_id") == rid, d.get("request_id"))
    check("delivery=deliverable / 可播放",
          (d.get("delivery") or {}).get("state") == "deliverable"
          and (d.get("delivery") or {}).get("playable") is True, d.get("delivery"))
    st, hdr = http("GET", d["reply_audio_url"])
    check("可播放的一轮 reply.wav → 200", st == 200 and hdr.get("_bytes", 0) > 1000,
          (st, hdr.get("_bytes")))
    good_turn = d

    check("完成后会话进入 speaking", wait_state(sid, "speaking", 3)[0])

    # ===================================================================
    section("C. 取消 · 识别期间 —— 任务段一行都不许执行")
    # ===================================================================
    n0 = collect_count()
    holder = {}
    rid_c = next_rid("cancel-asr")
    th = turn_async(holder, "a", wav=wav_speech("重新采集一次"), sid=sid, rid=rid_c,
                    inject={"inject_delay_ms": 3000}, say="重新采集一次")
    check("在途时会话进入 thinking", wait_state(sid, "thinking", 3)[0])
    st, b = http("GET", "/api/v1/voice/session/" + sid)
    check("会话 active_request_id 指向这一条",
          (b.get("data") or {}).get("active_request_id") == rid_c,
          (b.get("data") or {}).get("active_request_id"))

    st, b = http("POST", "/api/v1/voice/session/%s/cancel" % sid,
                 {"request_id": rid_c, "reason": "user_stop", "requested_by": "selftest"})
    cd = b.get("data") or {}
    check("取消 → 200", st == 200, st)
    check("取消登记在途（was_active=True）", cd.get("was_active") is True, cd)
    th.join(40)
    st, b = holder.get("a", (0, {}))
    d = b.get("data") or {}
    check("取消后请求仍然 200 返回（不切断连接）", st == 200, (st, b.get("_error")))
    check("★ final_state=cancelled", d.get("final_state") == "cancelled", d.get("final_state"))
    check("★ 任务段 status=skipped（没有执行）", (d.get("task") or {}).get("status") == "skipped",
          d.get("task"))
    check("★ task.intent 为空（连意图都没落）",
          (d.get("task") or {}).get("intent") is None, (d.get("task") or {}).get("intent"))
    check("★ action_taken=False", (d.get("cancel") or {}).get("action_taken") is False,
          d.get("cancel"))
    check("cancel_stage 记录在任务之前",
          (d.get("cancel") or {}).get("stage") in ("before_asr", "after_asr"),
          (d.get("cancel") or {}).get("stage"))
    check("cancel.requested_at 与 seen_at 都留痕",
          bool((d.get("cancel") or {}).get("requested_at_utc"))
          and bool((d.get("cancel") or {}).get("seen_at_utc")), d.get("cancel"))
    check("★ 取消后 collect_requests 表一条都没新增（没有下发采集指令）",
          collect_count() == n0, (n0, collect_count()))
    check("识别文本仍在（识别真发生过，如实保留）",
          isinstance(d.get("transcript"), str) and len(d["transcript"]) > 0, d.get("transcript"))
    check("★ delivery=quarantined_cancelled", (d.get("delivery") or {}).get("state")
          == "quarantined_cancelled", d.get("delivery"))
    check("★ playable=False", (d.get("delivery") or {}).get("playable") is False)
    check("★ reply_audio_url 为 null（前端拿不到可播地址）",
          d.get("reply_audio_url") is None, d.get("reply_audio_url"))
    check("错误说明写清了「任务一行都没执行」",
          "一行都没有执行" in ((d.get("error") or {}).get("message") or ""),
          (d.get("error") or {}).get("message"))
    check("取消后会话回到 idle", wait_state(sid, "idle", 3)[0])
    cancelled_asr = d

    # ===================================================================
    section("D. 取消 · 任务之后 —— 动作已发生，收不回来")
    # ===================================================================
    n1 = collect_count()
    holder = {}
    rid_d = next_rid("cancel-task")
    th = turn_async(holder, "b", wav=wav_speech("重新采集一次"), sid=sid, rid=rid_d,
                    inject={"inject_task_ms": 3000}, say="重新采集一次")
    time.sleep(1.6)      # 够识别 + 任务跑完，卡在注入的"任务后延迟"里
    st, b = http("POST", "/api/v1/voice/session/%s/cancel" % sid,
                 {"request_id": rid_d, "reason": "user_stop"})
    check("任务后取消 → 200", st == 200, st)
    th.join(40)
    st, b = holder.get("b", (0, {}))
    d = b.get("data") or {}
    check("★ final_state=cancelled", d.get("final_state") == "cancelled", d.get("final_state"))
    check("★ action_taken=True（任务真的跑过）",
          (d.get("cancel") or {}).get("action_taken") is True, d.get("cancel"))
    check("cancel_stage=after_task", (d.get("cancel") or {}).get("stage") == "after_task",
          (d.get("cancel") or {}).get("stage"))
    check("★ collect_requests 真的多了一条（如实承认副作用）",
          collect_count() == n1 + 1, (n1, collect_count()))
    check("★ 取消只拦住播报：playable=False",
          (d.get("delivery") or {}).get("playable") is False)
    check("★ 说明里明确「动作已发生不可撤回」",
          "收不回" in ((d.get("error") or {}).get("message") or ""),
          (d.get("error") or {}).get("message"))
    check("text 回答仍保留（任务结果没被抹掉）",
          bool((d.get("task") or {}).get("answer")), (d.get("task") or {}).get("answer", "")[:40])

    # ===================================================================
    section("E. 新任务顶掉旧任务 —— 旧结果不许覆盖新结果")
    # ===================================================================
    holder = {}
    rid_old = next_rid("old")
    th = turn_async(holder, "c", wav=wav_speech("查看上次数据"), sid=sid, rid=rid_old,
                    inject={"inject_delay_ms": 2500}, say="查看上次数据")
    time.sleep(0.4)
    rid_new = next_rid("new")
    st, b = turn(wav_speech("设备在线吗"), sid=sid, rid=rid_new, say='设备在线吗')
    dnew = b.get("data") or {}
    check("新任务正常完成", dnew.get("final_state") == "done", dnew.get("final_state"))
    check("新任务 turn_seq 更大", (dnew.get("session") or {}).get("turn_seq", 0) >= 3,
          (dnew.get("session") or {}).get("turn_seq"))
    th.join(40)
    st, b = holder.get("c", (0, {}))
    dold = b.get("data") or {}
    check("★ 旧任务 final_state=superseded", dold.get("final_state") == "superseded",
          dold.get("final_state"))
    check("★ superseded_by 指回新任务（可追溯「被谁顶掉」）",
          (dold.get("session") or {}).get("superseded_by") == rid_new,
          (dold.get("session") or {}).get("superseded_by"))
    check("★ 旧结果 delivery=quarantined_superseded",
          (dold.get("delivery") or {}).get("state") == "quarantined_superseded",
          dold.get("delivery"))
    check("★ 旧结果 reply_audio_url 为 null", dold.get("reply_audio_url") is None)
    st2, eb = http("GET", "/api/v1/voice/reply/%s.wav" % dold["turn_id"])
    check("★ 旧结果拿不到音频（被拦在识别之后，本来就没产出音频 → 404）",
          st2 == 404 and ((eb.get("error") or {}).get("code") == "NO_REPLY_AUDIO"),
          (st2, (eb.get("error") or {}).get("code")))
    check("★ 新结果 reply.wav → 200（没被旧的带坏）",
          http("GET", dnew["reply_audio_url"])[0] == 200)

    st, b = http("GET", "/api/v1/voice/session/" + sid)
    sd = b.get("data") or {}
    check("★ 会话没有被旧任务改写：state=speaking", sd.get("state") == "speaking",
          sd.get("state"))
    check("★ 会话 last_turn_id 指向新任务", sd.get("last_turn_id") == dnew["turn_id"],
          (sd.get("last_turn_id"), dnew["turn_id"]))
    check("会话没有在途残留", sd.get("active_request_id") is None,
          sd.get("active_request_id"))

    st, b = http("GET", "/api/v1/voice/turns?limit=50")
    rows = {t["turn_id"]: t for t in ((b.get("data") or {}).get("turns") or [])}
    check("列表里两条都在（都能查证，不只是丢了）",
          dold["turn_id"] in rows and dnew["turn_id"] in rows, len(rows))
    check("列表里旧的那条 playable=false", rows[dold["turn_id"]]["delivery"]["playable"] is False)
    check("列表里新的那条 playable=true", rows[dnew["turn_id"]]["delivery"]["playable"] is True)

    # ===================================================================
    section("F. 停止播报 —— 任务保留，只关声音")
    # ===================================================================
    tid = good_turn["turn_id"]
    st, b = http("POST", "/api/v1/voice/turn/%s/stop" % tid, {"played_ms": 900,
                                                             "reason": "user_stop"})
    sd = b.get("data") or {}
    check("停止播报 → 200", st == 200, st)
    check("★ final_state 仍是 done（任务没被否定）", sd.get("final_state") == "done",
          sd.get("final_state"))
    check("记录了 stopping 时间与已播毫秒", bool(sd.get("tts_stopped_at_utc"))
          and sd.get("played_ms") == 900, sd)
    check("说明里不声称「用户听到了」",
          "听" not in (sd.get("note") or "") or "不声称" in (sd.get("note") or ""),
          sd.get("note"))
    st, b = http("GET", "/api/v1/voice/turn/" + tid)
    check("详情里能读到 stop 记录", (b["data"]["stop"] or {}).get("played_ms") == 900,
          b["data"]["stop"])
    check("停止播报后会话回 idle", wait_state(sid, "idle", 3)[0])
    st, b = http("POST", "/api/v1/voice/turn/%s/stop" % cancelled_asr["turn_id"],
                 {"played_ms": 100})
    check("对没有音频的轮次停止播报 → 409", st == 409, st)
    check("409 说的是「没有播报音频」，不是「隔离」",
          ((b.get("error") or {}).get("code") or "") == "NO_REPLY_AUDIO",
          (b.get("error") or {}).get("code"))

    # ===================================================================
    section("G. 澄清 · 有限多轮")
    # ===================================================================
    st, b = http("POST", "/api/v1/voice/session/%s/reset" % sid, {"reason": "selftest_g"})
    check("reset 回到空闲", st == 200 and (b.get("data") or {}).get("state") == "idle", st)

    # G1 意图不明 → 反问（不调工具）
    rid_g1 = next_rid("clarify1")
    st, b = turn(wav_speech("嗯"), sid=sid, rid=rid_g1,
                 inject={"inject_asr_text": "那个"}, say='嗯')
    d1 = b.get("data") or {}
    clar = d1.get("clarify") or {}
    asked = clar.get("asked") or {}
    check("意图不明 → final_state=clarifying", d1.get("final_state") == "clarifying",
          d1.get("final_state"))
    check("★ 反问带问题文本", bool(asked.get("question")), asked.get("question"))
    check("★ 反问带候选项", isinstance(asked.get("options"), list) and len(asked["options"]) >= 2,
          asked.get("options"))
    check("澄清轮次=1", clar.get("round") == 1, clar.get("round"))
    check("★ 反问这一轮没有调用业务工具",
          (d1.get("task") or {}).get("source") in (None, "")
          or "未调用" in str((d1.get("task") or {}).get("source")),
          (d1.get("task") or {}).get("source"))
    check("★ 反问也要能播出来（语音助手不能在屏幕上问问题）",
          (d1.get("delivery") or {}).get("playable") is True, d1.get("delivery"))
    check("反问文本在播报里", "查看已有记录" in ((d1.get("tts") or {}).get("reply_text") or ""),
          (d1.get("tts") or {}).get("reply_text"))
    check("会话进入 clarifying", wait_state(sid, "clarifying", 3)[0])
    st, b = http("GET", "/api/v1/voice/session/" + sid)
    check("会话里存了待澄清上下文",
          bool((b["data"].get("clarify") or {}).get("question")),
          (b["data"].get("clarify") or {}).get("question"))

    # G2 用序号消解
    st, b = turn(wav_speech("第二个"), sid=sid, rid=next_rid("clarify2"),
                 inject={"inject_asr_text": "第二个"}, say='第二个')
    d2 = b.get("data") or {}
    c2 = d2.get("clarify") or {}
    check("★ 序号消解：正常执行（不再是 clarifying）",
          d2.get("final_state") in ("done", "task_failed", "clarify_exhausted"),
          d2.get("final_state"))
    check("★ clarify_how=intent_ordinal", c2.get("how") == "intent_ordinal", c2.get("how"))
    check("★ 消解成「重新采集一次」", c2.get("resolved_with") == "重新采集一次",
          c2.get("resolved_with"))
    check("★ clarify.from_turn_id 指回上一轮", c2.get("from_turn_id") == d1.get("turn_id"),
          (c2.get("from_turn_id"), d1.get("turn_id")))
    check("消解后任务段真的跑了 record（有 intent）",
          bool((d2.get("task") or {}).get("intent")), (d2.get("task") or {}).get("intent"))

    # G3 关键词消解
    st, b = http("POST", "/api/v1/voice/session/%s/reset" % sid, {})
    st, b = turn(wav_speech("嗯"), sid=sid, rid=next_rid("clarify3"),
                 inject={"inject_asr_text": "那个"}, say='嗯')
    check("再次进入 clarifying", (b.get("data") or {}).get("final_state") == "clarifying",
          (b.get("data") or {}).get("final_state"))
    st, b = turn(wav_speech("我想看看数据"), sid=sid, rid=next_rid("clarify4"),
                 inject={"inject_asr_text": "我想看看数据"}, say='我想看看数据')
    d4 = b.get("data") or {}
    check("★ 关键词消解：clarify_how=intent_keyword",
          (d4.get("clarify") or {}).get("how") == "intent_keyword",
          (d4.get("clarify") or {}).get("how"))
    check("★ 消解成「查看上次数据」",
          (d4.get("clarify") or {}).get("resolved_with") == "查看上次数据",
          (d4.get("clarify") or {}).get("resolved_with"))
    check("消解后 intent=query_latest",
          (d4.get("task") or {}).get("intent") == "query_latest",
          dict(d4.get("task") or {}, status=d4.get("final_state")))

    # G4 设备歧义（两台设备、句子里没点名）+ 序号消解
    st, b = http("POST", "/api/v1/voice/session/%s/reset" % sid, {})
    st, b = turn(wav_speech("查看上次数据"), sid=sid, rid=next_rid("dev1"), device=None, say='查看上次数据')
    d5 = b.get("data") or {}
    c5 = d5.get("clarify") or {}
    check("★ 两台设备未点名 → 澄清 kind=device",
          d5.get("final_state") == "clarifying" and (c5.get("asked") or {}).get("kind") == "device",
          (d5.get("final_state"), (c5.get("asked") or {}).get("kind")))
    check("澄清里列出了候选设备",
          len((c5.get("asked") or {}).get("candidates") or []) == 2,
          (c5.get("asked") or {}).get("candidates"))
    st, b = turn(wav_speech("第一台"), sid=sid, rid=next_rid("dev2"), device=None,
                 inject={"inject_asr_text": "第一台"}, say='第一台')
    d6 = b.get("data") or {}
    cand0 = ((c5.get("asked") or {}).get("candidates") or [{}])[0].get("device_id")
    check("★ 设备序号消解：clarify_how=device_ordinal",
          (d6.get("clarify") or {}).get("how") == "device_ordinal",
          (d6.get("clarify") or {}).get("how"))
    check("★ 消解后任务落在「候选第一位」那台设备上（不猜、按上一轮给的顺序）",
          (d6.get("device_id") or "") == (cand0 or ""), (d6.get("device_id"), cand0))
    check("★ 返回里标明设备来自澄清消解",
          d6.get("device_id_source") == "clarify_resolved", d6.get("device_id_source"))
    check("消解后拿到真实答案", bool((d6.get("task") or {}).get("answer")),
          ((d6.get("task") or {}).get("answer") or "")[:30])

    # G5 轮数上限：连续两轮都答不清 → 必须退出，不许无限追问
    st, b = http("POST", "/api/v1/voice/session/%s/reset" % sid, {})
    st, b = turn(wav_speech("嗯"), sid=sid, rid=next_rid("loop1"),
                 inject={"inject_asr_text": "那个"}, say='嗯')
    check("上限测试 · 第 1 轮 clarifying",
          (b.get("data") or {}).get("final_state") == "clarifying",
          (b.get("data") or {}).get("final_state"))
    st, b = turn(wav_speech("哦"), sid=sid, rid=next_rid("loop2"),
                 inject={"inject_asr_text": "哦"}, say='哦')
    r2 = b.get("data") or {}
    check("上限测试 · 第 2 轮仍在澄清（轮次=2）",
          r2.get("final_state") == "clarifying" and (r2.get("clarify") or {}).get("round") == 2,
          (r2.get("final_state"), (r2.get("clarify") or {}).get("round")))
    check("第 2 轮反问标注 repeated",
          bool(((r2.get("clarify") or {}).get("asked") or {}).get("repeated")),
          (r2.get("clarify") or {}).get("asked"))
    st, b = turn(wav_speech("啊"), sid=sid, rid=next_rid("loop3"),
                 inject={"inject_asr_text": "啊"}, say='啊')
    r3 = b.get("data") or {}
    check("★ 到上限 → clarify_exhausted（不再追问）",
          r3.get("final_state") == "clarify_exhausted", r3.get("final_state"))
    check("★ 到上限给出明确退路（不是死循环）",
          "文字入口" in ((r3.get("error") or {}).get("message") or ""),
          (r3.get("error") or {}).get("message"))
    check("到上限后会话回 idle（可重新开始）", wait_state(sid, "idle", 3)[0])

    # G6 文字补充轮：澄清时直接用键盘回答（备用路径，也是"重新输入"的出口）
    st, b = http("POST", "/api/v1/voice/session/%s/reset" % sid, {})
    st, b = turn(wav_speech("嗯"), sid=sid, rid=next_rid("txt0"),
                 inject={"inject_asr_text": "那个"}, say='嗯')
    check("（准备）再次进入 clarifying",
          (b.get("data") or {}).get("final_state") == "clarifying",
          (b.get("data") or {}).get("final_state"))
    st, b = http("POST", "/api/v1/voice/text",
                 {"text": "第二个", "session_id": sid, "device_id": DEV_A,
                  "request_id": next_rid("txt1")})
    dt = b.get("data") or {}
    check("★ 文字补充轮 → 200", st == 200, (st, b.get("_error")))
    check("★ 文字轮走完了同一条任务链", dt.get("final_state") in ("done", "task_failed"),
          dt.get("final_state"))
    check("★ audio_source=text_input（不伪装成麦克风）",
          dt.get("audio_source") == "text_input", dt.get("audio_source"))
    check("★ 文字轮没有音频回放地址", dt.get("audio_url") is None, dt.get("audio_url"))
    check("★ VAD 记录为 null（没音频，不假装判过）",
          (dt.get("audio") or {}).get("vad_passed") is None, dt.get("audio"))
    sgt = stages_of(dt)
    check("① 段标签是「输入」而不是「录音」", sgt["recorded"]["label"] == "输入",
          sgt["recorded"]["label"])
    check("② 段如实 skip（文字不需要静音检测）", sgt["vad"]["status"] == "skipped",
          sgt["vad"]["status"])
    check("★ asr.engine=keyboard（没有调用识别引擎）",
          (dt.get("asr") or {}).get("engine") == "keyboard", (dt.get("asr") or {}).get("engine"))
    check("★ 文字轮也能消解澄清（意图序号）",
          (dt.get("clarify") or {}).get("how") == "intent_ordinal",
          (dt.get("clarify") or {}).get("how"))
    check("文字轮也会合成播报（同样能听到）",
          (dt.get("tts") or {}).get("status") in ("ok", "unavailable"),
          (dt.get("tts") or {}).get("status"))

    # ===================================================================
    section("H. 隔离的绕过测试（直接访问接口，不经过前端）")
    # ===================================================================
    st, b = http("GET", "/api/v1/voice/reply/%s.wav" % cancelled_asr["turn_id"])
    check("★ 被取消的轮次根本没有播报音频 → 404 NO_REPLY_AUDIO（不是 403/500）",
          st == 404 and ((b.get("error") or {}).get("code") == "NO_REPLY_AUDIO"),
          (st, (b.get("error") or {}).get("code")))
    st, b = http("GET", "/api/v1/voice/audio/%s.wav" % cancelled_asr["turn_id"])
    check("原始录音仍可回放（那是用户自己说的，不受隔离影响）", st == 200, st)
    st, b = http("POST", "/api/v1/voice/turn/%s/played" % cancelled_asr["turn_id"],
                 {"reported_by": "rogue-client"})
    check("★ 没有音频的轮次不允许上报「已播放」 → 409", st == 409, st)
    st, b = http("GET", "/api/v1/voice/turn/" + cancelled_asr["turn_id"])
    cd = b["data"]
    check("详情里 playable=false / quarantined",
          (cd.get("delivery") or {}).get("playable") is False
          and (cd.get("delivery") or {}).get("state") == "quarantined_cancelled",
          cd.get("delivery"))
    check("详情里没有出现 heard / 已听到 字样",
          "已听到" not in json.dumps(cd, ensure_ascii=False))

    # ---- 410 的夹具：人工把一条**已完成且有音频**的轮标记成隔离 ----
    #   ★ 为什么要造这一条：真实链路里"有音频却被隔离"的窗口极窄
    #     （必须恰好在合成完成之后、落库之前被取消），靠自然时序跑不出来。
    #     但 410 这条代码路径必须被验到 —— 否则"迟到结果不许重新播放"就只是句口号。
    #     夹具只改 playable / delivery_state 两列，不动任何事实字段；用完立刻复原。
    import sqlite3 as _sq
    with _sq.connect(TMP_DB) as c:
        c.execute("UPDATE voice_turns SET playable=0, delivery_state='quarantined_superseded' "
                  "WHERE turn_id=?", (good_turn["turn_id"],))
        c.commit()
    st, b = http("GET", "/api/v1/voice/reply/%s.wav" % good_turn["turn_id"])
    check("★ 有音频但被隔离 → 410 Gone（文件在，但不给）", st == 410, st)
    check("410 用 REPLY_QUARANTINED 明确是「隔离」而不是「没生成」",
          ((b.get("error") or {}).get("code") or "") == "REPLY_QUARANTINED",
          (b.get("error") or {}).get("code"))
    check("410 指明了该怎么办（去看文字回答）",
          "文字回答" in (((b.get("error") or {}).get("details") or {}).get("hint") or ""),
          (b.get("error") or {}).get("details"))
    st, b = http("POST", "/api/v1/voice/turn/%s/played" % good_turn["turn_id"],
                 {"reported_by": "rogue-client"})
    check("★ 被隔离的轮次不许上报「已播放」 → 409", st == 409, st)
    check("409 的码是 PLAYBACK_QUARANTINED",
          ((b.get("error") or {}).get("code") or "") == "PLAYBACK_QUARANTINED",
          (b.get("error") or {}).get("code"))
    with _sq.connect(TMP_DB) as c:      # 夹具复原，避免影响后续断言
        c.execute("UPDATE voice_turns SET playable=1, delivery_state='deliverable' "
                  "WHERE turn_id=?", (good_turn["turn_id"],))
        c.commit()

    # ===================================================================
    section("I. 测试注入开关（教师侧入口，必须留痕）")
    # ===================================================================
    old = voice_pipeline.ALLOW_INJECT
    voice_pipeline.ALLOW_INJECT = False
    st, b = turn(wav_speech("设备在线吗"), sid=sid, rid=next_rid("inj-off"),
                 inject={"inject_delay_ms": 100, "inject_asr_text": "设备在线吗"})
    check("★ 关闭注入后 inject_delay_ms → 400", st == 400, st)
    check("400 说明了为什么关掉",
          ((b.get("error") or {}).get("code") or "") == "INJECT_DISABLED",
          (b.get("error") or {}).get("code"))
    voice_pipeline.ALLOW_INJECT = old

    st, b = turn(wav_speech("设备在线吗"), sid=sid, rid=next_rid("inj-bad"),
                 inject={"inject_delay_ms": "abc"})
    check("非法注入值 → 400", st == 400, st)

    st, b = turn(wav_speech("嗯"), sid=sid, rid=next_rid("inj-ok"),
                 inject={"inject_asr_text": "设备在线吗"}, say='嗯')
    d = b.get("data") or {}
    check("★ 注入的识别必须标 engine=injected",
          (d.get("asr") or {}).get("engine") == "injected", (d.get("asr") or {}).get("engine"))
    check("★ asr.injected=True（前端能据此标注「这是注入的」）",
          (d.get("asr") or {}).get("injected") is True)
    check("★ 记录里 inject_json 留了设定值",
          bool(((d.get("inject") or {}).get("raw"))), d.get("inject"))
    check("注入轮 run_location 写明「非真实识别」",
          "注入" in ((d.get("run_location") or {}).get("asr") or ""),
          (d.get("run_location") or {}).get("asr"))
    st, b = http("GET", "/api/v1/voice/turn/" + d["turn_id"])
    check("查详情也能看到注入标记",
          ((b["data"].get("asr") or {}).get("injected")) is True)

    # ===================================================================
    section("J. request_id 关联日志")
    # ===================================================================
    st, b = http("GET", "/api/v1/voice/timeline?request_id=" + rid_c)
    evs = [e["event"] for e in ((b.get("data") or {}).get("events") or [])]
    check("timeline 按 request_id 可查", st == 200 and len(evs) >= 3, evs)
    for want in ("turn_begin", "turn_pending", "cancel", "cancel_seen", "finish"):
        check("日志含 %s" % want, want in evs, evs)
    check("日志顺序是时间序（turn_begin 在最前）", evs and evs[0] == "turn_begin", evs[:3])
    st, b = http("GET", "/api/v1/voice/session/%s/timeline" % sid)
    check("按会话也能取整段对话的日志",
          len(((b.get("data") or {}).get("events") or [])) > 10,
          len(((b.get("data") or {}).get("events") or [])))
    st, b = http("GET", "/api/v1/voice/timeline?limit=5")
    check("全局 timeline 支持 limit", len((b.get("data") or {}).get("events") or []) <= 5)

    # ===================================================================
    section("K. 回归：无会话的单次调用（第 5 周行为）仍然可用")
    # ===================================================================
    st, b = turn(wav_speech("设备在线吗"), sid=None, rid=next_rid("legacy"), device=DEV_A, say='设备在线吗')
    d = b.get("data") or {}
    check("不带 session_id 仍能正常走完", st == 200 and d.get("final_state") == "done",
          (st, d.get("final_state")))
    check("无会话时如实说明「无状态单次调用」",
          "无会话" in ((d.get("session") or {}).get("note") or ""),
          (d.get("session") or {}).get("note"))
    check("无会话时不产生 turn_seq", (d.get("session") or {}).get("turn_seq") is None)
    check("无会话时 run_location 说明无法取消",
          "无会话" in ((d.get("run_location") or {}).get("session") or ""),
          (d.get("run_location") or {}).get("session"))

    st, b = http("POST", "/api/v1/nl/query", {"q": "查看上次数据", "device_id": DEV_A})
    check("★ 文字入口不受影响（200 + 有回答）",
          st == 200 and bool(((b.get("data") or {}).get("answer"))), st)

    st, b = http("GET", "/api/v1/voice/health")
    hd = b.get("data") or {}
    check("health 自述了会话/取消/注入能力",
          all(k in hd for k in ("session", "cancel", "inject", "timing")), list(hd.keys()))
    _hs = json.dumps(hd, ensure_ascii=False).lower()
    # 注意别用 "sk-" 判：模型目录叫 vosk-model-…，里面天然含 "sk-"，会误报
    check("health 不泄露任何密钥值（只报「配没配」）",
          "api_key" not in _hs and "authorization" not in _hs and "bearer" not in _hs,
          _hs[:120])

    st, b = http("GET", "/api/v1/voice/sessions")
    check("会话列表可用", st == 200 and (b.get("data") or {}).get("count", 0) >= 1, st)

    section("L. 老库升级：第 5 周形状的库上 db_init 必须能跑通")
    # ===================================================================
    # ★ 这一节是为一个**真实踩到的坑**留的回归：
    #   第 6 周的建表语句里带了
    #       CREATE INDEX ... ON voice_turns(request_id)
    #   而 `CREATE TABLE IF NOT EXISTS` 在老库上等于什么都不做，补列却写在 DDL 之后。
    #   结果：新库一切正常（自检跑的就是新库），**真实库**上后端每次启动都崩在
    #   db_init（sqlite3.OperationalError: no such column: request_id），
    #   守护进程 3/6/12/24 秒重拉、永远起不来，日志里只有一行像"库坏了"的报错。
    #   所以这里显式造一个"没有第 6 周那些列"的老表，再跑一次 db_init。
    import sqlite3 as _sq
    old_db = os.path.join(TMP, "old_shape.db")
    if os.path.exists(old_db):
        os.remove(old_db)
    oc = _sq.connect(old_db)
    oc.executescript("""
        CREATE TABLE voice_turns (
            turn_id TEXT PRIMARY KEY,
            device_id TEXT,
            audio_source TEXT,
            final_state TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        INSERT INTO voice_turns VALUES ('vt_old_1','dev-old','browser_mic','done','2026-01-01T00:00:00Z');
    """)
    oc.commit()
    ok_upgrade = True
    err_txt = ""
    try:
        voice_pipeline.db_init(oc)
    except Exception as e:
        ok_upgrade = False
        err_txt = "%s: %s" % (type(e).__name__, e)
    check("★ 在老库上 db_init 不抛异常（补列 + 建索引顺序正确）", ok_upgrade, err_txt)
    if ok_upgrade:
        cols = {r[1] for r in oc.execute("PRAGMA table_info(voice_turns)")}
        check("★ 第 6 周的列被补齐（request_id / playable / clarify_json）",
              {"request_id", "playable", "clarify_json"} <= cols, len(cols))
        idx = {r[0] for r in oc.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_voice%'")}
        check("★ 第 6 周的索引也建出来了（idx_voice_request / idx_voice_session）",
              {"idx_voice_request", "idx_voice_session"} <= idx, sorted(idx))
        row = oc.execute("SELECT * FROM voice_turns WHERE turn_id='vt_old_1'").fetchone()
        check("★ 老数据的值没被改写（只加列，不动历史行）",
              row is not None and row[3] == "done", row and row[:5])
    oc.close()

    # ===================================================================
    section("M. 存储层瞬时不可写 —— 预入库要重试，记账失败不许毁掉结果")
    # ★ 这一节同样来自一次**真实事故**：板子 10 Hz ingest + 摄像头 ~10 fps
    #   + 语音三路同时写同一个 sensor.db 时，Windows 上偶发
    #       sqlite3.OperationalError: attempt to write a readonly database
    #   它落在两处，后果完全不同：
    #     ① 落在 _insert_running（预入库）→ 后面无处落脚，**必须**重试，
    #        重试仍失败就抛 StorageBusy，接口回 503（可重试）而不是 500；
    #     ② 落在 _patch（阶段增量/收尾记账）→ 结果**已经算完了**，
    #        绝不能因为记账失败就把这一轮判成 internal_error 把结果丢掉。
    #   这里用"前 N 次 execute 必失败"的假连接把两条路径都戳一遍。
    _real_conn = voice_pipeline._conn

    def _flaky(fail_times=0, pred=None):
        state = {"left": fail_times}

        class _C(object):
            def __init__(self, inner):
                self._i = inner

            def execute(self, sql, *a, **k):
                if state["left"] != 0 and (pred is None or pred(sql)):
                    if state["left"] > 0:
                        state["left"] -= 1
                    raise _sq.OperationalError("attempt to write a readonly database")
                return self._i.execute(sql, *a, **k)

            def commit(self):
                return self._i.commit()

            def close(self):
                return self._i.close()

            def __getattr__(self, n):
                return getattr(self._i, n)

        voice_pipeline._conn = lambda: _C(_real_conn())
        return state

    def _restore_conn():
        voice_pipeline._conn = _real_conn

    # ---- M1：预入库连撞两次 readonly，第三次应成功，这一轮照常完成 ----
    sig_m1 = "m1_%d" % int(time.time() * 1000)
    try:
        st_m = _flaky(fail_times=2)
        code, body = http("POST", "/api/v1/voice/text", {
            "text": "查看上次数据",
            "request_id": sig_m1,
            "capture_location": "keyboard:selftest_voice6.py · 本机 PC",
        })
    finally:
        _restore_conn()
    check("★ 预入库撞上 readonly 两次后重试成功（接口仍 200，不是 500）",
          code == 200 and (body.get("ok") is True), "HTTP %s / %s" % (
              code, (body.get("error") or {}).get("code")))
    check("★ 重试确实发生过（失败计数被消耗完）", st_m["left"] == 0, st_m)
    rows_m1 = db_rows("SELECT * FROM voice_turns WHERE request_id=?", (sig_m1,))
    check("★ 这一轮真的落库且正常收尾（账本没丢）",
          len(rows_m1) == 1 and rows_m1[0]["final_state"] != "running",
          rows_m1 and rows_m1[0]["final_state"])

    # ---- M2：预入库彻底写不进去 → 503 + retryable，且一件事都没做 ----
    before_m2 = collect_count()
    sig_m2 = "m2_%d" % int(time.time() * 1000)
    try:
        _flaky(fail_times=-1)          # -1 = 永远失败
        code2, body2 = http("POST", "/api/v1/voice/text", {
            "text": "重新采集一次",
            "request_id": sig_m2,
            "device_id": DEV_A,
            "capture_location": "keyboard:selftest_voice6.py · 本机 PC",
        })
    finally:
        _restore_conn()
    check("★ 账本写不进去就回 503（存储忙），不谎报「内部异常」",
          code2 == 503, "HTTP %s / %s" % (code2, (body2.get("error") or {}).get("code")))
    check("★ 503 的错误码是 VOICE_STORAGE_BUSY 且标了 retryable",
          (body2.get("error") or {}).get("code") == "VOICE_STORAGE_BUSY"
          and (body2.get("error") or {}).get("retryable") is True,
          body2.get("error"))
    check("★ 没有账本就不许做动作（没有新增采集指令）",
          collect_count() == before_m2, "%d → %d" % (before_m2, collect_count()))
    check("★ 也没有留下半条记录", len(db_rows(
        "SELECT turn_id FROM voice_turns WHERE request_id=?", (sig_m2,))) == 0)

    # ---- M3：_patch 写失败（两次都失败 → 该段记账放弃）→ 结果不许丢 ----
    #   为什么只让它失败两次：`_patch` 的重试策略是"重试一次，仍失败就记警告后继续"。
    #   失败两次正好走完"放弃记账"这一支，而后面的最终落库照常 —— 于是可以验证
    #   **记账失败没有把已经算完的结果毁掉**，这正是当初事故的要害。
    sig_m3 = "m3_%d" % int(time.time() * 1000)
    try:
        st_m3 = _flaky(fail_times=2, pred=lambda s: s.strip().upper().startswith("UPDATE"))
        code3, body3 = http("POST", "/api/v1/voice/text", {
            "text": "设备在线吗",
            "request_id": sig_m3,
            "device_id": DEV_A,
            "capture_location": "keyboard:selftest_voice6.py · 本机 PC",
        })
    finally:
        _restore_conn()
    d3 = body3.get("data") or {}
    check("★ 记账撞上 readonly（重试后放弃）时，这一轮仍然 200",
          code3 == 200, "HTTP %s / %s" % (code3, (body3.get("error") or {}).get("code")))
    check("★ 重试过（两次失败都被消耗）", st_m3["left"] == 0, st_m3)
    check("★ 识别文本与任务回答都还在（记账失败没有毁掉结果）",
          bool(d3.get("transcript")) and bool((d3.get("task") or {}).get("answer")),
          "transcript=%r / answer=%r" % ((d3.get("transcript") or "")[:12],
                                         ((d3.get("task") or {}).get("answer") or "")[:12]))
    check("★ 这一轮没有被判成 internal_error（记账失败 ≠ 结果失败）",
          d3.get("final_state") not in ("internal_error", None), d3.get("final_state"))
    rows_m3 = db_rows("SELECT final_state, task_answer FROM voice_turns WHERE request_id=?",
                      (sig_m3,))
    check("★ 最终落库仍是 done（丢的只是那段阶段增量，不是整轮）",
          len(rows_m3) == 1 and rows_m3[0]["final_state"] == "done",
          rows_m3 and rows_m3[0]["final_state"])

    print("\n" + "=" * 78)
    # ---- 环境如实说明（不影响结论，但必须写出来，否则"全过"会被误读）----
    if CAPS["inject_needed"]:
        print("★ 环境说明：本机没有可用的音频输出设备，SAPI 无法合成真人语音。")
        print("  因此本轮自检有 %d 个 turn 用 inject_asr_text 指定了识别文本" % len(INJECTED_TURNS))
        print("  （记录里标 asr.engine=injected）。被测的取消/隔离/澄清/时序语义不受影响，")
        print("  但『识别引擎真的听懂了这句话』这一条本周没有被验证 —— 那属于第 5 周的结论。")
        print("=" * 78)
    if fails:
        print("失败 %d / %d 项：" % (len(fails), checks))
        for f in fails:
            print("  - " + f)
        print("=" * 78)
        return 1
    print("全部通过：%d 项断言" % checks)
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
