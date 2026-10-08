#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decode_backtrace.py —— 把 ESP32 崩溃日志里的地址翻译成"哪一行代码"

为什么需要它：本轮真的踩到过一次 `Guru Meditation Error: Core 1 panic'ed (LoadProhibited)`，
光看串口只知道"跑飞了"，只有把地址解析成函数名+文件:行号，才定位到是
`sampler_task → imu_read_raw_averaged → … → i2c_master_transmit_receive` 这条路径上
有内存被写坏（最后查明是包池槽位复用时没清零 `n`，越界写了约 1240 字节）。

用法：
    # 直接从日志里挑地址（推荐：日志里通常有好几段）
    python tools/decode_backtrace.py --log evidence/reset_diag.log

    # 手动给地址
    python tools/decode_backtrace.py 0x42012345 0x4200abcd

    # 指定 elf（默认取 firmware/.pio/build/esp32s3eye/firmware.elf）
    python tools/decode_backtrace.py --log x.log --elf path\\to\\firmware.elf
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                              # noqa: BLE001
    pass

DEFAULT_ELF = r"D:\ESP\firmware\.pio\build\esp32s3eye\firmware.elf"
DEFAULT_ADDR2LINE = (r"D:\ESP\.platformio\packages\toolchain-riscv32-esp\bin"
                     r"\riscv32-esp-elf-addr2line.exe")

# 形如：0x42012345:0x3fc9xxxx  或  Backtrace: 0x... 0x...
RE_ADDR = re.compile(r"0x4[0-9a-fA-F]{7}")
RE_PANIC = re.compile(r"Guru Meditation Error.*")


def find_addr2line(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.path.isfile(DEFAULT_ADDR2LINE):
        return DEFAULT_ADDR2LINE
    # 退路：在整个 .platformio 里找
    root = r"D:\ESP\.platformio\packages"
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn == "riscv32-esp-elf-addr2line.exe":
                return os.path.join(dirpath, fn)
    raise SystemExit("找不到 riscv32-esp-elf-addr2line.exe，请用 --addr2line 指定")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("addrs", nargs="*", help="十六进制地址（0x…）")
    ap.add_argument("--log", help="从日志文件里自动提取地址与 panic 行")
    ap.add_argument("--elf", default=DEFAULT_ELF)
    ap.add_argument("--addr2line", default=None)
    a = ap.parse_args()

    addrs = list(a.addrs)
    panics: list[str] = []
    if a.log:
        if not os.path.isfile(a.log):
            print("日志不存在:", a.log)
            return 2
        text = open(a.log, encoding="utf-8", errors="replace").read()
        panics = [m.group(0)[:160] for m in RE_PANIC.finditer(text)]
        # 保持出现顺序、去重
        seen, uniq = set(), []
        for m in RE_ADDR.finditer(text):
            v = m.group(0).lower()
            if v not in seen:
                seen.add(v)
                uniq.append(v)
        addrs = uniq + addrs

    if not addrs:
        print("没有地址可解析（日志里没有 0x4xxxxxxx 形式的地址？）")
        return 2
    if not os.path.isfile(a.elf):
        print("ELF 不存在:", a.elf, "（先编译一次：cd firmware && pio run）")
        return 2

    if panics:
        print("=" * 90)
        print("日志里的崩溃信息：")
        for p in panics:
            print("   ", p)
        print("=" * 90)

    tool = find_addr2line(a.addr2line)
    # -a 让每行都带地址前缀, -p 一行出"函数 + 文件:行号", -i 展开内联帧, -C 反解 C++ 符号
    cmd = [tool, "-pfiaC", "-e", a.elf] + addrs
    print("地址解析（共 %d 个）：%s" % (len(addrs), " ".join(addrs)))
    print("-" * 90)
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace").stdout
    for line in out.splitlines():
        if line.strip():
            print("  " + line.strip())
    print("-" * 90)
    print("提示：崩溃前最后调用的业务函数通常是最上面那几帧；")
    print("     若栈顶落在 i2c/字符串/内存函数里，多半是别处的内存被写坏了（越界写/野指针），")
    print("     要去查最近改动过的缓冲区、数组下标与队列结构。")
    return 0


if __name__ == "__main__":
    sys.exit(main())