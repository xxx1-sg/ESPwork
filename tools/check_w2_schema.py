# -*- coding: utf-8 -*-
"""检查第 2 周新表/新列是否已按迁移脚本建立（只读）。"""
import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else r"D:\ESP\backend\data\sensor.db"
conn = sqlite3.connect(DB)

tables = sorted(r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"))
print("现有表 (%d):" % len(tables))
for t in tables:
    n = conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
    print("   %-26s %d 行" % (t, n))

need = ["collect_requests", "request_acks", "observations",
        "rejected_observations", "devices", "device_events", "pending_commands"]
print()
print("第 2 周必需表:")
for t in need:
    print("   %-24s %s" % (t, "OK" if t in tables else "缺失!!"))

cols = [r[1] for r in conn.execute("PRAGMA table_info(sensor_records)")]
print()
print("sensor_records 新列 :",
      [c for c in ("request_id", "report_mode") if c in cols] or "缺失!!")
conn.close()