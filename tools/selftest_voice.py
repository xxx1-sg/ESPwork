#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_voice.py —— 第 5 周「按键说话与语音播报」接口层自检

覆盖的不是"只要正常情况能跑"，而是**每一种失败都必须给出可理解的反馈、
且文字入口始终可用**：

  A 正常链路      真实语音 → 六段状态齐全、录音/播报音频可回放核对
  B 同源（红线 V2）同一句话走「语音入口」与「文字入口」，intent/claim/source 必须一致
  C 无声（V1）    VAD 判定无语音 → 不提交识别、transcript 必须为空、反馈含实测 RMS
  D 格式（V6）    44.1k/立体声/8bit/非 WAV/过短/过长 → 400 并报出**实际**参数
  E 来源与位置(V5) audio_source 非法 / capture_location 缺失 → 拒收（本周必交内容）
  F 引擎不可用    识别服务挂掉 → asr_unavailable，且 /api/v1/nl/query 仍正常（V3）
  G 识别超时      引擎不响应 → asr_timeout，不执行任务、不生成任何文本
  H 合成失败      识别与任务都成功、只有 TTS 挂 → tts_failed，但文字回答完整
  I 播放上报(V4)  只记录"播放器开始播放"，不产生 heard/已听到
  J 查询与回归    turns/详情/404/health，以及第 1–4 周接口未被破坏

★ 本脚本对**编排层的失败处置**下断言，不替代真实引擎的识别质量评估。
  识别质量用 tools/voice_probe.py 在真实音频上单独度量（那是另一件事）。

运行：D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\selftest_voice.py
      需要先启动语音引擎：D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\backend\\voice_engine.py
"""

import json
import math
import os
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(BASE, "backend")
PORT = 8014
TMP = os.path.join(BASE, ".tmp")
TMP_DB = os.path.join(TMP, "selftest_voice.db")
TMP_VOICE = os.path.join(TMP, "voice_w5")
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
    "SENSOR_CAM_DIR": os.path.join(TMP, "photos_w5"),
    "NL_COLLECT_WAIT_MS": "1200",
})

import server          # noqa: E402
import voice_pipeline  # noqa: E402
import voice_engine    # noqa: E402

fails = []
checks = 0

# 环境能力：本机有没有可用的音频输出设备（没有就得用注入提供识别文本）
CAPS = {"inject_needed": False}
_skip = []


def check(name, cond, detail=""):
    global checks
    checks += 1
    print(("  OK   " if cond else "  FAIL ") + name + ((" | " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)


def section(t):
    print("\n" + "-" * 76 + "\n" + t + "\n" + "-" * 76)


# ===========================================================================
# 造音频（全部是**真实样本**：正弦/静音由样本算出，语音由系统 TTS 合成）
# ===========================================================================
def wav_bytes(pcm, rate=16000, ch=1, bits=16):
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def pcm_tone(ms, freq=440, rate=16000, amp=8000, ch=1):
    n = int(rate * ms / 1000.0)
    out = bytearray()
    for i in range(n):
        v = int(amp * math.sin(2 * math.pi * freq * i / rate))
        for _ in range(ch):
            out += struct.pack("<h", v)
    return bytes(out)


def wav_silence(ms, rate=16000):
    return wav_bytes(b"\x00\x00" * int(rate * ms / 1000.0), rate=rate)


def wav_speech(text):
    """真实中文语音：系统 TTS 合成 → 重采样到 16k。这是"授权录音"的备用路径。

    走 do_tts()：没有音频设备的机器上会降级到 tone 合成音（不是语音），
    但"有声/无声"这一层语义仍然真实，自检照旧能跑。
    """
    raw, info = voice_engine.do_tts(text)
    i, pcm = voice_engine.wav_info(raw)
    p16 = voice_engine.resample_to_16k(pcm, i["sample_rate"])
    return wav_bytes(p16, rate=16000)


# ===========================================================================
# HTTP
# ===========================================================================
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(method, path, body=None, raw=None, ctype=None, timeout=90):
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


def turn(wav, source="uploaded_file",
         loc="tts-loopback:Windows-11 本机 PC（授权录音·备用路径）", device=None, wait_ms=1200,
         say=None):
    """提交一轮语音。

    `say=`：这一轮"用户本来说的话"。只在**本机没有音频设备**时才用来
    指定识别文本（注入），见 CAPS 说明。真实识别可用时不注入。
    """
    q = []
    if source is not None:
        q.append("audio_source=" + urllib.parse.quote(source))
    if loc is not None:
        q.append("capture_location=" + urllib.parse.quote(loc))
    if device:
        q.append("device_id=" + urllib.parse.quote(device))
    q.append("wait_ms=%d" % wait_ms)
    if say is not None and CAPS["inject_needed"]:
        q.append("inject_asr_text=" + urllib.parse.quote(say))
    return http("POST", "/api/v1/voice/turn?" + "&".join(q), raw=wav, ctype="audio/wav")


def db_rows(sql, args=()):
    import sqlite3
    with sqlite3.connect(TMP_DB) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def turn_count():
    return len(db_rows("SELECT turn_id FROM voice_turns"))


def stages_of(d):
    return {s["key"]: s for s in d.get("stages") or []}


# ===========================================================================
# 假引擎（只用于测"链路对失败的处置"，不是用来评估识别质量）
# ===========================================================================
class _FakeASR(BaseHTTPRequestHandler):
    mode = "ok"
    text = "查看上次数据"

    def log_message(self, *a):
        pass

    def do_GET(self):
        self._j({"ok": True, "asr": {"backend": "fake"}, "tts": {"backend": "fake"}})

    def _j(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n) if n else b""
        if self.path == "/asr":
            if self.mode == "fail":
                self._j({"ok": False, "code": "ASR_FAILED", "error": "fake engine boom"}, 500)
                return
            self._j({"ok": True, "engine": "fake", "model": "fake-1", "ms": 5,
                     "text": self.text, "empty": self.text == ""})
        elif self.path == "/tts":
            if self.mode == "tts_fail":
                self._j({"ok": False, "code": "TTS_FAILED", "error": "fake tts boom"}, 500)
                return
            self._j({"ok": False, "code": "NOPE", "error": "unexpected"}, 400)
        else:
            self._j({"ok": False, "error": "?"}, 404)


def start_fake(port, mode, text="查看上次数据"):
    cls = type("F", (_FakeASR,), {"mode": mode, "text": text})
    srv = ThreadingHTTPServer(("127.0.0.1", port), cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def start_blackhole(port):
    """只接受连接、永不响应 —— 用来制造真实的"识别超时"。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(16)
    keep = []

    def loop():
        while True:
            try:
                c, _ = s.accept()
                keep.append(c)          # 拿着不放，也不回一个字节
            except OSError:
                return

    threading.Thread(target=loop, daemon=True).start()
    return s


