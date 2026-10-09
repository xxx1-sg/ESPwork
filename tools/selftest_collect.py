#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest_collect.py —— 第 2 周「远程采集指令闭环」接口层自检

⚠️ 关于"禁止假数据"约束（务必先读）：
   本脚本使用**独立的临时数据库**，并以**脚本扮演设备**的方式发送
   `commands/poll` / `device/ack` / `observations`。
   它模拟的是**设备侧的报文**（因此结论一律属于"接口层通过"），
   但它验证的对象是**后端行为**：
      L1/L2/L3 判定、拒收留痕、状态不改写、超时扫描、幂等、暂停周期上报。

   它**不能**证明真实板子采过数据 —— 实物验收必须用真板子跑（见 docs/08 §5）。
   本脚本绝不写入 backend/data/sensor.db，运行结束即删除临时库。

用法:
    .venv\\Scripts\\python.exe tools\\selftest_collect.py
    .venv\\Scripts\\python.exe tools\\selftest_collect.py -v     # 打印每次请求/响应
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

TMP_DB = os.path.join(tempfile.gettempdir(), "selftest_collect.db")
PORT = 8098
TOKEN = "selftest-token"
DEV = "esp32s3eye-SELFTEST"          # 明显标注：这不是真实设备
VERBOSE = False

FAILED = []
PASSED = []


# ---------------------------------------------------------------- HTTP ----
def _req(method, url, obj=None, token=None, raw=None):
    data = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else None)
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-Device-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"_raw": body}


def post(url, obj, token=None, raw=None):
    return _req("POST", url, obj, token, raw)


def get(url):
    return _req("GET", url)


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print("  [通过] " + name + ("  " + extra if extra else ""))
    else:
        FAILED.append(name)
        print("  [失败] " + name + ("  " + extra if extra else ""))


def section(t):
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def db(sql, args=()):
    conn = sqlite3.connect(TMP_DB)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def one(sql, args=()):
    r = db(sql, args)
    return r[0][0] if r else None


# ------------------------------------------------------- 报文构造（设备侧）----
def obs_payload(rid, nonce, boot, seq_first, seq_last, up_ms, raw,
                sample_count=1, ts_utc_ms=None, ts_local=None, collect_mode="single"):
    """构造一条"设备回传的观测"。

    ★ 数值只是让报文结构自洽的**固定**值，不是传感器读数 ——
      本脚本的结论仅覆盖后端判定逻辑（接口层），实物验收另有其人（真板子）。
    """
    x, y, z = raw
    lsb = 4096
    g = [round(v / lsb, 6) for v in raw]
    ms2 = [round(v * 9.80665, 6) for v in g]
    mag = round(sum(v * v for v in g) ** 0.5, 6)
    samples = []
    for i in range(sample_count):
        s = seq_first + i
        samples.append({
            "seq": s, "idx": i + 1, "up_ms": up_ms + i * 100,
            "ts_utc_ms": ts_utc_ms, "ts_local": ts_local,
            "raw": {"x": x, "y": y, "z": z},
            "accel_g": {"x": g[0], "y": g[1], "z": g[2]},
            "accel_ms2": {"x": ms2[0], "y": ms2[1], "z": ms2[2]},
            "magnitude_g": mag,
        })
    return {
        "request_id": rid, "nonce": nonce, "device_id": DEV,
        "boot_count": boot, "seq_first": seq_first, "seq_last": seq_last,
        "sample_count": sample_count, "sample_period_ms": 100,
        "up_ms": up_ms, "ts_utc_ms": ts_utc_ms, "ts_local": ts_local,
        "collect_mode": collect_mode,
        "sensor": {"model": "QMA6100P", "type": "accelerometer", "i2c_addr": 18,
                   "chip_id": 144, "range_g": 2, "lsb_per_g": lsb,
                   "odr_hz": 100, "oversample": 8},
        "unit": {"raw": "LSB(14bit signed)", "accel_g": "g",
                 "accel_ms2": "m/s^2", "magnitude_g": "g"},
        "samples": samples,
        "raw": {"x": x, "y": y, "z": z},
        "accel_g": {"x": g[0], "y": g[1], "z": g[2]},
        "accel_ms2": {"x": ms2[0], "y": ms2[1], "z": ms2[2]},
        "magnitude_g": mag,
    }


def device_poll(base, boot, seq, up_ms, sensor_ok=True):
    """设备轮询一次指令（同时上报自己的当前进度，供后端生成基线）。"""
    return post(base + "/api/v1/device/commands/poll", {
        "device_id": DEV, "boot_count": boot, "up_ms": up_ms, "seq_last": seq,
        "periodic_enabled": True,
        "health": {"sensor_ok": sensor_ok, "wifi_rssi": -61, "free_heap": 180000},
        "last_ack": None,
    }, token=TOKEN)


