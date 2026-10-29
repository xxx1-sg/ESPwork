# -*- coding: utf-8 -*-
"""第 7 周「按需环境快照」接口自检（独立端口 + 临时库，不碰常驻后端）

跑的是**真实的 server.py**，不是桩：一条命令起一个临时实例，把所有状态与红线
逐条打一遍，然后连库带目录一起删掉。

覆盖的核心问题（对应 docs/15）：
  A 画面质量度量本身（合成 JPEG → 亮度/平坦度 → 遮挡判定）
  B 设备没在上传画面 → 明确失败，不是含糊的"没有图"
  C 正常一轮：pending → requested → capturing → ok，三个时刻齐全
  D 陈旧帧：把请求之前/下发那一帧原样交回来 → 拒收，状态不变
  E 后到的图不能覆盖已确认的那一张
  F 超时 → timeout（终局），之后到的图只记「迟到」，不算本次结果
  G 设备明确回报失败
  H 用户取消 → cancelled，之后到的图同样只记迟到
  I 镜头遮挡 → occluded（有质量证据），且不算"看到环境"
  J 在途去重
  K 没有本次图像时绝不回退到上一张成功的图
  L 保留策略：只删文件、不删记录，且尊重"最少保留"
  M 汇总口径：live_ok / live_occluded / simulated / replayed 分开
  N 回放：原始采集时刻不变、counts_as_live=0、不影响 last_live_ok
  O 接口契约与静态页
  P 令牌与 ID 校验

用法：
    D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\selftest_snapshot.py
"""
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

PORT = 8012
TOKEN = "esp32s3eye-demo-token-2026"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "tools"))
sys.path.insert(0, os.path.join(BASE, "backend"))

import img_quality                      # noqa: E402  （被测对象之一，直接单测）
import jpeg_fixture as jf               # noqa: E402

TIMEOUT_S = 3                           # 自检里把超时调短，跑得快

tmp = tempfile.mkdtemp(prefix="snaptest_")
env = dict(os.environ)
env.update({
    "SENSOR_PORT": str(PORT),
    "SENSOR_HOST": "127.0.0.1",
    "SENSOR_DB": os.path.join(tmp, "t.db"),
    "SENSOR_CAM_DIR": os.path.join(tmp, "photos"),
    "SENSOR_SNAPSHOT_DIR": os.path.join(tmp, "snaps"),
    "SENSOR_SNAPSHOT_TIMEOUT_S": str(TIMEOUT_S),
    "SENSOR_SNAPSHOT_KEEP_MIN": "3",
    "SENSOR_SNAPSHOT_RETENTION_DAYS": "7",
    "PYTHONIOENCODING": "utf-8",
})
LOGFILE = os.path.join(tmp, "server.log")
_logfp = open(LOGFILE, "wb")
proc = subprocess.Popen([sys.executable, os.path.join(BASE, "backend", "server.py")],
                        env=env, stdout=_logfp, stderr=subprocess.STDOUT)

fails = []
checks = [0]


