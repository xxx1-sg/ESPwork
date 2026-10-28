#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
watch_cam.py —— 守着串口，等摄像头"真的被认出来"的那一刻

用途：现场把 J4 的 24P 摄像头排线重新插紧后，用它盯着串口。
固件每 5/10/20/30 秒自动重试探测，一旦成功会打出

    I (...) cam: 板载摄像头就绪：OV2640 (PID=0x26) 320x240 JPEG q12，目标 5 fps

本脚本看到这一行就以 0 退出（判定：硬件真的通了，不是"指令发出去了"）。
超时没看到则以 2 退出，并把期间出现过的失败原因打出来，方便对照。

用法:
    D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\watch_cam.py --port COM5 --seconds 180

注意:
  - 同一时刻只能有一个程序占用 COM5（先关掉串口监视器/其它抓日志脚本）。
  - 只 passively 收日志，不发复位脉冲，不会打断正在跑的采集/上传。
"""

import argparse
import sys
import time

READY_MARK = "板载摄像头就绪"
FAIL_MARK = "Detected camera not supported"
RETRY_MARK = "摄像头重试仍失败"

# 只关心这些关键词；其它日志（每 1 秒一条 [POST]）全部丢弃，避免刷屏
WATCH = ("cam:", "camera:", "cam_hal:", "ll_cam:", "sccb", "psram")


def main():
    ap = argparse.ArgumentParser(description="等摄像头被认出来的那一刻")
    ap.add_argument("--port", default="COM5", help="串口（默认 COM5）")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=int, default=180, help="最多盯多久（默认 180 秒）")
    args = ap.parse_args()

    try:
        import serial  # pyserial
    except ImportError:
        print("[FATAL] 缺少 pyserial：用 D:\\ESP\\.venv\\Scripts\\python.exe 运行本脚本")
        return 3

    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.2)
    except Exception as e:                      # noqa: BLE001
        print(f"[FATAL] 打不开 {args.port}: {e}")
        print("        是不是还有别的程序占着串口？（串口监视器 / serial_capture.py）")
        return 3

    print(f"[i] 正在监听 {args.port}，最多 {args.seconds} 秒……")
    print("[i] 现在去把主板上的 24P 摄像头排线（J4）插到底、扣紧；插好不用重启。")
    print("[i] 出现「板载摄像头就绪」即判定成功。Ctrl+C 可提前退出。\n")

    t0 = time.time()
    buf = ""
    fails = []
    try:
        while time.time() - t0 < args.seconds:
            data = ser.read(4096)
            if not data:
                continue
            buf += data.decode("utf-8", "replace")
            # 保留最后半行，避免把一行切断
            lines = buf.split("\n")
            buf = lines.pop()
            for line in lines:
                line = line.rstrip("\r")
                if READY_MARK in line:
                    print("[OK] " + line.strip())
                    print("\n[结论] 摄像头已探测成功：硬件通路是真的通了。")
                    print("       网页「相机预览」约 1 秒内会自动出现实时画面，无需刷新。")
                    return 0
                if FAIL_MARK in line or RETRY_MARK in line:
                    fails.append(line.strip())
                    print("[..] " + line.strip())
                elif any(k in line for k in WATCH):
                    print("     " + line.strip())
    except KeyboardInterrupt:
        print("\n[i] 手动中断")
    finally:
        ser.close()

    print(f"\n[结论] {args.seconds} 秒内没有出现「{READY_MARK}」——摄像头仍未应答。")
    if fails:
        print(f"       期间探测失败 {len(fails)} 次，最后一次: {fails[-1]}")
    print("       下一步：确认 J4 里确实插着 24P 排线且锁扣扣紧；再不行量 J4 脚 4/11=2.8V、"
          "脚 10=1.5V、脚 6=2.8V、脚 8=0V。")
    return 2


if __name__ == "__main__":
    sys.exit(main())
