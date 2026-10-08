#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_rate.py —— 从串口日志统计"真实采样节奏"与"每包上传开销"。

为什么用板端的 up= 字段而不是主机时间算采样间隔：
  串口是 USB-Serial-JTAG，宿主机读取时会把若干行攒在一起，主机时间戳会"成堆"
  出现；而 [DATA] 行里的 up=NNNNms 是开发板自己的单调时钟，反映的是**真实的
  采样时刻**，用它算出来的间隔才是数据密度本身。

统计内容：
  1. 采样节奏：相邻采样点的 up= 间隔（板端设定 100ms → 应集中在 90~110ms）
  2. 实际上报：每包几个点、包间隔、一包上传耗时（[DATA] 末点 → [POST]）
  3. 错误行：上传失败 / 重连 / 非 2xx 等如实列出
  4. 结论：真实采样 Hz、HTTPS 请求频率（数据密度与请求频率是两件事）

用法:
    python tools/analyze_rate.py evidence/batch10hz_run.log
    python tools/analyze_rate.py evidence/batch10hz_run.log --sample-ms 100
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import statistics
import sys

RE_LINE_TS = re.compile(r"\[(\d{4}-\d\d-\d\dT[\d:.]+)\]")
RE_DATA = re.compile(
    r"main: \[DATA\] dev=\S+ boot=(?P<boot>\d+) seq=(?P<seq>\d+) "
    r"idx=(?P<idx>\d+)/(?P<batch>\d+) up=(?P<up>\d+)ms")
# [POST] seq=12..14 n=30 http=201 ... / [POST] seq=12 n=10 http=201 ...
RE_POST = re.compile(r"main: \[POST\] seq=(?P<seq>\d+)(?:\.\.(?P<seq2>\d+))?"
                     r"(?: n=(?P<n>\d+))? http=(?P<http>-?\d+)")
RE_ERR = re.compile(r"上传失败|重建连接|非 2xx|超时|失败")


def host_ts(line: str):
    m = RE_LINE_TS.search(line)
    if not m:
        return None
    try:
        return dt.datetime.fromisoformat(m.group(1))
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?", default=r"D:\ESP\evidence\batch10hz_run.log")
    ap.add_argument("--sample-ms", type=int, default=100, help="板端设定的采样间隔(ms)")
    ap.add_argument("--upload-ms", type=int, default=1000, help="板端设定的上传周期(ms)")
    a = ap.parse_args()

    lines = open(a.log, encoding="utf-8", errors="replace").read().splitlines()

    data = []           # [(host_ts, up_ms, boot, seq, idx, batch)]
    posts = []          # [(host_ts, seq_first, seq_last, n, http)]
    errs = []
    for l in lines:
        m = RE_DATA.search(l)
        if m:
            data.append((host_ts(l), int(m.group("up")), int(m.group("boot")),
                         int(m.group("seq")), int(m.group("idx")), int(m.group("batch"))))
            continue
        m = RE_POST.search(l)
        if m:
            posts.append((host_ts(l), int(m.group("seq")),
                          int(m.group("seq2")) if m.group("seq2") else int(m.group("seq")),
                          int(m.group("n")) if m.group("n") else None,
                          int(m.group("http"))))
            continue
        if RE_ERR.search(l):
            errs.append(l.strip()[:140])

    print("=" * 84)
    print("串口日志分析:", a.log)
    print("=" * 84)
    print("[DATA] 采样点 %d 条；[POST] 上报 %d 次；可疑错误行 %d 条" %
          (len(data), len(posts), len(errs)))
    if not data:
        print("没有解析到 [DATA] 行")
        return 2

    # ---- 1. 采样节奏（板端单调时钟 up=，最能反映真实数据密度）----
    ups = [(d[1], d[2], d[3]) for d in data]     # (up_ms, boot, seq)
    iv = []
    for (u1, b1, s1), (u2, b2, s2) in zip(ups, ups[1:]):
        if b1 != b2:                              # 跨重启，不参与统计
            continue
        d = (u2 - u1) / 1000.0
        if 0 < d < 60:
            iv.append(d)
    if iv:
        near = sum(1 for x in iv if a.sample_ms * 0.9 / 1000 <= x <= a.sample_ms * 1.1 / 1000)
        print("-" * 84)
        print("① 采样节奏（板端 up= 单调时钟，设定 %d ms 一个点）:" % a.sample_ms)
        print("   中位 %.1f ms   均值 %.1f ms   最小 %.1f ms   最大 %.1f ms" %
              (statistics.median(iv) * 1000, sum(iv) / len(iv) * 1000,
               min(iv) * 1000, max(iv) * 1000))
        print("   落在 ±10%% 区间内的比例: %.1f%%   → 真实采样率 %.2f Hz" %
              (100 * near / len(iv), 1.0 / (sum(iv) / len(iv))))

    # ---- 2. 每次上报的点数与包间隔 ----
    print("-" * 84)
    sizes = {}
    for _, s1, s2, n, _ in posts:
        sizes[n if n is not None else (s2 - s1 + 1)] = \
            sizes.get(n if n is not None else (s2 - s1 + 1), 0) + 1
    print("② 每次上报的点数分布: %s" %
          ", ".join("%s 点 × %d 次" % (k, v) for k, v in sorted(sizes.items())))
    pt = [p[0] for p in posts if p[0]]
    if len(pt) > 1:
        pg = [(b - x).total_seconds() for x, b in zip(pt, pt[1:])]
        pg = [x for x in pg if 0 < x < 300]
        if pg:
            print("   包间隔: 中位 %.2f s  最小 %.2f s  最大 %.2f s（设定 %d ms）" %
                  (statistics.median(pg), min(pg), max(pg), a.upload_ms))
            print("   实际上报频率: %.2f 次/秒（HTTPS 请求频率，与数据密度是两件事）" %
                  (1.0 / (sum(pg) / len(pg))))

    # ---- 3. 每包上传耗时：包内最后一个 [DATA] 的主机时间 → [POST] 的主机时间 ----
    print("-" * 84)
    costs = []
    for (pts, s1, s2, n, http) in posts:
        if pts is None:
            continue
        # 找该包（seq 落在 [s1,s2]）里最后一个有主机时间的 [DATA]
        cand = [d[0] for d in data if s1 <= d[3] <= s2 and d[0] is not None and d[0] <= pts]
        if not cand:
            continue
        costs.append((pts - max(cand)).total_seconds())
    if costs:
        print("③ 每包上传耗时（板端采样完成 → 服务器响应到板端）:")
        print("   中位 %.2f s  最小 %.2f s  最大 %.2f s  平均 %.2f s" %
              (statistics.median(costs), min(costs), max(costs), sum(costs) / len(costs)))
    bad = [p for p in posts if p[4] != 201]
    print("   HTTP 201 成功 %d 次；非 201 %d 次" % (len(posts) - len(bad), len(bad)))

    # ---- 4. 错误行 ----
    if errs:
        print("-" * 84)
        print("④ 如实记录的错误行（不做任何修补）:")
        for e in errs[:10]:
            print("   ", e)

    print("-" * 84)
    print("结论: 数据密度 %.2f Hz，每包最多 %s 个点，HTTPS 请求约 %.2f 次/秒" % (
        1.0 / (sum(iv) / len(iv)) if iv else 0,
        max(sizes) if sizes else "?",
        1.0 / (sum(pg) / len(pg)) if len(pt) > 1 and pg else 0))
    print("=" * 84)
    return 0


if __name__ == "__main__":
    sys.exit(main())