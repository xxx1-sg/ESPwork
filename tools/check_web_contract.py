#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_web_contract.py —— 网页 ↔ 接口 契约自检（不依赖浏览器）

网页"实时显示真实数据"这件事，除了接口能返回数据，还必须保证：
  1. 后端真的把 index.html / app.js / style.css 发出来了（不是 404）；
  2. app.js 里用到的每一个 DOM 元素 id 在 index.html 里都存在
     （少一个就会在页面上抛异常，数值永远不刷新 —— 这类问题光看接口测不出来）；
  3. app.js 读取的每一个接口字段，接口响应里真的有
     （字段名写错同样会导致页面显示 '—'）；
  4. 页面代码里没有"兜底假数值"（例如 || 0.0 之类会把缺失数据显示成 0）。

用法:
    python tools/check_web_contract.py --api http://127.0.0.1:8000 --web web
"""

import argparse
import json
import os
import re
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                               # noqa: BLE001
    pass

RE_DOM = re.compile(r'\$\("([A-Za-z0-9_]+)"\)')


def get(url, raw=False, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "web-contract/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        return data if raw else json.loads(data.decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--web", default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "web"))
    a = ap.parse_args()

    ok = True
    print("=" * 100)
    print("网页 ↔ 接口 契约自检")
    print("=" * 100)

    # ---- 1. 静态资源 ----
    for path, needle in (("/", b"<html"), ("/app.js", b"POLL_MS"),
                         ("/style.css", b"{")):
        try:
            body = get(a.api.rstrip("/") + path, raw=True)
            good = needle in body and len(body) > 50
            print("%-12s %-6s %6d 字节  %s" % (path, "OK" if good else "异常",
                                               len(body), "内容正常 ✔" if good else "内容可疑 ✘"))
            ok = ok and good
        except Exception as e:                                  # noqa: BLE001
            print("%-12s 请求失败: %s" % (path, e))
            ok = False

    # ---- 2. DOM id 完整性 ----
    js = open(os.path.join(a.web, "app.js"), encoding="utf-8").read()
    html = open(os.path.join(a.web, "index.html"), encoding="utf-8").read()
    ids = sorted(set(RE_DOM.findall(js)))
    missing = [i for i in ids if ('id="%s"' % i) not in html]
    print("-" * 100)
    print("app.js 引用 DOM id 共 %d 个；index.html 缺失 %d 个" % (len(ids), len(missing)))
    if missing:
        print("   缺失:", ", ".join(missing))
        ok = False
    else:
        print("   全部存在 ✔")

    # ---- 3. 页面里是否存在"兜底假数值" ----
    suspicious = []
    for fn in ("app.js", "index.html"):
        text = open(os.path.join(a.web, fn), encoding="utf-8").read()
        for m in re.finditer(r"\|\|\s*([0-9]+\.[0-9]+|0)\b", text):
            line = text[:m.start()].count("\n") + 1
            suspicious.append("%s:%d  %s" % (fn, line, m.group(0)))
    print("疑似兜底默认数值 %d 处：%s" % (len(suspicious),
                                         "无 ✔" if not suspicious else ""))
    for s in suspicious:
        print("   ", s)

    # ---- 4. 接口字段 vs app.js 需要 ----
    print("-" * 100)
    latest = get(a.api.rstrip("/") + "/api/v1/latest")
    rec = latest.get("record") or latest
    need = {
        "记录对象": ["device_id", "boot_count", "seq", "uptime_ms", "ts_local",
                     "received_at_utc", "sensor", "unit", "raw", "accel_g",
                     "accel_ms2", "magnitude_g",
                     # 高密度采集新增：包内序号 / 包大小 / 采样间隔
                     "sample_idx", "sample_count", "sample_period_ms"],
    }
    for group, fields in need.items():
        miss = [f for f in fields if f not in rec]
        print("%s 字段 %d 个，缺失 %d 个 %s" % (group, len(fields), len(miss),
                                               "✔" if not miss else "✘ " + ",".join(miss)))
        ok = ok and not miss
    for sub in ("raw", "accel_g", "accel_ms2", "sensor", "unit"):
        if sub in rec:
            print("   %-10s -> %s" % (sub, json.dumps(rec[sub], ensure_ascii=False)))
    if "magnitude_g" in rec:
        print("   %-10s -> %s" % ("magnitude_g", rec["magnitude_g"]))
    if "sample_idx" in rec:
        print("   %-10s -> 包内第 %s/%s 点，采样间隔 %s ms" %
              ("高密度字段", rec.get("sample_idx"), rec.get("sample_count"),
               rec.get("sample_period_ms")))
        ok = ok and isinstance(rec.get("sample_period_ms"), int) and rec["sample_period_ms"] > 0

    hist = get(a.api.rstrip("/") + "/api/v1/records?limit=120")
    rows = hist.get("records") or []
    print("历史曲线接口 /api/v1/records 返回 %d 条（网页初始化曲线用）" % len(rows))
    ok = ok and len(rows) > 0

    # ---- 5. 增量拉取接口（10Hz 数据必须靠它，否则每次只拿到最新一条会丢样本）----
    print("-" * 100)
    if rows:
        anchor = rows[len(rows) // 2]["id"]
        inc = get(a.api.rstrip("/") + "/api/v1/records?after_id=%d&limit=50" % anchor)
        inc_rows = inc.get("records") or []
        ahead = all(r.get("id", 0) > anchor for r in inc_rows)
        asc = all(inc_rows[i]["id"] < inc_rows[i + 1]["id"] for i in range(len(inc_rows) - 1))
        print("增量接口 /api/v1/records?after_id=%d -> %d 条；全部大于锚点 %s；按 id 升序 %s"
              % (anchor, len(inc_rows), "✔" if ahead else "✘", "✔" if asc else "✘"))
        ok = ok and ahead and asc

    stats = get(a.api.rstrip("/") + "/api/v1/stats")
    print("统计接口 /api/v1/stats -> %s" % json.dumps(stats, ensure_ascii=False)[:220])

    print("=" * 100)
    print("契约自检结论:", "通过 ✔" if ok else "存在问题 ✘")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())