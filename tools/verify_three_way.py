#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_three_way.py —— 三方核对工具（本项目的验收核心）

三方 = ① 开发板串口打印的硬件实测值（[DATA] 行）
       ② 数据库里原样存储的记录（sensor_records / raw_uploads）
       ③ 后端 API（网页读的同一接口）

第四份旁证：串口里的 [POST] 行 —— 板子把服务器**回显的入库结果**打了出来，
            它等于「服务器当时到底存了什么」，可以和 ①②③ 交叉验证。

为什么用 (boot_count, seq, sample_idx) 做键：
  1) 开发板每次重启，seq 都从 1 重新开始；只按 seq 匹配会把不同次启动的数据
     串到一起，得出"数据不一致"的错误结论。真实日志里确实出现过
     boot=10 seq=3 与 boot=11 seq=3 同时存在的情况，所以必须带 boot。
  2) 固件现在是"100ms 采一个点、攒够整包上报"，而且网络慢时会把几个包
     合并成一个大包。所以同一个 seq 不再等于一包，必须再带包内序号
     sample_idx（串口 [DATA] 里的 idx=K/N）才能一一对应。

用法:
    python tools/verify_three_way.py --serial evidence/serial_upload.log \
        --db backend/data/sensor.db --api http://127.0.0.1:8000 \
        --out evidence/verify_report.json

判定:
    任一方缺失、或数值不一致 → FAIL，并指出差异出现在哪一方。
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                               # noqa: BLE001
    pass

# 串口 [DATA] 行（格式与 main.c 的打印严格对应）
#   idx=K/N 是包内序号，三方核对时与数据库 sample_idx 一一对应
RE_DATA = re.compile(
    r"\[DATA\] dev=(?P<dev>\S+) boot=(?P<boot>\d+) seq=(?P<seq>\d+) "
    r"idx=(?P<idx>\d+)/(?P<batch>\d+) up=(?P<up>\d+)ms "
    r"ts=(?P<ts>\S+) "
    r"raw=\((?P<rx>-?\d+),(?P<ry>-?\d+),(?P<rz>-?\d+)\) "
    r"g=\((?P<gx>-?\d+\.\d+),(?P<gy>-?\d+\.\d+),(?P<gz>-?\d+\.\d+)\) "
    r"ms2=\((?P<mx>-?\d+\.\d+),(?P<my>-?\d+\.\d+),(?P<mz>-?\d+\.\d+)\) "
    r"\|a\|=(?P<mag>-?\d+\.\d+)g"
)

# 串口 [POST] 行里的服务器回显。
#   新格式: [POST] seq=12..14 n=30 http=201 resp={...}   （网络慢时合并上传）
#   旧格式: [POST] seq=12 n=10 http=201 resp={...}
RE_POST = re.compile(
    r"\[POST\] seq=(?P<seq>\d+)(?:\.\.(?P<seq2>\d+))?"
    r"(?: n=(?P<n>\d+))? http=(?P<http>\d+) resp=(?P<json>\{.*\})"
)

TOL_G = 6e-4          # g 值容差（串口与报文都保留 3 位小数以上）
TOL_MS2 = 5.1e-3      # m/s^2：串口日志只印 2 位小数，容差必须覆盖 0.005 的显示误差
TOL_MAG = 6e-4
TOL_RAW = 0.5         # 原始码值是整数，允许 0.5 的抗浮点误差
TOL_TS_MS = 5         # 时间戳：容差 5ms（新固件已做到完全一致，此处只作为提示）