def check(name, cond, detail=""):
    checks[0] += 1
    print(("  OK   " if cond else "  FAIL ") + name + ((" | " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)


def req(method, path, body=None, token=True, ctype="application/json"):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=20)
    headers = {"Content-Type": ctype}
    if token:
        headers["X-Device-Token"] = TOKEN
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    data = r.read()
    hdrs = dict(r.getheaders())
    c.close()
    return r.status, hdrs, data


def jreq(method, path, obj=None, token=True):
    body = None if obj is None else json.dumps(obj).encode()
    s, h, d = req(method, path, body, token=token)
    try:
        return s, json.loads(d)
    except ValueError:
        return s, {"_raw": d[:200]}


_seq = {}


def frame(dev, jpeg=None, w=320, h=240):
    """上传一帧预览（返回响应 dict）。"""
    _seq[dev] = _seq.get(dev, 0) + 1
    jpeg = jpeg if jpeg is not None else SCENE
    s, _, d = req("POST", "/api/v1/cam/frame?device_id=%s&seq=%d&w=%d&h=%d"
                  % (dev, _seq[dev], w, h), jpeg, ctype="image/jpeg")
    return s, json.loads(d)


def start(dev, **kw):
    payload = {"device_id": dev, "mode": "qvga", "trigger": "user_request"}
    payload.update(kw)
    return jreq("POST", "/api/v1/cam/snapshot", payload, token=False)


def upload(sid, dev, jpeg, seq, extra=""):
    s, _, d = req("POST", "/api/v1/cam/snapshot/upload?sid=%s&device_id=%s&seq=%d"
                  "&w=320&h=240%s" % (sid, dev, seq, extra), jpeg, ctype="image/jpeg")
    try:
        return s, json.loads(d)
    except ValueError:
        return s, {"_raw": d[:200]}


def get(sid):
    return jreq("GET", "/api/v1/cam/snapshot/" + sid, token=False)


def wait_state(sid, want, timeout=8.0):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        s, j = get(sid)
        last = (j or {}).get("snapshot") or {}
        if last.get("state") in want:
            return last
        time.sleep(0.2)
    return last


SCENE = jf.make_scene_jpeg()
FLAT = jf.make_flat_jpeg()

for _ in range(80):
    try:
        if req("GET", "/healthz", token=False)[0] == 200:
            break
    except Exception:
        pass
    time.sleep(0.25)
else:
    print("后端未启动")
    proc.kill()
    _logfp.flush()
    print(open(LOGFILE, "rb").read().decode("utf-8", "ignore")[:3000])
    shutil.rmtree(tmp, True)
    sys.exit(1)

try:
    # =====================================================================
    print("\n[A] 画面质量度量（合成 JPEG，语法合法，走真实解析路径）")
    # =====================================================================
    q = img_quality.analyze_jpeg(SCENE)
    check("A1 有明暗的场景可测量", q["available"] is True, q.get("reason"))
    check("A2 宽高解析正确", (q["width"], q["height"]) == (320, 240), (q["width"], q["height"]))
    check("A3 平均亮度落在设定区间", 100 <= (q["mean_luma"] or 0) <= 140, q["mean_luma"])
    check("A4 块间标准差 > 10（说明画面有内容）", (q["luma_std"] or 0) > 10, q["luma_std"])
    check("A5 判为可用", img_quality.looks_occluded(q)[0] == "usable")

    qf = img_quality.analyze_jpeg(FLAT)
    check("A6 又暗又平的画面可测量", qf["available"] is True, qf.get("reason"))
    check("A7 平均亮度 ≈ 18（精确到 0.5）", abs((qf["mean_luma"] or 0) - 18) < 0.5, qf["mean_luma"])
    check("A8 标准差 ≈ 0", qf["luma_std"] is not None and qf["luma_std"] < 0.5, qf["luma_std"])
    check("A9 判为疑似遮挡", img_quality.looks_occluded(qf)[0] == "occluded")

    qa = img_quality.analyze_jpeg(b"not a jpeg at all")
    check("A10 非 JPEG → available=False（不猜）", qa["available"] is False)
    check("A11 非 JPEG 的判定是 unknown 而不是 usable",
          img_quality.looks_occluded(qa)[0] == "unknown", img_quality.looks_occluded(qa))
    qb = img_quality.analyze_jpeg(b"\xff\xd8" + b"\x00" * 40 + b"\xff\xd9")
    check("A12 结构残缺 → available=False", qb["available"] is False, qb.get("reason"))
    check("A13 describe_quality 给出人能读懂的句子",
          "平均亮度" in img_quality.describe_quality(q) or "无法测量" in img_quality.describe_quality(q))

    # =====================================================================
    print("\n[B] 设备没在上传画面 → 明确失败（不是含糊的「没有图」）")
    # =====================================================================
    s, j = start("devNOFRAME")
    check("B1 请求被拒 409", s == 409, s)
    check("B2 错误码 NO_FRAME_STREAM", (j.get("error") or {}).get("code") == "NO_FRAME_STREAM",
          j.get("error"))
    rec = j.get("snapshot") or {}
    check("B3 ★ 仍然留下了一条失败记录（失败也要有明确状态）", bool(rec.get("snapshot_id")),
          rec.get("snapshot_id"))
    check("B4 记录状态=failed", rec.get("state") == "failed", rec.get("state"))
    check("B5 counts_as_live=0（没拍到就不许算）", rec.get("counts_as_live") is False)
    check("B6 explain 说清了原因", "没有在上传画面" in (rec.get("explain") or ""), rec.get("explain"))

    # =====================================================================
    print("\n[C] 正常一轮：pending → requested → capturing → ok")
    # =====================================================================
    frame("devA", SCENE)
    s, j = start("devA")
    check("C1 返回 202", s == 202, s)
    rec = j["snapshot"]
    sid = rec["snapshot_id"]
    check("C2 初始状态 pending", rec["state"] == "pending", rec["state"])
    check("C3 记下了请求时刻", bool(rec["requested_at_utc"]))
    check("C4 记下了请求时的设备帧序号（陈旧判据的基线）", rec["requested_seq"] == _seq["devA"],
          (rec["requested_seq"], _seq["devA"]))
    check("C5 此刻没有任何图像", rec["has_image"] is False and rec["image_url"] is None)

    s, resp = frame("devA", SCENE)
    check("C6 下一帧的响应里带上了 snapshot_id", resp.get("snapshot_id") == sid, resp)
    check("C7 也带上了 mode", resp.get("snapshot_mode") == "qvga", resp.get("snapshot_mode"))
    s, resp = frame("devA")
    check("C7b ★ 设备确认之前，每一帧都重复带同一个 id（抗丢包：at-least-once）",
          resp.get("snapshot_id") == sid, resp.get("snapshot_id"))
    s, j = get(sid)
    check("C8 状态推进到 requested（已下发＝服务端可观测的事实）",
          j["snapshot"]["state"] == "requested", j["snapshot"]["state"])
    check("C9 有 delivered_at_utc", bool(j["snapshot"]["delivered_at_utc"]))

    s, j2 = jreq("POST", "/api/v1/cam/snapshot/begin",
                 {"snapshot_id": sid, "device_id": "devA", "uptime_ms": 12345})
    check("C10 设备自报开始取图 200", s == 200, s)
    s, j = get(sid)
    check("C11 状态=capturing", j["snapshot"]["state"] == "capturing", j["snapshot"]["state"])
    check("C12 有 begin_at_utc", bool(j["snapshot"]["begin_at_utc"]))

    _seq["devA"] += 1
    snap_seq = _seq["devA"]
    s, j = upload(sid, "devA", SCENE, snap_seq, extra="&upload_ms=40&uptime_ms=9999")
    check("C13 上传被接受 200", s == 200, "%s %s" % (s, str(j)[:120]))
    rec = j["snapshot"]
    s, resp = frame("devA")
    check("C13b 收尾之后不再重复下发（重发只在未确认期间发生）",
          resp.get("snapshot_id") == "", resp.get("snapshot_id"))
    check("C14 状态=ok", rec["state"] == "ok", rec["state"])
    check("C15 quality_verdict=usable", rec["quality_verdict"] == "usable", rec["quality_verdict"])
    check("C16 counts_as_live=True（真机 + 本次请求 + 新帧）", rec["counts_as_live"] is True)
    check("C17 快门时刻有值", bool(rec["captured_at_utc"]))
    check("C18 快门时刻标为估计值（设备无时钟，只能估）", rec["captured_is_est"] is True)
    check("C19 快门时刻早于到达时刻",
          rec["captured_at_utc"] <= rec["received_at_utc"],
          (rec["captured_at_utc"], rec["received_at_utc"]))
    check("C20 端到端时延是正数", (rec["latency_total_ms"] or -1) >= 0, rec["latency_total_ms"])
    check("C21 sha256 与实际字节一致",
          rec["sha256"] == hashlib.sha256(SCENE).hexdigest())
    check("C22 explain 里有「采集成功」", "采集成功" in (rec["explain"] or ""), rec["explain"])

    s, h, d = req("GET", "/api/v1/cam/snapshot/%s/image" % sid, token=False)
    check("C23 取图 200", s == 200, s)
    check("C24 图像字节逐字节一致", d == SCENE, (len(d), len(SCENE)))
    check("C25 响应头带 snapshot id", h.get("X-Snapshot-Id") == sid, h.get("X-Snapshot-Id"))
    check("C26 响应头带捕获时刻", bool(h.get("X-Snapshot-Captured-At")))
    check("C27 Cache-Control 禁止缓存", "no-store" in (h.get("Cache-Control") or ""))

    # =====================================================================
    print("\n[D] 陈旧帧：请求之前 / 下发那一帧，都不算本次采集")
    # =====================================================================
    frame("devB", SCENE)
    s, j = start("devB")
    sidD = j["snapshot"]["snapshot_id"]
    base_deliver = _seq["devB"]
    frame("devB", SCENE)                     # 下发那一帧（seq = base_deliver + 1）
    delivered_seq = _seq["devB"]

    s, j = upload(sidD, "devB", SCENE, base_deliver - 1)
    check("D1 请求之前的旧帧 → 400", s == 400, s)
    check("D2 错误码 SNAPSHOT_STALE_IMAGE",
          (j.get("error") or {}).get("code") == "SNAPSHOT_STALE_IMAGE", j.get("error"))
    check("D3 ★ 状态没有被改写（还是等取图）", (j.get("snapshot") or {}).get("state") == "requested",
          (j.get("snapshot") or {}).get("state"))

    s, j = upload(sidD, "devB", SCENE, delivered_seq)
    check("D4 ★ 把「下发那一帧」原样交回来也被拒（基线抬到了下发序号）", s == 400, s)
    check("D5 提示里写明基准序号",
          "基准" in ((j.get("error") or {}).get("message") or ""),
          (j.get("error") or {}).get("message"))

    s, j = get(sidD)
    check("D6 stale_rejects 计数=2", j["snapshot"]["stale_rejects"] == 2,
          j["snapshot"]["stale_rejects"])
    check("D7 仍然没有图像", j["snapshot"]["has_image"] is False)

    _seq["devB"] += 1
    s, j = upload(sidD, "devB", SCENE, _seq["devB"])
    check("D8 真正新取的一帧 → 接受", s == 200 and j["snapshot"]["state"] == "ok",
          (s, (j.get("snapshot") or {}).get("state")))

    # =====================================================================
    print("\n[E] 后到的图不能覆盖已确认的那一张")
    # =====================================================================
    old_sha = j["snapshot"]["sha256"]
    _seq["devB"] += 1
    s, j2 = upload(sidD, "devB", FLAT, _seq["devB"])
    check("E1 二次上传 409", s == 409, s)
    check("E2 错误码 SNAPSHOT_ALREADY_FINAL",
          (j2.get("error") or {}).get("code") == "SNAPSHOT_ALREADY_FINAL", j2.get("error"))
    s, j3 = get(sidD)
    check("E3 ★ 记录里的 sha256 一个字都没变", j3["snapshot"]["sha256"] == old_sha)
    check("E4 状态仍是 ok", j3["snapshot"]["state"] == "ok")

    # =====================================================================
    print("\n[F] 超时 → 终局；之后到的图只记「迟到」")
    # =====================================================================
    frame("devC", SCENE)
    s, j = start("devC")
    sidF = j["snapshot"]["snapshot_id"]
    frame("devC", SCENE)                      # 下发，但设备"假装没收到"
    rec = wait_state(sidF, ("timeout",), timeout=TIMEOUT_S + 6)
    check("F1 到点后状态=timeout", rec.get("state") == "timeout", rec.get("state"))
    check("F2 错误码 DEVICE_SLOW（已下发但没回）", rec.get("error_code") == "DEVICE_SLOW",
          rec.get("error_code"))
    check("F3 explain 明说不会拿上一次的图冒充",
          "不会" in (rec.get("explain") or "") or "没有" in (rec.get("explain") or ""),
          rec.get("explain"))

    s, h, d = req("GET", "/api/v1/cam/snapshot/%s/image" % sidF, token=False)
    check("F4 ★ 超时的请求取图 → 409（不是 404，也不是上一张成功的图）", s == 409, s)
    body = json.loads(d)
    check("F5 错误码 SNAPSHOT_NO_IMAGE", (body.get("error") or {}).get("code") == "SNAPSHOT_NO_IMAGE",
          body.get("error"))
    check("F6 不带任何图像字节", len(d) < 2000, len(d))

    _seq["devC"] += 1
    s, j = upload(sidF, "devC", SCENE, _seq["devC"])
    check("F7 迟到图像仍然收下（证据不能丢）200", s == 200, s)
    check("F8 accepted=False / late=True", j.get("accepted") is False and j.get("late") is True, j)
    s, j = get(sidF)
    rec = j["snapshot"]
    check("F9 ★ 终局仍然是 timeout（迟到不改结论）", rec["state"] == "timeout", rec["state"])
    check("F10 迟到图像单独存档", bool(rec["late_filename"]) and bool(rec["late_at_utc"]))
    check("F11 counts_as_live 仍然是 False", rec["counts_as_live"] is False)
    s, h, d = req("GET", "/api/v1/cam/snapshot/%s/image?late=1" % sidF, token=False)
    check("F12 迟到图像可单独取，且头里标了 late=1", s == 200 and h.get("X-Snapshot-Late") == "1",
          (s, h.get("X-Snapshot-Late")))
    check("F13 迟到图像字节与上传一致", d == SCENE)

    # =====================================================================
    print("\n[G] 设备明确回报失败")
    # =====================================================================
    frame("devD", SCENE)
    s, j = start("devD")
    sidG = j["snapshot"]["snapshot_id"]
    frame("devD", SCENE)
    s, j = jreq("POST", "/api/v1/cam/snapshot/fail",
                {"snapshot_id": sidG, "device_id": "devD",
                 "error_code": "GRAB_FAILED", "error_message": "取帧超时"})
    check("G1 回报失败 200", s == 200, s)
    check("G2 状态=failed", j["snapshot"]["state"] == "failed", j["snapshot"]["state"])
    check("G3 错误码原样落库", j["snapshot"]["error_code"] == "GRAB_FAILED")
    check("G4 explain 里有原因", "取帧超时" in (j["snapshot"]["explain"] or ""),
          j["snapshot"]["explain"])
    check("G5 没有图像", j["snapshot"]["has_image"] is False)

    # =====================================================================
    print("\n[H] 用户取消 → cancelled；之后到的图同样只记迟到")
    # =====================================================================
    frame("devE", SCENE)
    s, j = start("devE")
    sidH = j["snapshot"]["snapshot_id"]
    frame("devE", SCENE)
    s, j = jreq("POST", "/api/v1/cam/snapshot/%s/cancel" % sidH,
                {"reason": "user_cancel"}, token=False)
    check("H1 取消 200", s == 200, s)
    check("H2 状态=cancelled", j["snapshot"]["state"] == "cancelled", j["snapshot"]["state"])
    _seq["devE"] += 1
    s, j = upload(sidH, "devE", SCENE, _seq["devE"])
    check("H3 取消之后到的图 → 收下但标迟到", j.get("late") is True, j)
    s, j = get(sidH)
    check("H4 终局仍是 cancelled", j["snapshot"]["state"] == "cancelled", j["snapshot"]["state"])
    s, j = jreq("POST", "/api/v1/cam/snapshot/%s/cancel" % sidH, {}, token=False)
    check("H5 对已收尾的请求再取消 → 409", s == 409, s)

    # =====================================================================
    print("\n[I] 镜头遮挡 → occluded（有质量证据，且不算「看到环境」）")
    # =====================================================================
    frame("devF", SCENE)
    s, j = start("devF")
    sidI = j["snapshot"]["snapshot_id"]
    frame("devF", SCENE)
    _seq["devF"] += 1
    s, j = upload(sidI, "devF", FLAT, _seq["devF"])
    rec = j["snapshot"]
    check("I1 状态=occluded", rec["state"] == "occluded", rec["state"])
    check("I2 quality_verdict=occluded", rec["quality_verdict"] == "occluded")
    check("I3 质量数值落在记录里（可复算）", abs((rec["quality"] or {}).get("mean_luma", -1) - 18) < 1,
          (rec["quality"] or {}).get("mean_luma"))
    check("I4 ★ 仍然 counts_as_live=True（真拍到了，只是画面不可用）",
          rec["counts_as_live"] is True)
    check("I5 explain 说「不算一次成功的环境查看」",
          "不算" in (rec["explain"] or ""), rec["explain"])
    s, h, d = req("GET", "/api/v1/cam/snapshot/%s/image" % sidI, token=False)
    check("I6 ★ 遮挡的图仍然给看（它是证据，不是要藏起来）", s == 200, s)

    # =====================================================================
    print("\n[J] 在途去重")
    # =====================================================================
    frame("devG", SCENE)
    s, j = start("devG")
    sidJ = j["snapshot"]["snapshot_id"]
    s2, j2 = start("devG")
    check("J1 同一设备并发请求 → 409", s2 == 409, s2)
    check("J2 错误码 SNAPSHOT_BUSY", (j2.get("error") or {}).get("code") == "SNAPSHOT_BUSY",
          j2.get("error"))
    check("J3 回应里带回了在途的那一条", (j2.get("snapshot") or {}).get("snapshot_id") == sidJ)
    s, j = jreq("POST", "/api/v1/cam/snapshot/%s/cancel" % sidJ, {}, token=False)
    check("J4 取消后在途解除", s == 200, s)
    s3, j3 = start("devG")
    check("J5 取消后可以重新发起", s3 == 202, s3)
    sidJ2 = j3["snapshot"]["snapshot_id"]

    # =====================================================================
    print("\n[K] 没有本次图像时，绝不回退到上一张成功的图")
    # =====================================================================
    s, h, d = req("GET", "/api/v1/cam/snapshot/%s/image" % sidJ2, token=False)
    check("K1 等待中的请求取图 → 409", s == 409, s)
    check("K2 返回体里没有图像字节", d[:2] != b"\xff\xd8", d[:4])
    check("K3 提示是 retryable（还在等，不是永远没有）",
          (json.loads(d).get("error") or {}).get("retryable") is True,
          (json.loads(d).get("error") or {}).get("retryable"))
    s, j = jreq("GET", "/api/v1/cam/snapshots?device_id=devB", token=False)
    prev_ok = [x for x in j["snapshots"] if x["state"] == "ok"]
    check("K4 ★ 上一次成功的图仍在历史里（但不会被当成这次的结果）",
          len(prev_ok) >= 1 and prev_ok[0]["has_image"] is True, len(prev_ok))

    # =====================================================================
    print("\n[M] 汇总口径：四种来源必须分开")
    # =====================================================================
    s, j = jreq("GET", "/api/v1/cam/snapshot/summary", token=False)
    check("M1 汇总 200", s == 200, s)
    for k in ("live_ok", "live_occluded", "simulated", "replayed", "last_live_ok", "by_state"):
        check("M2 含字段 %s" % k, k in j, list(j.keys())[:8])
    check("M3 live_ok ≥ 1（devA/devB 各一次）", j["live_ok"] >= 1, j["live_ok"])
    check("M4 live_occluded ≥ 1（devF）", j["live_occluded"] >= 1, j["live_occluded"])
    check("M5 ★ 遮挡单独计数，没有被混进 live_ok",
          j["by_state"].get("occluded", 0) == j["live_occluded"],
          (j["by_state"].get("occluded"), j["live_occluded"]))
    check("M6 last_live_ok 的状态一定是 ok",
          (j["last_live_ok"] or {}).get("state") == "ok", (j["last_live_ok"] or {}).get("state"))
    check("M7 by_state 里能看到 timeout/failed",
          j["by_state"].get("timeout", 0) >= 1 and j["by_state"].get("failed", 0) >= 1, j["by_state"])

    # =====================================================================
    print("\n[N] 回放：原始采集时刻不变，counts_as_live=0，不影响 last_live_ok")
    # =====================================================================
    s, _, d = req("POST", "/api/v1/cam/photo?device_id=devA&seq=901&w=320&h=240",
                  SCENE, ctype="image/jpeg")
    photo = json.loads(d)["photo"]
    photo_t = photo["created_at_utc"]

    s, before = jreq("GET", "/api/v1/cam/snapshot/summary", token=False)
    last_before = (before.get("last_live_ok") or {}).get("snapshot_id")

    s, j = jreq("POST", "/api/v1/cam/snapshot/replay",
                {"device_id": "devA", "reason": "课堂回放"}, token=False)
    check("N1 回放 200", s == 200, s)
    rp = j["snapshot"]
    check("N2 状态=replayed", rp["state"] == "replayed", rp["state"])
    check("N3 is_replay=True", rp["is_replay"] is True)
    check("N4 ★ counts_as_live=False", rp["counts_as_live"] is False)
    check("N5 ★ 快门时刻就是原照片的采集时刻（不是「现在」）",
          rp["captured_at_utc"] == photo_t, (rp["captured_at_utc"], photo_t))
    check("N6 记下了被回放的那张照片", rp["replay_of_photo_id"] == photo["photo_id"])
    check("N7 explain 明说「不是本次采集」", "不是本次采集" in (rp["explain"] or ""), rp["explain"])
    check("N8 回放也有质量度量（同一套算法）", bool(rp["quality_verdict"]), rp["quality_verdict"])

    s, after = jreq("GET", "/api/v1/cam/snapshot/summary", token=False)
    check("N9 ★ 回放不改变「最近一次成功采集」",
          (after.get("last_live_ok") or {}).get("snapshot_id") == last_before,
          ((after.get("last_live_ok") or {}).get("snapshot_id"), last_before))
    check("N10 replayed 计数 +1", after["replayed"] == before["replayed"] + 1,
          (before["replayed"], after["replayed"]))
    check("N11 live_ok 没有因为回放而增加", after["live_ok"] == before["live_ok"],
          (before["live_ok"], after["live_ok"]))

    # =====================================================================
    print("\n[L] 保留策略：只删文件、不删记录、尊重最少保留")
    # =====================================================================
    # 先给 devA 造够 5 条带图像的记录（前面的 rev 已有多条，这里补齐到 ≥5）
    made = 0
    for i in range(5):
        frame("devP", SCENE)
        s, j = start("devP")
        sidp = j["snapshot"]["snapshot_id"]
        frame("devP", SCENE)
        _seq["devP"] += 1
        s, j = upload(sidp, "devP", SCENE, _seq["devP"])
        if s == 200:
            made += 1
    check("L1 造出 5 条成功快照", made == 5, made)

    s, j = jreq("POST", "/api/v1/cam/snapshot/prune",
                {"device_id": "devP", "dry_run": True, "keep_min": 2, "days": 0}, token=False)
    check("L2 dry_run 200", s == 200, s)
    check("L3 dry_run 报出将清理 3 条", j["pruned"] == 3, j["pruned"])
    check("L4 dry_run 不动磁盘、也不报已释放", j["bytes_freed"] == 0, j["bytes_freed"])
    check("L4b dry_run 报出「如果执行会释放多少」", j["bytes_would_free"] > 0, j["bytes_would_free"])

    s, j = jreq("POST", "/api/v1/cam/snapshot/prune",
                {"device_id": "devP", "dry_run": False, "keep_min": 2, "days": 0}, token=False)
    check("L5 真清理 200", s == 200, s)
    check("L6 清理了 3 条", j["pruned"] == 3, j["pruned"])
    check("L7 释放了字节数", j["bytes_freed"] > 0, j["bytes_freed"])

    s, j = jreq("GET", "/api/v1/cam/snapshots?device_id=devP&limit=20", token=False)
    rows = j["snapshots"]
    check("L8 ★ 记录一条都没少（5 条还在）", len(rows) == 5, len(rows))
    pruned = [r for r in rows if r["image_pruned"]]
    check("L9 3 条标了 image_pruned", len(pruned) == 3, len(pruned))
    check("L10 被清理的记录 has_image=False 但状态仍是 ok",
          all(not r["has_image"] and r["state"] == "ok" for r in pruned))
    check("L11 ★ 最近 2 条（保留下限）没有被清理",
          all(not r["image_pruned"] for r in rows[:2]), [r["image_pruned"] for r in rows])
    left = []
    for root, _, files in os.walk(os.path.join(tmp, "snaps", "devP")):
        left += files
    check("L12 磁盘上只剩 2 个图", len(left) == 2, left)

    # =====================================================================
    print("\n[O] 契约与静态页")
    # =====================================================================
    s, _, d = req("GET", "/", token=False)
    check("O1 首页 200", s == 200 and b"<html" in d, s)
    check("O2 页面里有快照区块", b"snapBox" in d and b"snapAsk" in d)
    check("O3 app.js 里能搜到快照接口",
          all(k in req("GET", "/app.js", token=False)[2]
              for k in (b"/api/v1/cam/snapshot", b"snapClearImage")))
    s, j = jreq("GET", "/api/v1/cam/snapshots?limit=3", token=False)
    check("O4 列表接口 200 且带 count", s == 200 and "count" in j, s)
    check("O5 列表按时间倒序", [x["requested_at_utc"] for x in j["snapshots"]]
          == sorted([x["requested_at_utc"] for x in j["snapshots"]], reverse=True))
    s, j = jreq("GET", "/api/v1/cam/snapshot/summary", token=False)
    check("O6 settings 里带上了保留参数", "keep_min" in j["settings"], j["settings"])
    check("O7 in_flight 是列表", isinstance(j["in_flight"], list))

    # =====================================================================
    print("\n[P] 令牌与 ID 校验")
    # =====================================================================
    _seq["devQ"] = 0
    s, _, d = req("POST", "/api/v1/cam/snapshot/upload?sid=snp_20260101T000000-abcdef&device_id=devQ&seq=1&w=320&h=240",
                  SCENE, token=False, ctype="image/jpeg")
    check("P1 无令牌上传 → 401", s == 401, s)
    s, j = upload("snp_20260101T000000-abcdef", "devQ", SCENE, 5)
    check("P2 不存在的 sid → 404", s == 404, s)
    check("P3 错误码 SNAPSHOT_NOT_FOUND", (j.get("error") or {}).get("code") == "SNAPSHOT_NOT_FOUND")
    s, j = jreq("GET", "/api/v1/cam/snapshot/snp_bogus_id", token=False)
    check("P4 非法 id 也当不存在，不抛异常", s == 404, s)
    s, j = jreq("POST", "/api/v1/cam/snapshot", {"device_id": "devA", "mode": "huge"}, token=False)
    check("P5 非法 mode → 400", s == 400, s)
    check("P6 错误码 SNAPSHOT_BAD_MODE", (j.get("error") or {}).get("code") == "SNAPSHOT_BAD_MODE")
    s, j = jreq("POST", "/api/v1/cam/snapshot", {"device_id": "devA", "trigger": "hack"},
                token=False)
    check("P7 非法 trigger → 400", s == 400, s)
    s, _, d = req("POST", "/api/v1/cam/snapshot/upload?sid=snp_20260101T000000-abcdef&device_id=devQ&seq=1",
                  b"not-jpeg", ctype="image/jpeg")
    check("P8 坏 JPEG → 400", s == 400, s)
    s, _, d = req("POST", "/api/v1/cam/snapshot/upload?device_id=devQ&seq=1",
                  SCENE, ctype="image/jpeg")
    check("P9 缺 sid → 400", s == 400, s)

finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    log = ""
    try:
        _logfp.flush()
        _logfp.close()
        with open(LOGFILE, "rb") as f:
            log = f.read().decode("utf-8", "ignore")
    except Exception:
        pass
    shutil.rmtree(tmp, True)

tb = [ln for ln in log.splitlines() if "Traceback" in ln or "Error" in ln]
if tb:
    print("\n--- 服务端异常 ---")
    print("\n".join(tb[-12:]))

print("\n=== %s（%d 项断言）===" % ("全部通过" if not fails else "失败 %d 项: %s"
                                 % (len(fails), ", ".join(fails)), checks[0]))
sys.exit(1 if fails else 0)
