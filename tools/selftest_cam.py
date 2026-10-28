"""第 4 周摄像头接口自测（独立端口 + 临时 DB，不影响常驻后端）"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import hashlib

PORT = 8011
TOKEN = "esp32s3eye-demo-token-2026"
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

tmp = tempfile.mkdtemp(prefix="camtest_")
env = dict(os.environ)
env.update({
    "SENSOR_PORT": str(PORT),
    "SENSOR_HOST": "127.0.0.1",
    "SENSOR_DB": os.path.join(tmp, "t.db"),
    "SENSOR_CAM_DIR": os.path.join(tmp, "photos"),
    "PYTHONIOENCODING": "utf-8",
})
proc = subprocess.Popen([sys.executable, os.path.join(BASE, "backend", "server.py")],
                        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
fails = []


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name + ((" | " + detail) if detail else ""))
    if not cond:
        fails.append(name)


def req(method, path, body=None, token=True, ctype="application/json"):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    headers = {"Content-Type": ctype}
    if token:
        headers["X-Device-Token"] = TOKEN
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, dict(r.getheaders()), data


# 等后端起来
for _ in range(60):
    try:
        s, _, d = req("GET", "/healthz", token=False)
        if s == 200:
            break
    except Exception:
        pass
    time.sleep(0.3)
else:
    print("后端未启动"); proc.kill(); shutil.rmtree(tmp, True); sys.exit(1)

try:
    jpeg = b"\xff\xd8" + b"\x11" * 500 + b"\xff\xd9"
    photo = b"\xff\xd8" + b"\x22" * 3000 + b"\xff\xd9"

    print("[1] 无帧状态")
    s, _, d = req("GET", "/api/v1/cam/status?device_id=devA", token=False)
    j = json.loads(d)
    check("status 200", s == 200, str(s))
    check("has_frame=false", j.get("has_frame") is False, str(j.get("has_frame")))
    check("devices 为空", j.get("devices_with_frames") == [], str(j.get("devices_with_frames")))

    print("[2] 无帧取图 → 404")
    s, _, d = req("GET", "/api/v1/cam/frame?device_id=devA", token=False)
    check("frame 404", s == 404, str(s))

    print("[3] 令牌错误 → 401")
    s, _, _ = req("POST", "/api/v1/cam/frame?device_id=devA&seq=1&w=320&h=240",
                  body=jpeg, token=False, ctype="image/jpeg")
    check("401", s == 401, str(s))

    print("[4] 非法 JPEG → 400")
    s, _, _ = req("POST", "/api/v1/cam/frame?device_id=devA&seq=1&w=320&h=240",
                  body=b"not-a-jpeg", ctype="image/jpeg")
    check("400", s == 400, str(s))

    print("[5] 上传预览帧")
    s, _, d = req("POST", "/api/v1/cam/frame?device_id=devA&seq=1&w=320&h=240",
                  body=jpeg, ctype="image/jpeg")
    j = json.loads(d)
    check("200", s == 200, str(s) + " " + d.decode("utf-8", "ignore")[:80])
    check("capture=false", j.get("capture") is False, str(j))

    print("[6] 取回同一帧（逐字节一致）")
    s, h, d = req("GET", "/api/v1/cam/frame?device_id=devA", token=False)
    check("200", s == 200, str(s))
    check("字节一致", d == jpeg, "%d vs %d" % (len(d), len(jpeg)))
    check("Content-Type", h.get("Content-Type") == "image/jpeg", str(h.get("Content-Type")))
    check("X-Frame-Seq=1", h.get("X-Frame-Seq") == "1", str(h.get("X-Frame-Seq")))

    print("[7] 点拍照 → 下一帧响应带 capture=true")
    s, _, d = req("POST", "/api/v1/cam/capture", body=json.dumps({"device_id": "devA"}).encode())
    check("capture 登记 200", s == 200, str(s) + d.decode()[:80])
    s, _, d = req("POST", "/api/v1/cam/frame?device_id=devA&seq=2&w=320&h=240",
                  body=jpeg, ctype="image/jpeg")
    j = json.loads(d)
    check("capture=true", j.get("capture") is True, str(j))
    s, _, d = req("POST", "/api/v1/cam/frame?device_id=devA&seq=3&w=320&h=240",
                  body=jpeg, ctype="image/jpeg")
    check("只下发一次", json.loads(d).get("capture") is False, str(json.loads(d)))

    print("[8] 无帧设备拍照 → 409")
    s, _, d = req("POST", "/api/v1/cam/capture", body=json.dumps({"device_id": "nope"}).encode())
    check("409", s == 409, str(s) + d.decode()[:80])

    print("[9] 上传照片 → 落盘 + 列表 + 下载")
    s, _, d = req("POST", "/api/v1/cam/photo?device_id=devA&seq=9&w=1600&h=1200",
                  body=photo, ctype="image/jpeg")
    j = json.loads(d)
    check("200", s == 200, str(s) + d.decode()[:120])
    photo_id = (j.get("photo") or {}).get("photo_id")
    check("有 photo_id", bool(photo_id), str(photo_id))

    s, _, d = req("GET", "/api/v1/cam/photos?device_id=devA", token=False)
    j = json.loads(d)
    check("列表 count=1", j.get("count") == 1, str(j.get("count")))
    check("sha256 一致", j["photos"][0]["sha256"] == hashlib.sha256(photo).hexdigest())

    s, _, d = req("GET", "/api/v1/cam/photo/%s.jpg" % photo_id, token=False)
    check("下载 200", s == 200, str(s))
    check("下载字节一致", d == photo, "%d vs %d" % (len(d), len(photo)))

    paths = []
    for root, _, files in os.walk(os.path.join(tmp, "photos")):
        paths += [os.path.join(root, f) for f in files]
    check("磁盘有 1 个文件", len(paths) == 1, str(paths))

    print("[10] 路径穿越防护")
    s, _, _ = req("GET", "/api/v1/cam/photo/..%2F..%2Fetc%2Fpasswd.jpg", token=False)
    check("不存在/拒绝", s in (400, 404), str(s))

    print("[11] 静态页仍可访问")
    s, _, d = req("GET", "/", token=False)
    check("index 200", s == 200 and b"cameraPanel" in d, str(s))

finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()
    shutil.rmtree(tmp, True)

print("\n=== %s ===" % ("全部通过" if not fails else "失败: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