def parse_post_json(text):
    """解析 [POST] 行里的 JSON 回显。

    串口是 USB-Serial-JTAG，偶尔会把两行输出揉在一起，导致整段 JSON 不完整。
    这时退化为「只把 stored 子对象扣出来解析」，保证核心证据（服务器实际入库的
    数值）仍然可取到，而不是整条 [POST] 记录被丢掉。
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    i = text.find('"stored"')
    if i < 0:
        return None
    j = text.find("{", i)
    if j < 0:
        return None
    depth = 0
    for k in range(j, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                try:
                    stored = json.loads(text[j:k + 1])
                except json.JSONDecodeError:
                    return None
                out = {"stored": stored}
                for key in ("id", "received_at_utc", "raw_sha256", "ok",
                            "seq", "sample_idx", "stored_count"):
                    m = re.search(r'"%s":\s*("?[^,}]+)' % key, text)
                    if m:
                        out[key] = m.group(1).strip('"')
                return out
    return None


def parse_serial(path):
    """提取 [DATA]（板端实测）与 [POST]（服务器回显）两类证据。"""
    data, posts = {}, []
    skipped = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = RE_DATA.search(line)
            if m:
                d = m.groupdict()
                key = (int(d["boot"]), int(d["seq"]), int(d["idx"]))
                data[key] = {
                    "device_id": d["dev"],
                    "boot_count": int(d["boot"]),
                    "seq": int(d["seq"]),
                    "sample_idx": int(d["idx"]),
                    "uptime_ms": int(d["up"]),
                    "ts_local": d["ts"],
                    "raw": {"x": int(d["rx"]), "y": int(d["ry"]), "z": int(d["rz"])},
                    "accel_g": {"x": float(d["gx"]), "y": float(d["gy"]), "z": float(d["gz"])},
                    "accel_ms2": {"x": float(d["mx"]), "y": float(d["my"]), "z": float(d["mz"])},
                    "magnitude_g": float(d["mag"]),
                }
                continue
            m = RE_POST.search(line)
            if m:
                body = parse_post_json(m.group("json"))
                if body is None:
                    skipped += 1
                    continue
                g = m.groupdict()
                posts.append({
                    "seq": int(g["seq"]),
                    "seq_last": int(g["seq2"]) if g["seq2"] else int(g["seq"]),
                    "n": int(g["n"]) if g["n"] else None,
                    "http": int(g["http"]),
                    "resp": body,
                })
    return data, posts, skipped


def db_rows(db_path):
    """读取数据库。返回 (记录列表, 错误信息)。

    旧固件的记录 sample_idx 为 NULL（那时一个 seq 就是一条记录），
    这里如实反映成 sample_idx=None，不假装它有包内序号。
    """
    if not os.path.isfile(db_path):
        return [], "数据库文件不存在: %s" % db_path
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM sensor_records ORDER BY id").fetchall()
    except sqlite3.Error as e:
        conn.close()
        return [], "读取数据库失败: %s" % e
    conn.close()

    out = []
    for r in rows:
        keys = r.keys()
        out.append({
            "id": r["id"],
            "device_id": r["device_id"],
            "boot_count": r["boot_count"],
            "seq": r["seq"],
            "sample_idx": r["sample_idx"] if "sample_idx" in keys else None,
            "sample_count": r["sample_count"] if "sample_count" in keys else None,
            "sample_period_ms": r["sample_period_ms"] if "sample_period_ms" in keys else None,
            "uptime_ms": r["uptime_ms"],
            "ts_local": r["ts_local"],
            "ts_utc_ms": r["ts_utc_ms"],
            "received_at_utc": r["received_at_utc"],
            "raw": {"x": r["raw_x"], "y": r["raw_y"], "z": r["raw_z"]},
            "accel_g": {"x": r["gx"], "y": r["gy"], "z": r["gz"]},
            "accel_ms2": {"x": r["ms2x"], "y": r["ms2y"], "z": r["ms2z"]},
            "magnitude_g": r["magnitude_g"],
            "verify_ok": r["verify_ok"],
            "verify_note": r["verify_note"],
        })
    return out, None


def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "verify_three_way/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def close(a, b, tol):
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


def ts_to_ms(text):
    """把 2026-09-30T09:02:45.151+0800 解析成 epoch 毫秒；失败返回 None。"""
    if not text or text in ("未对时", "null"):
        return None
    from datetime import datetime
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return int(datetime.strptime(text, fmt).timestamp() * 1000)
        except ValueError:
            continue
    return None


def cmp_sides(labels, recs, tol_g=TOL_G):
    """按字段比对若干方（每方一个 dict），返回差异列表。"""
    diffs = []

    def chk(name, getter, tol):
        vals = [getter(r) if r else None for r in recs]
        missing = [labels[i] for i, v in enumerate(vals) if v is None]
        if missing:
            diffs.append("%s 在 %s 缺失" % (name, "/".join(missing)))
            return
        base = vals[0]
        for i in range(1, len(vals)):
            other = vals[i]
            if name.startswith("raw"):
                ok = close(base, other, TOL_RAW)
            else:
                ok = close(base, other, tol)
            if not ok:
                diffs.append("%s 不一致: %s" % (
                    name, "  ".join("%s=%s" % (labels[j], vals[j]) for j in range(len(vals)))))

    for axis in ("x", "y", "z"):
        chk("raw.%s" % axis, lambda r, a=axis: r["raw"][a], TOL_RAW)
    for axis in ("x", "y", "z"):
        chk("accel_g.%s" % axis, lambda r, a=axis: r["accel_g"][a], tol_g)
    for axis in ("x", "y", "z"):
        chk("accel_ms2.%s" % axis, lambda r, a=axis: r["accel_ms2"][a], TOL_MS2)
    chk("magnitude_g", lambda r: r["magnitude_g"], TOL_MAG)
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", required=True, help="串口日志文件路径")
    ap.add_argument("--db", required=True, help="SQLite 数据库路径")
    ap.add_argument("--api", default="http://127.0.0.1:8000", help="后端地址")
    ap.add_argument("--out", default=None, help="核对报告输出 JSON")
    ap.add_argument("--max", type=int, default=30, help="最多核对多少条")
    a = ap.parse_args()

    serial_data, posts, post_broken = parse_serial(a.serial)
    db_list, db_err = db_rows(a.db)
    if db_err:
        print("[错误]", db_err)
        return 2
    if not serial_data:
        print("[错误] 串口日志里没有解析到 [DATA] 行")
        return 2

    # 数据库按 (boot, seq, sample_idx) 建索引；旧固件记录 sample_idx 为 None，
    # 它们不会与带 idx 的串口行匹配上（这是对的：那时一个 seq 就是一条记录）
    db = {(r["boot_count"], r["seq"], r["sample_idx"]): r for r in db_list}
    legacy = sum(1 for r in db_list if r["sample_idx"] is None)
    keys = sorted(set(serial_data) & set(db))[-a.max:]

    print("=" * 112)
    print("三方核对：① 串口 [DATA]   ② 数据库 sensor_records   ③ 后端 API（网页同源）")
    print("       旁证：串口 [POST] 行里的服务器回显 = 服务器当时实际入库内容")
    print("=" * 112)
    print("串口解析到 %d 条 [DATA]、%d 条 [POST] 回显（%d 条因串口粘行无法解析）"
          % (len(serial_data), len(posts), post_broken))
    print("数据库 %d 条（其中旧固件无包内序号的记录 %d 条，不参与 idx 匹配）" % (len(db_list), legacy))
    print("共同 (boot, seq, sample_idx) %d 组" % len(keys))
    print("-" * 112)

    report = {"serial_file": a.serial, "db_file": a.db, "api": a.api,
              "checked": [], "passed": 0, "failed": 0}

    # ---------- 逐采样点核对 ----------
    for key in keys:
        boot, seq, idx = key
        s = serial_data[key]
        d = db[key]

        # 对应的 [POST] 回显：一次 POST 可能合并了多个 seq，回显的是"最后一个采样点"。
        # 先看回显自带的 (seq, sample_idx)，没有就退化为按 raw 码值匹配。
        echo = None
        for p in posts:
            stored = (p["resp"].get("stored") or {})
            raw = stored.get("raw") or {}
            if not raw:
                continue
            e_seq = p["resp"].get("seq")
            e_idx = p["resp"].get("sample_idx")
            try:
                e_seq = int(e_seq) if e_seq is not None else None
                e_idx = int(e_idx) if e_idx is not None else None
            except (TypeError, ValueError):
                e_seq = e_idx = None
            if e_seq is not None and e_idx is not None:
                if (e_seq, e_idx) != (seq, idx):
                    continue
            elif not (p["seq"] <= seq <= p["seq_last"]):
                continue
            if all(close(raw.get(k), s["raw"][k], TOL_RAW) for k in ("x", "y", "z")):
                echo = p
                echo["stored"] = stored
                break

        label_map = [("①串口", s), ("②数据库", d)]
        if echo:
            label_map.append(("旁证POST回显", {
                "raw": echo["stored"].get("raw"),
                "accel_g": echo["stored"].get("accel_g"),
                "accel_ms2": echo["stored"].get("accel_ms2"),
                "magnitude_g": echo["stored"].get("magnitude_g"),
            }))
        labels = [x[0] for x in label_map]
        recs = [x[1] for x in label_map]

        diffs = cmp_sides(labels, recs)
        warns = []

        # 时间戳比对：解析成毫秒比，允许 5ms 的采样/打印先后差（记为提示而非失败）
        s_ms, d_ms = ts_to_ms(s["ts_local"]), ts_to_ms(d["ts_local"])
        if s["ts_local"] != d["ts_local"]:
            if s_ms is not None and d_ms is not None and abs(s_ms - d_ms) <= TOL_TS_MS:
                warns.append("ts_local 相差 %d ms（串口=%s 数据库=%s），在容差内"
                             % (abs(s_ms - d_ms), s["ts_local"], d["ts_local"]))
            else:
                diffs.append("ts_local 不一致: ①串口=%s ②数据库=%s"
                             % (s["ts_local"], d["ts_local"]))
        if echo and echo["http"] != 201:
            diffs.append("POST 回显的 HTTP 状态不是 201: %d" % echo["http"])
        if d["verify_ok"] not in (1, True):
            diffs.append("后端自洽校验未通过: %s" % d["verify_note"])

        ok = not diffs
        print("%-20s raw=(%6d,%6d,%6d)  g=(%8.3f,%8.3f,%8.3f)  |a|=%.3f  ts=%s  %s"
              % ("boot%d/seq%d/id%d" % (boot, seq, idx),
                 s["raw"]["x"], s["raw"]["y"], s["raw"]["z"],
                 s["accel_g"]["x"], s["accel_g"]["y"], s["accel_g"]["z"],
                 s["magnitude_g"], s["ts_local"], "一致 ✔" if ok else "不一致 ✘"))
        for msg in diffs:
            print("      └─ %s" % msg)
        for msg in warns:
            print("      └─ 提示: %s" % msg)

        report["checked"].append({
            "boot_count": boot, "seq": seq, "sample_idx": idx, "ok": ok,
            "diffs": diffs, "warnings": warns,
            "serial": s, "db": d,
            "post_echo": ({"http": echo["http"],
                           "stored": echo["stored"],
                           "raw_sha256": echo["resp"].get("raw_sha256"),
                           "received_at_utc": echo["resp"].get("received_at_utc")}
                          if echo else None),
        })
        report["passed" if ok else "failed"] += 1

    # ---------- 第三方：API ----------
    print("-" * 112)
    api_ok = None
    try:
        latest = http_get(a.api.rstrip("/") + "/api/v1/latest")
        rec = latest.get("record") or latest
        latest_db = max(db_list, key=lambda r: r["id"])
        api_diffs = []
        if rec.get("id") != latest_db["id"]:
            api_diffs.append("接口最新 id=%s 与数据库最新 id=%s 不同" % (rec.get("id"), latest_db["id"]))
        # 三轴逐轴比对（raw 用整数容差，accel_g 用 g 容差）
        for axis in ("x", "y", "z"):
            if not close(rec["raw"][axis], latest_db["raw"][axis], TOL_RAW):
                api_diffs.append("接口 raw.%s=%s 与数据库 %s 不一致"
                                 % (axis, rec["raw"][axis], latest_db["raw"][axis]))
            if not close(rec["accel_g"][axis], latest_db["accel_g"][axis], TOL_G):
                api_diffs.append("接口 accel_g.%s=%s 与数据库 %s 不一致"
                                 % (axis, rec["accel_g"][axis], latest_db["accel_g"][axis]))
        # 合矢量是标量，单独比（早先把标量塞进按轴循环里，导致工具自身报错）
        if not close(rec.get("magnitude_g"), latest_db["magnitude_g"], TOL_MAG):
            api_diffs.append("接口 magnitude_g=%s 与数据库 %s 不一致"
                             % (rec.get("magnitude_g"), latest_db["magnitude_g"]))
        if rec.get("ts_local") != latest_db["ts_local"]:
            api_diffs.append("接口 ts_local=%s 与数据库 %s 不同"
                             % (rec.get("ts_local"), latest_db["ts_local"]))
        # 新增字段：网页靠 sample_idx/sample_count 显示"包内第几点"
        for f in ("sample_idx", "sample_count", "sample_period_ms"):
            if rec.get(f) != latest_db.get(f):
                api_diffs.append("接口 %s=%s 与数据库 %s 不同" % (f, rec.get(f), latest_db.get(f)))
        api_ok = not api_diffs
        print("③ 接口 /api/v1/latest -> id=%s boot=%s seq=%s idx=%s/%s device=%s ts=%s  %s"
              % (rec.get("id"), rec.get("boot_count"), rec.get("seq"),
                 rec.get("sample_idx"), rec.get("sample_count"),
                 rec.get("device_id"), rec.get("ts_local"),
                 "与数据库一致 ✔" if api_ok else "不一致 ✘"))
        for msg in api_diffs:
            print("      └─ %s" % msg)

        # /records 抽样：网页初始化曲线用的就是这个接口
        hist = http_get(a.api.rstrip("/") + "/api/v1/records?limit=120")
        rows = hist.get("records") or []
        by_id = {r["id"]: r for r in db_list}
        hist_bad = 0
        for r in rows:
            dbr = by_id.get(r.get("id"))
            if dbr and dbr["raw"] != r.get("raw"):
                hist_bad += 1
        print("③ 接口 /api/v1/records 返回 %d 条，与数据库逐条比对：%s"
              % (len(rows), "全部一致 ✔" if hist_bad == 0 else "%d 条不一致 ✘" % hist_bad))

        # /records?after_id= 增量拉取：网页轮询用的就是它（10Hz 数据不能只拉最新一条）
        inc_ok, inc_note = None, ""
        if rows:
            anchor = rows[len(rows) // 2]["id"]
            inc = http_get(a.api.rstrip("/") + "/api/v1/records?after_id=%d&limit=200" % anchor)
            inc_rows = inc.get("records") or []
            inc_bad = [r for r in inc_rows if r.get("id", 0) <= anchor]
            expect = [r for r in db_list if r["id"] > anchor][:200]
            inc_ok = (not inc_bad) and len(inc_rows) == len(expect) and \
                all(a1["id"] == b1["id"] for a1, b1 in zip(inc_rows, expect))
            inc_note = ("after_id=%d 返回 %d 条，全部 > 锚点且与数据库顺序一致"
                        % (anchor, len(inc_rows))) if inc_ok else \
                       ("after_id=%d 返回 %d 条，期望 %d 条" % (anchor, len(inc_rows), len(expect)))
            print("③ 接口 /api/v1/records?after_id=%d 增量拉取：%s %s"
                  % (anchor, "正确 ✔" if inc_ok else "异常 ✘", inc_note))

        report["api_check"] = {"latest_id": rec.get("id"), "ok": api_ok,
                               "diffs": api_diffs, "records_count": len(rows),
                               "records_mismatch": hist_bad,
                               "after_id_ok": inc_ok, "after_id_note": inc_note}
    except Exception as e:                                      # noqa: BLE001
        print("③ 接口核对失败: %s" % e)
        report["api_check"] = {"ok": False, "error": str(e)}

    print("-" * 112)
    print("核对结论: (boot,seq,sample_idx) 逐采样点核对 通过 %d / 失败 %d；接口一致性 %s"
          % (report["passed"], report["failed"],
             "通过" if api_ok else ("失败" if api_ok is False else "未完成")))
    if not keys:
        print("!! 串口与数据库没有共同的 (boot,seq,sample_idx)：说明日志与数据库不是同一次上传")

    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print("报告已写入:", a.out)

    all_ok = report["failed"] == 0 and api_ok is True and bool(keys)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())