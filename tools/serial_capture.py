#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
serial_capture.py —— 采集开发板串口日志（用于留证据 / 三方核对的第一步）

用法:
    python serial_capture.py --port COM5 --out ..\\evidence\\serial.log --seconds 120
    python serial_capture.py --port COM5 --seconds 30 --only-data     # 只打印 [DATA] 行

说明:
  - 开发板通过原生 USB(USB-Serial-JTAG) 连到电脑，串口只用于烧录与排错打印，
    它不是数据上传通道（上传走 Wi-Fi）。
  - 日志里带时间戳写盘，方便和数据库记录对照。
"""

import argparse
import datetime
import os
import sys
import time

try:
    import serial  # pyserial
except ImportError:
    print("缺少 pyserial，请先安装：pip install pyserial")
    sys.exit(1)


def usb_jtag_hard_reset(ser) -> None:
    """
    通过 USB-Serial-JTAG 的 RTS/DTR 时序把芯片「硬复位并进入运行模式」。

    为什么要自己复位：USB-Serial-JTAG 的发送缓冲区很小，如果板子早就启动完，
    宿主机再打开串口就抓不到开机日志（IMU 初始化、CHIP_ID、自检结果都在开机阶段）。
    先打开串口、再复位，才能拿到从 ROM 到 app 的完整启动证据。

    时序与 esptool 的 HardReset(uses_usb=True) 完全一致：
    只拉 RTS(EN) 做复位、不动 DTR(IO0)，否则芯片会停在下载模式等烧录。
    （踩过的坑：用 esptool 的 USBJTAGSerialReset 时序会把板子按进 DOWNLOAD 模式，
      日志只剩 "waiting for download"，抓不到 app 启动日志。）
    """
    ser.setRTS(True)           # EN -> LOW（复位）
    time.sleep(0.2)            # 给芯片时间退出复位，之后才能继续 DTR/RTS 变化
    ser.setRTS(False)          # EN -> HIGH（退出复位，正常运行）


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True, help="串口名，例如 COM5")
    ap.add_argument("--baud", type=int, default=115200, help="波特率(USB-JTAG 下无关紧要)")
    ap.add_argument("--out", default=None, help="日志保存路径")
    ap.add_argument("--seconds", type=float, default=60.0, help="采集时长(秒)")
    ap.add_argument("--only-data", action="store_true", help="只回显 [DATA]/[POST] 行")
    ap.add_argument("--reset", action="store_true",
                    help="打开串口后先复位芯片，以便抓到完整启动日志")
    a = ap.parse_args()

    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    fp = open(a.out, "a", encoding="utf-8") if a.out else None
    if fp:
        fp.write("\n===== 采集开始 %s 端口=%s =====\n" %
                 (datetime.datetime.now().isoformat(timespec="milliseconds"), a.port))
        fp.flush()

    ser = serial.Serial(a.port, a.baud, timeout=1)
    print("已打开 %s，开始采集 %.0f 秒 ..." % (a.port, a.seconds))
    t0 = time.time()
    if a.reset:
        usb_jtag_hard_reset(ser)
        # 复位会让 USB-Serial-JTAG 重新枚举，原来打开的句柄会读不到任何数据，
        # 所以必须关掉重开（重试几次等设备回来）。
        ser.close()
        time.sleep(1.5)
        for attempt in range(1, 8):
            try:
                ser = serial.Serial(a.port, a.baud, timeout=1)
                print("复位后重新打开 %s 成功（第 %d 次尝试）" % (a.port, attempt))
                break
            except Exception as e:                      # noqa: BLE001
                print("复位后打开 %s 失败(%s)，1 秒后重试 ..." % (a.port, e))
                time.sleep(1.0)
        else:
            print("复位后始终无法重新打开串口，放弃")
            return 2
        print("已发送 USB-JTAG 硬复位，开始记录完整启动日志")
        t0 = time.time()
    buf = b""
    n_lines = 0
    try:
        while time.time() - t0 < a.seconds:
            chunk = ser.read(4096)
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", errors="replace").rstrip("\r")
                n_lines += 1
                stamp = datetime.datetime.now().isoformat(timespec="milliseconds")
                if fp:
                    fp.write("[%s] %s\n" % (stamp, text))
                    fp.flush()
                show = (not a.only_data) or ("[DATA]" in text) or ("[POST]" in text)
                if show:
                    print(text)
    except KeyboardInterrupt:
        print("\n用户中断")
    finally:
        ser.close()
        if fp:
            fp.write("===== 采集结束 %s，共 %d 行 =====\n" %
                     (datetime.datetime.now().isoformat(timespec="milliseconds"), n_lines))
            fp.close()
            print("日志已保存:", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())