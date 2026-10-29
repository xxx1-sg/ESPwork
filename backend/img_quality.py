# -*- coding: utf-8 -*-
"""img_quality.py —— 第 7 周：从 JPEG 字节里**量**出画面质量（纯标准库，零依赖）

为什么需要它
----------------------------------------------------------------------------
第 7 周要回答"这次按需采集到底成功了吗"。只看"HTTP 200 + 收到字节"是不够的：
镜头被挡住、镜头盖没摘、模组对着桌面时，设备照样能上传一张**合法 JPEG**。
如果没有别的证据，系统就只能说"采集成功"，而用户看到的是全黑。

本模块做的事：**不解码成 RGB、不做识别**，只把 baseline JPEG 的 DC 系数读出来，
得到每个 8×8 块的亮度均值，再统计：

    mean_luma   整块平均亮度（0-255，128 ≈ 中灰）
    luma_std    块间亮度标准差（画面有没有内容/纹理）
    dark_ratio  亮度 < 32 的块占比
    bright_ratio 亮度 > 224 的块占比

`mean_luma` 低 + `luma_std` 低 ⇒ 又暗又平 ⇒ 典型"镜头被遮挡/盖住/对着黑桌面"。
这两个数是**从这张图的字节里算出来的**，可以复算、可以复核，不是猜的。

为什么不用 Pillow / numpy
----------------------------------------------------------------------------
后端坚持"零第三方依赖"（`python backend/server.py` 直接能跑）。所以这里自己实现
一个**只取 DC 系数**的 baseline JPEG 解码器：Huffman 解码照做（不跳位就读不下去），
但 IDCT、色彩转换全部跳过 —— DC 系数 / 8 + 128 就已经是 8×8 块的平均亮度。

★ 三条纪律
----------------------------------------------------------------------------
1. **不支持就说不支持**：progressive（SOF2）、算术编码、16-bit 精度一律
   返回 `available=False` + 原因，绝不用"字节数大小"之类的间接指标冒充测量结果。
2. **只采样一部分块**（默认 600 个 MCU）：一张 UXGA 有 45000 个块，全解要好几秒，
   而"这一次拍的黑不黑"不需要精确到整幅。采样比例如实写进 `sampled_rows_pct`。
3. **不判断"图里是什么"**：这里只回答"暗不暗、平不平"，不回答"是不是有人、是不是遮挡物"。
   任何"遮挡"结论都是**疑似**，语义由状态机（state=occluded）与界面文案负责说清楚。
"""

import struct

__all__ = ["analyze_jpeg", "looks_occluded", "describe_quality", "QualityError"]

# ---------------------------------------------------------------------------
# 判定阈值（集中放置，便于在文档里逐条引用）
# ---------------------------------------------------------------------------
DARK_LUMA_MAX = 28.0        # 平均亮度低于它才算"暗"
FLAT_STD_MAX = 10.0         # 块间标准差低于它才算"平"
VERY_FLAT_STD = 3.0         # 平到这个程度：无论亮暗都说明画面没有内容
DARK_BLOCK_LUMA = 32.0      # 单块算"暗块"的门限
BRIGHT_BLOCK_LUMA = 224.0   # 单块算"亮块"的门限

DEFAULT_MAX_MCUS = 600      # 采样上限（QVGA 4:2:0 共 300 个 MCU，等于整幅）


class QualityError(Exception):
    """JPEG 结构问题（拿到的不是可解析的 baseline JPEG）。"""