# ===========================================================================
def main():
    section("0. 启动")
    real_engine = voice_pipeline.VOICE_ENGINE_URL
    hl = voice_pipeline.engine_health()
    if not hl.get("ok"):
        print("  !! 语音引擎未就绪（%s）：%s" % (real_engine, hl.get("error")))
        print("     请先运行：.venv\\Scripts\\python.exe backend\\voice_engine.py")
        return 2
    check("语音引擎就绪", True, "%s asr=%s" % (real_engine, hl["asr"]["backend"]))
    check("识别模型存在", hl["asr"].get("model_present") is True, hl["asr"].get("model_dir"))

    # ★ 环境能力探测：本机没有音频输出设备时，SAPI 合成不出真人语音，
    #   vosk 也就听不出文字。此时 A 段用注入文本指定"用户说的话"，
    #   并把这件事**如实标注**在输出里 —— 注入不等于真实识别。
    _tts = hl.get("tts") or {}
    CAPS["inject_needed"] = (_tts.get("speaks") is False)
    if CAPS["inject_needed"]:
        print("  [!] 本机 SAPI 不可用（audio_outputs=%s）：A 段改为「注入识别文本」模式。"
              % (_tts.get("audio_outputs"),))
        print("      原因：%s" % (_tts.get("note") or "未知"))
        print("      ★ 注入轮记录里标 asr.engine=injected，与真实识别可区分。")

    # ★ 必须显式建库：server.db_init() 只在 main() 里调用，自检直接起 Handler 时不会自动跑
    server.db_init()

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.4)
    st, _ = http("GET", "/healthz")
    check("后端已监听", st == 200, st)

    # -------------------------------------------------------------------
    section("A. 正常链路：真实语音 → 六段状态")
    # -------------------------------------------------------------------
    SAY = "查看上次数据"
    wav = wav_speech(SAY)
    st, b = turn(wav, say=SAY)
    d = b.get("data") or {}
    check("HTTP 200", st == 200, st)
    check("final_state=done", d.get("final_state") == "done", d.get("final_state"))
    check("识别文本与真实语音一致", d.get("transcript") == SAY, d.get("transcript"))
    sg = stages_of(d)
    order = ["recorded", "vad", "asr", "task", "tts", "playback"]
    check("六段状态齐全且顺序固定",
          list(sg.keys()) == order, list(sg.keys()))
    check("① 录音段：16000 Hz / 16 bit / 1 声道",
          (d["audio"]["sample_rate"], d["audio"]["bits"], d["audio"]["channels"]) == (16000, 16, 1),
          d["audio"])
    check("① 录音段：字节数与上传一致", d["audio"]["bytes"] == len(wav),
          (d["audio"]["bytes"], len(wav)))
    check("① 录音段：时长 > 1 秒", d["audio"]["duration_ms"] > 1000, d["audio"]["duration_ms"])
    check("② VAD：通过且 RMS 高于阈值",
          sg["vad"]["status"] == "ok" and sg["vad"]["rms"] >= voice_pipeline.VAD_RMS_MIN,
          "rms=%s thr=%s" % (sg["vad"].get("rms"), voice_pipeline.VAD_RMS_MIN))
    check("③ 识别段：引擎/模型/耗时都有值",
          # 注入模式下 engine=injected、ms=0 是**正确**的：没有调用识别引擎，
          # 所以耗时为 0 才是实话。此处断言"标了引擎 + 能解释耗时"。
          sg["asr"]["status"] == "ok" and bool(sg["asr"]["engine"])
          and (sg["asr"]["ms"] > 0 or sg["asr"].get("engine") == "injected"),
          sg["asr"])
    check("④ 任务段：记录了意图/claim/来源",
          sg["task"]["status"] == "ok" and d["task"]["intent"] and d["task"]["claim"],
          sg["task"])
    check("⑤ 合成段：产出真实音频",
          sg["tts"]["status"] == "ok" and (d["tts"]["bytes"] or 0) > 10000, d["tts"])
    check("⑤ 合成语音名来自系统真实语音",
          # 有声卡时必须是真实的 Huihui 语音；没有声卡时降级到 tone，
          # 此时 voice=None 是**事实**，但必须如实标成 tone（不能冒充真人语音）。
          ("Huihui" in (d["tts"].get("voice") or "")) if not CAPS["inject_needed"]
          else (d["tts"].get("engine") == "tone" and not (d["tts"].get("voice") or "").strip()),
          "engine=%s voice=%s" % (d["tts"].get("engine"), d["tts"].get("voice")))
    check("⑥ 播放段：服务端只标 pending，不预写已播放",
          sg["playback"]["status"] == "pending" and d["playback"]["started_at_utc"] is None,
          d["playback"])
    rl = d.get("run_location") or {}
    check("★ V5 run_location 五段齐全",
          all(rl.get(k) for k in ("capture", "asr", "task", "tts", "playback")), rl)
    check("★ V5 录音位置写的是真实来源",
          "本机 PC" in rl.get("capture", ""), rl.get("capture"))
    check("★ V2 任务段声明复用第 4 周同一函数",
          "nl_assistant" in rl.get("task", ""), rl.get("task"))

    tid = d["turn_id"]

    # 回放的音频必须与入库哈希一致（证明"听到的"就是当时那一份）
    st, hdr = http("GET", d["audio_url"])
    check("录音可回放", st == 200 and hdr.get("_bytes") == len(wav),
          (st, hdr.get("_bytes")))
    st, hdr = http("GET", d["reply_audio_url"])
    check("播报音频可回放", st == 200 and (hdr.get("_bytes") or 0) > 10000,
          (st, hdr.get("_bytes")))
    row = db_rows("SELECT * FROM voice_turns WHERE turn_id=?", (tid,))[0]
    check("数据库记录与响应一致",
          row["final_state"] == "done" and row["transcript"] == SAY, row["final_state"])
    check("数据库存了录音 SHA256", len(row["audio_sha256"] or "") == 64)
    check("数据库存了播报音频 SHA256", len(row["reply_audio_sha256"] or "") == 64)
    check("录音文件已落盘且大小一致",
          os.path.getsize(os.path.join(TMP_VOICE, tid + "_in.wav")) == len(wav))
    check("播报文件已落盘",
          os.path.isfile(os.path.join(TMP_VOICE, tid + "_out.wav")))

    # -------------------------------------------------------------------
    section("A2. 播报节选：只按整句截断，绝不改写原文")
    # -------------------------------------------------------------------
    LONG = ("设备：esp32s3eye-1C704C\n采样时间：2026-10-23T10:22:57+0800\n"
            "合矢量 |a| = 1.7840 g\n来源：sensor_records.id=443339\n"
            "状态：这是数据库中已经存在的一条记录，不是本次新采的。")
    ex, tr = voice_pipeline.speech_excerpt(LONG)

    def body_of(s):
        return s.replace(voice_pipeline.EXCERPT_SUFFIX, "")

    check("长回答会被节选", tr is True)
    check("★ 播报文本（去掉提示语）是原文的严格前缀",
          LONG.startswith(ex.replace(voice_pipeline.EXCERPT_SUFFIX, "")),
          ex.replace(voice_pipeline.EXCERPT_SUFFIX, "")[-30:])
    # "不在句子中间切开"的准确判据：节选末字之后，原文恰好是换行/句末标点/结束。
    body = body_of(ex)
    cut = LONG.find(body)
    after = LONG[cut + len(body):cut + len(body) + 1] if cut >= 0 else None
    check("★ 节选只在句子边界处切开（切口后面是换行/标点/结尾）",
          cut >= 0 and (after == "" or after in "\n。！？!?；;"),
          "after=%r" % (after,))
    check("★ 原文里的换行被保留（否则会粘成一句听不清）",
          "\n" in ex, repr(ex[:40]))
    check("节选带『完整回答见屏幕』提示语",
          voice_pipeline.EXCERPT_SUFFIX in ex)
    check("短回答不节选", voice_pipeline.speech_excerpt("短句。")[1] is False)
    check("空回答不节选也不报错", voice_pipeline.speech_excerpt("") == ("", False))
    # 句数上限从常量读，不写死：改 VOICE 节选策略时断言不会跟着变"假通过"。
    expect_sent = voice_pipeline.TTS_EXCERPT_SENTENCES
    src5 = "".join("第%d句。" % i for i in range(1, 6))
    ex2, tr2 = voice_pipeline.speech_excerpt(src5)
    check("超过句数上限时只取前 %d 句（条数由 TTS_EXCERPT_SENTENCES 决定）" % expect_sent,
          tr2 is True and ex2.count("。") == expect_sent + 1, ex2)   # N 句 + 提示语里的 1 个句号
    check("★ 第 %d 句确实被裁掉（不是原样返回全文）" % (expect_sent + 1),
          ("第%d句。" % (expect_sent + 1)) not in ex2, ex2)


    st2, b2 = http("POST", "/api/v1/nl/query", {"q": SAY, "wait_ms": 1200})
    d2 = b2.get("data") or {}
    check("文字入口返回 200", st2 == 200, st2)
    check("intent 一致（两条入口同一套解析）",
          d.get("task", {}).get("intent") == d2.get("intent"),
          (d.get("task", {}).get("intent"), d2.get("intent")))
    check("claim 一致", d.get("task", {}).get("claim") == d2.get("claim"),
          (d.get("task", {}).get("claim"), d2.get("claim")))
    check("远端状态一致", d.get("task", {}).get("remote_status") == d2.get("status"),
          (d.get("task", {}).get("remote_status"), d2.get("status")))
    check("证据来源一致", d.get("task", {}).get("source") == (d2.get("evidence") or {}).get("source"),
          (d.get("task", {}).get("source"), (d2.get("evidence") or {}).get("source")))
    check("回答文本完全一致", d.get("task", {}).get("answer") == d2.get("answer"))

    # -------------------------------------------------------------------
    section("C. 红线 V1：无声 → 不提交识别、不产生任何文本")
    # -------------------------------------------------------------------
    n_before = turn_count()
    st, b = turn(wav_silence(1500))
    d = b.get("data") or {}
    check("HTTP 200（这是可理解的业务结论，不是服务器错误）", st == 200, st)
    check("final_state=no_speech", d.get("final_state") == "no_speech", d.get("final_state"))
    check("★ transcript 必须为 null", d.get("transcript") is None, d.get("transcript"))
    sg = stages_of(d)
    check("★ 识别段被跳过（根本没提交给引擎）",
          sg["asr"]["status"] == "skipped" and sg["asr"].get("engine") is None, sg["asr"])
    check("任务段被跳过", sg["task"]["status"] == "skipped")
    check("合成段被跳过", sg["tts"]["status"] == "skipped")
    check("错误码=NO_SPEECH", (d.get("error") or {}).get("code") == "NO_SPEECH",
          d.get("error"))
    msg = (d.get("error") or {}).get("message") or ""
    check("反馈里给出实测 RMS 与阈值（可理解）",
          "RMS" in msg and str(voice_pipeline.VAD_RMS_MIN) in msg, msg[:90])
    check("没有任何播报音频", d.get("reply_audio_url") is None)
    row = db_rows("SELECT * FROM voice_turns WHERE turn_id=?", (d["turn_id"],))[0]
    check("★ 数据库里 transcript 也是 NULL（不是空串）",
          row["transcript"] is None, row["transcript"])
    check("被跳过的一轮没有多写任何任务记录", turn_count() == n_before + 1)

    # -------------------------------------------------------------------
    section("D. 红线 V6：采样格式必须校验并报出实际参数")
    # -------------------------------------------------------------------
    n_before = turn_count()
    st, b = turn(wav_bytes(pcm_tone(800, ch=2), rate=44100, ch=2))
    e = (b.get("error") or {})
    check("44.1k 立体声 → 400", st == 400, st)
    check("错误码 AUDIO_FORMAT_INVALID", e.get("code") == "AUDIO_FORMAT_INVALID", e.get("code"))
    check("报出实际参数（44100 / 2 声道）",
          "44100" in json.dumps(e, ensure_ascii=False) and
          "2" in json.dumps(e, ensure_ascii=False), e.get("message"))
    check("同时给出期望格式", (e.get("details") or {}).get("expected", {}).get("sample_rate") == 16000)

    st, b = turn(wav_bytes(pcm_tone(800), rate=8000))
    check("8 kHz 单声道 → 400", st == 400 and
          (b.get("error") or {}).get("code") == "AUDIO_FORMAT_INVALID", st)
    check("错误信息里点名要求 16000 Hz",
          "16000" in ((b.get("error") or {}).get("message") or ""),
          (b.get("error") or {}).get("message"))

    st, b = turn(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    check("非 WAV 文件 → 400", st == 400 and
          (b.get("error") or {}).get("code") == "AUDIO_FORMAT_INVALID", st)

    st, b = turn(b"")
    check("空请求体 → 400（在读取层就拦下）", st == 400, st)

    st, b = turn(wav_silence(100))
    check("过短（100ms）→ 400 AUDIO_TOO_SHORT",
          st == 400 and (b.get("error") or {}).get("code") == "AUDIO_TOO_SHORT", st)

    st, b = turn(wav_silence(41000))
    check("过长（41 秒）→ 400 AUDIO_TOO_LONG",
          st == 400 and (b.get("error") or {}).get("code") == "AUDIO_TOO_LONG", st)
    check("★ 以上被拒请求一条都没入库", turn_count() == n_before, turn_count())

    # -------------------------------------------------------------------
    section("E. 红线 V5：音频来源与运行位置必须如实声明")
    # -------------------------------------------------------------------
    st, b = turn(wav, source=None)
    check("缺少 audio_source → 400 AUDIO_SOURCE_INVALID",
          st == 400 and (b.get("error") or {}).get("code") == "AUDIO_SOURCE_INVALID", st)
    st, b = turn(wav, source="mic_of_someone_else")
    e = (b.get("error") or {})
    check("非法 audio_source → 400 并列出允许值",
          # 第 6 周新增了 text_input（澄清时用键盘补一句），所以允许值集合变了。
          # 这是**有意的演进**，不是放宽：仍然只接受白名单里的值，仍然 400。
          st == 400 and set((e.get("details") or {}).get("allowed") or []) ==
          {"browser_mic", "uploaded_file", "device_mic", "text_input"}, e.get("message"))
    st, b = turn(wav, loc=None)
    check("缺少 capture_location → 400 CAPTURE_LOCATION_MISSING",
          st == 400 and (b.get("error") or {}).get("code") == "CAPTURE_LOCATION_MISSING", st)
    st, b = turn(wav, source="browser_mic", loc="browser:Chrome/Windows-11 本机 PC")
    check("browser_mic 合法受理", st == 200 and
          (b["data"]["audio_source"] == "browser_mic"), (st, b["data"].get("audio_source")))

    # -------------------------------------------------------------------
    section("F. 引擎不可用：识别明确失败，但文字入口不受影响（V1+V3）")
    # -------------------------------------------------------------------
    voice_pipeline.VOICE_ENGINE_URL = "http://127.0.0.1:9"     # 死端口
    try:
        st, b = turn(wav)
        d = b.get("data") or {}
        check("HTTP 200 + final_state=asr_unavailable",
              st == 200 and d.get("final_state") == "asr_unavailable",
              (st, d.get("final_state")))
        check("★ transcript 为 null", d.get("transcript") is None, d.get("transcript"))
        msg = (d.get("error") or {}).get("message") or ""
        check("反馈指向下一步（改用文字入口）", "文字入口" in msg, msg[:100])
        st, b2 = http("POST", "/api/v1/nl/query", {"q": SAY, "wait_ms": 1200})
        check("★ V3 引擎挂了，/api/v1/nl/query 仍然 200", st == 200, st)
        check("★ V3 文字入口的回答仍然正确",
              (b2.get("data") or {}).get("intent") == "query_latest",
              (b2.get("data") or {}).get("intent"))
    finally:
        voice_pipeline.VOICE_ENGINE_URL = real_engine

    # -------------------------------------------------------------------
    section("G. 识别超时：不猜文本、不执行任务")
    # -------------------------------------------------------------------
    bh = start_blackhole(8024)
    old_to = voice_pipeline.ASR_TIMEOUT_S
    voice_pipeline.VOICE_ENGINE_URL = "http://127.0.0.1:8024"
    voice_pipeline.ASR_TIMEOUT_S = 2.0
    try:
        st, b = turn(wav)
        d = b.get("data") or {}
        check("final_state=asr_timeout", d.get("final_state") == "asr_timeout",
              d.get("final_state"))
        check("★ 超时后 transcript 为 null", d.get("transcript") is None)
        sg = stages_of(d)
        check("识别段状态=timeout", sg["asr"]["status"] == "timeout", sg["asr"])
        check("★ 没有执行任何任务", sg["task"]["status"] == "skipped")
        check("反馈里写明超时秒数", "2" in ((d.get("error") or {}).get("message") or ""),
              (d.get("error") or {}).get("message"))
    finally:
        voice_pipeline.VOICE_ENGINE_URL = real_engine
        voice_pipeline.ASR_TIMEOUT_S = old_to
        bh.close()

    # -------------------------------------------------------------------
    section("H. 合成失败：识别与任务都成功，文字回答仍然完整可用")
    # -------------------------------------------------------------------
    fk = start_fake(8025, "tts_fail", text=SAY)
    voice_pipeline.VOICE_ENGINE_URL = "http://127.0.0.1:8025"
    try:
        st, b = turn(wav)
        d = b.get("data") or {}
        check("final_state=tts_failed", d.get("final_state") == "tts_failed",
              d.get("final_state"))
        check("★ 识别文本仍然有值", d.get("transcript") == SAY, d.get("transcript"))
        check("★ 文字回答仍然完整", bool((d.get("task") or {}).get("answer")),
              ((d.get("task") or {}).get("answer") or "")[:60])
        check("任务段 status=ok", (d.get("task") or {}).get("status") == "ok")
        check("合成段 status=failed", (d.get("tts") or {}).get("status") == "failed")
        msg = (d.get("error") or {}).get("message") or ""
        check("反馈说明文字回答不受影响", "文字回答" in msg, msg[:100])
        check("没有播报音频", d.get("reply_audio_url") is None)
        st, b = http("POST", "/api/v1/voice/turn/%s/played" % d["turn_id"], {"reported_by": "selftest"})
        check("对没有音频的轮次上报播放 → 409", st == 409, st)
        check("409 说明了原因", (b.get("error") or {}).get("code") == "NO_REPLY_AUDIO")
    finally:
        voice_pipeline.VOICE_ENGINE_URL = real_engine
        fk.shutdown()

    # -------------------------------------------------------------------
    section("I. 红线 V4：播放上报只记事实，不推断「已听到」")
    # -------------------------------------------------------------------
    st, b = http("POST", "/api/v1/voice/turn/%s/played" % tid, {"reported_by": "selftest"})
    check("上报播放 → 200", st == 200, st)
    body_txt = json.dumps(b, ensure_ascii=False)
    check("★ 响应里不出现 heard / 已听到 / delivered",
          not any(w in body_txt for w in ("heard", "已听到", "delivered")), body_txt[:120])
    check("★ 响应明确说明「不等于已听到」", "不等于" in body_txt, body_txt[:160])
    st, b = http("GET", "/api/v1/voice/turn/" + tid)
    p = (b.get("data") or {}).get("playback") or {}
    check("详情里已写入播放开始时间", bool(p.get("started_at_utc")), p)
    check("★ final_state 没有被播放动作改写（仍是 done）",
          (b.get("data") or {}).get("final_state") == "done",
          (b.get("data") or {}).get("final_state"))

    # -------------------------------------------------------------------
    section("J. 任务失败：识别成功但设备越界")
    # -------------------------------------------------------------------
    st, b = turn(wav, device="no-such-device-xyz", say=SAY)
    d = b.get("data") or {}
    check("final_state=task_failed", d.get("final_state") == "task_failed",
          d.get("final_state"))
    check("★ 识别文本仍然保留（失败发生在任务段，不是识别段）",
          d.get("transcript") == SAY, d.get("transcript"))
    check("任务错误被如实记录", bool((d.get("task") or {}).get("error")),
          (d.get("task") or {}).get("error"))
    check("不合成语音（不会播报一句编出来的话）",
          (d.get("tts") or {}).get("status") == "skipped")

    # -------------------------------------------------------------------
    section("K. 查询接口与回归")
    # -------------------------------------------------------------------
    st, b = http("GET", "/api/v1/voice/turns?limit=10")
    chk = (b.get("data") or {}).get("turns") or []
    check("turns 列表可用", st == 200 and len(chk) > 0, (st, len(chk)))
    check("列表按时间倒序（最新在前）",
          chk[0]["created_at_utc"] >= chk[-1]["created_at_utc"] if len(chk) > 1 else True)
    check("列表项带 final_state 与 audio_source",
          all(t.get("final_state") and t.get("audio_source") for t in chk))
    st, b = http("GET", "/api/v1/voice/turn/vt_does_not_exist")
    check("不存在的 turn → 404", st == 404, st)
    st, b = http("GET", "/api/v1/voice/health")
    dd = b.get("data") or {}
    check("health 报引擎就绪", (dd.get("engine") or {}).get("ok") is True)
    check("health 列出来源枚举与格式要求",
          # ★ 第 6 周把来源枚举从 3 个扩到 4 个（加了 text_input：澄清轮打字回答）。
          #   这里改成"必须包含原来那三个"，而不是钉死总数 —— 钉死总数会让
          #   每一次合理的枚举扩展都被判成回归。
          {"browser_mic", "uploaded_file", "device_mic"}
          <= {s.get("key") for s in (dd.get("audio_sources") or [])}
          and "16000" in (dd.get("limits") or {}).get("required_format", ""),
          [s.get("key") for s in (dd.get("audio_sources") or [])])
    for path in ("/", "/api/v1/stats", "/api/v1/cam/status", "/api/v1/nl/tools"):
        st, _ = http("GET", path)
        check("回归 %s 正常" % path, st == 200, st)
    # /api/v1/latest 在空库上**如实返回 404**（第 1 周的既有语义：没有数据就说没有，
    # 不造一条假记录），所以这里 200/404 都算"接口没坏"。
    st, b = http("GET", "/api/v1/latest")
    check("回归 /api/v1/latest 接口未损坏（空库如实 404 属正常语义）",
          st in (200, 404) and (st == 200 or (b.get("error") or True)), st)

    print("\n" + "=" * 76)
    if fails:
        print("失败 %d / %d 项：" % (len(fails), checks))
        for f in fails:
            print("  - " + f)
        print("=" * 76)
        return 1
    print("全部通过：%d 项断言" % checks)
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
