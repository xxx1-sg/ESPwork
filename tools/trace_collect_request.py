# -*- coding: utf-8 -*-
"""打印一次真实 completed 请求的完整状态迁移轨迹（用于核对状态图与实现一致）。"""
import json
import sqlite3

c = sqlite3.connect(r"D:\ESP\backend\data\sensor.db")
c.row_factory = sqlite3.Row

r = c.execute("SELECT * FROM collect_requests WHERE status='completed' "
              "ORDER BY completed_at_utc DESC LIMIT 1").fetchone()
if not r:
    print("没有 completed 的请求")
    raise SystemExit(0)

print("request_id :", r["request_id"])
print("status     :", r["status"], "(终态)")
print("submitted  :", r["submitted_at_utc"], " <-- submitted")
print("dispatched :", r["dispatched_at_utc"], " <-- 设备 poll 取走指令")
print("acked      :", r["acked_at_utc"], " <-- acked (ack_type=received)")
print("completed  :", r["completed_at_utc"], " <-- completed (L1^L2^L3 全真)")
print("baseline   : boot=%s seq=%s up=%s" % (
    r["baseline_boot_count"], r["baseline_seq_last"], r["baseline_uptime_ms"]))
print("attempt    :", r["attempt"])
print("failure    :", r["failure_reason"])

print()
print("request_acks:")
for a in c.execute("SELECT ack_type, ack_at_utc, attempt, boot_count, device_uptime_ms "
                   "FROM request_acks WHERE request_id=?", (r["request_id"],)):
    print("   ", dict(a))

print()
print("observations:")
for o in c.execute("SELECT seq_first, seq_last, sample_count, freshness, verify_ok, "
                   "record_id, upload_id, value_json FROM observations WHERE request_id=?",
                   (r["request_id"],)):
    d = dict(o)
    v = json.loads(d.pop("value_json"))
    print("   ", d)
    print("    value =", v)

print()
print("sensor_records (绑定行):")
for s in c.execute("SELECT id, request_id, report_mode, seq, raw_x, raw_y, raw_z, "
                   "magnitude_g, verify_ok FROM sensor_records WHERE request_id=?",
                   (r["request_id"],)):
    print("   ", dict(s))

print()
print("raw_uploads (逐字节原文):")
for u in c.execute("SELECT u.id, u.byte_len, u.sha256, substr(u.body,1,90) b "
                   "FROM raw_uploads u JOIN observations o ON o.upload_id=u.id "
                   "WHERE o.request_id=?", (r["request_id"],)):
    print("   ", dict(u))

# 三表时间一致性
from datetime import datetime
def t(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ")
print()
print("时间顺序 提交<=下发<=回执<=完成 :",
      t(r["submitted_at_utc"]) <= t(r["dispatched_at_utc"]) <=
      t(r["acked_at_utc"]) <= t(r["completed_at_utc"]))
c.close()