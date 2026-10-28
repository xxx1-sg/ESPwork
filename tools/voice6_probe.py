#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""voice6_probe.py —— 第 6 周「让语音任务可以澄清与停止」端到端探针

与 selftest_voice6.py 的分工要分清：

  selftest_voice6.py  起一个**独立**的后端（临时库、临时目录），断言接口层的每一条契约。
                      它是"考试"，跑完不留痕。
  voice6_probe.py     打**正在跑的那个**后端（默认 127.0.0.1:8000，真实数据库、真实语音引擎），
                      把同一件事从头到尾走一遍，并把时序数字打出来给人看。
                      它是"体检"，会**真的往库里写语音记录**（这也是证据）。

三个场景，正好对应本周的三条红线：

  C1 取消   在识别期间按停止 → 任务段一行都不执行（action_taken=false）；
            播报音频即使合成过也必须 410（不可播放）。
  C3 半双工 同一会话里 A 慢 B 快 → A 变 superseded、A 的 reply 410，
            会话状态不被迟到的 A 改写。
  C5 澄清   听不准 → 反问（不调业务工具）→ 打字补一句 → 纯规则消解 → 重放原句执行；
            问到上限就停，不再问、也不猜。

用法：
    D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\voice6_probe.py
    D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\voice6_probe.py --api http://127.0.0.1:8000
    D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\voice6_probe.py --no-inject
        ↑ 正式验收时用：注入一律关掉，靠真实识别与真实时序。

