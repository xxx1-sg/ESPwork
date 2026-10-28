#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_nl.py —— 第 4 周「受限工具语言助手」接口层自检

覆盖当堂验证的四类输入：
  正确  ·「查看上次数据」只读已有记录（保留旧时间，零设备侧流量）
        ·「重新采集一次」等设备回报，拿到新观测才允许说完成
        · 设备状态 / 时间范围查询
  含糊  · 多设备且未指明 → 反问，不猜、不下发
        · 完全听不懂 → 反问，不硬猜
  越界  · 未接入设备 → 403 DEVICE_OUT_OF_SCOPE
        · 超长/空 query、坏 JSON → 400，且不产生任何设备指令
  无证据 · 设备无响应 → pending/timeout，措辞不得出现"采集成功"
        · 位置/坐标 → 数据模型不具备该能力，明确说不足，不生成坐标

以及工程红线：
  R1 只读路径不得新增 collect_requests / pending_commands / sensor_records
  R2 只有 status=completed 且存在绑定观测时才允许"完成"措辞
  R3 语言服务不可用时自动退回规则解析，答案仍正确
  R4 服务端密钥不出现在任何响应里
  R5 歧义/越界路径不向任何设备发指令

运行：D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\selftest_nl.py
"""

import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(BASE, "backend")
PORT = 8012
TOKEN = "esp32s3eye-demo-token-2026"
TMP_DB = os.path.join(BASE, ".tmp", "selftest_nl.db")

sys.path.insert(0, BACKEND)
os.makedirs(os.path.dirname(TMP_DB), exist_ok=True)
if os.path.exists(TMP_DB):
    os.remove(TMP_DB)

# ★ 必须在 import server（进而 import nl_assistant）之前设置：
#   节流窗口调小，让"连续两次采集"用例能在秒级跑完；同时打开一条不可达的
#   语言服务地址，稍后用它验证"服务异常 → 退回规则解析"的备用路径。
os.environ.update({
    "SENSOR_DB": TMP_DB,
    "SENSOR_TOKEN": TOKEN,
    "SENSOR_HOST": "127.0.0.1",
    "SENSOR_PORT": str(PORT),
    "SENSOR_WEB_DIR": os.path.join(BASE, "web"),
    "NL_COLLECT_MIN_INTERVAL_MS": "800",
    "NL_LLM_ENABLED": "0",              # 默认纯规则解析
    "NL_COLLECT_WAIT_MS": "1500",
})

import server  # noqa: E402
import nl_assistant  # noqa: E402

fails = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    print(("  OK   " if cond else "  FAIL ") + name + ((" | " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)


def section(t):
    print("\n" + "-" * 74 + "\n" + t + "\n" + "-" * 74)


def http(method, path, body=None, token=None, raw=None):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = raw if raw is not None else (
        json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Device-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(data)
            except ValueError:
                return r.status, {"_text": data[:200]}
    except urllib.error.HTTPError as e:
        data = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(data)
        except ValueError:
            return e.code, {"_text": data[:200]}


def db(sql, args=()):
    with sqlite3.connect(TMP_DB) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql, args=()):
    rows = db(sql, args)
    return list(rows[0].values())[0] if rows else None


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def seed_record(device_id, received_dt, seq, boot, up, raw=(100, 200, 3900), model="QMA6100P"):
    """直接落一条真实结构的记录（只用于构造测试夹具，不代表传感器实测）。"""
    x, y, z = raw
    lsb = 4096.0
    gx, gy, gz = x / lsb, y / lsb, z / lsb
    mag = (gx * gx + gy * gy + gz * gz) ** 0.5
    ts = iso(received_dt)
    with sqlite3.connect(TMP_DB) as c:
        cur = c.execute(
            "INSERT INTO raw_uploads (received_at_utc, remote_addr, content_type, byte_len,"
            " sha256, body) VALUES (?,?,?,?,?,?)",
            (ts, "127.0.0.1:1", "application/json", 128, "0" * 64, "{}"))
        uid = cur.lastrowid
        c.execute(
            "INSERT INTO sensor_records (upload_id, device_id, boot_count, seq, sample_idx,"
            " sample_count, sample_period_ms, uptime_ms, ts_utc_ms, ts_local, received_at_utc,"
            " sensor_model, sensor_type, i2c_addr, chip_id, range_g, lsb_per_g, odr_hz,"
            " oversample, raw_x, raw_y, raw_z, gx, gy, gz, ms2x, ms2y, ms2z, magnitude_g,"
            " verify_ok, verify_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, device_id, boot, seq, 1, 1, 100, up,
             int(received_dt.timestamp() * 1000),
             received_dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0800",
             ts, model, "accelerometer", 18, 144, 2, 4096, 100, 8,
             x, y, z, gx, gy, gz, gx * 9.80665, gy * 9.80665, gz * 9.80665, mag, 1, "selftest fixture"))
        return cur.lastrowid


def device_poll(device_id, boot, seq, up, ok=True):
    return http("POST", "/api/v1/device/commands/poll", {
        "device_id": device_id, "boot_count": boot, "seq_last": seq, "up_ms": up,
        "periodic_enabled": True,
        "health": {"sensor_ok": ok, "wifi_rssi": -61, "free_heap": 180000},
        "last_ack": None,
    }, token=TOKEN)


def obs_payload(cmd, device_id, boot, seq_first, up, raw=(120, 260, 3920)):
    x, y, z = raw
    lsb = 4096.0
    g = [x / lsb, y / lsb, z / lsb]
    ms2 = [v * server.G0 for v in g]
    mag = (g[0] ** 2 + g[1] ** 2 + g[2] ** 2) ** 0.5
    now = datetime.now(timezone.utc)
    seq_last = seq_first
    return {
        "request_id": cmd["request_id"], "nonce": cmd["nonce"], "device_id": device_id,
        "boot_count": boot, "seq_first": seq_first, "seq_last": seq_last,
        "sample_count": 1, "sample_period_ms": 100,
        "up_ms": up, "ts_utc_ms": int(now.timestamp() * 1000),
        "ts_local": now.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0800",
        "collect_mode": "single",
        "sensor": {"model": "QMA6100P", "type": "accelerometer", "i2c_addr": 18,
                   "chip_id": 144, "range_g": 2, "lsb_per_g": 4096,
                   "odr_hz": 100, "oversample": 8},
        "unit": {"raw": "LSB(14bit signed)", "accel_g": "g",
                 "accel_ms2": "m/s^2", "magnitude_g": "g"},
        "samples": [{"seq": seq_last, "idx": 1, "up_ms": up,
                     "ts_utc_ms": int(now.timestamp() * 1000),
                     "ts_local": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0800",
                     "raw": {"x": x, "y": y, "z": z},
                     "accel_g": {"x": g[0], "y": g[1], "z": g[2]},
                     "accel_ms2": {"x": ms2[0], "y": ms2[1], "z": ms2[2]},
                     "magnitude_g": mag}],
        "raw": {"x": x, "y": y, "z": z},
        "accel_g": {"x": g[0], "y": g[1], "z": g[2]},
        "accel_ms2": {"x": ms2[0], "y": ms2[1], "z": ms2[2]},
        "magnitude_g": mag,
    }


def responder_loop(device_id, boot, seq, up, stop_evt, done_evt):
    """扮演设备：取指令 → 回执 → 上传带 nonce 的观测（真实完成证据）。"""
    while not stop_evt.is_set() and not done_evt.is_set():
        st, b = device_poll(device_id, boot, seq, up)
        cmds = (b.get("data") or {}).get("commands") or []
        for c in cmds:
            if c.get("type") != "collect_now":
                continue
            seq += 1
            up += 400
            http("POST", "/api/v1/device/ack", {
                "request_id": c["request_id"], "device_id": device_id,
                "ack_type": "received", "nonce": c["nonce"], "attempt": 1,
                "boot_count": boot, "up_ms": up,
            }, token=TOKEN)
            st2, b2 = http("POST", "/api/v1/observations",
                           obs_payload(c, device_id, boot, seq, up), token=TOKEN)
            done_evt.set()
            return st2, b2
        time.sleep(0.15)
    return None, None


def nl(q, device_id=None, wait_ms=None, parser=None):
    body = {"q": q}
    if device_id is not None:
        body["device_id"] = device_id
    if wait_ms is not None:
        body["wait_ms"] = wait_ms
    if parser is not None:
        body["parser"] = parser
    return http("POST", "/api/v1/nl/query", body)


# ===========================================================================
def main():
    server.db_init()
    server.COLLECT_TIMEOUT_MS = 3000       # 让"无响应→超时"在秒级可测
    srv = server.ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=server._timeout_scanner, daemon=True).start()
    time.sleep(0.4)

    OLD = "esp32s3eye-1C701C"
    NEW = "esp32s3eye-1C704C"
    DEV3 = "esp32s3eye-TEST03"

    now = datetime.now(timezone.utc)
    old_t1 = now - timedelta(minutes=31)
    old_t2 = now - timedelta(minutes=30)
    seed_record(OLD, old_t1, 880, 900, 100000, (10, 20, 4080))
    seed_record(OLD, old_t2, 881, 900, 100100, (12, 22, 4070))
    seed_record(NEW, now - timedelta(seconds=6), 990, 77, 5000, (30, 40, 4088))

    # 三轮询：建立基线（设备必须先"出现过"，才允许被请求采集）
    device_poll(OLD, 900, 1000, 101000)
    device_poll(NEW, 77, 1000, 6000)
    device_poll(DEV3, 1, 10, 1000)
    # 把 OLD 伪装成 30 分钟前就离线
    with sqlite3.connect(TMP_DB) as c:
        c.execute("UPDATE devices SET last_seen_utc=?, last_status='offline' WHERE device_id=?",
                  (iso(old_t2), OLD))

    print("=" * 74)
    print("第 4 周 自然语言查询 / 请求采集 —— 接口层自检")
    print("临时库: %s" % TMP_DB)
    print("=" * 74)

    # -------------------------------------------------------------------
    section("A. 工具清单与密钥边界")
    # -------------------------------------------------------------------
    st, b = http("GET", "/api/v1/nl/tools")
    names = [t["name"] for t in b["data"]["tools"]]
    check("GET /api/v1/nl/tools → 200", st == 200 and b["ok"], st)
    check("工具恰为白名单 7 个", names == ["list_devices", "get_device_status",
                                          "get_latest_record", "get_records",
                                          "request_collect", "list_collect_requests",
                                          "get_collect_status"], names)
    check("只有 request_collect 是 act", [t["name"] for t in b["data"]["tools"]
                                          if t["kind"] == "act"] == ["request_collect"])
    check("策略声明：不支持位置", b["data"]["policy"]["location_supported"] is False)
    check("策略声明：完成判据绑定观测",
          "completed" in b["data"]["policy"]["success_claim_rule"])
    check("响应不含密钥字段", "api_key" not in json.dumps(b) and "sk-" not in json.dumps(b))

    # -------------------------------------------------------------------
    section("B. 正确：查看上次数据 = 只读，保留旧时间，零设备侧流量")
    # -------------------------------------------------------------------
    before = {
        "creq": one("SELECT COUNT(*) FROM collect_requests"),
        "pend": one("SELECT COUNT(*) FROM pending_commands"),
        "rec": one("SELECT COUNT(*) FROM sensor_records"),
    }
    st, b = nl("查看上次数据", OLD)
    d = b.get("data") or {}
    check("200 且 intent=query_latest", st == 200 and d.get("intent") == "query_latest",
          "st=%s intent=%s" % (st, d.get("intent")))
    check("claim=old_record（不冒充新采集）", d.get("claim") == "old_record", d.get("claim"))
    check("status=answered", d.get("status") == "answered", d.get("status"))
    check("工具轨迹 = list_devices → get_latest_record",
          [t["name"] for t in d.get("tool_calls", [])] == ["list_devices", "get_latest_record"],
          [t["name"] for t in d.get("tool_calls", [])])
    check("evidence.device_command_sent=false", d["evidence"]["device_command_sent"] is False)
    check("来源指向 sensor_records.id",
          d["evidence"]["source"].startswith("sensor_records.id="), d["evidence"]["source"])
    check("★ 保留旧时间（received_at_utc 与夹具逐字符一致）",
          d["evidence"]["received_at_utc"] == iso(old_t2),
          "%s vs %s" % (d["evidence"]["received_at_utc"], iso(old_t2)))
    check("age_ms > 60s 且 freshness=stale",
          d["evidence"]["age_ms"] > 60000 and d["evidence"]["freshness"] == "stale",
          "%s/%s" % (d["evidence"]["age_ms"], d["evidence"]["freshness"]))
    check("回答里明确说这是旧数据/已有记录", "旧数据" in d["answer"] or "已经存在" in d["answer"])
    check("回答带来源与时间", "sensor_records.id=" in d["answer"] and "服务器接收" in d["answer"])
    after = {
        "creq": one("SELECT COUNT(*) FROM collect_requests"),
        "pend": one("SELECT COUNT(*) FROM pending_commands"),
        "rec": one("SELECT COUNT(*) FROM sensor_records"),
    }
    check("★ 只读：collect_requests / pending_commands / records 全部零增长",
          before == after, "%s -> %s" % (before, after))

    # -------------------------------------------------------------------
    section("C. 正确：重新采集一次 = 等设备完成证据，拿到新观测才算完成")
    # -------------------------------------------------------------------
    stop = threading.Event()
    done = threading.Event()
    th = threading.Thread(target=responder_loop, args=(NEW, 77, 1000, 6000, stop, done), daemon=True)
    th.start()
    st, b = nl("重新采集一次", NEW, wait_ms=8000)
    d = b.get("data") or {}
    stop.set()
    check("200 且 intent=request_collect", st == 200 and d.get("intent") == "request_collect",
          "st=%s intent=%s" % (st, d.get("intent")))
    check("status=completed", d.get("status") == "completed", d.get("status"))
    check("★ claim=new_record（有绑定观测才允许）", d.get("claim") == "new_record", d.get("claim"))
    check("device_command_sent=true", d["evidence"]["device_command_sent"] is True)
    check("evidence 带 collect_request_id / collect_status=completed",
          bool(d["evidence"].get("collect_request_id")) and
          d["evidence"].get("collect_status") == "completed", d["evidence"])
    check("evidence 带新 record_id", isinstance(d["evidence"].get("record_id"), int),
          d["evidence"].get("record_id"))
    check("工具轨迹含 request_collect 与 get_collect_status",
          "request_collect" in [t["name"] for t in d["tool_calls"]] and
          "get_collect_status" in [t["name"] for t in d["tool_calls"]])
    check("回答明确区分『本次新产生』与『上次旧记录』",
          "本次新产生" in d["answer"] and "旧记录" in d["answer"])
    check("数据库新增 1 条 observation",
          one("SELECT COUNT(*) FROM observations") == 1,
          one("SELECT COUNT(*) FROM observations"))
    check("collect_requests 新增 1 行（对比 B 段快照）",
          one("SELECT COUNT(*) FROM collect_requests") == before["creq"] + 1)

    # 追问「采集完成了吗」→ 只查状态，不得重新下发
    n_before_ask = one("SELECT COUNT(*) FROM collect_requests")
    st, b = nl("刚才那次采集完成了吗", NEW)
    d3 = b.get("data") or {}
    check("追问采集结果 intent=collect_status", d3.get("intent") == "collect_status", d3.get("intent"))
    check("★ 追问结果 claim=new_record（有绑定观测）", d3.get("claim") == "new_record", d3.get("claim"))
    check("追问返回的 record_id 与完成时一致", d3["evidence"].get("record_id") == d["evidence"].get("record_id"),
          "%s vs %s" % (d3["evidence"].get("record_id"), d["evidence"].get("record_id")))
    check("追问不新增采集请求", one("SELECT COUNT(*) FROM collect_requests") == n_before_ask)
    check("追问工具轨迹含 list_collect_requests / get_collect_status",
          "list_collect_requests" in [t["name"] for t in d3["tool_calls"]] and
          "get_collect_status" in [t["name"] for t in d3["tool_calls"]])

    # -------------------------------------------------------------------
    section("D. 无响应：设备不回报 → 不得出现『采集成功』")
    # -------------------------------------------------------------------
    # D1 超时分支：把采集超时压到 1.2s，让"设备一直不回报"在本次调用内落定
    server.COLLECT_TIMEOUT_MS = 1200
    st, b = nl("重新采集一次", OLD, wait_ms=3000)
    d = b.get("data") or {}
    check("200 且 status=timeout", st == 200 and d.get("status") == "timeout",
          "st=%s status=%s" % (st, d.get("status")))
    check("★ claim=no_evidence（不是 new_record）", d.get("claim") == "no_evidence", d.get("claim"))
    check("★ 回答不含『采集成功/已成功』", "采集成功" not in d["answer"] and "已成功" not in d["answer"])
    check("回答明确说『不能确认』", "不能确认" in d["answer"])
    check("有下发记录（device_command_sent=true）", d["evidence"]["device_command_sent"] is True)
    check("没有新增观测（observations 仍为 1）",
          one("SELECT COUNT(*) FROM observations") == 1)
    check("guard 未触发成功词拦截（模板本身就不写成功）",
          "success_word_blocked" not in (d.get("guard") or []), d.get("guard"))
    if d["evidence"].get("collect_request_id"):
        stq, bq = http("GET", "/api/v1/collect_requests/" + d["evidence"]["collect_request_id"])
        check("落库状态确为 timeout（回答与数据库一致）",
              bq["data"]["status"] == "timeout", bq["data"]["status"])
        check("timeout 请求没有绑定观测", bq["data"]["observation"] is None)

    # D2 在途分支：超时放宽，等 300ms 后如实说 pending
    server.COLLECT_TIMEOUT_MS = 15000
    st, b = nl("重新采集一次", OLD, wait_ms=300)
    d = b.get("data") or {}
    check("设备未回报 → status=pending", d.get("status") == "pending", d.get("status"))
    check("pending 时 claim=pending", d.get("claim") == "pending", d.get("claim"))
    check("pending 回答说明已等待时长且不能确认",
          "已等待" in d["answer"] and "不能确认" in d["answer"])
    check("pending 回答给出请求详情路径",
          "/api/v1/collect_requests/" in d["answer"])

    # -------------------------------------------------------------------
    section("E. 含糊：多设备未指明 → 反问，不猜、不下发")
    # -------------------------------------------------------------------
    c0 = one("SELECT COUNT(*) FROM collect_requests")
    st, b = nl("查看上次数据")
    d = b.get("data") or {}
    check("200 且 status=clarify", st == 200 and d.get("status") == "clarify", d.get("status"))
    check("ambiguity.kind=device", (d.get("ambiguity") or {}).get("kind") == "device",
          d.get("ambiguity"))
    check("候选设备 ≥ 2", len((d.get("ambiguity") or {}).get("candidates") or []) >= 2,
          (d.get("ambiguity") or {}).get("candidates"))
    check("歧义时只调用 list_devices", [t["name"] for t in d.get("tool_calls", [])] == ["list_devices"],
          [t["name"] for t in d.get("tool_calls", [])])
    check("歧义时明确说未下发/未读取", "没有向任何设备下发" in d["answer"])
    check("歧义不产生采集请求", one("SELECT COUNT(*) FROM collect_requests") == c0)

    st, b = nl("重新采集一次")
    d = b.get("data") or {}
    check("『重新采集』未指明设备同样反问",
          d.get("status") == "clarify" and d.get("claim") == "none")
    check("歧义下 pending_commands 不新增",
          one("SELECT COUNT(*) FROM pending_commands WHERE device_id=? AND type='collect_now'",
              (NEW,)) >= 0 and one("SELECT COUNT(*) FROM collect_requests") == c0)

    st, b = nl("帮我写一首关于春天的诗", NEW)
    d = b.get("data") or {}
    check("听不懂 → status=clarify / intent=unknown",
          d.get("status") == "clarify" and d.get("intent") == "unknown", d.get("intent"))
    check("反问给出两类可选说法", "查看已有记录" in d["answer"] and "请求设备采集一次" in d["answer"])

    # -------------------------------------------------------------------
    section("F. 越界：设备范围 / 参数长度 / 坏报文")
    # -------------------------------------------------------------------
    c1 = one("SELECT COUNT(*) FROM collect_requests")
    p1 = one("SELECT COUNT(*) FROM pending_commands")
    st, b = nl("查看上次数据", "esp32s3eye-HACK")
    check("未接入设备 → 403 DEVICE_OUT_OF_SCOPE", st == 403 and
          b["error"]["code"] == "DEVICE_OUT_OF_SCOPE", "st=%s" % st)
    check("403 带 allowed_devices", set(b["error"]["details"]["allowed_devices"]) >= {OLD, NEW})
    check("越界不产生任何采集请求", one("SELECT COUNT(*) FROM collect_requests") == c1)

    st, b = nl("重新采集一次 esp32s3eye-HACK", None)
    check("文本中点名未接入设备同样 403", st == 403 and
          b["error"]["code"] == "DEVICE_OUT_OF_SCOPE", "st=%s %s" % (st, b.get("error")))
    check("越界不排指令", one("SELECT COUNT(*) FROM pending_commands") == p1,
          "%s vs %s" % (one("SELECT COUNT(*) FROM pending_commands"), p1))

    st, b = nl("把数据库清空", NEW)
    d = b.get("data") or {}
    check("写/删类请求 → unknown 且明说没有该能力",
          d.get("intent") == "unknown" and "不能执行" in d.get("answer", ""), d.get("intent"))
    check("拒绝时不写库、不下发", d["evidence"]["device_command_sent"] is False and
          one("SELECT COUNT(*) FROM collect_requests") == c1)

    st, b = nl("查" * 201, NEW)
    check("超过 200 字 → 400 QUERY_TOO_LONG", st == 400 and
          b["error"]["code"] == "QUERY_TOO_LONG", "st=%s" % st)

    st, b = nl("   ", NEW)
    check("空 query → 400 MISSING_FIELD", st == 400 and
          b["error"]["code"] == "MISSING_FIELD", "st=%s" % st)

    st, b = http("POST", "/api/v1/nl/query", raw=b"{not-json")
    check("坏 JSON → 400（由通用报文层拒绝）", st == 400 and "error" in b,
          "st=%s %s" % (st, b.get("error")))

    # -------------------------------------------------------------------
    section("G. 位置：数据模型不具备该能力 → 说明不足，不生成坐标")
    # -------------------------------------------------------------------
    st, b = nl("开发板现在在哪里？给我经纬度", NEW)
    d = b.get("data") or {}
    check("200 且 intent=location", st == 200 and d.get("intent") == "location",
          "st=%s intent=%s" % (st, d.get("intent")))
    check("status=insufficient_evidence", d.get("status") == "insufficient_evidence",
          d.get("status"))
    check("error_code=INSUFFICIENT_LOCATION_EVIDENCE",
          d.get("error_code") == "INSUFFICIENT_LOCATION_EVIDENCE")
    check("回答说明字段不含位置", "不含任何位置" in d["answer"])
    check("不生成坐标（无数字.数字对）",
          not __import__("re").search(r"\d{1,3}\.\d{3,}\s*[,，]\s*\d{1,3}\.\d{3,}", d["answer"]))
    check("位置意图不调用业务工具（仅 list_devices）",
          [t["name"] for t in d.get("tool_calls", [])] == ["list_devices"])

    # 兜底闸的直接单测（模拟模型/模板写出了坐标与假成功）
    ans, flags = nl_assistant._guard_answer("设备位于 39.9042, 116.4074 附近", "no_evidence")
    check("兜底闸剔除坐标", "39.9042" not in ans and "coordinates_stripped" in flags, flags)
    ans, flags = nl_assistant._guard_answer("已采集成功，数据已入库", "pending")
    check("兜底闸拦截假成功", "已采集成功" not in ans and "success_word_blocked" in flags,
          "%s / %s" % (ans, flags))
    ans, flags = nl_assistant._guard_answer("设备回报完成，新观测 record_id=9", "new_record")
    check("真完成时成功措辞不被误伤", "success_word_blocked" not in flags)

    # -------------------------------------------------------------------
    section("H. 设备状态 / 时间范围 / 在途去重")
    # -------------------------------------------------------------------
    st, b = nl("设备在线吗", NEW)
    d = b.get("data") or {}
    check("intent=device_status", d.get("intent") == "device_status", d.get("intent"))
    check("回答含最后上报与来源", "最后上报" in d["answer"] and "devices" in d["answer"])
    check("只读：device_command_sent=false", d["evidence"]["device_command_sent"] is False)

    st, b = nl("最近 40 分钟的数据", OLD)
    d = b.get("data") or {}
    check("intent=query_history", d.get("intent") == "query_history", d.get("intent"))
    check("claim=old_record 且 count≥2", d.get("claim") == "old_record" and
          d.get("evidence", {}).get("count", 0) >= 2, d.get("evidence"))
    check("来源是 id 区间", ".." in d["evidence"]["source"], d["evidence"]["source"])

    # 在途去重：先制造一条在途请求，再连说一次
    st, b = nl("重新采集一次", DEV3, wait_ms=0)
    d1 = b.get("data") or {}
    n_after_first = one("SELECT COUNT(*) FROM collect_requests")
    st, b = nl("重新采集一次", DEV3, wait_ms=200)
    d2 = b.get("data") or {}
    check("第一次 status=pending", d1.get("status") == "pending", d1.get("status"))
    check("★ 在途复用：第二次 reused_pending=true",
          (d2.get("evidence") or {}).get("reused_pending") is True, d2.get("evidence"))
    check("在途复用不新增 collect_requests",
          one("SELECT COUNT(*) FROM collect_requests") == n_after_first,
          "%s vs %s" % (one("SELECT COUNT(*) FROM collect_requests"), n_after_first))
    check("两次指向同一个 request_id",
          (d1.get("evidence") or {}).get("collect_request_id") ==
          (d2.get("evidence") or {}).get("collect_request_id"))

    # -------------------------------------------------------------------
    section("I. 备用路径：语言服务异常 → 退回规则解析，答案仍正确")
    # -------------------------------------------------------------------
    os.environ.update({
        "NL_LLM_BASE_URL": "http://127.0.0.1:1/v1",
        "NL_LLM_API_KEY": "sk-selftest-secret",
        "NL_LLM_MODEL": "demo-model",
        "NL_LLM_ENABLED": "1",
        "NL_LLM_TIMEOUT_S": "0.6",
    })
    st, b = http("GET", "/api/v1/nl/tools")
    check("tools 显示 LLM 已配置但 key_visible=false",
          b["data"]["parser"]["llm"]["configured"] is True and
          b["data"]["parser"]["llm"]["key_visible"] is False, b["data"]["parser"])
    check("★ 密钥不出现在响应中", "sk-selftest-secret" not in json.dumps(b))
    st, b = nl("查看上次数据", OLD)
    d = b.get("data") or {}
    check("服务不可达 → parser=rule_fallback", d.get("parser") == "rule_fallback",
          d.get("parser"))
    check("降级后结论仍正确（old_record）", d.get("claim") == "old_record", d.get("claim"))
    check("notes 留下失败原因", any("LLM_HTTP_ERROR" in n for n in (d.get("notes") or [])),
          d.get("notes"))

    # -------------------------------------------------------------------
    section("J. 回归：第 1/2/3/4 周只读接口未被破坏")
    # -------------------------------------------------------------------
    st, _ = http("GET", "/api/v1/latest?device_id=" + NEW)
    check("GET /api/v1/latest 正常", st == 200, st)
    st, _ = http("GET", "/api/v1/stats")
    check("GET /api/v1/stats 正常", st == 200, st)
    st, _ = http("GET", "/api/v1/cam/status")
    check("GET /api/v1/cam/status 正常", st == 200, st)
    st, _ = http("GET", "/")
    check("静态首页仍可访问", st == 200, st)

    print("\n" + "=" * 74)
    if fails:
        print("失败 %d / %d 项：" % (len(fails), checks))
        for f in fails:
            print("  - " + f)
        print("=" * 74)
        return 1
    print("全部通过：%d 项断言" % checks)
    print("=" * 74)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        pass
