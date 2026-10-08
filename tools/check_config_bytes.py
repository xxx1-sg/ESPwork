#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_config_bytes.py —— 检查 app_config.h 里的 SSID/密码/URL 是否含不可见字符

用途：Wi-Fi 连不上时，先排除「配置字符串里混入了全角空格 / NBSP / 尾随空格 /
      零宽字符」这类肉眼看不见的问题。这些字符会让设备报 reason=201(找不到 AP)。

用法:
    python tools/check_config_bytes.py
"""

import re
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT = os.path.join(os.path.dirname(HERE), "firmware", "src", "app_config.h")

KEYS = ("APP_WIFI_SSID", "APP_WIFI_PASSWORD", "APP_SERVER_URL", "APP_DEVICE_TOKEN")

# 危险字符：非 ASCII 可打印、以及常见的“看起来像空格”的字符
SUSPECT = {
    0x00A0: "NBSP 不换行空格",
    0x3000: "全角空格",
    0x200B: "零宽空格",
    0xFEFF: "BOM/零宽不换行空格",
    0x2018: "左单引号", 0x2019: "右单引号",
    0x201C: "左双引号", 0x201D: "右双引号",
}


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    print("检查文件:", path)
    if not os.path.isfile(path):
        print("文件不存在")
        return 1

    data = open(path, "rb").read()
    problems = 0
    found = 0
    for raw_line in data.split(b"\n"):
        for key in KEYS:
            if ("#define " + key).encode() not in raw_line:
                continue
            found += 1
            line = raw_line.decode("utf-8", errors="replace")
            m = re.search(r'"([^"]*)"', line)
            if not m:
                print("  %-18s 没有找到字符串字面量: %s" % (key, line.strip()))
                problems += 1
                continue
            val = m.group(1)
            print("  %-18s = %r" % (key, val))
            print("  %-18s   字节: %s" % ("", " ".join("%02X" % c for c in val.encode("utf-8"))))
            for i, ch in enumerate(val):
                code = ord(ch)
                if code in SUSPECT:
                    print("     !! 位置 %d 出现可疑字符 U+%04X (%s)" % (i, code, SUSPECT[code]))
                    problems += 1
                elif not (0x20 <= code <= 0x7E):
                    print("     !! 位置 %d 为非 ASCII 可打印字符 U+%04X" % (i, code))
                    problems += 1
            if val != val.strip():
                print("     !! 首尾有多余空白字符（空格/制表符），必须去掉")
                problems += 1

    if found == 0:
        print("没有找到任何待检查的宏，请确认文件路径")
        return 1
    print()
    print("结论:", "发现问题 %d 处，请修正后重新编译" % problems if problems
          else "全部为纯 ASCII 可打印字符，没有不可见字符问题")
    return 0 if problems == 0 else 2


if __name__ == "__main__":
    sys.exit(main())