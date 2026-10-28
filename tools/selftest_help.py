#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest_help.py —— 第 3 周「按键求助与物理反馈闭环」接口层自检

⚠️ 关于"禁止假数据"约束（务必先读）：
   本脚本用**独立的临时数据库**，并以**脚本扮演设备**的方式发送
   `POST /api/v1/help_events` 与 `POST /api/v1/device/ack`。
   它模拟的是**设备侧报文**，因此结论一律属于"接口层通过"，
   **不能**证明真实板子按过键、蜂鸣器响过 —— 实体输入输出必须用真板子验收
   （见 docs/08 §5 与 docs/10 的当堂验证记录）。
   本脚本绝不写入 backend/data/sensor.db，运行结束即删除临时库。

覆盖的核心红线（当堂验证第 2 项）：
   ★ 没有远端回执证据（remote_acked_at_utc 为 NULL）时，
     任何接口都**不得**给出可以让页面显示「对方已收到」的字段。

用法:
    .venv\\Scripts\\python.exe tools\\selftest_help.py
    .venv\\Scripts\\python.exe tools\\selftest_help.py -v    # 打印每次请求/响应
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

TMP_DB = os.path.join(tempfile.gettempdir(), "selftest_help.db")
PORT = 8097
TOKEN = "selftest-help-token"
DEV = "esp32s3eye-HELPTEST"          # 明显标注：这不是真实设备
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
            body = r.read().decode()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"_raw": body}


def post(url, obj, token=None, raw=None):
    st, b = _req("POST", url, obj, token, raw)
    if VERBOSE:
        print("      POST %s -> %d %s" % (url, st, json.dumps(b, ensure_ascii=False)[:260]))
    return st, b


def get(url):
    st, b = _req("GET", url)
    if VERBOSE:
        print("      GET  %s -> %d %s" % (url, st, json.dumps(b, ensure_ascii=False)[:260]))
    return st, b


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
def help_payload(help_id, boot=808, up_ms=318440, trigger_type="button",
                 local_feedback="buzzer+led", client_key=None,
                 pending_local=False, trigger_at_utc="2026-10-08T13:49:31.437+0800"):
    """构造一条"设备上传的按键求助事件"（docs/06 §7.1）。

    ★ 数值只是让报文结构自洽的**固定**值，不是真实按键记录 ——
      本脚本的结论仅覆盖后端判定逻辑（接口层）。
    """
    return {
        "device_id": DEV,
        "help_id": help_id,
        "client_request_key": client_key or ("btn-%d-%d" % (boot, up_ms)),
        "trigger_type": trigger_type,
        "trigger_at_utc": trigger_at_utc,
        "trigger_up_ms": up_ms,
        "boot_count": boot,
        "queued_at_utc": trigger_at_utc,
        "local_feedback": local_feedback,
        "local_feedback_at_utc": trigger_at_utc,
        "message": None,
        "pending_local": pending_local,
    }