# ---------------------------------------------------------------------------
# 位读取器：处理 0xFF00 填充与 RSTn 重启标记
# ---------------------------------------------------------------------------
class _BitReader(object):
    __slots__ = ("data", "pos", "acc", "nbits", "end")

    def __init__(self, data, pos):
        self.data = data
        self.pos = pos
        self.acc = 0
        self.nbits = 0
        self.end = len(data)

    def _fill(self):
        if self.pos >= self.end:
            raise QualityError("熵编码数据提前结束")
        b = self.data[self.pos]
        self.pos += 1
        if b == 0xFF:
            nxt = self.data[self.pos] if self.pos < self.end else 0
            if nxt == 0x00:          # 转义过的 FF
                self.pos += 1
            elif 0xD0 <= nxt <= 0xD7:  # RSTn：交给调用方在块边界处理
                raise _Restart(nxt)
            else:
                # 遇到真正的标记：说明扫描数据已经结束（截断），不再往下读
                raise QualityError("扫描数据提前遇到标记 0x%02X" % nxt)
        self.acc = (self.acc << 8) | b
        self.nbits += 8

    def bit(self):
        if self.nbits == 0:
            self._fill()
        self.nbits -= 1
        return (self.acc >> self.nbits) & 1

    def bits(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | self.bit()
        return v

    def align(self):
        """丢弃当前字节里剩余的位（RST 标记前必须字节对齐）。"""
        self.nbits = self.nbits % 8
        if self.nbits:
            self.acc &= (1 << self.nbits) - 1

    def skip_restart(self):
        """跳到下一个 RSTn 之后（重新字节对齐）。"""
        while self.pos < self.end - 1:
            if self.data[self.pos] == 0xFF and 0xD0 <= self.data[self.pos + 1] <= 0xD7:
                self.pos += 2
                self.acc = 0
                self.nbits = 0
                return True
            self.pos += 1
        return False


class _Restart(Exception):
    def __init__(self, marker):
        Exception.__init__(self, marker)
        self.marker = marker


# ---------------------------------------------------------------------------
# Huffman 表
# ---------------------------------------------------------------------------
def _build_huff(counts, symbols):
    """按 JPEG 规范 B.2.4.2 生成 (码长, 码字) → 符号 的查找表。"""
    table = {}
    code = 0
    k = 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            if k >= len(symbols):
                raise QualityError("Huffman 表长度与符号数不一致")
            table[(length, code)] = symbols[k]
            k += 1
            code += 1
        code <<= 1
    return table


def _huff_decode(br, table):
    code = 0
    for length in range(1, 17):
        code = (code << 1) | br.bit()
        sym = table.get((length, code))
        if sym is not None:
            return sym
    raise QualityError("Huffman 码字越界（数据损坏）")


def _extend(v, n):
    """JPEG 的"符号扩展"：n 位值的补码表示。"""
    return v - (1 << n) + 1 if v < (1 << (n - 1)) else v


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def analyze_jpeg(data, max_mcus=DEFAULT_MAX_MCUS):
    """解析 baseline JPEG，返回画面质量指标 dict（永不抛异常）。

    返回字段：
        available         bool    是否真的量到了（False 时 reason 说明原因）
        reason            str     不可用的原因
        width / height
        ncomp             分量数
        subsampling       str     如 "4:2:0"
        sample_mcus       实际解码的 MCU 数
        total_mcus        整幅 MCU 数
        sampled_rows_pct  采样覆盖的高度百分比
        mean_luma         0-255
        luma_std
        dark_ratio / bright_ratio
        bytes_per_pixel
    """
    out = {
        "available": False,
        "reason": "",
        "width": 0, "height": 0, "ncomp": 0, "subsampling": "",
        "sample_mcus": 0, "total_mcus": 0, "sampled_rows_pct": 0.0,
        "mean_luma": None, "luma_std": None,
        "dark_ratio": None, "bright_ratio": None,
        "bytes_per_pixel": None,
    }
    if not data or len(data) < 4:
        out["reason"] = "空数据"
        return out
    if data[0] != 0xFF or data[1] != 0xD8:
        out["reason"] = "不是 JPEG（缺 SOI）"
        return out

    try:
        _parse(data, max_mcus, out)
    except QualityError as e:
        out["available"] = False
        out["reason"] = str(e)
        return out
    except Exception as e:                                   # noqa: BLE001
        # 解析器自身的边界情况：不影响主流程，如实标为"量不了"
        out["available"] = False
        out["reason"] = "解析异常: %r" % (e,)
        return out
    return out


def _parse(data, max_mcus, out):
    pos = 2
    qt = {}
    huff_dc = {}
    huff_ac = {}
    frame = None
    restart_interval = 0

    while pos < len(data) - 1:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        pos += 2
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xD9:
            break
        if pos + 2 > len(data):
            break
        seg_len = struct.unpack(">H", data[pos:pos + 2])[0]
        seg = data[pos + 2:pos + seg_len]
        nxt = pos + seg_len

        if marker == 0xDB:                                   # DQT
            i = 0
            while i < len(seg):
                pq, tq = seg[i] >> 4, seg[i] & 15
                i += 1
                if pq == 0:
                    tbl = list(seg[i:i + 64])
                    i += 64
                else:
                    tbl = list(struct.unpack(">64H", seg[i:i + 128]))
                    i += 128
                qt[tq] = tbl
        elif marker == 0xC4:                                 # DHT
            i = 0
            while i < len(seg):
                tc, th = seg[i] >> 4, seg[i] & 15
                i += 1
                counts = list(seg[i:i + 16])
                i += 16
                n = sum(counts)
                symbols = list(seg[i:i + n])
                i += n
                (huff_dc if tc == 0 else huff_ac)[th] = _build_huff(counts, symbols)
        elif marker == 0xDD:                                 # DRI
            restart_interval = struct.unpack(">H", seg[0:2])[0]
        elif marker in (0xC0, 0xC1):                         # SOF0 / SOF1 = baseline
            frame = _parse_sof(seg)
        elif marker in (0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
            out["reason"] = "progressive/算术编码 JPEG，本模块不解析（marker=0x%02X）" % marker
            return
        elif marker == 0xDA:                                 # SOS
            if frame is None:
                raise QualityError("SOS 出现在 SOF 之前")
            # ★ 注意：这里必须传 pos（指向长度字段），不是 nxt —— 熵编码数据
            #   紧跟在 SOS 段之后，起点 = pos + seg_len。传错会去把熵编码数据
            #   当成段长度解析（症状：分量表全乱、"亮度分量不在扫描里"）。
            _decode_scan(data, pos, frame, qt, huff_dc, huff_ac,
                         restart_interval, max_mcus, out)
            return
        pos = nxt

    out["reason"] = "没有找到扫描数据（SOS）"


def _parse_sof(seg):
    if len(seg) < 6:
        raise QualityError("SOF 段太短")
    precision = seg[0]
    if precision != 8:
        raise QualityError("只支持 8-bit 精度（JPEG 里是 %d）" % precision)
    height, width = struct.unpack(">HH", seg[1:5])
    ncomp = seg[5]
    comps = []
    for i in range(ncomp):
        base = 6 + i * 3
        if base + 3 > len(seg):
            raise QualityError("SOF 分量表越界")
        cid, hv, tq = seg[base], seg[base + 1], seg[base + 2]
        comps.append({"id": cid, "h": hv >> 4, "v": hv & 15, "tq": tq})
    if not comps:
        raise QualityError("SOF 没有分量")
    hmax = max(c["h"] for c in comps) or 1
    vmax = max(c["v"] for c in comps) or 1
    # 子采样只按"亮度相对最大值"来标（3 分量时亮度一定是最大的那一组）
    sub = {(1, 1): "4:4:4", (2, 1): "4:2:2", (2, 2): "4:2:0",
           (4, 1): "4:1:1", (4, 2): "4:1:0"}.get((hmax, vmax),
                                                 "%dx%d" % (hmax, vmax))
    return {"width": width, "height": height, "ncomp": ncomp,
            "comps": comps, "hmax": hmax, "vmax": vmax, "sub": sub}


def _decode_scan(data, pos, frame, qt, huff_dc, huff_ac, restart_interval,
                 max_mcus, out):
    """解码熵编码段，只保留亮度分量的 DC。"""
    # 扫描头：Ns + 每分量 (Cs, Td/Ta) + Ss/Se/AhAl
    if pos + 2 > len(data):
        raise QualityError("SOS 段不完整")
    seg_len = struct.unpack(">H", data[pos:pos + 2])[0]
    seg = data[pos + 2:pos + seg_len]
    ns = seg[0]
    scan = []
    for i in range(ns):
        cs = seg[1 + i * 2]
        td = seg[2 + i * 2] >> 4
        ta = seg[2 + i * 2] & 15
        scan.append({"id": cs, "td": td, "ta": ta})
    if pos + seg_len > len(data):
        raise QualityError("SOS 段越界")

    comps = frame["comps"]
    by_id = {c["id"]: c for c in comps}
    # 亮度 = 采样因子最大的那个分量（等大时取第一个，即 Y）
    luma = max(comps, key=lambda c: (c["h"] * c["v"], -comps.index(c)))
    luma_id = luma["id"]
    if luma_id not in [s["id"] for s in scan]:
        out["reason"] = "亮度分量不在扫描里"
        return

    hmax, vmax = frame["hmax"], frame["vmax"]
    mcu_w, mcu_h = hmax * 8, vmax * 8
    width, height = frame["width"], frame["height"]
    if mcu_w <= 0 or mcu_h <= 0:
        raise QualityError("MCU 尺寸非法")
    mcux = (width + mcu_w - 1) // mcu_w
    mcuy = (height + mcu_h - 1) // mcu_h
    total = mcux * mcuy
    limit = max(1, min(int(max_mcus or total), total))

    tbl_dc = {s["id"]: huff_dc.get(s["td"]) for s in scan}
    tbl_ac = {s["id"]: huff_ac.get(s["ta"]) for s in scan}
    for s in scan:
        if tbl_dc.get(s["id"]) is None or tbl_ac.get(s["id"]) is None:
            raise QualityError("缺少 Huffman 表（分量 id=%d）" % s["id"])

    pred = {c["id"]: 0 for c in comps}
    blocks = []          # 亮度 DC（已反量化）
    br = _BitReader(data, pos + seg_len)
    luma_q = qt.get(luma["tq"])
    if luma_q is None:
        raise QualityError("缺少亮度量化表")

    done = 0
    try:
        while done < limit:
            # ---- 重启标记（每 restart_interval 个 MCU 一个）----
            if restart_interval and done and done % restart_interval == 0:
                br.align()
                for c in comps:
                    pred[c["id"]] = 0
                if not br.skip_restart():
                    break
            for s in scan:
                c = by_id.get(s["id"])
                if c is None:
                    raise QualityError("扫描里的分量 id=%d 不在 SOF 里" % s["id"])
                for _ in range(c["h"] * c["v"]):
                    dc = _read_block(br, tbl_dc[c["id"]], tbl_ac[c["id"]], pred, c["id"])
                    if c["id"] == luma_id:
                        blocks.append(dc)
            done += 1
    except _Restart:
        # 遇到 RST 时跳过它并继续（若还没达到采样上限）
        pass
    except QualityError as e:
        # 已经解出来的块仍然有效 —— 如实标注"部分采样"，不整段作废
        if not blocks:
            raise
        out["reason"] = "部分解码: %s" % (e,)

    if not blocks:
        out["reason"] = out["reason"] or "没有解出任何亮度块"
        return

    q0 = float(luma_q[0]) or 1.0
    lumas = [b * q0 / 8.0 + 128.0 for b in blocks]
    lumas = [0.0 if v < 0 else (255.0 if v > 255 else v) for v in lumas]
    n = len(lumas)
    mean = sum(lumas) / n
    var = sum((v - mean) ** 2 for v in lumas) / n
    # 采样覆盖的是**画面顶部若干个 MCU 行**（熵编码只能顺序解码，跳不过去），
    # 所以覆盖率要按行算，不能按"已解码 MCU 数 / 总行数"算。
    rows_done = (done + mcux - 1) // mcux if mcux else 0
    rows_pct = 100.0 * min(rows_done, mcuy) / mcuy if mcuy else 0.0

    out.update({
        "available": True,
        "width": width, "height": height, "ncomp": frame["ncomp"],
        "subsampling": frame["sub"],
        "sample_mcus": done, "total_mcus": total,
        "sampled_rows_pct": round(rows_pct, 1),
        "mean_luma": round(mean, 2),
        "luma_std": round(var ** 0.5, 2),
        "dark_ratio": round(sum(1 for v in lumas if v < DARK_BLOCK_LUMA) / n, 4),
        "bright_ratio": round(sum(1 for v in lumas if v > BRIGHT_BLOCK_LUMA) / n, 4),
        "bytes_per_pixel": round(len(data) / float(max(1, width * height)), 5),
    })


def _read_block(br, dc_tbl, ac_tbl, pred, cid):
    """读一个 8×8 块：返回反量化**之前**的 DC（差分已累加）。"""
    s = _huff_decode(br, dc_tbl)
    if s:
        diff = _extend(br.bits(s), s)
    else:
        diff = 0
    pred[cid] += diff
    # AC 系数不关心值，但必须逐位走完，否则后续块全部错位
    k = 1
    while k < 64:
        rs = _huff_decode(br, ac_tbl)
        r, sz = rs >> 4, rs & 15
        if sz == 0:
            if r == 15:
                k += 16
                continue
            break
        k += r + 1
        if k > 64:
            break
        br.bits(sz)
    return pred[cid]


# ---------------------------------------------------------------------------
# 判定与文案
# ---------------------------------------------------------------------------
def looks_occluded(q):
    """根据质量指标判断"疑似遮挡/画面不可用"。

    返回 (verdict, why)：
        verdict: "occluded" | "usable" | "unknown"
    ★ 量不到就返回 unknown —— 绝不因为"没数据"而判成"可用"或"不可用"。
    """
    if not q or not q.get("available"):
        return "unknown", "画面质量无法测量（%s）" % ((q or {}).get("reason") or "原因未知")
    mean = q.get("mean_luma")
    std = q.get("luma_std")
    if mean is None or std is None:
        return "unknown", "画面质量缺少亮度指标"
    if std < VERY_FLAT_STD:
        return "occluded", ("画面几乎没有任何明暗变化（块间标准差 %.1f < %.1f），"
                            "典型表现是镜头被挡住/盖住或对着纯色表面" % (std, VERY_FLAT_STD))
    if mean < DARK_LUMA_MAX and std < FLAT_STD_MAX:
        return "occluded", ("画面又暗又平（平均亮度 %.1f < %.1f，标准差 %.1f < %.1f），"
                            "典型表现是镜头被遮挡或在昏暗环境中对着镜头盖"
                            % (mean, DARK_LUMA_MAX, std, FLAT_STD_MAX))
    return "usable", ("画面有正常明暗分布（平均亮度 %.1f，标准差 %.1f）" % (mean, std))


def describe_quality(q):
    """一句人能读懂的话（界面直接用，不自己编文案）。"""
    if not q:
        return "没有画面质量数据"
    if not q.get("available"):
        return "画面质量无法测量：%s" % (q.get("reason") or "原因未知")
    return ("平均亮度 %.1f / 块间标准差 %.1f / 暗块占比 %.0f%%（采样 %d/%d 个 MCU，"
            "覆盖高度 %.0f%%，%s）"
            % (q.get("mean_luma") or 0, q.get("luma_std") or 0,
               100 * (q.get("dark_ratio") or 0), q.get("sample_mcus") or 0,
               q.get("total_mcus") or 0, q.get("sampled_rows_pct") or 0,
               q.get("subsampling") or "?"))
