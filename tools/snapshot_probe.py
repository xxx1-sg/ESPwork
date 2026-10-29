# -*- coding: utf-8 -*-
"""snapshot_probe.py —— 第 7 周：打**正在跑的真实后端**的按需快照端到端探针

它和 selftest_snapshot.py 的分工
----------------------------------------------------------------------------
  · selftest_snapshot.py：临时库 + 临时端口，逐条打**接口语义**（148 项）
  · 本文件：打 `127.0.0.1:8000` 这个**你自己在用的后端**，走完整链路
    （请求 → 随帧下发 → 设备取图 → 上传 → 落盘 → 取图 → 汇总），
    并把过程打印成可以直接贴进交付文档的形态。

没有硬件时怎么办（`--sim`）
----------------------------------------------------------------------------
真实的板子不在手边时，本文件可以起一个**协议级模拟设备**：它订阅同一个后端，
按设备协议上传预览帧、接收 snapshot_id、再按指定行为（正常/遮挡/失败/沉默）
把图像送回来 —— 用来验证**协议与状态机**，并打印一行醒目的
`【SIM】本次结论来自协议演练`。

★ 模拟设备的图像**绝不会被记成真机采集**：
  它上传时带 `sim=1`，后端据此把 `simulated=1`、`counts_as_live=0` 落库，
  汇总里也单独计数 —— 所以"演练"和"实测"在数据上不会混。

用法
----------------------------------------------------------------------------
    # 真机（板子在线时）
    D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\snapshot_probe.py

    # 没有硬件：起一个模拟设备
    D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\snapshot_probe.py --sim

    # 指定设备与日志
    ... --device esp32s3eye-1C704C --log D:\\ESP\\evidence\\snapshot_probe.log
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools"))
sys.path.insert(0, os.path.join(BASE, "backend"))
import jpeg_fixture as jf                       # noqa: E402
import img_quality as iq                        # noqa: E402  ★ 复用后端阈值，别让探针自己写死数字

TOKEN = os.environ.get("SENSOR_TOKEN", "esp32s3eye-demo-token-2026")
# ★ 连本机不该走代理：宿主的 HTTP_PROXY 会把 127.0.0.1 也送去代理，结果回 502，
#   排查方向会被带偏成"后端挂了"（第 6 周 D4 踩过）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

checks = [0]
fails = []


def check(name, cond, detail=""):
    checks[0] += 1
    line = ("  OK   " if cond else "  FAIL ") + name + ((" | " + str(detail)) if detail else "")
    print(line)
    if not cond:
        fails.append(name)


def api(method, path, body=None, token=False, ctype="application/json", timeout=30):
    url = ARGS.api.rstrip("/") + path
    headers = {"Content-Type": ctype, "User-Agent": "snapshot-probe/1.0"}
    if token:
        headers["X-Device-Token"] = TOKEN
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, raw, dict(r.getheaders())
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def japi(method, path, obj=None, token=False):
    body = None if obj is None else json.dumps(obj).encode()
    s, raw, _ = api(method, path, body, token=token)
    try:
        return s, json.loads(raw)
    except ValueError:
        return s, {"_raw": raw[:200].decode("utf-8", "ignore")}


def wait_final(sid, deadline_s):
    t0 = time.time()
    last = {}
    while time.time() - t0 < deadline_s:
        s, j = japi("GET", "/api/v1/cam/snapshot/" + sid)
        last = (j or {}).get("snapshot") or {}
        if last.get("state") in ("ok", "occluded", "failed", "timeout", "cancelled", "replayed"):
            return last
        time.sleep(0.4)
    return last


# ===========================================================================
# 协议级模拟设备
# ===========================================================================
class SimDevice(threading.Thread):
    """按设备协议行事：上传预览帧 → 解析响应里的 snapshot_id → 取图并上传。

    behavior:
        normal  正常送一张有内容的画面
        dark    送一张"镜头被盖住"的画面（又暗又平）
        fail    回报取帧失败
        silent  对请求不作任何反应（用来验证超时）
    """

    def __init__(self, device_id, api_base, behavior="normal", fps=4.0):
        threading.Thread.__init__(self, daemon=True)
        self.device_id = device_id
        self.api_base = api_base
        self.behavior = behavior
        self.period = 1.0 / max(0.5, fps)
        self.seq = 0
        self.seen = []                 # 收到过的 snapshot_id
        self.stop_flag = False
        self.errors = []
        self.frames = 0

    def _post(self, path, body, ctype):
        try:
            s, raw, _ = api("POST", path, body, token=True, ctype=ctype, timeout=15)
            return s, raw
        except Exception as e:                            # noqa: BLE001
            self.errors.append(repr(e))
            return 0, b""

    def run(self):
        while not self.stop_flag:
            self.seq += 1
            frame = jf.make_scene_jpeg(320, 240)
            s, raw = self._post("/api/v1/cam/frame?device_id=%s&seq=%d&w=320&h=240&sim=1"
                                % (self.device_id, self.seq), frame, "image/jpeg")
            if s == 200:
                self.frames += 1
                try:
                    resp = json.loads(raw)
                except ValueError:
                    resp = {}
                sid = (resp or {}).get("snapshot_id") or ""
                if sid:
                    self.seen.append(sid)
                    self._handle(sid)
            time.sleep(self.period)

    def _handle(self, sid):
        q = "&sim=1&device_id=%s" % self.device_id
        if self.behavior == "silent":
            return                                  # 什么都不做 → 让后端超时
        if self.behavior == "fail":
            self._post("/api/v1/cam/snapshot/begin",
                       json.dumps({"snapshot_id": sid, "device_id": self.device_id}).encode(),
                       "application/json")
            self._post("/api/v1/cam/snapshot/fail",
                       json.dumps({"snapshot_id": sid, "device_id": self.device_id,
                                   "error_code": "GRAB_FAILED",
                                   "error_message": "模拟：UXGA 取帧超时"}).encode(),
                       "application/json")
            return
        # ★ 模拟设备也走"先报开始，再取新一帧"的真实时序
        self._post("/api/v1/cam/snapshot/begin",
                   json.dumps({"snapshot_id": sid, "device_id": self.device_id,
                               "uptime_ms": self.seq * 250}).encode(), "application/json")
        t_get = time.time()
        self.seq += 1                                # ★ 新取一帧 → 序号严格变大
        img = jf.make_flat_jpeg(320, 240, 18) if self.behavior == "dark" \
            else jf.make_scene_jpeg(320, 240)
        prep_ms = int((time.time() - t_get) * 1000)
        self._post("/api/v1/cam/snapshot/upload?sid=%s&seq=%d&w=320&h=240&upload_ms=%d%s"
                   % (sid, self.seq, prep_ms, q), img, "image/jpeg")


# ===========================================================================
# 主流程
# ===========================================================================
def main():
    print("=" * 96)
    print("第 7 周 · 按需环境快照 端到端探针")
    print("后端: %s" % ARGS.api)
    print("=" * 96)

    # ---- S0 预检 ----
    s, j = japi("GET", "/healthz")
    check("S0.1 后端在线", s == 200 and j.get("ok") is True, s)
    s, st = japi("GET", "/api/v1/cam/status")
    check("S0.2 摄像头状态接口可用", s == 200, s)
    settings = {"timeout_s": 25}
    s, summ = japi("GET", "/api/v1/cam/snapshot/summary")
    if s == 200 and summ.get("settings"):
        settings = summ["settings"]
    # ★ 基线：库里可能已经有别的记录（真机跑过、或上一轮探针），
    #   下面只断言"本轮让计数怎么变"，不断言"库里的绝对值是多少"。
    live0 = summ.get("live_ok", 0) + summ.get("live_occluded", 0)
    sim0 = summ.get("simulated", 0)
    print("  保留参数: 超时 %.0fs / 每设备最少保留 %s 张 / 保留 %s 天"
          % (settings.get("timeout_s", 25), settings.get("keep_min"), settings.get("retention_days")))

    devs = st.get("devices_with_frames") or []
    sim = None
    if ARGS.sim or not devs:
        dev = ARGS.sim_device
        why = "命令行指定了 --sim" if ARGS.sim else "devices_with_frames=%r 是空的" % (devs,)
        print("\n★【SIM】本次起一个**协议模拟设备**：%s（原因：%s）" % (dev, why))
        print("        模拟设备上传时带 sim=1，后端会把 simulated=1 / counts_as_live=0 落库，")
        print("        所以下面的结论只证明**协议与状态机**成立，不是这台机器拍到的画面。")
        sim = SimDevice(dev, ARGS.api, behavior="normal", fps=4.0)
        sim.start()
        for _ in range(40):
            if sim.frames > 0:
                break
            time.sleep(0.2)
        check("S0.3 模拟设备已开始上传预览帧", sim.frames > 0, sim.frames)
    else:
        dev = ARGS.device or devs[0]
        print("\n真机: %s（已有预览帧流）" % dev)

    def set_behavior(b):
        if sim:
            sim.behavior = b

    # ---- S1 正常一轮 ----
    print("\n【S1】正常一轮（mode=qvga，请求之后重新取一帧）")
    set_behavior("normal")
    s, j = japi("POST", "/api/v1/cam/snapshot",
                {"device_id": dev, "mode": "qvga", "trigger": "user_request",
                 "reason": "probe:normal"})
    check("S1.1 请求被接受 202", s == 202, (s, str(j)[:120]))
    sid = (j.get("snapshot") or {}).get("snapshot_id")
    check("S1.2 拿到 snapshot_id", bool(sid), sid)
    if sid:
        rec = wait_final(sid, 30)
        for _ in range(3):
            if rec.get("state") in ("ok", "occluded"):
                break
            time.sleep(1.0)
            rec = wait_final(sid, 5)
        print("      state=%s  quality=%s  captured=%s  received=%s"
              % (rec.get("state"), rec.get("quality_verdict"),
                 rec.get("captured_at_utc"), rec.get("received_at_utc")))
        print("      端到端 %s ms（其中快门前后 %s ms）  simulated=%s  counts_as_live=%s"
              % (rec.get("latency_total_ms"), rec.get("latency_capture_ms"),
                 rec.get("simulated"), rec.get("counts_as_live")))
        check("S1.3 终局是 ok 或 occluded（两者都是真的拍到了；occluded 只是画面不可用）",
              rec.get("state") in ("ok", "occluded"), rec.get("state"))
        check("S1.4 有快门时刻，且标出了是否为估计值",
              bool(rec.get("captured_at_utc")) and "captured_is_est" in rec)
        check("S1.5 请求时刻 ≤ 快门时刻 ≤ 到达时刻",
              (rec.get("requested_at_utc") or "9") <= (rec.get("captured_at_utc") or "0")
              <= (rec.get("received_at_utc") or "0"),
              (rec.get("requested_at_utc"), rec.get("captured_at_utc"), rec.get("received_at_utc")))
        check("S1.6 有质量度量（可复算）", bool(rec.get("quality_verdict")),
              rec.get("quality_verdict"))
        s, raw, h = api("GET", "/api/v1/cam/snapshot/%s/image" % sid)
        check("S1.7 取图 200 且是 JPEG", s == 200 and raw[:2] == b"\xff\xd8", (s, raw[:4]))
        check("S1.8 响应头带 snapshot id", (h.get("X-Snapshot-Id") or "") == sid,
              h.get("X-Snapshot-Id"))
        check("S1.9 counts_as_live 与 simulated 互斥（模拟不算真机）",
              (rec.get("simulated") is True and rec.get("counts_as_live") is False)
              or (rec.get("simulated") is False and rec.get("counts_as_live") is True),
              (rec.get("simulated"), rec.get("counts_as_live")))

    # ---- S2 陈旧帧 ----
    # ★ 模拟设备必须**取走请求但不回图**（behavior=silent）：
    #   否则它会像真机一样在 1 帧之内（4fps → 250ms）就把请求做完，
    #   等我们上传"旧序号"时状态已经终局，后端返回的是 ALREADY_FINAL 而不是 STALE_IMAGE
    #   —— 测的就不是"陈旧帧拦截"了（这是本节最初 3 项失败的真实原因）。
    # ★ 真机相反：我们没法让它沉默，所以**不等待**，趁请求刚登记就用旧序号交回，
    #   免得和真板自己的正常回图赛跑（那一跑赢了就变成测 ALREADY_FINAL）。
    print("\n【S2】把旧帧当本次结果交回来 → 必须拒收")
    if sim:
        set_behavior("silent")
    s, j = japi("POST", "/api/v1/cam/snapshot", {"device_id": dev, "mode": "qvga",
                                                "reason": "probe:stale"})
    sid2 = (j.get("snapshot") or {}).get("snapshot_id")
    check("S2.1 第二个请求被接受 202", s == 202, s)
    if sim:
        time.sleep(1.5)                   # 让模拟设备把请求取走（状态 → requested）
    else:
        print("      （真机：不等待，立刻用旧序号交回）")
    base = 0
    if sid2:
        _s, _j = japi("GET", "/api/v1/cam/snapshot/" + sid2)
        rec2 = _j.get("snapshot") or {}
        base = rec2.get("baseline_seq") or rec2.get("requested_seq") or 0
        if sim:
            check("S2.1b 请求已被取走但还没回图（停在 requested）",
                  rec2.get("state") in ("requested", "capturing"),
                  (rec2.get("state"), "baseline_seq=%s" % rec2.get("baseline_seq")))
        raw_s, raw_b, _ = api(
            "POST", "/api/v1/cam/snapshot/upload?sid=%s&device_id=%s&seq=%d&w=320&h=240&sim=1"
            % (sid2, dev, max(0, base)), jf.make_scene_jpeg(320, 240),
            token=True, ctype="image/jpeg")
        s = raw_s
        try:
            j = json.loads(raw_b)
        except ValueError:
            j = {"_raw": raw_b[:120]}
        check("S2.2 旧序号 → 400", s == 400, (s, str(j)[:120]))
        check("S2.3 错误码 SNAPSHOT_STALE_IMAGE",
              (j.get("error") or {}).get("code") == "SNAPSHOT_STALE_IMAGE", j.get("error"))
        check("S2.4 状态没有被改写",
              (j.get("snapshot") or {}).get("state") in ("pending", "requested", "capturing"),
              (j.get("snapshot") or {}).get("state"))
        japi("POST", "/api/v1/cam/snapshot/%s/cancel" % sid2, {"reason": "probe_cleanup"})

    # ---- S3 终局之后到达的图 ----
    #   模拟设备：silent → 到点 25s 自动超时（DEVICE_SLOW）
    #   真机：没法让它不响应，改成"请求一发出就取消"（USER_CANCELLED）——
    #         要验的是**同一条红线**：终局之后到的图只记迟到、不改写结论。
    timeout_s = float(settings.get("timeout_s") or 25)
    if sim:
        print("\n【S3】设备不响应 → 超时终局（最多等 %.0fs）→ 之后到的图只记迟到" % timeout_s)
        end_state, end_code = "timeout", "DEVICE_SLOW"
        set_behavior("silent")
        s, j = japi("POST", "/api/v1/cam/snapshot", {"device_id": dev, "mode": "qvga",
                                                    "reason": "probe:timeout"})
        sid3 = (j.get("snapshot") or {}).get("snapshot_id")
        check("S3.1 请求被接受 202", s == 202, s)
        rec3 = wait_final(sid3, timeout_s + 8)
    else:
        print("\n【S3】真机没法让它不响应 → 请求发出后立刻取消 → 之后到的图只记迟到")
        end_state, end_code = "cancelled", "USER_CANCELLED"
        s, j = japi("POST", "/api/v1/cam/snapshot", {"device_id": dev, "mode": "qvga",
                                                    "reason": "probe:cancel"})
        sid3 = (j.get("snapshot") or {}).get("snapshot_id")
        check("S3.1 请求被接受 202", s == 202, s)
        _s, _j = japi("POST", "/api/v1/cam/snapshot/%s/cancel" % sid3, {"reason": "probe"})
        rec3 = (_j.get("snapshot") or {})
    check("S3.2 到达终局 %s" % end_state, rec3.get("state") == end_state, rec3.get("state"))
    check("S3.3 错误码是 %s" % end_code, rec3.get("error_code") == end_code, rec3.get("error_code"))
    s, raw, _ = api("GET", "/api/v1/cam/snapshot/%s/image" % sid3)
    check("S3.4 ★ 取图 409（不回退到上一次成功的图）", s == 409, s)
    check("S3.5 返回体里没有 JPEG 字节", raw[:2] != b"\xff\xd8", raw[:4])
    # 迟到
    _s, _b, _ = api("POST",
                    "/api/v1/cam/snapshot/upload?sid=%s&device_id=%s&seq=99999&w=320&h=240&sim=1"
                    % (sid3, dev), jf.make_scene_jpeg(320, 240),
                    token=True, ctype="image/jpeg")
    s = _s
    try:
        j = json.loads(_b)
    except ValueError:
        j = {"_raw": _b[:120]}
    check("S3.6 迟到图像被收下但标 late", s == 200 and j.get("late") is True, (s, str(j)[:100]))
    s, j = japi("GET", "/api/v1/cam/snapshot/" + sid3)
    rec3b = j.get("snapshot") or {}
    check("S3.7 ★ 终局仍然是 %s（迟到不改结论）" % end_state,
          rec3b.get("state") == end_state, rec3b.get("state"))
    check("S3.8 迟到图像单独留档", bool(rec3b.get("late_filename")), rec3b.get("late_filename"))
    check("S3.9 counts_as_live 仍是 False", rec3b.get("counts_as_live") is False)

    # ---- S4 遮挡 ----
    #   模拟设备：送一张"又暗又平"的图 → 必须判 occluded
    #   真机：没法用代码把镜头盖住 → 只验"度量确实跑了、结论与度量自洽"；
    #         想验遮挡就用手挡住镜头再跑一次（这时应看到 state=occluded）。
    print("\n【S4】镜头被遮挡 → occluded（有质量证据，不算看到环境）")
    if sim:
        set_behavior("dark")
    else:
        print("      （真机：无法用代码遮住镜头；本段只验「度量链路」，含遮挡自洽性）")
    s, j = japi("POST", "/api/v1/cam/snapshot", {"device_id": dev, "mode": "qvga",
                                                "reason": "probe:occluded"})
    sid4 = (j.get("snapshot") or {}).get("snapshot_id")
    check("S4.1 请求被接受 202", s == 202, s)
    rec4 = wait_final(sid4, 30)
    q4 = rec4.get("quality") or {}
    print("      state=%s  mean_luma=%s  std=%s  note=%s"
          % (rec4.get("state"), q4.get("mean_luma"), q4.get("luma_std"),
             (rec4.get("quality_note") or "")[:76]))
    if sim:
        check("S4.2 状态=occluded", rec4.get("state") == "occluded", rec4.get("state"))
        check("S4.3 quality_verdict=occluded", rec4.get("quality_verdict") == "occluded")
    else:
        check("S4.2 终局是 ok 或 occluded（两者都必须来自真实度量）",
              rec4.get("state") in ("ok", "occluded"), rec4.get("state"))
        check("S4.3 ★ 结论与亮度度量自洽（暗+平 ⇔ occluded）",
              (rec4.get("state") == "occluded")
              == bool(q4.get("mean_luma") is not None
                      and q4.get("mean_luma") < iq.DARK_LUMA_MAX
                      and (q4.get("luma_std") or 0) < iq.FLAT_STD_MAX),
              (rec4.get("state"), q4.get("mean_luma"), q4.get("luma_std")))
    check("S4.4 记录里有可复算的亮度指标", q4.get("mean_luma") is not None, q4.get("mean_luma"))
    s, raw, _ = api("GET", "/api/v1/cam/snapshot/%s/image" % sid4)
    check("S4.5 遮挡的图仍然可取（证据不藏）", s == 200, s)

    # ---- S5 回放 ----
    print("\n【S5】回放历史图像 → 标为 replayed，不算本次采集")
    s, j = japi("GET", "/api/v1/cam/photos?limit=1&device_id=" + dev)
    photos = (j or {}).get("photos") or []
    if not photos:
        s, j = japi("GET", "/api/v1/cam/photos?limit=1")
        photos = (j or {}).get("photos") or []
    if not photos:
        print("      （没有历史照片，跳过本段：先让设备拍一张，或跑一次第 4 周的拍照）")
    else:
        ph = photos[0]
        s, before = japi("GET", "/api/v1/cam/snapshot/summary")
        last_before = (before.get("last_live_ok") or {}).get("snapshot_id")
        s, j = japi("POST", "/api/v1/cam/snapshot/replay",
                    {"device_id": ph["device_id"], "photo_id": ph["photo_id"],
                     "reason": "probe:replay"})
        rp = (j or {}).get("snapshot") or {}
        check("S5.1 回放 200", s == 200, s)
        check("S5.2 状态=replayed", rp.get("state") == "replayed", rp.get("state"))
        check("S5.3 ★ counts_as_live=False", rp.get("counts_as_live") is False)
        check("S5.4 ★ 快门时刻=原照片采集时刻（不是现在）",
              rp.get("captured_at_utc") == ph["created_at_utc"],
              (rp.get("captured_at_utc"), ph["created_at_utc"]))
        s, after = japi("GET", "/api/v1/cam/snapshot/summary")
        check("S5.5 ★ 回放不改变「最近一次成功采集」",
              (after.get("last_live_ok") or {}).get("snapshot_id") == last_before,
              ((after.get("last_live_ok") or {}).get("snapshot_id"), last_before))
        check("S5.6 replayed 计数 +1", after.get("replayed", 0) >= 1, after.get("replayed"))

    # ---- S6 汇总 ----
    print("\n【S6】汇总口径：真机 / 模拟 / 回放 分开")
    s, summ = japi("GET", "/api/v1/cam/snapshot/summary")
    if sim:
        check("S6.1 模拟轮的结论被单独计数（simulated 增加）",
              summ.get("simulated", 0) > sim0, (summ.get("simulated"), sim0))
        check("S6.2 ★ 模拟一条都没混进 live_ok（本轮 live_ok 没变）",
              summ.get("live_ok", 0) <= live0, (summ.get("live_ok"), live0))
        print("      live_ok=%s live_occluded=%s simulated=%s replayed=%s by_state=%s"
              % (summ.get("live_ok"), summ.get("live_occluded"), summ.get("simulated"),
                 summ.get("replayed"), summ.get("by_state")))
    else:
        check("S6.1 真机轮增加了 live 计数（本轮真拍到的记在真机口径里）",
              (summ.get("live_ok", 0) + summ.get("live_occluded", 0)) > live0,
              (summ.get("live_ok"), summ.get("live_occluded"), live0))
        # ★ 别把"这次镜头恰好没被遮住"写成失败：遮挡只有在 S4 真判出来时才该计数。
        print("      live_ok=%s live_occluded=%s simulated=%s replayed=%s by_state=%s"
              % (summ.get("live_ok"), summ.get("live_occluded"), summ.get("simulated"),
                 summ.get("replayed"), summ.get("by_state")))
        check("S6.2 ★ 真机轮的 counts_as_live 为真（模拟才会是 False）",
              rec.get("counts_as_live") is True,
              (rec.get("simulated"), rec.get("counts_as_live")))
    check("S6.3 last_live_ok 若不是空，状态必须是 ok",
          (summ.get("last_live_ok") or {}).get("state") in (None, "ok"),
          (summ.get("last_live_ok") or {}).get("state"))
    check("S6.4 汇总带保留参数", "keep_min" in (summ.get("settings") or {}), summ.get("settings"))

    if sim:
        sim.stop_flag = True
        print("\n★【SIM】模拟设备共上传 %d 帧，处理快照请求 %d 次（errors=%d）"
              % (sim.frames, len(sim.seen), len(sim.errors)))
        print("   本次全部结论来自协议演练：证明「请求—下发—取图—上传—落盘」这条链路成立，")
        print("   不证明这台机器的摄像头真的拍到了什么（那要接上板子、去掉 --sim 再跑一次）。")

    print("\n" + "=" * 96)
    print("探针%s：%d 项断言" % ("全部通过" if not fails else "有失败（%d 项）" % len(fails), checks[0]))
    if fails:
        print("失败项: " + " | ".join(fails))
    print("=" * 96)
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--device", default=None, help="指定真机 device_id")
    ap.add_argument("--sim", action="store_true", help="强制启用协议模拟设备")
    ap.add_argument("--sim-device", default="simdevice-lab-01",
                    help="模拟设备的 device_id（默认 simdevice-lab-01）")
    ap.add_argument("--log", default=None, help="同时把输出写入该文件（证据留档）")
    ARGS = ap.parse_args()

    if ARGS.log:
        class _Tee(object):
            def __init__(self, *files):
                self.files = files

            def write(self, data):
                for f in self.files:
                    f.write(data)
                    f.flush()

            def flush(self):
                for f in self.files:
                    f.flush()

        _fp = open(ARGS.log, "w", encoding="utf-8")
        sys.stdout = _Tee(sys.__stdout__, _fp)
    rc = main()
    sys.exit(rc)
