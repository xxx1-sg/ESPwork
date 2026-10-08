#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_evidence.py —— 把数据库里**真实采集到的记录**导出成可直接放进文档的证据材料。

产出：
  evidence/records.csv                 全部记录（可用 Excel 打开）
  evidence/records.md                  前 N 条记录的 Markdown 表格
  evidence/raw_uploads.jsonl           每条上传的原始报文（一行一条，原样）
  evidence/summary.json                统计摘要（条数、时间范围、合矢量分布、校验结论）

注意：本脚本只做「读取 + 格式化」，不修改、不插值、不补任何数据。
      数据库为空时会直接报错退出，不会生成任何示例数据。
"""

import argparse
import csv
import json
import os
import sqlite3
import sys

COLS = ["id", "device_id", "boot_count", "seq", "uptime_ms", "ts_local",
        "received_at_utc", "sensor_model", "range_g", "lsb_per_g", "oversample",
        "raw_x", "raw_y", "raw_z", "gx", "gy", "gz", "ms2x", "ms2y", "ms2z",
        "magnitude_g", "verify_ok", "verify_note"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 "..", "backend", "data", "sensor.db"))
    ap.add_argument("--out-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                      "..", "evidence"))
    ap.add_argument("--md-rows", type=int, default=25, help="Markdown 表格里放多少条")
    a = ap.parse_args()

    if not os.path.isfile(a.db):
        print("[错误] 数据库不存在: %s" % a.db)
        return 2

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM sensor_records ORDER BY id").fetchall()
    raw_rows = conn.execute("SELECT * FROM raw_uploads ORDER BY id").fetchall()
    conn.close()

    if not rows:
        print("[错误] 数据库里没有任何真实记录（设备还没上传过），拒绝生成空样例。")
        return 2

    os.makedirs(a.out_dir, exist_ok=True)

    # ---- 1) CSV ----
    csv_path = os.path.join(a.out_dir, "records.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        for r in rows:
            w.writerow([r[c] for c in COLS])

    # ---- 2) 原始报文 JSONL（逐字节原样） ----
    raw_path = os.path.join(a.out_dir, "raw_uploads.jsonl")
    with open(raw_path, "w", encoding="utf-8") as f:
        for r in raw_rows:
            f.write(json.dumps({
                "upload_id": r["id"], "received_at_utc": r["received_at_utc"],
                "remote_addr": r["remote_addr"], "byte_len": r["byte_len"],
                "sha256": r["sha256"], "body": r["body"],
            }, ensure_ascii=False) + "\n")

    # ---- 3) Markdown 表格 ----
    md_path = os.path.join(a.out_dir, "records.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("| # | seq | 原始码值 raw (x,y,z) | 加速度 g (x,y,z) | \|a\| (g) | 设备本地时间戳 | 服务器接收时间 |\n")
        f.write("|---|---|---|---|---|---|---|\n")
        for r in rows[:a.md_rows]:
            f.write("| %d | %s | (%s, %s, %s) | (%.4f, %.4f, %.4f) | %.4f | %s | %s |\n" % (
                r["id"], r["seq"], r["raw_x"], r["raw_y"], r["raw_z"],
                r["gx"], r["gy"], r["gz"], r["magnitude_g"],
                r["ts_local"] or "未对时", r["received_at_utc"]))

    # ---- 4) 统计摘要（全部来自真实数据） ----
    mags = [r["magnitude_g"] for r in rows if r["magnitude_g"] is not None]
    gxs = [r["gx"] for r in rows if r["gx"] is not None]
    gys = [r["gy"] for r in rows if r["gy"] is not None]
    gzs = [r["gz"] for r in rows if r["gz"] is not None]

    def stat(v):
        if not v:
            return None
        return {"min": round(min(v), 6), "max": round(max(v), 6),
                "mean": round(sum(v) / len(v), 6), "n": len(v)}

    summary = {
        "source_db": os.path.abspath(a.db),
        "records": len(rows),
        "raw_uploads": len(raw_rows),
        "device_ids": sorted({r["device_id"] for r in rows}),
        "sensor_models": sorted({r["sensor_model"] for r in rows if r["sensor_model"]}),
        "range_g": sorted({r["range_g"] for r in rows if r["range_g"] is not None}),
        "lsb_per_g": sorted({r["lsb_per_g"] for r in rows if r["lsb_per_g"] is not None}),
        "first_received_at_utc": rows[0]["received_at_utc"],
        "last_received_at_utc": rows[-1]["received_at_utc"],
        "first_ts_local": rows[0]["ts_local"],
        "last_ts_local": rows[-1]["ts_local"],
        "verify_ok_true": sum(1 for r in rows if r["verify_ok"] == 1),
        "verify_ok_false": sum(1 for r in rows if r["verify_ok"] == 0),
        "magnitude_g": stat(mags),
        "gx": stat(gxs), "gy": stat(gys), "gz": stat(gzs),
        "note": "全部数值直接来自数据库，未做任何插值/补齐；raw_uploads.jsonl 为原始上传报文",
    }
    sum_path = os.path.join(a.out_dir, "summary.json")
    with open(sum_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("已导出：")
    print("  ", csv_path, "(%d 条)" % len(rows))
    print("  ", raw_path, "(%d 条原始报文)" % len(raw_rows))
    print("  ", md_path)
    print("  ", sum_path)
    print()
    print("统计摘要：")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("note",)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())