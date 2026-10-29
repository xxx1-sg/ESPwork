# -*- coding: utf-8 -*-
"""jpeg_fixture.py —— 测试用 JPEG 生成器（纯标准库，不需要 Pillow）

为什么要有它
----------------------------------------------------------------------------
第 7 周要把"镜头遮挡"从"采集成功"里分出来，这件事必须能被**测**。
真机现场不可能为了跑一次自检就把镜头挡住，所以这里自己造 JPEG：

  · 它是**语法完全合法**的 baseline JPEG（SOI/DQT/SOF0/DHT/SOS/EOI 齐全），
    后端与 `img_quality` 走的都是同一条真实解析路径 —— 不是"造假数据绕过校验"；
  · 每块一个 DC 系数、AC 全部 EOB，所以生成的是"每 8×8 块恒定"的图像，
    亮度值精确可控（实测解析回来和设定值一位不差）；
  · `make_flat_jpeg()` 造的是又暗又平的画面 —— 就是"镜头被盖住"的样子；
    `make_scene_jpeg()` 造的是明暗相间的画面 —— 就是"有内容"的样子。

★ 这两个函数**只用于测试**，绝不参与任何"补数据"的路径。
"""

import struct

DC_BITS = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
DC_VALS = list(range(12))
AC_BITS = [1] + [0] * 15
AC_VALS = [0x00]        # 只需要 EOB


def _seg(marker, payload):
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def _huff_codes(bits, vals):
    codes = {}
    code = 0
    k = 0
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            codes[vals[k]] = (length, code)
            k += 1
            code += 1
        code <<= 1
    return codes


_DC_CODES = _huff_codes(DC_BITS, DC_VALS)
_AC_CODES = _huff_codes(AC_BITS, AC_VALS)


class _BitWriter(object):
    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.n = 0

    def put(self, code, length):
        for i in range(length - 1, -1, -1):
            self.acc = (self.acc << 1) | ((code >> i) & 1)
            self.n += 1
            if self.n == 8:
                self.out.append(self.acc)
                if self.acc == 0xFF:
                    self.out.append(0x00)       # 字节填充
                self.acc = 0
                self.n = 0

    def flush(self):
        while self.n:
            self.put(1, 1)                       # 用 1 补齐最后一个字节


def make_jpeg(width, height, luma_of_block):
    """按"每块亮度"生成单分量 baseline JPEG。

    luma_of_block(bx, by) -> 0..255，bx/by 是 8×8 块坐标。
    """
    bw = _BitWriter()
    prev_dc = 0
    by = 0
    while by * 8 < height:
        bx = 0
        while bx * 8 < width:
            luma = max(0, min(255, int(luma_of_block(bx, by))))
            dc = int(round((luma - 128) * 8))
            diff = dc - prev_dc
            prev_dc = dc
            s = 0 if diff == 0 else abs(diff).bit_length()
            if s > 11:
                raise ValueError("亮度跳变太大（s=%d），这个简易生成器只支持 ≤11" % s)
            length, code = _DC_CODES[s]
            bw.put(code, length)
            if s:
                v = diff if diff > 0 else diff + (1 << s) - 1
                bw.put(v, s)
            length, code = _AC_CODES[0x00]
            bw.put(code, length)                 # EOB
            bx += 1
        by += 1
    bw.flush()

    out = bytearray(b"\xff\xd8")
    out += _seg(0xDB, bytes([0x00]) + bytes([1] * 64))                       # 量化表全 1
    out += _seg(0xC0, bytes([8]) + struct.pack(">HH", height, width)
                + bytes([1, 1, 0x11, 0]))                                    # 1 分量 1x1
    out += _seg(0xC4, bytes([0x00]) + bytes(DC_BITS) + bytes(DC_VALS))
    out += _seg(0xC4, bytes([0x10]) + bytes(AC_BITS) + bytes(AC_VALS))
    out += _seg(0xDA, bytes([1, 1, 0x00, 0, 63, 0]))
    out += bw.out
    out += b"\xff\xd9"
    return bytes(out)


def make_flat_jpeg(width=320, height=240, luma=18):
    """又暗又平：典型"镜头被盖住/对着桌面"的画面。"""
    return make_jpeg(width, height, lambda bx, by: luma)


def make_scene_jpeg(width=320, height=240):
    """有明暗结构：典型"看得到环境"的画面（左暗右亮 + 上下渐变）。"""
    def f(bx, by):
        return 40 + (bx * 13) % 120 + (by * 7) % 60
    return make_jpeg(width, height, f)
