#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_db_rate.py —— 用**数据库本身**当观察者，统计真实采集/入库速率。

为什么要有这个工具：
  * 抓串口会让 ESP32-S3 复位（打开 USB-Serial-JTAG 就会复位），
    所以"板子有没有在稳定上传、数据密度到底是多少"最好从后端库里看。
  * 这个工具只读数据库（sqlite 只读模式），零干扰、可反复跑。

统计口径（全部来自真实落库的数据，不做任何推算填充）：
  1. 包（一次 POST）的数量、大小分布、相邻包的上传间隔
  2. 采样点间隔分布 —— 板端设定 100ms 一个点时，这里应该集中在 90~110ms
  3. 实存采样率 = 采样点数 / 时间跨度（Hz）
  4. 每个 boot_count 的运行时长（出现很多短命 boot 就是"重启循环"）
  5. 最大的几个空洞（网络/供电异常造成的真实缺口，如实列出）

用法：
    python tools/analyze_db_rate.py                        # 默认看最近 5 分钟
    python tools/analyze_db_rate.py --minutes 30
    python tools/analyze_db_rate.py --db D:\\ESP\\backend\\data\\sensor.db --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import statistics
import sys

DEFAULT_DB = r"D:\ESP\backend\data\sensor.db"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def parse_ts(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    try:
        return dt.datetime.strptime(text[:23], "%Y-%m-%dT%H:%M:%S.%f")
    except ValueError:
        try:
            return dt.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("SENSOR_DB", DEFAULT_DB))
    ap.add_argument("--minutes", type=float, default=5.0,
                    help="统计最近多少分钟（按服务器接收时间）")
    ap.add_argument("--expect-hz", type=float, default=10.0,
                    help="板端设定的采样率，用于对比（默认 10Hz = 100ms 一个点）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（便于脚本消费）")
    a = ap.parse_args()

    if not os.path.exists(a.db):
        print("数据库不存在:", a.db)
        return 2

    # 只读打开：绝不修改任何数据
    conn = sqlite3.connect("file:%s?mode=ro" % a.db.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) AS n FROM sensor_records").fetchone()["n"]
    since = (now_utc() - dt.timedelta(minutes=a.minutes)).strftime("%Y-%m-%dT%H:%M:%S")

    rows = conn.execute(
        """SELECT id, boot_count, seq, sample_idx, sample_count, sample_period_ms,
                  ts_local, received_at_utc, magnitude_g, verify_ok
             FROM sensor_records
            WHERE received_at_utc >= ?
         ORDER BY id ASC""", (since,)).fetchall()
    conn.close()

    out: dict = {
        "db": a.db, "db_total_rows": total, "window_minutes": a.minutes,
        "window_start_utc": since, "rows_in_window": len(rows),
    }

    if not rows:
        print("最近 %.1f 分钟内没有任何记录（板子没在上传，或后端刚重启）" % a.minutes)
        print("历史总行数:", total)
        return 1

    # ---- 1. 按 (boot_count, seq) 分组 = 一次 POST 一个包 ----
    packets: list[dict] = []
    cur_key = None
    for r in rows:
        key = (r["boot_count"], r["seq"])
        if key != cur_key:
            packets.append({"key": key, "n": 0, "last_recv": r["received_at_utc"],
                            "idx_min": None, "idx_max": None})
            cur_key = key
        p = packets[-1]
        p["n"] += 1
        p["last_recv"] = r["received_at_utc"]
        if r["sample_idx"] is not None:
            p["idx_min"] = r["sample_idx"] if p["idx_min"] is None else min(p["idx_min"], r["sample_idx"])
            p["idx_max"] = r["sample_idx"] if p["idx_max"] is None else max(p["idx_max"], r["sample_idx"])

    pkt_times = [parse_ts(p["last_recv"]) for p in packets]
    pkt_gaps = [(pkt_times[i + 1] - pkt_times[i]).total_seconds()
                for i in range(len(pkt_times) - 1)]
    out["packets"] = len(packets)
    out["packet_sizes"] = sorted({p["n"] for p in packets})
    if pkt_gaps:
        out["packet_gap_median_s"] = round(statistics.median(pkt_gaps), 3)
        out["packet_gap_min_s"] = round(min(pkt_gaps), 3)
        out["packet_gap_max_s"] = round(max(pkt_gaps), 3)

    # ---- 2. 采样点间隔（用板端本地时间戳，最能反映真实采集节奏）----
    ts = [parse_ts(r["ts_local"]) for r in rows]
    ts = [t for t in ts if t]
    gaps = [(ts[i + 1] - ts[i]).total_seconds() for i in range(len(ts) - 1)]
    gaps = [g for g in gaps if 0 < g < 60]          # 跨重启/跨空洞的间隔单独统计
    if gaps:
        out["sample_gap_median_ms"] = round(statistics.median(gaps) * 1000, 1)
        out["sample_gap_mean_ms"] = round(sum(gaps) / len(gaps) * 1000, 1)
        out["pct_90_110ms"] = round(100 * sum(1 for g in gaps if 0.090 <= g <= 0.110) / len(gaps))

    # ---- 3. 实存采样率 ----
    all_ts = [parse_ts(r["received_at_utc"]) for r in rows]
    all_ts = [t for t in all_ts if t]
    span = (all_ts[-1] - all_ts[0]).total_seconds()
    out["span_s"] = round(span, 1)
    out["stored_hz"] = round((len(rows) - 1) / span, 2) if span > 0 else None
    out["expect_hz"] = a.expect_hz

    # ---- 4. 每个 boot 的运行时长（重启循环一眼可见）----
    boots: dict[int, list[dt.datetime]] = {}
    for r, t in zip(rows, all_ts):
        boots.setdefault(r["boot_count"], []).append(t)
    out["boots"] = [
        {"boot": b, "runtime_s": round((v[-1] - v[0]).total_seconds(), 1),
         "first_utc": v[0].strftime("%H:%M:%S"), "rows": len(v)}
        for b, v in sorted(boots.items(), key=lambda kv: kv[1][0])
    ]

    # ---- 5. 最大的空洞 ----
    out["worst_gaps_s"] = [round(g, 1) for g in sorted(pkt_gaps, reverse=True)[:5]] if pkt_gaps else []

    # ---- 6. 校验字段 ----
    vok = sum(1 for r in rows if r["verify_ok"] == 1)
    vbad = sum(1 for r in rows if r["verify_ok"] == 0)
    out["verify_ok_rows"] = vok
    out["verify_bad_rows"] = vbad

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print("=" * 78)
    print("数据库入库速率分析（只读，零干扰）  库: %s" % a.db)
    print("库内总行数 %d；本窗口: 最近 %.1f 分钟（%s UTC 起）" %
          (total, a.minutes, since))
    print("=" * 78)
    print("本窗口采样点 : %d 行" % len(rows))
    print("本窗口数据包 : %d 个（= 一次 POST 一个包）" % len(packets))
    print("包大小分布   : %s 点/包" % out["packet_sizes"])
    if pkt_gaps:
        print("包间隔       : 中位 %.2fs  最小 %.2fs  最大 %.2fs" %
              (out["packet_gap_median_s"], out["packet_gap_min_s"], out["packet_gap_max_s"]))
    if gaps:
        print("采样点间隔   : 中位 %.1fms  均值 %.1fms  落在 90~110ms 的比例 %.0f%%" %
              (out["sample_gap_median_ms"], out["sample_gap_mean_ms"], out["pct_90_110ms"]))
    print("时间跨度     : %.1fs" % out["span_s"])
    print("实存采样率   : %s Hz （板端设定 %.1f Hz）" % (out["stored_hz"], a.expect_hz))
    print("后端换算校验 : 通过 %d 行 / 异常 %d 行" % (vok, vbad))
    if out["worst_gaps_s"]:
        print("最大的几个空洞: %s 秒（真实缺口，未做任何补造）" %
              ", ".join(str(g) for g in out["worst_gaps_s"]))
    print("-" * 78)
    print("各次开机(boot_count)在本窗口内的情况：")
    for b in out["boots"]:
        flag = "  <-- 短命, 疑似重启" if b["runtime_s"] < 60 else ""
        print("   boot %-5s 运行 %6.1fs  %4d 行%s" % (b["boot"], b["runtime_s"], b["rows"], flag))
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())