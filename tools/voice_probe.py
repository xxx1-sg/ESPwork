#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""voice_probe.py —— 第 5 周端到端探针（真实语音 → 识别 → 任务 → 播报）

跑的是**真实链路**，不是模拟：
  1. 用系统中文语音（Microsoft Huihui）合成一句话，得到一段真实的中文语音波形；
  2. 把它当作「授权录音」按 16 kHz/16 bit/单声道 WAV 上传给后端；
  3. 后端调本机离线识别引擎（vosk）得到文本，再把文本交给第 4 周同一个
     `nl_assistant.handle_query` 执行任务；
  4. 后端把回答交给系统语音合成，返回一段可播放的 WAV；
  5. 脚本把六段状态、识别文本、任务结论、两段音频与全部元数据落盘留证。

★ 为什么用"合成语音回灌"而不是麦克风：
   本机（后端所在机器）没有接入麦克风，本周的音频来源按课程约定用"电脑音频"。
   合成语音回灌对应课程里写明的**备用路径**："录音设备故障时用授权录音完成真实识别"。
   识别、任务、合成三段都是真实的；唯一不真实的是"这句话是人对着麦克风说的"。
   页面上走的是浏览器麦克风（`browser_mic`），记录里的 `audio_source` 会如实区分。
   要补验"现场对着麦克风说话"，请在浏览器里点「语音助手」→ 点击开始说话。

用法：
  # 单句（默认连本机 8000）
  D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\voice_probe.py --say "查看上次数据"
  # 一组标准句（覆盖只读 / 采集 / 状态 / 位置 / 无关）
  D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\voice_probe.py --suite
  # 无声分支（验证"没有识别文本"而不是"识别出空字符串"）
  D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\voice_probe.py --silence
  # 同一句话走「语音入口」与「文字入口」，输出对照
  D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\voice_probe.py --compare "查看上次数据"
  # 用一段真实录音文件（备用路径）
  D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\voice_probe.py --file my.wav
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(BASE_DIR, "backend")
sys.path.insert(0, BACKEND)

OUT_DIR = os.path.join(BASE_DIR, "evidence", "voice_week5")
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ===========================================================================
def _one_line(v, n):
    """把一个可能带换行的值压成一行（表格里绝不能出现 \\n，否则整张表都会错位）。"""
    s = "—" if v is None else str(v).replace("\r", " ").replace("\n", " ⏎ ")
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"


def wav16(pcm):
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return buf.getvalue()


def speech_wav(text):
    """真实中文语音：系统 TTS 合成 → 重采样到 16 kHz。返回 (wav_bytes, meta)。

    走 do_tts() 而不是直接 tts_sapi()：没有音频设备的机器上会降级到
    tone 合成音，此时 meta 里的 tts_voice 为 None、mode 会如实标出来，
    不会把合成音冒充成真人语音。
    """
    import voice_engine as ve
    raw, info = ve.do_tts(text)
    i, pcm = ve.wav_info(raw)
    p16 = ve.resample_to_16k(pcm, i["sample_rate"])
    tts_engine = info.get("engine", "sapi")
    return wav16(p16), {
        "mode": "tts_loopback" if tts_engine == "sapi" else "tone_loopback",
        "tts_engine": tts_engine,
        "tts_voice": info.get("voice"),
        "tts_sample_rate": i["sample_rate"],
        "tts_audio_ms": i["duration_ms"],
        "text": text,
    }


def silence_wav(ms=1500):
    return wav16(b"\x00\x00" * int(16000 * ms / 1000.0)), {"mode": "silence", "ms": ms}


def file_wav(path):
    """读入任意 WAV 并转成 16 kHz/16 bit/mono（真实录音走这条）。"""
    import voice_engine as ve
    with open(path, "rb") as f:
        data = f.read()
    i, pcm = ve.wav_info(data)
    if i["channels"] != 1:
        raise SystemExit("这个文件是 %d 声道，请先转成单声道" % i["channels"])
    p16 = ve.resample_to_16k(pcm, i["sample_rate"])
    return wav16(p16), {"mode": "file", "path": os.path.abspath(path),
                        "src_rate": i["sample_rate"], "src_bits": i["bits"],
                        "duration_ms": i["duration_ms"]}


