#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
serial_watch.py —— 能扛住 USB 重新枚举的串口监听（排错用）

普通抓取工具用 serial.Serial 打开一次就读到底；但 ESP32-S3 的 USB-Serial-JTAG
在芯片复位时会重新枚举（设备先消失再出现），于是会出现：
    ClearCommError failed (PermissionError(13, ...))
    could not open port 'COM5': FileNotFoundError(2, ...)
这时既抓不到日志、也判断不出板子到底在干什么。

本脚本循环「打开 -> 读 -> 出错就重开」，并把设备的消失/出现时间点记下来：
  - 如果端口只是偶尔闪断一次，说明板子复位过一次（或有新固件烧录）；
  - 如果端口每秒闪断一次，说明板子在反复复位（崩溃/掉电重启）；
  - 如果端口一直稳定但没有任何输出，才是真正的"板子不打印"。

用法:
    python tools/serial_watch.py --port COM5 --seconds 45 --out D:\\ESP\\evidence\\watch.log
"""

import argparse
import datetime
import os
import sys
import time

import serial
import serial.tools.list_ports as list_ports

# Windows 控制台默认是 GBK，打印带替换字符的日志会抛 UnicodeEncodeError
# 把日志本身搞崩（这会掩盖真正的串口问题）。这里统一用 UTF-8 + 替换策略输出。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                               # noqa: BLE001
    pass


def ts() -> str:
    return datetime.datetime.now().isoformat(timespec="milliseconds")


def port_present(port: str) -> bool:
    return any(p.device.upper() == port.upper() for p in list_ports.comports())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM5")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=45.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--only-data", action="store_true",
                    help="屏幕上只显示 [DATA]/[POST] 行（手工核对时更清爽；文件仍然记全量）")
    ap.add_argument("--attach", action="store_true",
                    help="只附着、不复位板子：打开串口后立刻拉低 DTR/RTS，"
                         "用于观察板子当前正在发生什么（比如断流、崩溃重启），"
                         "而不会把芯片复位掉。注意：看不到启动日志是正常的。")
    a = ap.parse_args()

    fp = None
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        fp = open(a.out, "a", encoding="utf-8")

    def log(line: str, is_data: bool = False) -> None:
        # --only-data 只影响终端显示，日志文件始终写全量，保证证据完整
        if not (a.only_data and not is_data):
            print(line, flush=True)
        if fp:
            fp.write(line + "\n")
            fp.flush()

    log("\n===== serial_watch 开始 %s 端口=%s 时长=%.0fs =====" % (ts(), a.port, a.seconds))
    t_end = time.time() + a.seconds
    opens = 0
    total_bytes = 0
    buf = b""

    # 打开 USB-Serial-JTAG 串口会让芯片复位，芯片随即重新枚举 USB，
    # 结果就是「刚打开的句柄立刻失效」。所以固定走两步：
    #   第 1 次打开：触发复位（随后句柄会失效，直接关掉）
    #   第 2 次打开：此时板子已重新出现，从这里开始稳定读取（能拿到完整启动日志）
    # 之后不再做"端口是否在"的轮询 —— 那会导致刚开的口又被自己关掉，陷入复位死循环。
    ser = None
    while opens < 2 and time.time() < t_end:
        if not port_present(a.port):
            time.sleep(0.4)
            continue
        try:
            ser = serial.Serial(a.port, a.baud, timeout=0.5)
        except Exception as e:                                  # noqa: BLE001
            log("[%s] 打开失败: %s" % (ts(), e))
            time.sleep(0.7)
            continue
        opens += 1
        log("[%s] 已打开 %s（第 %d 次）" % (ts(), a.port, opens))

        if a.attach:
            # 附着模式：不复位，只把 DTR/RTS 拉低，避免触碰 USB-Serial-JTAG 的复位时序
            try:
                ser.dtr = False
                ser.rts = False
                ser.reset_input_buffer()
            except Exception as e:                              # noqa: BLE001
                log("[%s] 设置 DTR/RTS 失败（可能仍会复位）: %s" % (ts(), e))
            log("[%s] 附着模式：未触发复位，直接观察板子当前输出" % ts())
            break

        if opens == 1:
            time.sleep(0.6)                                     # 让复位动作完成
            try:
                ser.close()
            except Exception:                                   # noqa: BLE001
                pass
            ser = None
            log("[%s] 已触发芯片复位，等待 USB 重新枚举 ..." % ts())
            time.sleep(1.6)

    if ser is None:
        log("[%s] 无法打开串口，退出" % ts())
        if fp:
            fp.close()
        return 2

    try:
        while time.time() < t_end:
            chunk = ser.read(1024)
            if not chunk:
                continue
            total_bytes += len(chunk)
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").rstrip("\r")
                if text.strip():
                    log("[%s] %s" % (ts(), text),
                        is_data=("[DATA]" in text or "[POST]" in text))
    except Exception as e:                                      # noqa: BLE001
        log("[%s] 读取出错（设备重新枚举或拔出）: %s" % (ts(), e))
    finally:
        try:
            ser.close()
        except Exception:                                       # noqa: BLE001
            pass

    log("[%s] 结束：共打开 %d 次，收到 %d 字节" % (ts(), opens, total_bytes))
    log("===== serial_watch 结束 =====\n")
    if fp:
        fp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())