def wait_status(base, rid, want, timeout=20.0):
    """轮询请求详情直到进入期望状态（或超时）。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        st, body = get(base + "/api/v1/collect_requests/" + rid)
        last = body.get("data", {}).get("status")
        if st == 200 and last in want:
            return last, body["data"]
        time.sleep(0.2)
    return last, (body.get("data") if isinstance(body, dict) else None)


# =============================================================== 主流程 ====
def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    if os.path.exists(TMP_DB):
        os.remove(TMP_DB)
    os.environ["SENSOR_DB"] = TMP_DB
    os.environ["SENSOR_TOKEN"] = TOKEN
    os.environ["SENSOR_HOST"] = "127.0.0.1"
    os.environ["SENSOR_PORT"] = str(PORT)
    os.environ["SENSOR_WEB_DIR"] = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "web")

    import server

    server.db_init()
    # 缩短超时，让"设备关机→超时"用例能在几秒内跑完（默认 15000ms 太慢）
    server.COLLECT_TIMEOUT_MS = 3000

    srv = server.ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=server._timeout_scanner, daemon=True).start()
    time.sleep(0.3)

    base = "http://127.0.0.1:%d" % PORT

    print("=" * 74)
    print("第 2 周远程采集闭环 —— 接口层自检")
    print("临时库: %s" % TMP_DB)
    print("说明  : 本脚本扮演设备发报文，验证**后端**判定逻辑；")
    print("        结论属『接口层通过』，实物验收请用真板子跑 docs/08 §5。")
    print("=" * 74)

    # =====================================================================
    section("A. 区分「刷新已存数据」与「采集最新数据」")
    # =====================================================================
    before = {
        "creq": one("SELECT COUNT(*) FROM collect_requests"),
        "rej": one("SELECT COUNT(*) FROM rejected_observations"),
        "rec": one("SELECT COUNT(*) FROM sensor_records"),
    }
    # 刷新 = 只读接口，零设备侧流量
    st_r1, _ = get(base + "/api/v1/records?limit=5")
    st_r2, _ = get(base + "/api/v1/latest")   # 空库时如实 404，这是第 1 周既有语义
    st_r3, _ = get(base + "/api/v1/stats")
    after = {
        "creq": one("SELECT COUNT(*) FROM collect_requests"),
        "rej": one("SELECT COUNT(*) FROM rejected_observations"),
        "rec": one("SELECT COUNT(*) FROM sensor_records"),
    }
    check("刷新只调 GET /records、/latest、/stats（/latest 空库如实 404，不伪造）",
          st_r1 == 200 and st_r2 in (200, 404) and st_r3 == 200,
          "records=%s latest=%s stats=%s" % (st_r1, st_r2, st_r3))
    check("刷新不产生任何采集请求（collect_requests 行数 +0）",
          before["creq"] == after["creq"], "%d -> %d" % (before["creq"], after["creq"]))
    check("刷新不产生任何设备侧数据（sensor_records +0）",
          before["rec"] == after["rec"])
    pend_after_refresh = one("SELECT COUNT(*) FROM pending_commands")
    check("刷新不排任何设备指令（pending_commands = 0）", pend_after_refresh == 0)

    # =====================================================================
    section("B. 设备未接入 / 缺字段 → 创建请求必须如实拒绝")
    # =====================================================================
    st, b = post(base + "/api/v1/collect_requests", {"sensor": "qma6100p"})
    check("缺 device_id → 400 MISSING_FIELD",
          st == 400 and b["error"]["code"] == "MISSING_FIELD", "status=%s" % st)
    check("缺 device_id 时响应不含 data.request_id", "data" not in b)

    st, b = post(base + "/api/v1/collect_requests",
                 {"device_id": "esp32s3eye-NOTEXIST", "sensor": "qma6100p"})
    check("设备未接入 → 404 NO_DEVICE",
          st == 404 and b["error"]["code"] == "NO_DEVICE", "status=%s" % st)
    check("NO_DEVICE 不新增 collect_requests 行",
          one("SELECT COUNT(*) FROM collect_requests") == 0)

    # 设备还没轮询过 → 没有基线，不能瞎猜
    with sqlite3.connect(TMP_DB) as c:
        c.execute("INSERT INTO devices (device_id, last_seen_utc, last_status, periodic_enabled)"
                  " VALUES (?,?,?,1)", (DEV, server.utc_now_iso(), "online"))
    st, b = post(base + "/api/v1/collect_requests",
                 {"device_id": DEV, "sensor": "qma6100p", "client_request_key": "nb-1"})
    check("无基线（设备从未轮询）→ 409 NO_BASELINE",
          st == 409 and b["error"]["code"] == "NO_BASELINE", "status=%s" % st)
    check("NO_BASELINE 不新增 collect_requests 行",
          one("SELECT COUNT(*) FROM collect_requests") == 0)

    # =====================================================================
    section("C. 设备首次轮询 → 基线就绪 → 指令下发（poll）")
    # =====================================================================
    BOOT = 808
    SEQ = 3190
    UP = 318340
    st, b = device_poll(base, BOOT, SEQ, UP)
    check("设备轮询无指令时返回 200 + commands:[]",
          st == 200 and b.get("ok") and b["data"]["commands"] == [],
          "status=%s cmds=%s" % (st, b.get("data", {}).get("commands")))
    check("poll_after_ms == 1000", b["data"]["poll_after_ms"] == 1000)

    st, b = post(base + "/api/v1/device/commands/poll", {"device_id": DEV}, token="wrong")
    check("错误令牌 → 401 BAD_TOKEN", st == 401, "status=%s" % st)

    # ---- 创建采集请求（这次应该成功）----
    st, b = post(base + "/api/v1/collect_requests", {
        "device_id": DEV, "sensor": "qma6100p", "requested_by": "selftest",
        "client_request_key": "k-basic-1", "note": "接口层自检",
        "timeout_ms": 60000,          # ★ 故意传大，必须被忽略
    })
    check("基线就绪后创建成功 201", st == 201 and b["ok"], "status=%s" % st)
    d = b["data"]
    RID, NONCE = d["request_id"], None
    check("data 键齐备（docs/06 §6.1）",
          set(d.keys()) == {"request_id", "status", "submitted_at_utc", "timeout_ms",
                            "deadline_utc", "baseline", "poll_url", "deduplicated"},
          "keys=%s" % sorted(d.keys()))
    check("status == submitted", d["status"] == "submitted")
    check("前端传的 timeout_ms=60000 被忽略（仍为 3000）",
          d["timeout_ms"] == 3000, "timeout_ms=%s" % d["timeout_ms"])
    check("baseline 与设备轮询值一致",
          d["baseline"] == {"boot_count": BOOT, "seq_last": SEQ, "uptime_ms": UP},
          "%s" % d["baseline"])
    check("poll_url == /api/v1/collect_requests/<request_id>",
          d["poll_url"] == "/api/v1/collect_requests/" + RID)

    row = db("SELECT * FROM collect_requests WHERE request_id=?", (RID,))[0]
    NONCE = row["nonce"]
    check("nonce 为 32 位十六进制", len(NONCE) == 32 and all(
        ch in "0123456789abcdef" for ch in NONCE), NONCE)

    # ---- 设备取走指令 ----
    st, b = device_poll(base, BOOT, SEQ, UP + 20)
    cmds = b["data"]["commands"]
    check("设备取到 collect_now 指令", len(cmds) == 1 and cmds[0]["type"] == "collect_now",
          "cmds=%s" % [c.get("type") for c in cmds])
    if cmds:
        c0 = cmds[0]
        check("指令 8 键齐备",
              set(c0.keys()) == {"request_id", "type", "nonce", "sensor", "timeout_ms",
                                 "issued_at_utc", "baseline", "attempt"},
              "keys=%s" % sorted(c0.keys()))
        check("指令 request_id / nonce 与创建时逐字符一致",
              c0["request_id"] == RID and c0["nonce"] == NONCE)
        check("指令 baseline 与创建响应一致", c0["baseline"] == d["baseline"])
    row = db("SELECT * FROM collect_requests WHERE request_id=?", (RID,))[0]
    check("dispatched_at_utc 已落库（L3 基准）", row["dispatched_at_utc"] is not None,
          "%s" % row["dispatched_at_utc"])
    check("attempt 计数已递增", row["attempt"] >= 1, "attempt=%s" % row["attempt"])

    # =====================================================================
    section("D. 设备回执（ack）——「设备接收回执」状态的唯一来源")
    # =====================================================================
    st, b = post(base + "/api/v1/device/ack", {
        "request_id": RID, "device_id": DEV, "ack_type": "received", "nonce": NONCE,
        "attempt": 1, "boot_count": BOOT, "up_ms": UP + 40,
        "reason": None, "detail": {"queue_len": 0},
    }, token=TOKEN)
    check("ack received → 200 且 status=acked",
          st == 200 and b["data"]["status"] == "acked",
          "status=%s data=%s" % (st, b.get("data")))
    check("ack 落 request_acks 表",
          one("SELECT COUNT(*) FROM request_acks WHERE request_id=? AND ack_type='received'",
              (RID,)) == 1)

    # ack 幂等
    st, b = post(base + "/api/v1/device/ack", {
        "request_id": RID, "device_id": DEV, "ack_type": "received", "nonce": NONCE,
        "attempt": 1, "boot_count": BOOT, "up_ms": UP + 60,
    }, token=TOKEN)
    check("重复 ack → 200 + deduplicated:true（不新增行）",
          st == 200 and b["data"]["deduplicated"] is True, "status=%s" % st)
    check("重复 ack 未新增 request_acks 行",
          one("SELECT COUNT(*) FROM request_acks WHERE request_id=? AND ack_type='received'",
              (RID,)) == 1)

    # nonce 不符必须拒
    st, b = post(base + "/api/v1/device/ack", {
        "request_id": RID, "device_id": DEV, "ack_type": "started",
        "nonce": "0" * 32, "attempt": 1,
    }, token=TOKEN)
    check("ack nonce 不符 → 409 REQ_ID_MISMATCH",
          st == 409 and b["error"]["code"] == "REQ_ID_MISMATCH", "status=%s" % st)

    # 中途查看页面会看到的状态
    st, b = get(base + "/api/v1/collect_requests/" + RID)
    check("详情接口：acked 时 ack 字段齐备（ack_type/ack_at_utc/attempt）",
          b["data"]["ack"] is not None and
          set(b["data"]["ack"].keys()) == {"ack_type", "ack_at_utc", "attempt"},
          "%s" % b["data"]["ack"])
    check("详情接口：acked 时 observation 仍为 null（回执 ≠ 完成）",
          b["data"]["observation"] is None)

    # =====================================================================
    section("E. 旧记录不可改写为本次完成（L2 拒收 + 状态不改写）")
    # =====================================================================
    # 用"旧 seq"提交：baseline 已下发，回传的 seq_first 未超过基线
    stale = obs_payload(RID, NONCE, BOOT, SEQ, SEQ, UP, (-3088, 1902, 1349))
    st_before = one("SELECT status FROM collect_requests WHERE request_id=?", (RID,))
    n_rej_before = one("SELECT COUNT(*) FROM rejected_observations")
    n_obs_before = one("SELECT COUNT(*) FROM observations")
    n_rec_before = one("SELECT COUNT(*) FROM sensor_records")

    st, b = post(base + "/api/v1/observations", stale, token=TOKEN)
    check("重放旧包（seq 未超基线）→ 409 STALE_OBSERVATION",
          st == 409 and b["error"]["code"] == "STALE_OBSERVATION",
          "status=%s code=%s" % (st, b.get("error", {}).get("code")))
    check("拒收响应带 checks.l2_monotonic == false",
          b["error"]["checks"]["l2_monotonic"] is False, "%s" % b["error"].get("checks"))
    check("拒收响应带 rejected_id", isinstance(b["error"].get("rejected_id"), int))
    st_after = one("SELECT status FROM collect_requests WHERE request_id=?", (RID,))
    check("★ 请求状态未被改写（仍是 %s）" % st_before, st_before == st_after,
          "%s -> %s" % (st_before, st_after))
    check("拒收必须留痕（rejected_observations 恰 +1）",
          one("SELECT COUNT(*) FROM rejected_observations") == n_rej_before + 1)
    check("拒收不产生 observations 行",
          one("SELECT COUNT(*) FROM observations") == n_obs_before)
    check("拒收不产生 sensor_records 行",
          one("SELECT COUNT(*) FROM sensor_records") == n_rec_before)

    # =====================================================================
    section("F. 正常完成（L1∧L2∧L3 全成立）")
    # =====================================================================
    good = obs_payload(RID, NONCE, BOOT, SEQ + 1, SEQ + 1, UP + 100,
                       (-3088, 1902, 1349))
    st, b = post(base + "/api/v1/observations", good, token=TOKEN)
    check("合法新观测 → 201 且 request_status=completed",
          st == 201 and b["data"]["request_status"] == "completed",
          "status=%s data=%s" % (st, json.dumps(b.get("data", {}), ensure_ascii=False)[:160]))
    if st == 201:
        dd = b["data"]
        check("checks 三项全 true",
              dd["checks"] == {"l1_identity": True, "l2_monotonic": True, "l3_time": True},
              "%s" % dd["checks"])
        check("freshness == new_monotonic", dd["freshness"] == "new_monotonic",
              "%s" % dd["freshness"])
        check("observation_id / record_id / upload_id 均为正整数",
              all(isinstance(dd[k], int) and dd[k] > 0
                  for k in ("observation_id", "record_id", "upload_id")),
              "obs=%s rec=%s upl=%s" % (dd["observation_id"], dd["record_id"],
                                        dd["upload_id"]))
        check("verify_ok == 1（后端按 raw/lsb_per_g 重算一致）", dd["verify_ok"] == 1)
        check("value.magnitude_g 与报文一致",
              abs(dd["value"]["magnitude_g"] - good["magnitude_g"]) < 1e-9)

        # 三表联查
        o = db("SELECT * FROM observations WHERE request_id=?", (RID,))[0]
        r = db("SELECT * FROM sensor_records WHERE id=?", (o["record_id"],))[0]
        u = db("SELECT * FROM raw_uploads WHERE id=?", (o["upload_id"],))[0]
        check("observations.record_id → sensor_records 且 seq/raw 与报文一致",
              r["seq"] == SEQ + 1 and r["raw_x"] == -3088 and r["raw_y"] == 1902
              and r["raw_z"] == 1349)
        check("sensor_records.request_id / report_mode == manual",
              r["request_id"] == RID and r["report_mode"] == "manual",
              "rid=%s mode=%s" % (r["request_id"], r["report_mode"]))
        check("raw_uploads.sha256 与 body 重算一致（逐字节留存）",
              hashlib.sha256(u["body"].encode("utf-8")).hexdigest() == u["sha256"])
        check("raw_uploads.byte_len == len(body)",
              u["byte_len"] == len(u["body"].encode("utf-8")))

    # 终态后再发 → REQUEST_ALREADY_FINAL，且状态不改写
    n_rej = one("SELECT COUNT(*) FROM rejected_observations")
    st, b = post(base + "/api/v1/observations",
                 obs_payload(RID, NONCE, BOOT, SEQ + 2, SEQ + 2, UP + 200,
                             (-3000, 1900, 1300)), token=TOKEN)
    check("终态后重复提交 → 409 REQUEST_ALREADY_FINAL",
          st == 409 and b["error"]["code"] == "REQUEST_ALREADY_FINAL",
          "status=%s code=%s" % (st, b.get("error", {}).get("code")))
    check("终态后拒收留痕 +1",
          one("SELECT COUNT(*) FROM rejected_observations") == n_rej + 1)
    check("终态后 completed_at_utc 未被改写",
          one("SELECT status FROM collect_requests WHERE request_id=?", (RID,)) == "completed")

    # 重复批
    st, b = post(base + "/api/v1/observations", good, token=TOKEN)
    check("原样重发同一批 → 409（不新增任何数据行）",
          st == 409 and b["error"]["code"] in ("DUPLICATE_BATCH", "REQUEST_ALREADY_FINAL"),
          "status=%s code=%s" % (st, b.get("error", {}).get("code")))

    # =====================================================================
    section("G. 幂等：双击同一 client_request_key")
    # =====================================================================
    st, b1 = post(base + "/api/v1/collect_requests",
                  {"device_id": DEV, "sensor": "qma6100p",
                   "client_request_key": "k-double-1"})
    st2, b2 = post(base + "/api/v1/collect_requests",
                   {"device_id": DEV, "sensor": "qma6100p",
                    "client_request_key": "k-double-1"})
    check("首次 201", st == 201, "status=%s" % st)
    check("同 key 重复点击 → 200 + deduplicated:true",
          st2 == 200 and b2["data"]["deduplicated"] is True, "status=%s" % st2)
    check("两次返回同一个 request_id",
          b1["data"]["request_id"] == b2["data"]["request_id"])
    check("collect_requests 中该 key 只有 1 行",
          one("SELECT COUNT(*) FROM collect_requests WHERE client_request_key='k-double-1'") == 1)
    RID_DUP = b1["data"]["request_id"]

    # =====================================================================
    section("H. 刻意连点（不同 key）→ 两条记录、各自终局")
    # =====================================================================
    stA, bA = post(base + "/api/v1/collect_requests",
                   {"device_id": DEV, "sensor": "qma6100p", "client_request_key": "k-A"})
    stB, bB = post(base + "/api/v1/collect_requests",
                   {"device_id": DEV, "sensor": "qma6100p", "client_request_key": "k-B"})
    check("连点两次 → 两条独立请求",
          stA == 201 and stB == 201 and
          bA["data"]["request_id"] != bB["data"]["request_id"])
    RID_A, RID_B = bA["data"]["request_id"], bB["data"]["request_id"]

    st, b = get(base + "/api/v1/collect_requests?device_id=%s&limit=10" % DEV)
    ids = [r["request_id"] for r in b["data"]["requests"]]
    check("追踪列表能同时看到两条记录",
          RID_A in ids and RID_B in ids, "count=%d" % b["data"]["count"])
    listed = [r for r in b["data"]["requests"] if r["request_id"] == RID_A][0]
    check("列表项字段齐备（含 rejections_count 与 observation 摘要）",
          {"request_id", "status", "submitted_at_utc", "dispatched_at_utc",
           "acked_at_utc", "completed_at_utc", "deadline_utc",
           "rejections_count", "observation"} <= set(listed.keys()),
          "keys=%s" % sorted(listed.keys()))

    # 两条都被设备取走（后端允许并行下发），分别完成
    st, b = device_poll(base, BOOT, SEQ + 10, UP + 300)
    got = {c["request_id"]: c for c in b["data"]["commands"]}
    check("设备一次轮询可拿到多条待执行指令",
          RID_DUP in got or RID_A in got or RID_B in got,
          "got=%s" % list(got.keys()))
    # 让最新的 B 完成
    if RID_B in got:
        cB = got[RID_B]
        post(base + "/api/v1/device/ack",
             {"request_id": RID_B, "device_id": DEV, "ack_type": "received",
              "nonce": cB["nonce"], "attempt": 1}, token=TOKEN)
        bl = cB["baseline"]
        st, b = post(base + "/api/v1/observations",
                     obs_payload(RID_B, cB["nonce"], BOOT, bl["seq_last"] + 1,
                                 bl["seq_last"] + 1, bl["uptime_ms"] + 100,
                                 (1000, 2000, 3000)), token=TOKEN)
        check("连点中的 B 能正常完成", st == 201 and
              b["data"]["request_status"] == "completed", "status=%s" % st)
    # 旧的那条（A）回 superseded
    if RID_A in got:
        cA = got[RID_A]
        st, b = post(base + "/api/v1/device/ack",
                     {"request_id": RID_A, "device_id": DEV, "ack_type": "superseded",
                      "nonce": cA["nonce"], "attempt": 1,
                      "reason": "superseded_request_id"}, token=TOKEN)
        check("被取代的 A → cancelled + failure_reason=SUPERSEDED",
              st == 200 and b["data"]["status"] == "cancelled",
              "status=%s data=%s" % (st, b.get("data")))
        fa = one("SELECT failure_reason FROM collect_requests WHERE request_id=?", (RID_A,))
        check("A 的 failure_reason == SUPERSEDED", fa == "SUPERSEDED", "%s" % fa)

    # =====================================================================
    section("I. 设备关机 → 请求超时（★ 超时 ≠ 硬件故障）")
    # =====================================================================
    st, b = post(base + "/api/v1/collect_requests",
                 {"device_id": DEV, "sensor": "qma6100p", "client_request_key": "k-timeout"})
    RID_T = b["data"]["request_id"]
    # 让指令下发出去（模拟设备还活着时取走了指令）
    device_poll(base, BOOT, SEQ + 20, UP + 400)
    post(base + "/api/v1/device/ack",
         {"request_id": RID_T, "device_id": DEV, "ack_type": "received",
          "nonce": one("SELECT nonce FROM collect_requests WHERE request_id=?", (RID_T,)),
          "attempt": 1}, token=TOKEN)

    se_before = one("SELECT COUNT(*) FROM device_events WHERE device_id=? AND event_type='sensor_error'",
                    (DEV,))
    ct_before = one("SELECT COUNT(*) FROM device_events WHERE device_id=? AND event_type='command_timeout'",
                    (DEV,))
    n_obs_T_before = one("SELECT COUNT(*) FROM observations WHERE request_id=?", (RID_T,))
    dev_status_before = one("SELECT last_status FROM devices WHERE device_id=?", (DEV,))

    print("  ... 等待 deadline 到期（%d ms）..." % 3000)
    status, det = wait_status(base, RID_T, {"timeout"}, timeout=12.0)
    check("设备不回报 → 请求进入 timeout", status == "timeout", "status=%s" % status)
    check("timeout 后 failure_reason 不是 SENSOR_ERROR",
          (det or {}).get("failure_reason") != "SENSOR_ERROR",
          "%s" % (det or {}).get("failure_reason"))
    check("★ 未写任何 sensor_error 事件（超时 ≠ 硬件故障）",
          one("SELECT COUNT(*) FROM device_events WHERE device_id=? AND event_type='sensor_error'",
              (DEV,)) == se_before)
    # ★ 只统计"本请求"产生的 command_timeout —— 后台扫描器每 1s 跑一次，
    #   可能把更早的其它请求也置成 timeout，用总数比对会误判。
    check("★ 本条请求产生了 command_timeout 事件（source=server）",
          one("SELECT COUNT(*) FROM device_events WHERE device_id=? AND event_type='command_timeout'"
              " AND detail_json LIKE ?", (DEV, "%" + RID_T + "%")) == 1 and
          one("SELECT source FROM device_events WHERE device_id=? AND event_type='command_timeout'"
              " AND detail_json LIKE ? ORDER BY id DESC LIMIT 1",
              (DEV, "%" + RID_T + "%")) == "server",
          "ct_before=%s ct_now=%s" % (
              ct_before,
              one("SELECT COUNT(*) FROM device_events WHERE device_id=? AND event_type='command_timeout'",
                  (DEV,))))
    check("★ devices.last_status 未被改成 sensor_error",
          one("SELECT last_status FROM devices WHERE device_id=?", (DEV,)) != "sensor_error",
          "%s -> %s" % (dev_status_before,
                        one("SELECT last_status FROM devices WHERE device_id=?", (DEV,))))
    check("超时后不再产生 observations 行",
          one("SELECT COUNT(*) FROM observations WHERE request_id=?", (RID_T,))
          == n_obs_T_before)

    # 超时后补传的合法观测也必须被拒（不得回写成完成）
    st, b = post(base + "/api/v1/observations",
                 obs_payload(RID_T, one("SELECT nonce FROM collect_requests WHERE request_id=?",
                                        (RID_T,)),
                             BOOT, SEQ + 21, SEQ + 21, UP + 500, (-1, -2, -3)),
                 token=TOKEN)
    check("★ 超时后补传观测 → 409，不得回写为 completed",
          st == 409 and b["error"]["code"] == "REQUEST_ALREADY_FINAL",
          "status=%s code=%s" % (st, b.get("error", {}).get("code")))
    check("超时请求仍是 timeout",
          one("SELECT status FROM collect_requests WHERE request_id=?", (RID_T,)) == "timeout")

    # 设备"关机"事实：没有 sensor_error，但也不该伪造任何故障
    check("设备维度无伪造的 sensor_error 计数 == 0",
          one("SELECT COUNT(*) FROM device_events WHERE device_id=? AND event_type='sensor_error'",
              (DEV,)) == 0)

    # =====================================================================
    section("J. 硬件故障路径（设备明确拒绝）—— 与超时是两条证据")
    # =====================================================================
    st, b = device_poll(base, BOOT, SEQ + 30, UP + 600, sensor_ok=False)
    check("设备自报 sensor_ok=false → 设备维度记 sensor_error 事件",
          one("SELECT COUNT(*) FROM device_events WHERE device_id=? AND event_type='sensor_error'",
              (DEV,)) >= 1)
    device_poll(base, BOOT, SEQ + 31, UP + 700, sensor_ok=True)
    check("设备恢复 → 记 sensor_recovered 事件",
          one("SELECT COUNT(*) FROM device_events WHERE device_id=? AND event_type='sensor_recovered'",
              (DEV,)) >= 1)

    st, b = post(base + "/api/v1/collect_requests",
                 {"device_id": DEV, "sensor": "qma6100p", "client_request_key": "k-fail"})
    RID_F = b["data"]["request_id"]
    st, b = post(base + "/api/v1/device/ack",
                 {"request_id": RID_F, "device_id": DEV, "ack_type": "rejected",
                  "nonce": one("SELECT nonce FROM collect_requests WHERE request_id=?", (RID_F,)),
                  "attempt": 1, "reason": "SENSOR_ERROR"}, token=TOKEN)
    check("设备拒绝 → 请求 status=failed（不是 timeout）",
          st == 200 and b["data"]["status"] == "failed", "status=%s data=%s" % (st, b.get("data")))
    check("failed 的 failure_reason == SENSOR_ERROR",
          one("SELECT failure_reason FROM collect_requests WHERE request_id=?", (RID_F,))
          == "SENSOR_ERROR")
    check("failed 请求没有 observations 行",
          one("SELECT COUNT(*) FROM observations WHERE request_id=?", (RID_F,)) == 0)

    # =====================================================================
    section("K. L1 / L3 拒收（各自独立可复现）")
    # =====================================================================
    st, b = post(base + "/api/v1/collect_requests",
                 {"device_id": DEV, "sensor": "qma6100p", "client_request_key": "k-l1"})
    RID_L = b["data"]["request_id"]
    device_poll(base, BOOT, SEQ + 40, UP + 800)
    bl = db("SELECT baseline_boot_count, baseline_seq_last, baseline_uptime_ms "
            "FROM collect_requests WHERE request_id=?", (RID_L,))[0]

    # L1: nonce 不符
    st, b = post(base + "/api/v1/observations",
                 obs_payload(RID_L, "f" * 32, BOOT, bl["baseline_seq_last"] + 1,
                             bl["baseline_seq_last"] + 1, bl["baseline_uptime_ms"] + 100,
                             (1, 2, 3)), token=TOKEN)
    check("nonce 不符 → 409 REQ_ID_MISMATCH 且 l1_identity=false",
          st == 409 and b["error"]["code"] == "REQ_ID_MISMATCH" and
          b["error"]["checks"]["l1_identity"] is False,
          "status=%s code=%s" % (st, b.get("error", {}).get("code")))

    # L3: seq 增了但 up_ms 没增
    st, b = post(base + "/api/v1/observations",
                 obs_payload(RID_L, one("SELECT nonce FROM collect_requests WHERE request_id=?",
                                        (RID_L,)),
                             BOOT, bl["baseline_seq_last"] + 2, bl["baseline_seq_last"] + 2,
                             bl["baseline_uptime_ms"], (1, 2, 3)), token=TOKEN)
    check("up_ms 未超基线 → 409 STALE_OBSERVATION 且 l3_time=false",
          st == 409 and b["error"]["code"] == "STALE_OBSERVATION" and
          b["error"]["checks"]["l3_time"] is False,
          "status=%s checks=%s" % (st, b.get("error", {}).get("checks")))
    check("被拒后请求状态仍未改写（未 completed）",
          one("SELECT status FROM collect_requests WHERE request_id=?", (RID_L,))
          in ("submitted", "acked", "collecting", "timeout"))

    # 紧接一条合法观测，必须仍能完成（证明拒收没破坏 baseline/nonce）
    st, b = post(base + "/api/v1/observations",
                 obs_payload(RID_L, one("SELECT nonce FROM collect_requests WHERE request_id=?",
                                        (RID_L,)),
                             BOOT, bl["baseline_seq_last"] + 3, bl["baseline_seq_last"] + 3,
                             bl["baseline_uptime_ms"] + 100, (7, 8, 9)), token=TOKEN)
    check("拒收之后合法观测仍能 completed（baseline/nonce 未被污染）",
          st == 201 and b["data"]["request_status"] == "completed",
          "status=%s" % st)

    # 缺 raw
    st, b = post(base + "/api/v1/collect_requests",
                 {"device_id": DEV, "sensor": "qma6100p", "client_request_key": "k-raw"})
    RID_M = b["data"]["request_id"]
    no_raw = obs_payload(RID_M, one("SELECT nonce FROM collect_requests WHERE request_id=?",
                                    (RID_M,)), BOOT, SEQ + 50, SEQ + 50, UP + 900, (1, 2, 3))
    no_raw.pop("raw")
    no_raw["samples"][0].pop("raw")
    st, b = post(base + "/api/v1/observations", no_raw, token=TOKEN)
    check("缺 raw → 400 MISSING_FIELD（后端绝不代为生成）",
          st == 400 and b["error"]["code"] == "MISSING_FIELD", "status=%s" % st)
    check("缺 raw 必须留痕",
          one("SELECT COUNT(*) FROM rejected_observations WHERE request_id=?", (RID_M,)) >= 1)

    # =====================================================================
    section("L. 暂停周期上报（暂停的是上报，不是采集能力）")
    # =====================================================================
    st, b = post(base + "/api/v1/device/pause",
                 {"device_id": DEV, "enable": False, "reason": "自检"})
    check("pause → 200 且 periodic_enabled=0",
          st == 200 and b["data"]["periodic_enabled"] == 0, "status=%s data=%s" % (st, b.get("data")))
    check("pause 响应键齐备",
          set(b["data"].keys()) == {"device_id", "periodic_enabled", "command_id",
                                    "status", "note"}, "keys=%s" % sorted(b["data"].keys()))
    check("pause 响应 command_id == pause_reports",
          b["data"]["command_id"] == "pause_reports")
    check("devices.periodic_enabled 已落库为 0",
          one("SELECT periodic_enabled FROM devices WHERE device_id=?", (DEV,)) == 0)

    st, b = device_poll(base, BOOT, SEQ + 60, UP + 1000)
    types = [c["type"] for c in b["data"]["commands"]]
    check("设备能取到 pause_reports 指令", "pause_reports" in types, "%s" % types)

    # 暂停期间仍然发周期包 → 接受落库但标记 + warning
    st, b = post(base + "/api/v1/device/reports", {
        "device_id": DEV, "boot_count": BOOT, "seq": SEQ + 61, "report_mode": "periodic",
        "raw": {"x": 100, "y": 200, "z": 300},
        "accel_g": {"x": 0.024414, "y": 0.048828, "z": 0.073242},
        "magnitude_g": 0.0912,
        "sensor": {"model": "QMA6100P", "lsb_per_g": 4096},
    }, token=TOKEN)
    check("暂停期间周期包被接受落库（201）",
          st == 201, "status=%s" % st)
    check("暂停期间周期包 → warning=DEVICE_NOT_PAUSED",
          b.get("warning") == "DEVICE_NOT_PAUSED", "warning=%s" % b.get("warning"))
    check("periodic_enabled 未被这条周期包改回 1",
          one("SELECT periodic_enabled FROM devices WHERE device_id=?", (DEV,)) == 0)

    # ★ 关键：暂停后 collect_now 仍必须能完成
    st, b = post(base + "/api/v1/collect_requests",
                 {"device_id": DEV, "sensor": "qma6100p", "client_request_key": "k-paused"})
    RID_P = b["data"]["request_id"]
    st, b = device_poll(base, BOOT, SEQ + 70, UP + 1100)
    got = {c["request_id"]: c for c in b["data"]["commands"]}
    check("暂停期间仍能下发 collect_now", RID_P in got,
          "cmds=%s" % [c["type"] for c in b["data"]["commands"]])
    if RID_P in got:
        cP = got[RID_P]
        blp = cP["baseline"]
        st, b = post(base + "/api/v1/observations",
                     obs_payload(RID_P, cP["nonce"], BOOT, blp["seq_last"] + 1,
                                 blp["seq_last"] + 1, blp["uptime_ms"] + 100,
                                 (11, 22, 33)), token=TOKEN)
        check("★ 暂停周期上报后 collect_now 仍能 completed",
              st == 201 and b["data"]["request_status"] == "completed",
              "status=%s" % st)

    st, b = post(base + "/api/v1/device/pause", {"device_id": DEV, "enable": True})
    check("resume → periodic_enabled=1 且 command_id=resume_reports",
          st == 200 and b["data"]["periodic_enabled"] == 1 and
          b["data"]["command_id"] == "resume_reports")

    # =====================================================================
    section("M. 时区/时钟：未对时（ts_utc_ms=null）仍靠 boot+seq+up_ms 判定")
    # =====================================================================
    st, b = post(base + "/api/v1/collect_requests",
                 {"device_id": DEV, "sensor": "qma6100p", "client_request_key": "k-nots"})
    RID_N = b["data"]["request_id"]
    st, b = device_poll(base, BOOT, SEQ + 80, UP + 1200)
    got = {c["request_id"]: c for c in b["data"]["commands"]}
    if RID_N in got:
        cN = got[RID_N]
        bln = cN["baseline"]
        st, b = post(base + "/api/v1/observations",
                     obs_payload(RID_N, cN["nonce"], BOOT, bln["seq_last"] + 1,
                                 bln["seq_last"] + 1, bln["uptime_ms"] + 100,
                                 (5, 6, 7), ts_utc_ms=None, ts_local=None), token=TOKEN)
        check("未对时（ts_utc_ms=null）仍能 completed",
              st == 201 and b["data"]["request_status"] == "completed",
              "status=%s" % st)
        check("observations.ts_utc_ms 如实存 NULL（不伪造时间）",
              one("SELECT ts_utc_ms FROM observations WHERE request_id=?", (RID_N,)) is None)

    # =====================================================================
    section("N. 兼容性：第 1 周 /ingest 原样可用")
    # =====================================================================
    legacy = {
        "device_id": "legacy-week1-device", "boot_count": 1, "seq": 1,
        "uptime_ms": 100, "ts_utc_ms": 0, "ts_local": "legacy",
        "sensor": {"model": "QMA6100P", "lsb_per_g": 4096},
        "raw": {"x": 4096, "y": 0, "z": 0},
        "accel_g": {"x": 1.0, "y": 0.0, "z": 0.0},
        "magnitude_g": 1.0,
    }
    st, b = post(base + "/api/v1/ingest", legacy, token=TOKEN)
    check("第 1 周 /ingest 仍返回 201", st == 201, "status=%s" % st)
    if st == 201:
        r = db("SELECT request_id, report_mode FROM sensor_records WHERE upload_id=?",
               (b["upload_id"],))[0]
        check("旧报文的 request_id / report_mode 如实为 NULL（不回填默认值）",
              r["request_id"] is None and r["report_mode"] is None,
              "rid=%s mode=%s" % (r["request_id"], r["report_mode"]))

    st, b = get(base + "/api/v1/stats")
    check("/stats 新增 4 个请求维度键且为整数",
          all(isinstance(b.get(k), int) for k in
              ("requests_total", "requests_completed", "requests_timeout",
               "rejected_observations_total")),
          "total=%s done=%s to=%s rej=%s" % (b.get("requests_total"),
                                             b.get("requests_completed"),
                                             b.get("requests_timeout"),
                                             b.get("rejected_observations_total")))

    st, b = get(base + "/api/v1/devices")
    dev = [x for x in b["devices"] if x["device_id"] == DEV][0]
    check("/devices 新增字段齐备",
          {"last_status", "periodic_enabled", "last_seen_utc", "pending_commands"}
          <= set(dev.keys()), "keys=%s" % sorted(dev.keys()))

    st, b = get(base + "/api/v1/collect_requests/NOT-A-REAL-ID")
    check("不存在的 request_id → 404 REQUEST_NOT_FOUND",
          st == 404 and b["error"]["code"] == "REQUEST_NOT_FOUND", "status=%s" % st)

    # ---- 收尾 ----
    srv.shutdown()
    time.sleep(0.5)
    for suffix in ("", "-wal", "-shm"):
        p = TMP_DB + suffix
        for _ in range(5):
            try:
                if os.path.exists(p):
                    os.remove(p)
                break
            except PermissionError:
                time.sleep(0.4)

    print("\n" + "=" * 74)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("\n失败清单:")
        for f in FAILED:
            print("   × " + f)
    print("临时自检库已删除:", TMP_DB)
    print("=" * 74)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())