# ===========================================================================
def post_turn(base, wav, device=None, source="uploaded_file",
              loc="tts-loopback:Windows-11 本机 PC（授权录音·备用路径）", wait_ms=8000):
    q = {"audio_source": source, "capture_location": loc,
         "playback_location": "browser <audio> · 本机 PC", "wait_ms": str(wait_ms)}
    if device:
        q["device_id"] = device
    url = base + "/api/v1/voice/turn?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, data=wav, method="POST",
                                 headers={"Content-Type": "audio/wav"})
    try:
        with _OPENER.open(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def post_json(base, path, payload, timeout=60):
    req = urllib.request.Request(base + path,
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def get_bytes(base, path):
    with _OPENER.open(base + path, timeout=30) as r:
        return r.read()


# ===========================================================================
def show(d, wav_in=None, wav_out=None, meta=None):
    st = {s["key"]: s for s in d.get("stages") or []}
    icon = {"ok": "✔", "failed": "✘", "timeout": "⏱", "unavailable": "✘",
            "skipped": "·", "pending": "○", "empty": "·"}
    print("  turn_id      : %s" % d.get("turn_id"))
    print("  final_state  : %s" % d.get("final_state"))
    print("  音频来源     : %s（%s）" % (d.get("audio_source"), d.get("audio_source_label")))
    print("  音频事实     : %.2f 秒 · %d Hz/%d bit/%d 声道 · %d 字节 · sha256=%s…" % (
        (d["audio"]["duration_ms"] or 0) / 1000.0, d["audio"]["sample_rate"],
        d["audio"]["bits"], d["audio"]["channels"], d["audio"]["bytes"],
        (d["audio"]["sha256"] or "")[:12]))
    print("  --- 六段 ---")
    for k in ("recorded", "vad", "asr", "task", "tts", "playback"):
        s = st.get(k) or {}
        ms = s.get("ms")
        print("   %s %-4s %-6s %s" % (icon.get(s.get("status"), "?"), s.get("label", k),
                                      ("%dms" % ms) if ms else "", (s.get("detail") or "")[:100]))
    print("  --- 结果 ---")
    print("  识别文本     : %s" % (("“%s”" % d["transcript"]) if d.get("transcript") else "（无）"))
    t = d.get("task") or {}
    print("  意图 / claim : %s / %s（%s）" % (t.get("intent"), t.get("claim"), t.get("remote_status")))
    print("  来源         : %s" % (t.get("source") or "—"))
    if t.get("answer"):
        print("  回答         :")
        for line in str(t["answer"]).splitlines():
            print("      " + line)
    tt = d.get("tts") or {}
    # 缺字段就写 "—"，不要拼出 "Nonems" 这种假数据样子的串
    def _f(v, unit=""):
        return "—" if v is None else ("%s%s" % (v, unit))
    print("  播报         : %s · %s · %s · %s" % (
        _f(tt.get("status")), _f(tt.get("voice") or tt.get("engine")),
        _f(tt.get("ms"), "ms"), _f(tt.get("bytes"), " 字节")))
    if d.get("error"):
        print("  错误         : [%s] %s" % (d["error"].get("code"), d["error"].get("message")))
    print("  --- 运行位置 ---")
    for k, v in (d.get("run_location") or {}).items():
        print("   %-9s %s" % (k, v))
    print("  总耗时       : %sms" % d.get("elapsed_ms"))

    # 留证
    os.makedirs(OUT_DIR, exist_ok=True)
    tid = d.get("turn_id") or ("probe_%d" % int(time.time()))
    info = {"meta": meta or {}, "turn": d}
    # 音频以服务端回放的字节为准（证明"存下来的就是它"）
    try:
        info["audio_in_bytes"] = len(get_bytes(args.base, d["audio_url"]))
    except Exception as e:
        info["audio_in_bytes"] = "ERR " + repr(e)
    if d.get("reply_audio_url"):
        try:
            out = get_bytes(args.base, d["reply_audio_url"])
            info["audio_out_bytes"] = len(out)
            with open(os.path.join(OUT_DIR, tid + "_out.wav"), "wb") as f:
                f.write(out)
        except Exception as e:
            info["audio_out_bytes"] = "ERR " + repr(e)
    if wav_in:
        with open(os.path.join(OUT_DIR, tid + "_in.wav"), "wb") as f:
            f.write(wav_in)
    with open(os.path.join(OUT_DIR, tid + ".json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    print("  已留证       : %s" % os.path.join(OUT_DIR, tid + ".json"))


def run_one(text, args, source_kind="tts"):
    if text is None:
        wav, meta = silence_wav()
    elif source_kind == "file":
        wav, meta = file_wav(text)
    else:
        wav, meta = speech_wav(text)
    st, body = post_turn(args.base, wav, args.device, wait_ms=args.wait)
    print("  HTTP %s" % st)
    if not body.get("ok"):
        print("  错误: %s" % json.dumps(body.get("error"), ensure_ascii=False))
        return None
    d = body["data"]
    show(d, wav, meta=meta)
    return d


# ===========================================================================
def cmd_compare(args, text):
    """同一句话：先走语音入口，再走文字入口，比对结论。"""
    print("=" * 78)
    print("同一句话的两条入口对照：「%s」" % text)
    print("=" * 78)
    wav, meta = speech_wav(text)
    st, body = post_turn(args.base, wav, args.device, wait_ms=args.wait)
    if not body.get("ok"):
        print("语音入口失败：", json.dumps(body.get("error"), ensure_ascii=False))
        return 1
    v = body["data"]
    print("\n【语音入口】/api/v1/voice/turn")
    print("  识别文本 : %s" % v.get("transcript"))
    vt = v.get("task") or {}
    print("  意图     : %s" % vt.get("intent"))
    print("  claim    : %s" % vt.get("claim"))
    print("  状态     : %s" % vt.get("remote_status"))
    print("  来源     : %s" % vt.get("source"))

    payload = {"q": v.get("transcript") or text, "wait_ms": args.wait}
    if args.device:
        payload["device_id"] = args.device
    st2, body2 = post_json(args.base, "/api/v1/nl/query", payload)
    if not body2.get("ok"):
        print("文字入口失败：", json.dumps(body2.get("error"), ensure_ascii=False))
        return 1
    n = body2["data"]
    print("\n【文字入口】/api/v1/nl/query")
    print("  输入     : %s" % payload["q"])
    print("  意图     : %s" % n.get("intent"))
    print("  claim    : %s" % n.get("claim"))
    print("  状态     : %s" % n.get("status"))
    print("  来源     : %s" % (n.get("evidence") or {}).get("source"))

    rows = [
        ("意图", vt.get("intent"), n.get("intent")),
        ("claim", vt.get("claim"), n.get("claim")),
        ("状态", vt.get("remote_status"), n.get("status")),
        ("来源", vt.get("source"), (n.get("evidence") or {}).get("source")),
    ]
    print("\n【对照】")
    print("  %-6s %-32s %-32s %s" % ("项", "语音入口", "文字入口", "结论"))
    for k, a, b in rows:
        print("  %-6s %-32s %-32s %s" % (k, _one_line(a, 31), _one_line(b, 31),
                                         "一致" if a == b else "不同"))

    # 回答是多行长文本，塞不进表格（塞进去会把下面每一行都顶歪），单独并排显示
    ans_v, ans_n = vt.get("answer") or "", n.get("answer") or ""
    print("\n【回答对照】")
    print("  ── 语音入口 ──")
    for line in ans_v.splitlines() or [""]:
        print("    " + line)
    print("  ── 文字入口 ──")
    for line in ans_n.splitlines() or [""]:
        print("    " + line)
    if ans_v == ans_n:
        print("\n  ✔ 逐字相同（%d 字）" % len(ans_v))
    else:
        i = next((k for k, (x, y) in enumerate(zip(ans_v, ans_n)) if x != y),
                 min(len(ans_v), len(ans_n)))
        print("\n  · 回答不是逐字相同：首次差异在第 %d 字附近" % i)
        print("      语音: …%s…" % ans_v[max(0, i - 18):i + 18].replace("\n", "⏎"))
        print("      文字: …%s…" % ans_n[max(0, i - 18):i + 18].replace("\n", "⏎"))
        if "秒前" in ans_v or "秒前" in ans_n:
            print("    已知原因：回答里带「相对时间」（“不到 1 秒前 / 1 秒前”）——这句是")
            print("    **按请求那一刻算的**。设备还在 10 Hz 上传，两条入口相隔 1~2 秒，")
            print("    措辞就会差一层。这不是解析不一致，是两次回答的“现在”不同。")
            print("    要逐字对照：先点「暂停周期上报」，再依次跑两条入口。")

    core_same = (vt.get("intent") == n.get("intent")
                 and vt.get("claim") == n.get("claim")
                 and vt.get("remote_status") == n.get("status"))
    src_a, src_b = vt.get("source") or "", (n.get("evidence") or {}).get("source") or ""
    src_prefix_same = (src_a.split("=")[0] == src_b.split("=")[0])

    print("\n  ── 结论 ──")
    if core_same:
        print("  ✔ 语义一致：意图 / claim / 远端状态三项完全相同 ——")
        print("    语音只是换了一种输入方式，任务语义没有被改变。")
    else:
        print("  ✘ 语义不一致：意图或 claim 有差异，需要排查解析链路。")
    if src_a != src_b and src_prefix_same:
        print("  · 来源 id 不同（%s vs %s）是**正常的**：" % (src_a, src_b))
        print("    设备仍在 10 Hz 持续上传，两次查询之间确实来了新采样点，")
        print("    两次读到的都是各自当时「最新的一条」。这不是解析不一致，是数据在动。")
        if vt.get("intent") == "request_collect":
            print("    （采集类请求本来就是每次产生一条新记录，id 必然不同。）")
        print("    要做逐字对照，可以先点「暂停周期上报」，再依次跑两条入口。")
    elif src_a != src_b:
        print("  · 来源类型也不同（%s vs %s），需要确认是不是两条入口走了不同的工具。" % (src_a, src_b))
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "compare_%s.json" % int(time.time())), "w",
              encoding="utf-8") as f:
        json.dump({"text": text, "voice": v, "nl": n,
                   "same": {"intent": rows[0][1] == rows[0][2],
                            "claim": rows[1][1] == rows[1][2],
                            "status": rows[2][1] == rows[2][2],
                            "source": rows[3][1] == rows[3][2],
                            "answer": ans_v == ans_n}},
                  f, ensure_ascii=False, indent=2)
    return 0


SUITE = [
    ("查看上次数据", "只读：读数据库最新一条，零设备侧流量"),
    ("重新采集一次", "写入：产生设备侧流量，等真实观测"),
    ("设备在线吗", "只读：设备状态"),
    ("开发板在哪里", "越界：本工程没有位置能力，应明确说不足"),
    ("今天天气怎么样", "无关：应说明无法处理，不硬猜成某条指令"),
]


def main():
    global args
    ap = argparse.ArgumentParser(description="第 5 周端到端语音探针")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址")
    ap.add_argument("--device", default=None, help="设备上下文 device_id")
    ap.add_argument("--wait", type=int, default=8000, help="采集类任务最长等待 ms")
    ap.add_argument("--say", default=None, help="合成并识别这一句话")
    ap.add_argument("--suite", action="store_true", help="跑一组标准句")
    ap.add_argument("--silence", action="store_true", help="发一段静音，验证无声分支")
    ap.add_argument("--compare", default=None, metavar="TEXT", help="同一句走两条入口对照")
    ap.add_argument("--file", default=None, metavar="WAV", help="用一段真实录音文件")
    args = ap.parse_args()

    print("后端：%s" % args.base)
    try:
        with _OPENER.open(args.base + "/api/v1/voice/health", timeout=5) as r:
            h = json.loads(r.read().decode("utf-8"))["data"]
        e = h.get("engine") or {}
        if not e.get("ok"):
            print("!! 语音引擎未就绪：%s" % e.get("error"))
            print("   请先运行：.venv\\Scripts\\python.exe backend\\voice_engine.py")
            return 2
        print("引擎：ASR=%s  TTS=%s  格式要求=%s" % (
            (e.get("asr") or {}).get("backend"), (e.get("tts") or {}).get("backend"),
            h["limits"]["required_format"]))
    except Exception as ex:
        print("!! 连不上后端 %s：%r" % (args.base, ex))
        return 2

    if args.compare:
        return cmd_compare(args, args.compare)

    if args.silence:
        print("\n" + "=" * 78 + "\n【无声分支】\n" + "=" * 78)
        run_one(None, args)
        return 0

    if args.file:
        print("\n" + "=" * 78 + "\n【授权录音文件】%s\n" % args.file + "=" * 78)
        run_one(args.file, args, source_kind="file")
        return 0

    if args.suite:
        for i, (text, why) in enumerate(SUITE, 1):
            print("\n" + "=" * 78)
            print("【%d/%d】%s   —— %s" % (i, len(SUITE), text, why))
            print("=" * 78)
            run_one(text, args)
        return 0

    text = args.say or "查看上次数据"
    print("\n" + "=" * 78 + "\n【单句】%s\n" % text + "=" * 78)
    run_one(text, args)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
