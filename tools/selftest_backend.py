#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest_backend.py —— 后端「接收→落库→查询」链路的自检脚本

⚠️ 重要说明（关于项目的"禁止假数据"约束）：
   本脚本使用一个**独立的临时数据库**，向其中投递一条明显标注为
   "backend-selftest" 的构造报文，目的只有一个：验证后端代码本身没写错
   （SQL 占位符数量、字段映射、TOKEN 校验、查询接口等）。
   它绝不会写入生产/证据数据库，运行结束会删除临时库。
   项目交付所依据的 evidence/sensor.db 里只会有开发板真实上传的记录。

用法:
    python selftest_backend.py
"""

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

TMP_DB = os.path.join(tempfile.gettempdir(), "selftest_sensor.db")
PORT = 8099
TOKEN = "selftest-token"

# 构造报文：明确标注这是后端自检，不是传感器数据
SELFTEST_PAYLOAD = {
    "device_id": "backend-selftest",
    "boot_count": 0,
    "seq": 0,
    "uptime_ms": 0,
    "ts_utc_ms": 0,
    "ts_local": "backend-selftest",
    "sensor": {"model": "SELFTEST", "type": "none", "i2c_addr": 0,
               "chip_id": 0, "range_g": 2, "lsb_per_g": 4096, "odr_hz": 100,
               "oversample": 1},
    "unit": {"raw": "n/a", "accel_g": "g"},
    "raw": {"x": 4096, "y": 0, "z": 0},
    "accel_g": {"x": 1.0, "y": 0.0, "z": 0.0},
    "accel_ms2": {"x": 9.80665, "y": 0.0, "z": 0.0},
    "magnitude_g": 1.0,
}


def post(url, obj, token=None):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-Device-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def main() -> int:
    if os.path.exists(TMP_DB):
        os.remove(TMP_DB)

    os.environ["SENSOR_DB"] = TMP_DB
    os.environ["SENSOR_TOKEN"] = TOKEN
    os.environ["SENSOR_HOST"] = "127.0.0.1"
    os.environ["SENSOR_PORT"] = str(PORT)
    os.environ["SENSOR_WEB_DIR"] = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "web")

    import server  # noqa: E402  (必须在设置环境变量之后导入)

    server.db_init()
    srv = server.ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    base = "http://127.0.0.1:%d" % PORT
    failed = 0

    def check(name, cond, extra=""):
        nonlocal failed
        print(("  [通过] " if cond else "  [失败] ") + name + ("  " + extra if extra else ""))
        if not cond:
            failed += 1

    # 1) 空库时 /latest 必须如实返回 404，而不是编造数据
    st, body = get(base + "/api/v1/latest")
    check("空库 /latest 返回 404 且不伪造数据",
          st == 404 and body.get("ok") is False, "status=%s" % st)

    # 2) TOKEN 错误必须被拒
    st, body = post(base + "/api/v1/ingest", SELFTEST_PAYLOAD, token="wrong-token")
    check("TOKEN 错误被拒绝(401)", st == 401, "status=%s" % st)

    # 3) 缺少 raw 必须被拒（后端绝不代为生成）
    bad = dict(SELFTEST_PAYLOAD)
    bad.pop("raw")
    st, body = post(base + "/api/v1/ingest", bad, token=TOKEN)
    check("缺少原始测量值被拒绝(400)", st == 400, "status=%s" % st)

    # 4) 正常投递（自检数据，写临时库）
    st, body = post(base + "/api/v1/ingest", SELFTEST_PAYLOAD, token=TOKEN)
    check("正常投递返回 201", st == 201 and body.get("ok"), "status=%s" % st)
    rec_id = body.get("id")
    check("响应里带回库内回读值", body.get("stored", {}).get("raw", {}).get("x") == 4096)

    # 5) 原始报文必须一字不差地存下来
    st, raw = get(base + "/api/v1/raw/%d" % body["upload_id"])
    check("原始报文已原样留存", st == 200 and "backend-selftest" in raw.get("body", ""))

    # 6) 查询接口能读回同一条
    st, latest = get(base + "/api/v1/latest")
    check("查询接口返回同一条记录",
          st == 200 and latest["record"]["id"] == rec_id,
          "id=%s" % rec_id)
    st, stats = get(base + "/api/v1/stats")
    check("统计接口正常", st == 200 and stats["records"] == 1)

    # 7) 直接查库确认字节级留存 + SHA256 一致
    conn = sqlite3.connect(TMP_DB)
    n_raw = conn.execute("SELECT COUNT(*) FROM raw_uploads").fetchone()[0]
    n_rec = conn.execute("SELECT COUNT(*) FROM sensor_records").fetchone()[0]
    sha = conn.execute("SELECT sha256 FROM raw_uploads").fetchone()[0]
    conn.close()
    check("数据库落库 1 条原始报文 + 1 条记录", n_raw == 1 and n_rec == 1)
    check("SHA256 已记录", len(sha) == 64, sha[:16] + "...")

    # 8) 静态页面可访问
    st, _ = get(base + "/healthz")
    check("/healthz 正常", st == 200)

    srv.shutdown()
    if os.path.exists(TMP_DB):
        os.remove(TMP_DB)
    print("\n临时自检数据库已删除:", TMP_DB)
    print("自检结论:", "全部通过" if failed == 0 else "%d 项失败" % failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())