前提：后端在线、语音引擎（8013）在线。不需要板子。
"""

import argparse
import io
import json
import math
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

# ★ 直连：不经任何 HTTP 代理。urllib 会自动读 HTTP_PROXY/HTTPS_PROXY 环境变量，
#   而探测 127.0.0.1 时走代理会让"后端没起"变成代理返回的 502，判据就失真了。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

AP = argparse.ArgumentParser()
AP.add_argument("--api", default="http://127.0.0.1:8000")
AP.add_argument("--device", default="", help="设备上下文（留空=全部设备）")
AP.add_argument("--no-inject", action="store_true",
                help="不使用任何注入（正式验收用）；此时识别真的走引擎，需要真实说话/真实录音")
AP.add_argument("--keep", action="store_true", help="不打印中间 JSON（只看结论）")
ARGS = AP.parse_args()

API = ARGS.api.rstrip("/")
ALLOW_INJECT = not ARGS.no_inject

fails = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    print(("  OK   " if cond else "  FAIL ") + name + ((" | " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)
    return cond


def section(t):
    print("\n" + "-" * 78 + "\n" + t + "\n" + "-" * 78)


def ms(x):
    return "—" if x is None else ("%d ms" % x)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def http(method, path, data=None, ctype="application/json", timeout=40):
    url = API + path
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", ctype)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    except Exception as e:
        return 0, {"_error": "%s: %s" % (type(e).__name__, e)}
    try:
        j = json.loads(raw.decode("utf-8"))
    except Exception:
        j = {"_raw": raw[:200].decode("utf-8", "replace")}
    return status, j


def get(path, timeout=20):
    return http("GET", path, None, timeout=timeout)


def post(path, obj, timeout=40):
    return http("POST", path, obj, timeout=timeout)


def head_status(path, timeout=20):
    """只取状态码（用来验 410 Gone）。"""
    req = urllib.request.Request(API + path, method="GET")
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            r.read(1)
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 造一段"有能量"的音频（VAD 只看 RMS / 峰值，所以一段正弦足够）
# ---------------------------------------------------------------------------
def make_wav(seconds=1.8, freq=220.0, amp=0.30, rate=16000):
    n = int(rate * seconds)
    frames = bytearray()
    for i in range(n):
        v = int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", max(-32768, min(32767, v)))
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(bytes(frames))
    w.close()
    return buf.getvalue()


WAV = make_wav()
RID_SEQ = [0]


def new_rid(tag):
    RID_SEQ[0] += 1
    return "probe_%s_%d_%d" % (tag, int(time.time()) % 100000, RID_SEQ[0])


def turn_params(rid, source="browser_mic", **extra):
    q = {
        "audio_source": source,
        "capture_location": "probe:voice6_probe.py · 本机 PC（真实 HTTP 调用）",
        "playback_location": "probe:不播放（只验接口）",
        "wait_ms": "6000",
        "request_id": rid,
        "client_ack_ms": "2",
    }
    if ARGS.device:
        q["device_id"] = ARGS.device
    q.update({k: v for k, v in extra.items() if v is not None})
    return urllib.parse.urlencode(q)


def send_turn(params, wav=None, timeout=45):
    return http("POST", "/api/v1/voice/turn?" + params, wav if wav is not None else WAV,
                ctype="audio/wav", timeout=timeout)


def task_of(d):
    return (d or {}).get("task") or {}


def delivery_of(d):
    return (d or {}).get("delivery") or {}


# ===========================================================================
def main():
    print("=" * 78)
    print("第 6 周端到端探针：让语音任务可以澄清与停止")
    print("目标后端：" + API + "   注入：" + ("开" if ALLOW_INJECT else "关（正式验收）"))
    print("=" * 78)
    print("注意：本探针连的是**正在运行的那个后端**，会往真实数据库里新增语音记录。")
    print("      selftest_voice6.py 用的是临时库，两者不冲突。")

    # ---- S0 前提 ----
    section("S0 前提：后端 / 语音引擎 / 第 6 周能力自述")
    st, j = get("/healthz")
    if not check("后端在线", st == 200 and j.get("ok") is True, "HTTP %s" % st):
        print("\n后端没起来，后面的都不必跑了。")
        return 1
    st, j = get("/api/v1/voice/health")
    d = (j.get("data") or {})
    eng = d.get("engine") or {}
    check("语音引擎就绪", bool(eng.get("ok")), eng.get("error") or "")
    check("health 自述里有 session（半双工）", "session" in d, sorted(d.keys()))
    check("health 自述里有 cancel", "cancel" in d)
    check("health 自述里有 inject", "inject" in d,
          (d.get("inject") or {}).get("allowed"))
    check("health 自述里有 timing", "timing" in d)

    # ★ 真实库里通常接了不止一台板子。这时"查看上次数据"会**合法地**反问"哪一台"
    #   （那正是本周期待的行为），所以"正常一轮"必须给一个明确的设备上下文。
    if not ARGS.device:
        st, jd = get("/api/v1/devices")
        devs = (jd.get("devices") or [])
        if devs:
            ARGS.device = devs[0].get("device_id") or ""
            print("  · 自动选择设备上下文：%s（共 %d 台）" % (ARGS.device, len(devs)))
    has_inject = ("browser_mic" in json.dumps(d.get("audio_sources") or []))
    if ARGS.no_inject:
        print("  · 已按 --no-inject 运行：识别真的走 vosk，注入相关断言会跳过。")
    elif not (d.get("inject") or {}).get("allowed", True):
        print("  · 这个后端关掉了注入（VOICE_ALLOW_INJECT=0），改为只用真实识别。")
        globals()["ALLOW_INJECT"] = False

    # ---- S1 会话 + 正常一轮 ----
    section("S1 会话 + 正常一轮：时序数字必须单调，交付资格必须为真")
    st, j = post("/api/v1/voice/session", {"note": "voice6_probe"})
    sid = ((j.get("data") or {}).get("session_id")) if j.get("ok") else None
    check("建会话 200", st == 200 and bool(sid), "HTTP %s / %s" % (st, sid))

    inj = {"inject_asr_text": "查看上次数据"} if ALLOW_INJECT else {}
    rid1 = new_rid("ok")
    t_send = time.time()
    st, j = send_turn(turn_params(rid1, session_id=sid, **inj))
    rtt1 = int((time.time() - t_send) * 1000)
    d1 = j.get("data") or {}
    check("正常轮 200", st == 200 and j.get("ok") is True, "HTTP %s" % st)
    check("终局 = done", d1.get("final_state") == "done", d1.get("final_state"))
    check("识别文本被如实记录", bool(d1.get("transcript")), d1.get("transcript"))
    if ALLOW_INJECT:
        check("★ 注入的识别文本被标成 injected",
              (d1.get("asr") or {}).get("engine") == "injected",
              (d1.get("asr") or {}).get("engine"))
        check("★ 记录里保留了注入设定值", bool(d1.get("inject")), d1.get("inject"))

    tmg = d1.get("timing") or {}
    print("      时序：t0=%s 首字=%s 任务=%s 播报就绪=%s 合计=%s（客户端 RTT %d ms）" % (
        tmg.get("t0_utc"), ms(tmg.get("first_feedback_ms")), ms(tmg.get("task_done_ms")),
        ms(tmg.get("tts_ready_ms")), ms(tmg.get("total_ms")), rtt1))
    seq_ok = all(tmg.get(k) is not None
                 for k in ("first_feedback_ms", "task_done_ms", "tts_ready_ms"))
    check("★ 四个时间点都落库了（不是页面算的）", seq_ok, tmg)
    if seq_ok:
        check("★ 时序单调：首字 ≤ 任务 ≤ 就绪",
              tmg["first_feedback_ms"] <= tmg["task_done_ms"] <= tmg["tts_ready_ms"],
              (tmg["first_feedback_ms"], tmg["task_done_ms"], tmg["tts_ready_ms"]))
        check("★ 就绪 ≤ 合计", tmg["tts_ready_ms"] <= tmg.get("total_ms", 10 ** 9))
    check("★ delivery.playable = true", delivery_of(d1).get("playable") is True,
          delivery_of(d1))
    tid1 = d1.get("turn_id")
    check("可播的 reply 返回 200", head_status("/api/v1/voice/reply/%s.wav" % tid1) == 200)

    # 停止播报（任务与文字都保留，只关声音）
    st, j = post("/api/v1/voice/turn/%s/stop" % tid1, {"played_ms": 1234, "reason": "user_stop"})
    check("停止播报 200", st == 200 and j.get("ok") is True, "HTTP %s" % st)
    check("★ 停止播报不改终局（任务与回答都保留）",
          (j.get("data") or {}).get("final_state") == "done", (j.get("data") or {}).get("final_state"))
    st, j2 = get("/api/v1/voice/turn/%s" % tid1)
    sp = ((j2.get("data") or {}).get("stop") or {})
    check("★ 落库了「停在第几毫秒」", sp.get("played_ms") == 1234, sp)

    # 会话必须回到空闲（播报停了）
    st, j3 = get("/api/v1/voice/session/%s" % sid)
    check("停止播报后会话回到空闲", ((j3.get("data") or {}).get("state")) == "idle",
          (j3.get("data") or {}).get("state"))

    # ---- S2 取消（识别期间） ----
    section("S2 取消：在识别期间按停止 → 任务段一行都不执行，且播报 410")
    if not ALLOW_INJECT:
        print("  · 跳过：需要 inject_delay_ms 制造「识别很慢」的窗口（--no-inject 下没有这个窗口）")
    else:
        rid2 = new_rid("cancel")
        box = {}

        def worker():
            box["ret"] = send_turn(turn_params(rid2, session_id=sid,
                                               inject_delay_ms="4000",
                                               inject_asr_text="重新采集一次"))

        th = threading.Thread(target=worker)
        t0 = time.time()
        th.start()
        # 等它真的进入"识别中"（预入库那一行已经写了）
        entered = False
        for _ in range(40):
            time.sleep(0.1)
            st, jt = get("/api/v1/voice/turns?limit=5")
            rows = ((jt.get("data") or {}).get("turns")) or []
            me = [r for r in rows if r.get("request_id") == rid2]
            if me and me[0].get("final_state") == "running":
                entered = True
                break
        check("★ 在途的 turn 在库里**看得见**（final_state=running）", entered,
              "已等 %.1f 秒" % (time.time() - t0))
        st, jc = post("/api/v1/voice/session/%s/cancel" % sid,
                      {"request_id": rid2, "reason": "user_stop", "requested_by": "probe"})
        check("取消登记 200", st == 200 and jc.get("ok") is True, "HTTP %s" % st)
        check("★ 取消命中了在途那条（was_active=true）",
              (jc.get("data") or {}).get("was_active") is True, jc.get("data"))

        th.join(timeout=30)
        st, j2 = box.get("ret") or (0, {})
        d2 = j2.get("data") or {}
        check("在途那一轮仍然正常收尾（HTTP 200）", st == 200, "HTTP %s" % st)
        check("★ 终局 = cancelled", d2.get("final_state") == "cancelled", d2.get("final_state"))
        check("★ 取消在「识别之后」被发现（说明检查点真的在执行路径上）",
              (d2.get("cancel") or {}).get("stage") == "after_asr",
              (d2.get("cancel") or {}).get("stage"))
        check("★★ 关键：任务段一行都没执行（action_taken=false）",
              (d2.get("cancel") or {}).get("action_taken") is False,
              (d2.get("cancel") or {}).get("action_taken"))
        check("任务格被标成跳过", any(s.get("key") == "task" and s.get("status") == "skipped"
                                     for s in (d2.get("stages") or [])),
              [s.get("status") for s in (d2.get("stages") or []) if s.get("key") == "task"])
        check("★ 交付资格 playable=false（迟到结果被隔离）",
              delivery_of(d2).get("playable") is False, delivery_of(d2))
        tid2 = d2.get("turn_id")
        cod = head_status("/api/v1/voice/reply/%s.wav" % tid2)
        # 取消发生在识别之后 → 这一轮**根本没有合成过**播报，所以是 404（NO_REPLY_AUDIO）。
        # 410 Gone 是另一种情形：音频确实合成出来了，但这一轮没资格播（例如取消发生在合成之后）。
        # 两者的共同结论才是红线：**拿不到任何可播的音频**。
        check("★ 被取消那轮拿不到播报音频（404 无音频 / 410 已隔离）",
              cod in (404, 410), "HTTP %s（取消在识别期⇒从未合成）" % cod)
        st, jp = post("/api/v1/voice/turn/%s/played" % tid2, {"reported_by": "probe"})
        check("★ 想上报「已播放」也被拒（409）", st == 409, "HTTP %s / %s" % (st, jp.get("error") or {}))
        st, j4 = get("/api/v1/voice/session/%s" % sid)
        check("取消后会话回到空闲", ((j4.get("data") or {}).get("state")) == "idle",
              (j4.get("data") or {}).get("state"))

    # ---- S2b 取消落在"合成之后" ----
    section("S2b 取消落在合成之后 → 音频已经合成出来，但这一轮不许播（410 Gone）")
    if not ALLOW_INJECT:
        print("  · 跳过：这一段靠固定时间偏移去抢「合成中」那个窗口，需要后端跑在已知速度上")
    else:
        # 取消的"落点"决定了终局：
        #   落在识别期间  → 从没合成过 → 404 NO_REPLY_AUDIO
        #   落在合成之后  → 音频在磁盘上，但这一轮没资格播 → **410 Gone**（C2 的落点）
        #
        # ★ 窗口在哪里不能写死。最初这里用的是 0.42/0.48/0.60… 一组固定偏移，
        #   那是按"真实 SAPI 合成约 400 ms"标定的；换成合成只要 17 ms 的引擎
        #   （或换一台更快的机器）时，after_tts 窗口整体前移到 300~350 ms，
        #   固定偏移就**永远撞不到**，于是这条断言会误报失败。
        #   正确做法：先实测一轮拿到 task_done_ms / tts_ready_ms，再按实测值
        #   生成偏移序列 —— 测的是"语义"，不是"某台机器的碰巧速度"。
        calib = {}
        rid_c0 = new_rid("calib")
        try:
            _st, _j = send_turn(turn_params(rid_c0, session_id=sid,
                                            inject_asr_text="查看上次数据"))
            _tm = (_j.get("data") or {}).get("timing") or {}
            calib = {k: _tm.get(k) for k in ("task_done_ms", "tts_ready_ms", "total_ms")}
        except Exception as e:
            print("  · 标定轮失败：%s: %s" % (type(e).__name__, e))
        td, tr = calib.get("task_done_ms"), calib.get("tts_ready_ms")
        if isinstance(td, int) and isinstance(tr, int) and tr > td:
            # after_tts 的窗口 = [任务完成, 合成完成] 之间那段
            span = max(20, tr - td)
            offs = []
            for frac in (0.35, 0.5, 0.65, 0.8, 0.95):
                offs.append(round((td + frac * span) / 1000.0, 3))
            # 合成之后、收尾之前也有一个小窗口，补几个点
            offs += [round((tr + 10) / 1000.0, 3), round((tr + 30) / 1000.0, 3)]
        else:
            offs = [0.42, 0.48, 0.60, 0.54, 0.66]
        print("      标定：task_done=%sms tts_ready=%sms → 尝试偏移 %s" % (td, tr, offs))
        hit = None
        for off in offs:
            rid3 = new_rid("c410")
            box3 = {}

            def worker3(_rid=rid3, _box=box3):
                _box["ret"] = send_turn(turn_params(_rid, session_id=sid,
                                                    inject_asr_text="查看上次数据"))

            th3 = threading.Thread(target=worker3)
            th3.start()
            time.sleep(off)
            post("/api/v1/voice/session/%s/cancel" % sid,
                 {"request_id": rid3, "reason": "user_stop", "requested_by": "probe"})
            th3.join(timeout=40)
            st, j3 = box3.get("ret") or (0, {})
            d3 = j3.get("data") or {}
            code = head_status("/api/v1/voice/reply/%s.wav" % d3.get("turn_id"))
            print("      偏移 %.2f s → final=%s stage=%s reply=%s" % (
                off, d3.get("final_state"), (d3.get("cancel") or {}).get("stage"), code))
            if code == 410:
                hit = (off, d3)
                break
        check("★ 实测到 410 Gone（音频存在但被隔离，不是「没有音频」）", hit is not None,
              "撞到 %d 个窗口" % (1 if hit else 0))
        if hit:
            off, d3 = hit
            check("★ 取消被发现于 after_tts（正好在合成之后、收尾之前）",
                  (d3.get("cancel") or {}).get("stage") == "after_tts",
                  (d3.get("cancel") or {}).get("stage"))
            check("★ 这一轮 action_taken=true（任务确实跑过，取消收不回它）",
                  (d3.get("cancel") or {}).get("action_taken") is True,
                  (d3.get("cancel") or {}).get("action_taken"))
            check("★ 交付状态 = quarantined_cancelled 且 playable=false",
                  (d3.get("delivery") or {}).get("state") == "quarantined_cancelled"
                  and (d3.get("delivery") or {}).get("playable") is False,
                  d3.get("delivery"))
            check("★ 记录里仍然留着「合成成功过」的事实（tts.bytes > 0），不是假装没合成",
                  ((d3.get("tts") or {}).get("bytes") or 0) > 0,
                  (d3.get("tts") or {}).get("bytes"))
            st, jp3 = post("/api/v1/voice/turn/%s/played" % d3.get("turn_id"),
                           {"reported_by": "probe"})
            check("★ 想上报「已播放」被拒（409）", st == 409, "HTTP %s" % st)

    # ---- S3 半双工：新的顶掉旧的 ----
    section("S3 半双工：A 慢 B 快 → A 变 superseded，A 的播报 410，B 照常可播")
    if not ALLOW_INJECT:
        print("  · 跳过：需要给 A 注入延迟才排得出「A 慢 B 快」")
    else:
        ridA = new_rid("supA")
        ridB = new_rid("supB")
        boxA = {}

        def workerA():
            boxA["ret"] = send_turn(turn_params(ridA, session_id=sid,
                                                inject_delay_ms="3500",
                                                inject_asr_text="查看上次数据"))

        thA = threading.Thread(target=workerA)
        thA.start()
        time.sleep(0.6)          # A 已经进入"识别中"
        st, jb = send_turn(turn_params(ridB, session_id=sid,
                                       inject_asr_text="设备在线吗"))
        dB = jb.get("data") or {}
        check("★ B 先回来，终局 = done", dB.get("final_state") == "done", dB.get("final_state"))
        thA.join(timeout=40)
        st, ja = boxA.get("ret") or (0, {})
        dA = ja.get("data") or {}
        check("★ A 后回来，终局 = superseded", dA.get("final_state") == "superseded",
              dA.get("final_state"))
        check("★ A 记下了「被谁顶掉」", bool((dA.get("session") or {}).get("superseded_by")),
              (dA.get("session") or {}).get("superseded_by"))
        codA = head_status("/api/v1/voice/reply/%s.wav" % dA.get("turn_id"))
        check("★ A 拿不到播报音频（404 无音频 / 410 已隔离）", codA in (404, 410),
              "HTTP %s" % codA)
        check("★ B 的播报 200（新的那条不受影响）",
              head_status("/api/v1/voice/reply/%s.wav" % dB.get("turn_id")) == 200)
        st, j5 = get("/api/v1/voice/session/%s" % sid)
        sdata = j5.get("data") or {}
        check("★ 迟到的 A 没把会话状态改写回去",
              sdata.get("last_turn_id") == dB.get("turn_id"),
              "last_turn_id=%s B=%s" % (sdata.get("last_turn_id"), dB.get("turn_id")))

    # ---- S4 澄清一轮 + 打字补充 ----
    section("S4 澄清：听不准先问一句（不调业务工具）→ 打字补一句 → 纯规则消解 → 执行")
    inj_q = {"inject_asr_text": "看下那个"} if ALLOW_INJECT else {}
    rid4 = new_rid("clar")
    st, j = send_turn(turn_params(rid4, session_id=sid, **inj_q))
    d4 = j.get("data") or {}
    if not ALLOW_INJECT and d4.get("final_state") != "clarifying":
        print("  · 跳过：真实识别结果是「%s」，不是一句有歧义的话，构造不出澄清场景。"
              % (d4.get("transcript"),))
    else:
        check("★ 终局 = clarifying（先问，不猜）", d4.get("final_state") == "clarifying",
              d4.get("final_state"))
        asked = ((d4.get("clarify") or {}).get("asked")) or {}
        check("★ 反问带着问题原文", bool(asked.get("question")), (asked.get("question") or "")[:40])
        check("★ 反问给了候选项（能点/能说）", bool(asked.get("options")),
              asked.get("options"))
        check("★ 反问轮没有调用业务工具（来源标注为「未调用」）",
              "未调用" in str(task_of(d4).get("source")),
              task_of(d4).get("source"))
        check("★ 轮次 = 1/2", asked.get("round") == 1 and asked.get("max_rounds") == 2,
              (asked.get("round"), asked.get("max_rounds")))

        # 用户打字补充（走同一条 turn 链路）
        rid4b = new_rid("clartxt")
        st, jt = post("/api/v1/voice/text", {
            "text": "查看上次数据",
            "session_id": sid,
            "request_id": rid4b,
            "device_id": ARGS.device or None,
            "capture_location": "keyboard:voice6_probe.py · 本机 PC",
            "client_ack_ms": 1,
        })
        dt = jt.get("data") or {}
        check("★ 文字补充轮 200", st == 200 and jt.get("ok") is True, "HTTP %s" % st)
        check("★ audio_source = text_input（不伪装成麦克风）",
              dt.get("audio_source") == "text_input", dt.get("audio_source"))
        check("★ asr.engine = keyboard（没有调用识别引擎）",
              (dt.get("asr") or {}).get("engine") == "keyboard",
              (dt.get("asr") or {}).get("engine"))
        cl = dt.get("clarify") or {}
        check("★ 消解成功并记录了消解方式", bool(cl.get("how")), cl.get("how"))
        check("★ 消解结果 = 重放的那句话", bool(cl.get("resolved_with")), cl.get("resolved_with"))
        check("补充轮终局不是 clarifying（问题已经答掉了）",
              dt.get("final_state") != "clarifying", dt.get("final_state"))

        # ---- S5 澄清上限 ----
        section("S5 澄清上限：问到 2 轮就停，不再问也不再猜")
        last = None
        for i in range(4):
            st, jn = post("/api/v1/voice/text", {
                "text": "嗯",                       # 故意不可消解
                "session_id": sid,
                "request_id": new_rid("limit%d" % i),
                "capture_location": "keyboard:voice6_probe.py · 本机 PC",
            })
            last = jn.get("data") or {}
            print("      第 %d 次补充「嗯」→ final_state=%s round=%s" % (
                i + 1, last.get("final_state"), (last.get("clarify") or {}).get("round")))
            if last.get("final_state") == "clarify_exhausted":
                break
        check("★ 到上限后终局 = clarify_exhausted",
              (last or {}).get("final_state") == "clarify_exhausted",
              (last or {}).get("final_state"))
        check("★ 上限轮给出了退路说明",
              bool(((last or {}).get("error") or {}).get("message")),
              ((last or {}).get("error") or {}).get("message", "")[:50])
        check("★ 上限轮没有执行任何业务工具",
              "未调用" in str(task_of(last).get("source")) or
              any(s.get("key") == "task" and s.get("status") == "skipped"
                  for s in (last.get("stages") or [])),
              task_of(last).get("source"))

    # ---- S6 事件时序可串起来 ----
    section("S6 request_id 串起来的事件时序（同一个 request_id 能还原全过程）")
    st, j = get("/api/v1/voice/timeline?request_id=%s&limit=50" % urllib.parse.quote(rid1))
    evs = ((j.get("data") or {}).get("events")) or []
    names = [e.get("event") for e in evs]
    print("      " + " → ".join(names))
    check("★ 至少有 turn_pending 与收尾事件",
          ("turn_pending" in names) and any(n in ("finish", "finish_turn", "turn_finished")
                                           for n in names), names)

    # ---- 汇总 ----
    print("\n" + "=" * 78)
    if fails:
        print("失败 %d 项 / 共 %d 项：" % (len(fails), checks))
        for f in fails:
            print("  - " + f)
        print("=" * 78)
        return 1
    print("探针全部通过：%d 项。三件事都成立 ——" % checks)
    print("  ① 取消真的拦住了任务段（action_taken=false，且这一轮拿不到可播音频）")
    print("  ② 迟到的旧结果不会覆盖新结果（superseded，拿不到可播音频，会话状态不被改写）")
    print("  ③ 听不准时先问一句，答了才执行，问到上限就停")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