def device_poll(base, boot=808, seq=3190, up_ms=318400):
    """设备轮询一次（顺带建立 devices 基线，便于第 2 周链路继续可用）。"""
    return post(base + "/api/v1/device/commands/poll", {
        "device_id": DEV, "boot_count": boot, "up_ms": up_ms, "seq_last": seq,
        "periodic_enabled": True,
        "health": {"sensor_ok": True, "wifi_rssi": -61, "free_heap": 180000},
        "last_ack": None,
    }, token=TOKEN)


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
    srv = server.ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    base = "http://127.0.0.1:%d" % PORT
    H = base + "/api/v1/help_events"

    print("=" * 74)
    print("第 3 周按键求助闭环 —— 接口层自检")
    print("临时库: %s" % TMP_DB)
    print("说明  : 本脚本扮演设备发报文，验证**后端**判定逻辑；")
    print("        结论属『接口层通过』，实体按键/物理反馈请用真板子验收。")
    print("=" * 74)

    # =====================================================================
    section("A. 设备上传求助事件（POST /api/v1/help_events）")
    # =====================================================================
    device_poll(base)                       # 先让设备出现（供 device_effective_status）

    # A1 缺令牌 → 401（沿用第 1 周同一套令牌，不新增凭证）
    st, b = post(H, help_payload(str(uuid.uuid4())))
    check("A1 无 X-Device-Token 上传 → 401", st == 401, "http=%d" % st)

    # A2 正常上传 → 201 且四键齐备（TC-W3-01(b)）
    hid1 = str(uuid.uuid4())
    st, b = post(H, help_payload(hid1), token=TOKEN)
    d = b.get("data") or {}
    check("A2 正常上传 → 201", st == 201, "http=%d" % st)
    check("A2b data 含 help_id/status/remote_ack_required/notice 四键",
          all(k in d for k in ("help_id", "status", "remote_ack_required", "notice")),
          "keys=%s" % sorted(d.keys()))
    check("A2c status == 'uploaded'（逐字符）", d.get("status") == "uploaded",
          "status=%r" % d.get("status"))
    check("A2d remote_ack_required == true", d.get("remote_ack_required") is True)
    check("A2e help_events 恰新增 1 行",
          one("SELECT COUNT(*) FROM help_events WHERE help_id=?", (hid1,)) == 1)
    check("A2f 新行 remote_acked_at_utc 为 NULL（还没人回应）",
          one("SELECT remote_acked_at_utc FROM help_events WHERE help_id=?", (hid1,)) is None)
    check("A2g local_feedback_received_at_utc 永远非空（服务端口径）",
          one("SELECT local_feedback_received_at_utc FROM help_events WHERE help_id=?",
              (hid1,)) is not None)
    check("A2h received_at_utc 非空", one(
        "SELECT received_at_utc FROM help_events WHERE help_id=?", (hid1,)) is not None)

    # A3 缺必填字段 → 400 MISSING_FIELD，且不落行（绝不代为生成）
    n_before = one("SELECT COUNT(*) FROM help_events")
    bad = help_payload(str(uuid.uuid4()))
    del bad["trigger_type"]
    st, b = post(H, bad, token=TOKEN)
    check("A3a 缺 trigger_type → 400 MISSING_FIELD",
          st == 400 and (b.get("error") or {}).get("code") == "MISSING_FIELD",
          "http=%d code=%s" % (st, (b.get("error") or {}).get("code")))
    check("A3b 被拒的上传不落行", one("SELECT COUNT(*) FROM help_events") == n_before)

    # A4 local_feedback 越界 → 400（禁止谎报本地反馈，TC-W3-04）
    bad2 = help_payload(str(uuid.uuid4()))
    bad2["local_feedback"] = "buzzer+led+screen"
    st, b = post(H, bad2, token=TOKEN)
    check("A4 local_feedback 越界 → 400（禁止谎报本地反馈）", st == 400,
          "http=%d" % st)

    # A5 trigger_type 越界 → 400（枚举只有 button/touch）
    bad3 = help_payload(str(uuid.uuid4()))
    bad3["trigger_type"] = "voice"
    st, b = post(H, bad3, token=TOKEN)
    check("A5 trigger_type 越界 → 400（枚举仅 button/touch）", st == 400, "http=%d" % st)

    # A6 幂等：同一 client_request_key 重复上传 → 200 + deduplicated:true，且不新增行
    key = "btn-808-999001"
    hid6 = str(uuid.uuid4())
    st, b = post(H, help_payload(hid6, up_ms=999001, client_key=key), token=TOKEN)
    n1 = one("SELECT COUNT(*) FROM help_events")
    st2, b2 = post(H, help_payload(str(uuid.uuid4()), up_ms=999001, client_key=key),
                   token=TOKEN)
    d2 = b2.get("data") or {}
    check("A6a 同 client_request_key 重复上传 → 200", st2 == 200, "http=%d" % st2)
    check("A6b deduplicated == true", d2.get("deduplicated") is True)
    check("A6c 返回同一个 help_id", d2.get("help_id") == hid6,
          "%s vs %s" % (d2.get("help_id"), hid6))
    check("A6d 连按 5 次只产生 1 行", one("SELECT COUNT(*) FROM help_events") == n1)

    # A7 trigger_up_ms 如实保存（不得伪造触发时间，TC-W3-05）
    check("A7 trigger_up_ms 与上传值逐值相等",
          one("SELECT trigger_up_ms FROM help_events WHERE help_id=?", (hid6,)) == 999001)
    check("A7b boot_count 如实保存",
          one("SELECT boot_count FROM help_events WHERE help_id=?", (hid6,)) == 808)

    # A8 未对时：trigger_at_utc 为 None 时必须存 NULL，绝不用服务端时间顶替
    hid8 = str(uuid.uuid4())
    p8 = help_payload(hid8, up_ms=888001, trigger_at_utc=None)
    st, b = post(H, p8, token=TOKEN)
    check("A8 未对时 trigger_at_utc 存 NULL（不顶替）",
          one("SELECT trigger_at_utc FROM help_events WHERE help_id=?", (hid8,)) is None)

    # A9 断网滞留补传：pending_local=true 落库，trigger_up_ms 未被改写
    hid9 = str(uuid.uuid4())
    st, b = post(H, help_payload(hid9, up_ms=777001, pending_local=True,
                                 trigger_at_utc=None), token=TOKEN)
    check("A9a pending_local=true 如实落库",
          one("SELECT pending_local FROM help_events WHERE help_id=?", (hid9,)) == 1)
    check("A9b 补传的 trigger_up_ms 仍是触发那一刻的值（未被伪造）",
          one("SELECT trigger_up_ms FROM help_events WHERE help_id=?", (hid9,)) == 777001)

    # =====================================================================
    section("B. 查看者列表与详情（GET /api/v1/help_events）")
    # =====================================================================
    st, b = get(H + "?device_id=" + DEV)
    d = b.get("data") or {}
    items = d.get("help_events") or []
    check("B1 列表 200 且含刚上传的事件", st == 200 and len(items) > 0,
          "http=%d count=%s" % (st, d.get("count")))

    # B2 键集合是契约（TC-W3-12(c)：少一键即失败）
    required = ["help_id", "status", "trigger_type", "trigger_up_ms", "trigger_at_utc",
                "queued_at_utc", "received_at_utc", "local_feedback",
                "local_feedback_at_utc", "local_feedback_received_at_utc",
                "pending_local", "viewed_at_utc", "responded_at_utc",
                "remote_acked_at_utc", "accepted_by", "cancel_reason", "age_ms"]
    first = items[0] if items else {}
    miss = [k for k in required if k not in first]
    check("B2 列表项含全部契约键（%d 个）" % len(required), not miss,
          "缺失=%s" % miss if miss else "")

    # B3 age_ms ≈ now - received_at_utc（误差 ≤2000ms）
    tgt = [x for x in items if x["help_id"] == hid1]
    if tgt:
        age = tgt[0]["age_ms"]
        check("B3 age_ms 为正且量级正确（≤2000ms）",
              isinstance(age, int) and 0 <= age <= 2000, "age_ms=%s" % age)
    else:
        check("B3 age_ms 为正且量级正确（≤2000ms）", False, "未找到该事件")

    # B4 状态过滤
    st, b = get(H + "?status=uploaded&limit=50")
    ups = (b.get("data") or {}).get("help_events") or []
    check("B4a ?status=uploaded 过滤后每项 status 都是 uploaded",
          all(x["status"] == "uploaded" for x in ups), "n=%d" % len(ups))
    st, b = get(H + "?limit=1")
    check("B4b ?limit=1 时条数 == 1",
          len((b.get("data") or {}).get("help_events") or []) == 1)

    # B5 ★★ 红线：此刻还没有任何回应/回执 → remote_acked_at_utc 必须全为 NULL
    check("B5 ★ 列表中 remote_acked_at_utc 全为 NULL（无远端回执证据）",
          all(x["remote_acked_at_utc"] is None for x in items),
          "非空项=%d" % sum(1 for x in items if x["remote_acked_at_utc"]))

    # B6 详情：打开即记 viewed，写 help_actions 一行
    hid_view = str(uuid.uuid4())
    post(H, help_payload(hid_view, up_ms=666001), token=TOKEN)
    st, b = get(H + "/" + hid_view)
    d = b.get("data") or {}
    check("B6a 详情 200 且 status 推进到 viewed", st == 200 and d.get("status") == "viewed",
          "http=%d status=%s" % (st, d.get("status")))
    check("B6b 详情里 remote_acked_at_utc 仍为 NULL",
          d.get("remote_acked_at_utc") is None)
    check("B6c help_actions 记入 1 行 viewed",
          one("""SELECT COUNT(*) FROM help_actions
                 WHERE help_id=? AND action='viewed'""", (hid_view,)) == 1)
    check("B6d viewed_at_utc 非空",
          one("SELECT viewed_at_utc FROM help_events WHERE help_id=?", (hid_view,)) is not None)

    # B7 不存在的 help_id → 404
    st, b = get(H + "/" + str(uuid.uuid4()))
    check("B7 不存在的 help_id → 404 REQUEST_NOT_FOUND",
          st == 404 and (b.get("error") or {}).get("code") == "REQUEST_NOT_FOUND",
          "http=%d" % st)

    # =====================================================================
    section("C. 查看者回应（POST /help_events/<id>/response）")
    # =====================================================================
    hid_r = str(uuid.uuid4())
    post(H, help_payload(hid_r, up_ms=555001), token=TOKEN)

    st, b = post(H + "/" + hid_r + "/response", {"actor": "viewer-self", "message": "已通知值班"})
    d = b.get("data") or {}
    check("C1 回应 → 200", st == 200, "http=%d" % st)
    check("C1b data 含六键（TC-W3-14(a)）",
          all(k in d for k in ("help_id", "status", "responded_at_utc",
                               "remote_ack_command", "remote_acked_at_utc", "note")),
          "keys=%s" % sorted(d.keys()))
    check("C1c status == 'responded'", d.get("status") == "responded")
    check("C1d remote_ack_command == 'help_ack'", d.get("remote_ack_command") == "help_ack")

    # ★★★ 本文件最重要的一条断言 ★★★
    check("C2 ★★ 回应响应里 remote_acked_at_utc == null（设备尚未确认）",
          d.get("remote_acked_at_utc") is None,
          "值=%r" % d.get("remote_acked_at_utc"))
    check("C3 ★★ 数据库 remote_acked_at_utc 仍为 NULL（三重一致）",
          one("SELECT remote_acked_at_utc FROM help_events WHERE help_id=?",
              (hid_r,)) is None)
    st, b = get(H + "?device_id=" + DEV)
    hit = [x for x in ((b.get("data") or {}).get("help_events") or [])
           if x["help_id"] == hid_r]
    check("C4 ★★ 列表接口该字段也为 NULL（三重一致）",
          bool(hit) and hit[0]["remote_acked_at_utc"] is None)
    st, b = get(H + "/" + hid_r)
    check("C5 ★★ 详情接口该字段也为 NULL（三重一致）",
          (b.get("data") or {}).get("remote_acked_at_utc") is None)

    check("C6 help_actions 记入 responded 一行且 actor 正确",
          one("""SELECT actor FROM help_actions
                 WHERE help_id=? AND action='responded'""", (hid_r,)) == "viewer-self")
    check("C7 responded_at_utc 落库",
          one("SELECT responded_at_utc FROM help_events WHERE help_id=?", (hid_r,)) is not None)
    check("C8 accepted_by 记录为回应者",
          one("SELECT accepted_by FROM help_events WHERE help_id=?", (hid_r,)) == "viewer-self")

    # C9 回应自动生成 help_ack 指令（但入队 ≠ 送达）
    st, b = device_poll(base)
    cmds = ((b.get("data") or {}).get("commands") or [])
    ack_cmd = [c for c in cmds if c.get("type") == "help_ack"]
    check("C9 设备轮询取到 help_ack 指令（回应后自动下发）", len(ack_cmd) >= 1,
          "types=%s" % [c.get("type") for c in cmds])
    if ack_cmd:
        c0 = ack_cmd[0]
        check("C9b 指令含 request_id/nonce/timeout_ms/issued_at_utc/attempt/baseline 键",
              all(k in c0 for k in ("request_id", "nonce", "timeout_ms",
                                    "issued_at_utc", "attempt", "baseline")),
              "keys=%s" % sorted(c0.keys()))
        check("C9c 指令 request_id 可追溯到 help_id",
              hid_r in (c0.get("request_id") or ""), "rid=%s" % c0.get("request_id"))
    else:
        check("C9b 指令含 request_id/nonce/timeout_ms/issued_at_utc/attempt/baseline 键", False)
        check("C9c 指令 request_id 可追溯到 help_id", False)

    # C10 ★ 设备取走指令后、真正 ack 之前，remote_acked_at_utc 必须**仍然**是 NULL
    check("C10 ★★ 指令被取走后、ack 前仍为 NULL（入队≠送达）",
          one("SELECT remote_acked_at_utc FROM help_events WHERE help_id=?",
              (hid_r,)) is None)

    # =====================================================================
    section("D. 设备确认（ack_type='help_ack'）→ 才允许写 remote_acked_at_utc")
    # =====================================================================
    rid = "helpcmd-%s" % hid_r
    st, b = post(base + "/api/v1/device/ack", {
        "request_id": rid, "device_id": DEV, "ack_type": "help_ack",
        "nonce": None, "attempt": 1, "boot_count": 808, "up_ms": 319000,
        "reason": None, "detail": {"led": "green"},
    }, token=TOKEN)
    d = b.get("data") or {}
    check("D1 设备 ack help_ack → 200", st == 200, "http=%d body=%s"
          % (st, json.dumps(b, ensure_ascii=False)[:200]))
    ra = one("SELECT remote_acked_at_utc FROM help_events WHERE help_id=?", (hid_r,))
    check("D2 ★★ remote_acked_at_utc 变为非空（设备真的确认了）", ra is not None,
          "值=%r" % ra)
    check("D3 返回值与库中一致", d.get("remote_acked_at_utc") == ra,
          "%r vs %r" % (d.get("remote_acked_at_utc"), ra))

    # D4 重复 ack 幂等：200 + deduplicated:true，时间戳逐字符不变
    time.sleep(0.05)
    st, b = post(base + "/api/v1/device/ack", {
        "request_id": rid, "device_id": DEV, "ack_type": "help_ack",
        "nonce": None, "attempt": 1, "boot_count": 808, "up_ms": 319500,
    }, token=TOKEN)
    d2 = b.get("data") or {}
    check("D4a 重复 ack → 200", st == 200, "http=%d" % st)
    check("D4b 重复 ack → deduplicated == true", d2.get("deduplicated") is True)
    check("D4c ★ 重复 ack 不覆盖 remote_acked_at_utc（逐字符不变）",
          one("SELECT remote_acked_at_utc FROM help_events WHERE help_id=?", (hid_r,)) == ra)

    # D5 页面此时才允许显示"已收到"——接口层只负责给出证据
    st, b = get(H + "/" + hid_r)
    check("D5 详情接口此时给出非空 remote_acked_at_utc（页面据此才可显示已收到）",
          (b.get("data") or {}).get("remote_acked_at_utc") is not None)

    # D6 不存在的 help_id → 404
    st, b = post(base + "/api/v1/device/ack", {
        "request_id": "helpcmd-does-not-exist", "device_id": DEV,
        "ack_type": "help_ack", "attempt": 1,
    }, token=TOKEN)
    check("D6 不存在的 help_id ack → 404", st == 404, "http=%d" % st)

    # =====================================================================
    section("E. 取消逻辑 C1–C4（POST /help_events/<id>/cancel）")
    # =====================================================================
    # E1 C3：缺 reason → 400 MISSING_FIELD，且**不改写状态**
    hid_c = str(uuid.uuid4())
    post(H, help_payload(hid_c, up_ms=444001), token=TOKEN)
    st_before = one("SELECT status FROM help_events WHERE help_id=?", (hid_c,))
    st, b = post(H + "/" + hid_c + "/cancel", {"actor": "wearer-self"})
    check("E1a C3 缺 reason → 400 MISSING_FIELD",
          st == 400 and (b.get("error") or {}).get("code") == "MISSING_FIELD",
          "http=%d code=%s" % (st, (b.get("error") or {}).get("code")))
    check("E1b ★ 缺 reason 时状态未被改写",
          one("SELECT status FROM help_events WHERE help_id=?", (hid_c,)) == st_before,
          "status=%r" % one("SELECT status FROM help_events WHERE help_id=?", (hid_c,)))
    check("E1c 缺 reason 时 cancelled_at_utc 仍为 NULL",
          one("SELECT cancelled_at_utc FROM help_events WHERE help_id=?", (hid_c,)) is None)

    # E2 C2：非终态（uploaded）可取消
    st, b = post(H + "/" + hid_c + "/cancel", {"actor": "wearer-self", "reason": "误触"})
    d = b.get("data") or {}
    check("E2a C2 uploaded 可取消 → 200 + status=cancelled",
          st == 200 and d.get("status") == "cancelled",
          "http=%d status=%s" % (st, d.get("status")))
    check("E2b cancelled_at_utc 非空", d.get("cancelled_at_utc") is not None)
    check("E2c cancel_reason 逐字符等于请求值",
          one("SELECT cancel_reason FROM help_events WHERE help_id=?", (hid_c,)) == "误触")
    check("E2d device_command == 'clear_alert'", d.get("device_command") == "clear_alert")
    check("E2e help_actions 记入 cancelled 一行",
          one("""SELECT COUNT(*) FROM help_actions
                 WHERE help_id=? AND action='cancelled'""", (hid_c,)) == 1)

    # E3 C4：终态重复取消 → 200 + deduplicated:true，时间戳不变，不新增动作行
    nact = one("SELECT COUNT(*) FROM help_actions WHERE help_id=?", (hid_c,))
    t_before = one("SELECT cancelled_at_utc FROM help_events WHERE help_id=?", (hid_c,))
    st, b = post(H + "/" + hid_c + "/cancel", {"actor": "wearer-self", "reason": "再点一次"})
    d = b.get("data") or {}
    check("E3a C4 重复取消 → 200（不得 409/500）", st == 200, "http=%d" % st)
    check("E3b C4 deduplicated == true", d.get("deduplicated") is True)
    check("E3c C4 cancelled_at_utc 逐字符不变",
          one("SELECT cancelled_at_utc FROM help_events WHERE help_id=?", (hid_c,)) == t_before)
    check("E3d C4 不新增 cancelled 动作行",
          one("SELECT COUNT(*) FROM help_actions WHERE help_id=?", (hid_c,)) == nact)

    # E4 C3：取消不删除任何记录
    check("E4a C3 help_events 行仍在（取消 ≠ 删除）",
          one("SELECT COUNT(*) FROM help_events WHERE help_id=?", (hid_c,)) == 1)
    check("E4b C3 help_actions 行仍在", nact >= 1)
    st, b = get(H + "?device_id=" + DEV)
    check("E4c 列表仍能查到该 help_id 且 cancel_reason 可见",
          any(x["help_id"] == hid_c and x["cancel_reason"] == "误触"
              for x in ((b.get("data") or {}).get("help_events") or [])))

    # E5 C2：responded 之后也能取消
    hid_cr = str(uuid.uuid4())
    post(H, help_payload(hid_cr, up_ms=443001), token=TOKEN)
    post(H + "/" + hid_cr + "/response", {"actor": "viewer-self"})
    st, b = post(H + "/" + hid_cr + "/cancel", {"actor": "wearer-self", "reason": "已处理完"})
    check("E5 C2 responded 状态也可取消 → cancelled",
          st == 200 and (b.get("data") or {}).get("status") == "cancelled",
          "http=%d" % st)

    # E6 取消会作废队列里未取走的 help_ack（已取消的求助不该再让灯转绿）
    hid_c2 = str(uuid.uuid4())
    post(H, help_payload(hid_c2, up_ms=442001), token=TOKEN)
    post(H + "/" + hid_c2 + "/response", {"actor": "viewer-self"})
    post(H + "/" + hid_c2 + "/cancel", {"actor": "wearer-self", "reason": "复测"})
    left = one("""SELECT COUNT(*) FROM pending_commands
                  WHERE request_id=? AND type='help_ack'""", ("helpcmd-%s" % hid_c2,))
    check("E6 取消后队列里不再残留 help_ack（防止已取消仍转绿）", left == 0,
          "残留=%s" % left)

    # E7 取消自动下发 clear_alert
    st, b = device_poll(base)
    cmds = ((b.get("data") or {}).get("commands") or [])
    check("E7 设备轮询取到 clear_alert 指令",
          any(c.get("type") == "clear_alert" for c in cmds),
          "types=%s" % [c.get("type") for c in cmds])

    # =====================================================================
    section("F. 状态机与枚举契约（三课接口检查的一部分）")
    # =====================================================================
    rows = db("SELECT DISTINCT status FROM help_events")
    got = sorted(r[0] for r in rows)
    allowed = set(server.HELP_STATUSES)
    check("F1 help_events.status 取值 ⊆ 契约枚举 %s" % sorted(allowed),
          set(got) <= allowed, "实际=%s" % got)
    check("F2 契约枚举未被越界使用（无自造状态如 open/new/done）",
          not (set(got) - allowed), "越界=%s" % sorted(set(got) - allowed))

    acts = sorted(r[0] for r in db("SELECT DISTINCT action FROM help_actions"))
    check("F3 help_actions.action ⊆ {viewed, responded, cancelled}",
          set(acts) <= {"viewed", "responded", "cancelled"}, "实际=%s" % acts)

    cmd_types = list(server.COMMAND_TYPES)
    check("F4 command.type 契约含 help_ack/clear_alert/collect_now",
          set(("help_ack", "clear_alert", "collect_now")) <= set(cmd_types))

    # F5 状态不回退：viewed 之后重复上传同一 client_request_key 不应回到 uploaded
    check("F5 状态不回退（responded 不因重复上传回到 uploaded）",
          one("SELECT status FROM help_events WHERE help_id=?", (hid_r,)) == "responded",
          "status=%r" % one("SELECT status FROM help_events WHERE help_id=?", (hid_r,)))

    # F6 pending_local 与 trigger_up_ms 的补传语义
    check("F6 滞留补传事件仍只有 1 行（补传不产生重复）",
          one("SELECT COUNT(*) FROM help_events WHERE help_id=?", (hid9,)) == 1)

    # =====================================================================
    section("G. 第 1/2 周回归（新增表不影响既有链路）")
    # =====================================================================
    check("G1 第 1 周表仍在且列名未变",
          all(c in [r[1] for r in db("PRAGMA table_info(raw_uploads)")]
              for c in ("id", "body", "sha256", "device_id")),
          "cols=%s" % [r[1] for r in db("PRAGMA table_info(raw_uploads)")])
    check("G2 sensor_records 含第 2 周新增列 request_id/report_mode",
          {"request_id", "report_mode"} <=
          {r[1] for r in db("PRAGMA table_info(sensor_records)")})
    check("G3 /api/v1/ingest 仍可用（第 1 周上传链路未被破坏）",
          post(base + "/api/v1/ingest", {"device_id": DEV}, token=TOKEN)[0] == 400,
          "缺 raw 时应 400（不是 404/500）")
    check("G4 第 2 周 collect_requests 仍可用（缺 device_id → 400）",
          post(base + "/api/v1/collect_requests", {}, token=None)[0] == 400)
    check("G5 /healthz 正常", get(base + "/healthz")[0] == 200)
    check("G6 第 3 周新表不干扰 raw_uploads 计数",
          one("SELECT COUNT(*) FROM raw_uploads") == 0,
          "本轮求助用例不应写入任何 raw_uploads")

    # =====================================================================
    print("\n" + "=" * 74)
    print("自检结论: 通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("\n失败项：")
        for f in FAILED:
            print("  ✘ " + f)
    print("=" * 74)

    try:
        srv.shutdown()
    except Exception:                                       # noqa: BLE001
        pass
    if os.path.exists(TMP_DB):
        try:
            os.remove(TMP_DB)
        except OSError:
            pass
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())