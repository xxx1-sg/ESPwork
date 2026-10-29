# -*- coding: utf-8 -*-
"""voice_engine.py —— 第 5 周：语音引擎（识别 ASR / 合成 TTS），独立进程

为什么单独一个进程？
----------------------------------------------------------------------------
`backend/server.py` 全工程一直遵守「零第三方依赖、只用标准库」的约定，
而真实的语音识别/合成必然要引入运行时（离线识别模型、系统语音 COM 组件）。
所以这里**不把重依赖塞进主后端**，而是做成一个可独立启停的本机服务：

    server.py ──HTTP──► voice_engine.py (默认 127.0.0.1:8013)
                          ├─ ASR: vosk 离线中文模型（CPU，不联网）
                          └─ TTS: Windows SAPI 中文语音（离线）

这样做带来三个对本课重要的性质：
  1. **运行位置是可声明的**：每个 turn 都会记录 ASR/TTS 跑在哪台机器的哪个端口、
     用的哪个引擎/模型。设备音频没就绪时用电脑音频，页面与实际记录对得上。
  2. **故障是局部的**：引擎没起 / 超时 / 报错，主后端的文字入口
     （`POST /api/v1/nl/query`）一行代码都不受影响 —— 这是本周的红线之一。
  3. **可以单独换引擎**：换外部授权 ASR 服务时只改环境变量
     （`VOICE_ASR_BACKEND=http_openai_compat`），主后端与前端都不用动。

接口（只对本机监听，不对外暴露）
----------------------------------------------------------------------------
    GET  /health                     引擎清单与就绪状态（不含任何密钥）
    POST /asr      body=WAV           → {"ok":true,"text":"...","engine":...,"ms":...}
    POST /tts      body={"text":...}  → audio/wav（二进制）

环境变量
----------------------------------------------------------------------------
    VOICE_ENGINE_HOST        默认 127.0.0.1
    VOICE_ENGINE_PORT        默认 8013
    VOICE_ASR_BACKEND        vosk | http_openai_compat | none     （默认 vosk）
    VOICE_ASR_MODEL_DIR      vosk 模型目录，默认 <工程>/.models/vosk-model-small-cn-0.22
    VOICE_ASR_TIMEOUT_S      单次识别上限，默认 25
    VOICE_ASR_API_BASE       外部 ASR 服务基址（http_openai_compat 时必需）
    VOICE_ASR_API_KEY        外部 ASR 服务密钥（只在本进程内存里，绝不进响应）
    VOICE_ASR_API_MODEL      外部 ASR 模型名，默认 whisper-1
    VOICE_TTS_BACKEND        sapi | tone | none                   （默认 sapi，可自动降级）
    VOICE_TTS_VOICE          指定语音名片段，默认 Huihui
    VOICE_TTS_TIMEOUT_S      单次合成上限，默认 20
    VOICE_TTS_RATE           语速倍率，-10..10 的 SAPI 档位（默认 1；调大更快、更短）
    VOICE_TTS_FALLBACK       1=系统无音频设备时自动降级到 tone，0=不降级直接报错（默认 1）

★ 关于 tone（无音频设备时的降级合成）
---------------------------------------------------------------------------
SAPI 的 `Speak()` 即使只想写进文件，也要求系统至少有一个可用的音频输出设备
（`SpVoice.GetAudioOutputs()` 不为空）。实测在**没有音频适配器**的机器上，
`GetAudioOutputs()` 返回 0，`Speak()` 直接抛
`COMError(0x80045040 / 0x8004503A)`，所有 SAPI 路径（FileStream / MemoryStream /
默认输出）全部失败 —— 这不是代码问题，是机器没有声卡。

此时本引擎退回 `tone` 后端：**生成一段可被 VAD 判为"有声音"的合成音**，
用来把语音链路的**状态机、取消、隔离、澄清、时序**全部跑通。
它**不是语音**，`/tts` 的 `X-Tts-Engine` 头与每个 turn 的 `tts.engine`
都会如实写 `tone`，绝不允许被当成真人播报记录。
"""

import io
import json
import math
import os
import platform
import re
import struct
import sys
import threading
import time
import traceback
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

HOST = os.environ.get("VOICE_ENGINE_HOST", "127.0.0.1")
PORT = int(os.environ.get("VOICE_ENGINE_PORT", "8013"))

ASR_BACKEND = os.environ.get("VOICE_ASR_BACKEND", "vosk").strip().lower()
ASR_MODEL_DIR = os.environ.get(
    "VOICE_ASR_MODEL_DIR",
    os.path.join(PROJECT, ".models", "vosk-model-small-cn-0.22"))
ASR_TIMEOUT_S = float(os.environ.get("VOICE_ASR_TIMEOUT_S", "25"))
ASR_API_BASE = os.environ.get("VOICE_ASR_API_BASE", "").rstrip("/")
ASR_API_KEY = os.environ.get("VOICE_ASR_API_KEY", "")
ASR_API_MODEL = os.environ.get("VOICE_ASR_API_MODEL", "whisper-1")

TTS_BACKEND = os.environ.get("VOICE_TTS_BACKEND", "sapi").strip().lower()
TTS_VOICE = os.environ.get("VOICE_TTS_VOICE", "Huihui")
TTS_TIMEOUT_S = float(os.environ.get("VOICE_TTS_TIMEOUT_S", "20"))
# 语速：-10..10，0 是正常。默认略快一点（1），因为播报的多半是标识符和数字，
# 太慢会更难听清；可调到 0 或负值放慢。
TTS_RATE = int(os.environ.get("VOICE_TTS_RATE", "1"))
# 系统无音频设备时是否自动降级到 tone（见文件头说明）。默认开。
TTS_FALLBACK = os.environ.get("VOICE_TTS_FALLBACK", "1").strip() not in ("0", "false", "no")

MAX_ASR_BYTES = 8 * 1024 * 1024     # 16k/16bit/mono 下约 4 分钟，足够一句话
MAX_TTS_CHARS = 600                 # 播报文本上限（超过就如实拒绝，不静默截断改语义）

_ASR_LOCK = threading.Lock()        # vosk 的 Model 是线程安全的，但加载过程要互斥
_MODEL_CACHE = {}


# ===========================================================================
# WAV 解析（只认 PCM，格式必须说清楚；解析不了就拒绝，不猜）
# ===========================================================================
def wav_info(data):
    """返回 (info_dict) 或抛 ValueError（说明哪里不合法）。"""
    if len(data) < 44:
        raise ValueError("音频太短（%d 字节），不是有效 WAV" % len(data))
    if data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("不是 RIFF/WAVE 容器（头部为 %r）" % data[0:12])
    pos = 12
    fmt = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        (size,) = struct.unpack("<I", data[pos + 4:pos + 8])
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            if len(body) < 16:
                raise ValueError("fmt 块不完整")
            afmt, ch, rate, _bps, align, bits = struct.unpack("<HHIIHH", body[:16])
            fmt = {"format": afmt, "channels": ch, "sample_rate": rate,
                   "byte_rate": _bps, "block_align": align, "bits": bits}
        elif cid == b"data":
            if fmt is None:
                raise ValueError("data 块出现在 fmt 之前")
            frames = size // max(1, fmt["block_align"])
            info = dict(fmt)
            info["data_bytes"] = size
            info["frames"] = frames
            info["duration_ms"] = int(round(frames * 1000.0 / max(1, rate)))
            if afmt != 1:
                raise ValueError("只支持 PCM（format=1），收到 format=%d（可能被压缩过）" % afmt)
            if bits != 16:
                raise ValueError("只支持 16 bit 采样，收到 %d bit" % bits)
            if ch != 1:
                raise ValueError("只支持单声道，收到 %d 声道" % ch)
            if rate not in (8000, 16000, 22050, 32000, 44100, 48000):
                raise ValueError("采样率 %d Hz 不在支持范围" % rate)
            return info, body
        pos += 8 + size + (size & 1)
    raise ValueError("没有找到 data 块")


def pcm_stats(pcm):
    """真实统计：RMS / 峰值 / 过零率。全部由样本算出，不来自任何估计。"""
    n = len(pcm) // 2
    if n <= 0:
        return {"samples": 0, "rms": 0.0, "peak": 0.0, "zcr": 0.0, "rms_dbfs": -120.0}
    vals = struct.unpack("<%dh" % n, pcm[:n * 2])
    s2 = 0
    peak = 0
    zc = 0
    prev = 0
    for v in vals:
        s2 += v * v
        av = v if v >= 0 else -v
        if av > peak:
            peak = av
        if (v >= 0) != (prev >= 0):
            zc += 1
        prev = v
    rms = (s2 / float(n)) ** 0.5
    r = rms / 32768.0
    return {
        "samples": n,
        "rms": round(r, 6),
        "peak": round(peak / 32768.0, 6),
        "zcr": round(zc / float(n), 6),
        "rms_dbfs": round(-120.0 if r <= 0 else 20.0 * math.log10(r), 2),
    }


def resample_to_16k(pcm, src_rate):
    """线性插值重采样到 16 kHz（标准库即可，用于把 22.05k 的合成音回灌做自检）。"""
    if src_rate == 16000:
        return pcm
    n = len(pcm) // 2
    vals = struct.unpack("<%dh" % n, pcm[:n * 2])
    ratio = 16000.0 / float(src_rate)
    out_n = int(n * ratio)
    out = []
    for i in range(out_n):
        x = i / ratio
        i0 = int(x)
        i1 = min(n - 1, i0 + 1)
        f = x - i0
        out.append(int(vals[i0] * (1 - f) + vals[i1] * f))
    return struct.pack("<%dh" % len(out), *out)


# ===========================================================================
# ASR：vosk 离线模型
# ===========================================================================
def _vosk_model():
    if "vosk" in _MODEL_CACHE:
        return _MODEL_CACHE["vosk"]
    if not os.path.isdir(ASR_MODEL_DIR):
        raise RuntimeError("vosk 模型目录不存在：%s" % ASR_MODEL_DIR)
    from vosk import Model, SetLogLevel
    SetLogLevel(-1)
    t0 = time.time()
    m = Model(ASR_MODEL_DIR)
    _MODEL_CACHE["vosk"] = m
    _MODEL_CACHE["vosk_load_ms"] = int((time.time() - t0) * 1000)
    return m


def asr_vosk(pcm16k):
    from vosk import KaldiRecognizer
    with _ASR_LOCK:
        model = _vosk_model()
    rec = KaldiRecognizer(model, 16000)
    rec.SetWords(False)
    t0 = time.time()
    # 分块喂入：大音频一次性 AcceptWaveform 会让内存峰值很高
    chunk = 16000 * 2 * 2       # 2 秒
    for i in range(0, len(pcm16k), chunk):
        rec.AcceptWaveform(pcm16k[i:i + chunk])
    tail = json.loads(rec.FinalResult() or "{}")
    ms = int((time.time() - t0) * 1000)
    text = (tail.get("text") or "").strip()
    return {"text": text, "ms": ms, "raw_keys": sorted(tail.keys())}


# ===========================================================================
# ASR：外部 OpenAI 兼容端点（真实调用失败就如实报错，不做任何本地兜底）
# ===========================================================================
def asr_openai(pcm16k):
    import urllib.error
    import urllib.request
    import uuid
    if not ASR_API_BASE:
        raise RuntimeError("未配置 VOICE_ASR_API_BASE")
    if not ASR_API_KEY:
        raise RuntimeError("未配置 VOICE_ASR_API_KEY")
    # 手工拼 multipart/form-data（标准库没有现成封装）
    boundary = "----voice" + uuid.uuid4().hex
    wav = wav_bytes_from_pcm16k(pcm16k)
    parts = []
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n%s\r\n"
                  % (boundary, ASR_API_MODEL)).encode("utf-8"))
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\nzh\r\n"
                  % boundary).encode("utf-8"))
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                  "Content-Type: audio/wav\r\n\r\n" % boundary).encode("utf-8"))
    parts.append(wav)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(
        ASR_API_BASE + "/audio/transcriptions", data=body, method="POST",
        headers={"Authorization": "Bearer " + ASR_API_KEY,
                 "Content-Type": "multipart/form-data; boundary=" + boundary})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=ASR_TIMEOUT_S) as r:
        payload = json.loads(r.read().decode("utf-8"))
    ms = int((time.time() - t0) * 1000)
    return {"text": (payload.get("text") or "").strip(), "ms": ms,
            "raw_keys": sorted(payload.keys())}


def wav_bytes_from_pcm16k(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return buf.getvalue()


def do_asr(pcm16k):
    if ASR_BACKEND == "none":
        raise RuntimeError("ASR 引擎未配置（VOICE_ASR_BACKEND=none）")
    if ASR_BACKEND == "vosk":
        r = asr_vosk(pcm16k)
        r["engine"] = "vosk"
        r["model"] = os.path.basename(ASR_MODEL_DIR.rstrip("\\/"))
        r["offline"] = True
        return r
    if ASR_BACKEND == "http_openai_compat":
        r = asr_openai(pcm16k)
        r["engine"] = "http_openai_compat"
        r["model"] = ASR_API_MODEL
        r["offline"] = False
        return r
    raise RuntimeError("未知的 ASR 后端：%s" % ASR_BACKEND)


# ===========================================================================
# TTS：Windows SAPI（中文语音，离线）
# ===========================================================================
_tts_lock = threading.Lock()


def tts_sapi(text):
    """用系统 SAPI 合成 WAV。返回 (wav_bytes, info)。

    ★ 这里只做"合成"，不播放。合成成功 ≠ 用户听到 —— 播放发生在浏览器，
      是否真的听到只有现场能判断（docs/13 §5 红线 V4）。
    """
    import comtypes
    import comtypes.client
    with _tts_lock:
        t0 = time.time()
        # 同 _sapi_audio_outputs()：HTTP 工作线程里必须先 CoInitialize，否则
        # CreateObject 直接抛 "尚未调用 CoInitialize"，看起来像引擎坏了。
        try:
            comtypes.CoInitialize()
        except Exception:
            pass
        voice = comtypes.client.CreateObject("SAPI.SpVoice")
        stream = comtypes.client.CreateObject("SAPI.SpFileStream")
        tmp = os.path.join(PROJECT, ".tmpbuild")
        os.makedirs(tmp, exist_ok=True)
        # ★ 固定文件名 + 覆盖写，**不是**每次新文件名 + os.remove。
        #   踩过的坑（第 6 周自检跑到一半突然 RemoteDisconnected）：每合成一句就
        #   remove 一个临时文件，宿主沙箱的 safe-delete 防护按"累计删除数"计数，
        #   到第 50 个就把这次删除拦下并直接切断连接 —— 引擎本身没崩（/health 仍 200），
        #   但调用方看到的是"HTTP 连接被对端关闭"，极容易误判成引擎挂掉。
        #   改成固定文件后：不产生删除动作，顺带省掉每句一次的文件创建。
        #   并发安全由上面的 _tts_lock 保证（同一时刻只有一个合成在写这个文件）。
        path = os.path.join(tmp, "tts_%d.wav" % os.getpid())
        picked = None
        try:
            toks = voice.GetVoices("", "")
            for i in range(toks.Count):
                desc = toks.Item(i).GetDescription()
                if TTS_VOICE.lower() in desc.lower():
                    voice.Voice = toks.Item(i)
                    picked = desc
                    break
            if picked is None and toks.Count:
                voice.Voice = toks.Item(0)
                picked = toks.Item(0).GetDescription()
            stream.Open(path, 3, False)      # 3 = SSFMCreateForWrite
            voice.AudioOutputStream = stream
            try:
                voice.Rate = TTS_RATE        # 语速，见文件头配置说明
            except Exception:
                pass
            voice.Speak(text)
            stream.Close()
            with open(path, "rb") as f:
                data = f.read()
        finally:
            # 刻意不删（见上面 path 处的说明）。留一个固定文件，进程退出时随 .tmpbuild 清掉。
            pass
        ms = int((time.time() - t0) * 1000)
    info, _ = wav_info(data)
    return data, {"voice": picked, "ms": ms, "audio_ms": info["duration_ms"],
                  "sample_rate": info["sample_rate"], "bits": info["bits"],
                  "channels": info["channels"], "bytes": len(data)}


def _sapi_audio_outputs():
    """返回 SAPI 能看到的音频输出设备数量。

    ★ 这是"能不能真发声"的前置判据：`GetAudioOutputs()` 返回 0 时，
      `Speak()` 必定失败（即使目标是文件流）。先探一次，好让错误信息
      直指"机器没有声卡"，而不是一句看不懂的 COMError 数字。

    ★ 注意 COM 单元：这个函数会在 HTTP 工作线程里被 /health 调用，
      而 comtypes 的 CreateObject 要求当前线程先 CoInitialize。
      直接调用会抛 `OSError [WinError -2147221008] 尚未调用 CoInitialize`，
      于是自述里变成"探测失败"，看着像引擎坏了。这里显式初始化一次
      （COINIT_APARTMENTTHREADED；每个工作线程都会各自初始化，是幂等的）。
    """
    import ctypes
    import comtypes
    import comtypes.client
    try:
        comtypes.CoInitialize()
    except Exception:
        # 已经初始化过（或由框架初始化）时忽略，不改变后续行为
        pass
    v = comtypes.client.CreateObject("SAPI.SpVoice")
    return v.GetAudioOutputs("", "").Count


def tts_tone(text):
    """无音频设备时的降级合成：生成一段"有声音"的合成音 WAV。

    ★ 它**不是语音**，只是让 VAD/时长/字节数这些与"有没有音频"相关的
      下游环节能被真实跑通。engine 字段固定为 "tone"，任何消费方
      都必须能看出这不是真人播报（见文件头说明）。

    生成方式：按文本长度决定时长（中文约 0.18 s/字，夹 0.6~8 s），
    用一句三音和弦 + 轻微包络，保证 RMS/峰值都高于 VAD 阈值
    （实测阈值 RMS≥0.006 / 峰值≥0.02）。
    """
    t0 = time.time()
    rate = 16000
    n_chars = max(1, len(text))
    # 时长与文本长度相关 —— 让"念长回答"比"念短回答"慢，时序才有区分度
    dur_s = max(0.6, min(8.0, 0.18 * n_chars))
    total = int(rate * dur_s)
    # 三个音高轮换，避免听起来像纯报警音；频率取语音常见频段
    freqs = (220.0, 330.0, 440.0)
    amp = 9000           # 约 0.27 满量程 → RMS 约 0.19，远高于阈值 0.006
    frames = bytearray()
    for i in range(total):
        tt = i / float(rate)
        f = freqs[(i // int(rate * 0.25)) % len(freqs)]
        # 10 ms 淡入淡出，避免爆音把 ZCR 推歪
        env = min(1.0, tt / 0.01, (dur_s - tt) / 0.01)
        s = int(amp * env * math.sin(2.0 * math.pi * f * tt))
        frames += struct.pack("<h", s)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    data = buf.getvalue()
    ms = int((time.time() - t0) * 1000)
    return data, {"voice": None, "ms": ms, "audio_ms": int(dur_s * 1000),
                  "sample_rate": rate, "bits": 16, "channels": 1,
                  "bytes": len(data), "engine": "tone",
                  "note": "系统无可用音频输出设备，这是合成音占位，不是语音"}


_FALLBACK_WARNED = set()


def _warn_fallback_once(err):
    """同类降级警告只打一次（按错误内容去重），避免刷屏淹没真正的诊断信息。"""
    key = type(err).__name__ + ":" + str(err)[:200]
    if key in _FALLBACK_WARNED:
        return
    _FALLBACK_WARNED.add(key)
    sys.stderr.write(
        "[voice] SAPI 合成不可用（%s），降级到 tone 合成音。\n"
        "        这一轮记录里的 tts.engine 会是 'tone'，不是语音。\n"
        "        同类警告本次只提示一次（每轮的事实仍逐条记在记录的 tts 段里）。\n" % (err,))


def do_tts(text):
    if TTS_BACKEND == "none":
        raise RuntimeError("TTS 引擎未配置（VOICE_TTS_BACKEND=none）")
    if TTS_BACKEND == "tone":
        return tts_tone(text)
    if TTS_BACKEND == "sapi":
        try:
            # 先探设备：0 个输出设备时 Speak() 必然失败，且错误码毫无信息量
            n_out = _sapi_audio_outputs()
            if n_out == 0:
                raise RuntimeError(
                    "系统没有可用的音频输出设备（SpVoice.GetAudioOutputs() 返回 0）。"
                    "SAPI 即使只想把语音写进文件也要求存在音频设备，所以无法合成。")
            data, info = tts_sapi(text)
            info["engine"] = "sapi"
            return data, info
        except Exception as e:
            if not TTS_FALLBACK:
                raise RuntimeError("SAPI 合成失败：%s" % (e,))
            # ★ 只在第一次降级时说一次。理由：SAPI 不可用是**环境状态**，
            #   一个自检跑下来会触发几十上百次，每轮都写两行 stderr 会把
            #   真正的诊断信息淹掉（实测一次全量回归刷了几百行）。
            #   第一次说清楚 + 之后静默，日志才可读；
            #   每轮的事实仍然完整落在记录的 tts.engine/fallback_reason 里。
            _warn_fallback_once(e)
            data, info = tts_tone(text)
            info["fallback_from"] = "sapi"
            info["fallback_reason"] = str(e)
            return data, info
    raise RuntimeError("未知的 TTS 后端：%s" % TTS_BACKEND)


# ===========================================================================
# HTTP 层
# ===========================================================================
class EngineServer(ThreadingHTTPServer):
    """线程化 HTTP 服务；把"客户端提前断开"这种正常收尾从错误日志里摘出去。

    为什么要管这件事：探活（`GET /health`）与超时取消都会让客户端先 close，
    此时服务端下一轮 `rfile.readline()` 会抛 ConnectionResetError（WinError 10054），
    socketserver 默认会打一整段 "Exception occurred during processing of request from …"
    ——它看起来像引擎故障，其实只是对方走了。真正的异常仍然照常打印。
    """
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


class H(BaseHTTPRequestHandler):
    server_version = "VoiceEngine/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("[voice %s] %s\n" % (time.strftime("%H:%M:%S"), fmt % a))

    def _json(self, status, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _bin(self, status, data, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, health())
        else:
            self._json(404, {"ok": False, "error": "未知接口"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_ASR_BYTES:
            self._json(400, {"ok": False, "error": "报文长度非法: %d" % n})
            return
        body = self.rfile.read(n)

        if path == "/asr":
            self._handle_asr(body)
        elif path == "/tts":
            self._handle_tts(body)
        else:
            self._json(404, {"ok": False, "error": "未知接口"})

    def _handle_asr(self, body):
        t_all = time.time()
        try:
            info, pcm = wav_info(body)
        except ValueError as e:
            self._json(400, {"ok": False, "code": "BAD_AUDIO", "error": str(e)})
            return
        try:
            pcm16k = resample_to_16k(pcm, info["sample_rate"])
        except Exception as e:
            self._json(400, {"ok": False, "code": "RESAMPLE_FAILED", "error": repr(e)})
            return
        stats = pcm_stats(pcm16k)
        try:
            r = do_asr(pcm16k)
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"ok": False, "code": "ASR_FAILED",
                             "error": "%s: %s" % (type(e).__name__, e),
                             "engine": ASR_BACKEND})
            return
        out = {"ok": True, "audio": info, "stats": stats,
               "total_ms": int((time.time() - t_all) * 1000)}
        out.update(r)
        # ★ 空文本不是成功：明确交给调用方判断（主后端会落成 no_speech）
        out["empty"] = (r.get("text") or "") == ""
        self._json(200, out)

    def _handle_tts(self, body):
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            self._json(400, {"ok": False, "code": "BAD_JSON", "error": "请求体不是 JSON"})
            return
        text = (payload.get("text") or "").strip()
        if not text:
            self._json(400, {"ok": False, "code": "EMPTY_TEXT", "error": "要合成的文本为空"})
            return
        if len(text) > MAX_TTS_CHARS:
            self._json(400, {"ok": False, "code": "TEXT_TOO_LONG",
                             "error": "播报文本 %d 字，超过上限 %d 字（不静默截断，避免改变语义）"
                                      % (len(text), MAX_TTS_CHARS)})
            return
        try:
            data, info = do_tts(text)
        except Exception as e:
            traceback.print_exc()
            self._json(500, {"ok": False, "code": "TTS_FAILED",
                             "error": "%s: %s" % (type(e).__name__, e),
                             "engine": TTS_BACKEND})
            return
        hdr = {"X-Tts-Engine": str(info.get("engine", TTS_BACKEND)),
               "X-Tts-Voice": (info.get("voice") or "").encode("utf-8", "ignore").decode("latin-1", "ignore"),
               "X-Tts-Ms": str(info.get("ms", 0)),
               "X-Tts-Audio-Ms": str(info.get("audio_ms", 0))}
        self._bin(200, data, "audio/wav", hdr)


def _tts_capability():
    """TTS 到底能不能真发声 —— 自述里要把这件事说清楚，而不是等请求时才炸。"""
    cap = {"backend": TTS_BACKEND, "voice_hint": TTS_VOICE,
           "timeout_s": TTS_TIMEOUT_S, "fallback_to_tone": TTS_FALLBACK}
    if TTS_BACKEND == "tone":
        cap.update({"speaks": False, "audio_outputs": None,
                    "note": "tone 后端：生成的是合成音占位，不是语音"})
        return cap
    if TTS_BACKEND != "sapi":
        cap.update({"speaks": None, "audio_outputs": None})
        return cap
    try:
        n = _sapi_audio_outputs()
        cap["audio_outputs"] = n
        cap["speaks"] = n > 0
        if n == 0:
            cap["degraded_to"] = "tone" if TTS_FALLBACK else None
            cap["note"] = ("系统没有可用的音频输出设备，SAPI 无法合成；"
                           + ("已降级到 tone 合成音（不是语音）" if TTS_FALLBACK
                              else "且已关闭降级，/tts 会失败"))
    except Exception as e:
        cap["audio_outputs"] = None
        cap["speaks"] = False
        cap["error"] = "%s: %s" % (type(e).__name__, e)
    return cap


def health():
    """引擎自述：只报"能不能用、用什么、在哪"，不报任何密钥。"""
    info = {
        "ok": True,
        "host": HOST,
        "port": PORT,
        "python": platform.python_version(),
        "os": platform.platform(),
        "asr": {"backend": ASR_BACKEND, "model_dir": ASR_MODEL_DIR,
                "model_present": os.path.isdir(ASR_MODEL_DIR),
                "timeout_s": ASR_TIMEOUT_S,
                "loaded": "vosk" in _MODEL_CACHE,
                "load_ms": _MODEL_CACHE.get("vosk_load_ms")},
        "tts": _tts_capability(),
        "limits": {"max_audio_bytes": MAX_ASR_BYTES, "max_tts_chars": MAX_TTS_CHARS},
    }
    if ASR_BACKEND == "http_openai_compat":
        info["asr"]["api_base"] = ASR_API_BASE
        info["asr"]["api_key_present"] = bool(ASR_API_KEY)   # 只报有没有，不报值
    return info


def main():
    print("=" * 70)
    print("语音引擎启动")
    print("  监听      : http://%s:%d" % (HOST, PORT))
    print("  ASR       : %s" % ASR_BACKEND)
    if ASR_BACKEND == "vosk":
        print("  模型目录  : %s (存在=%s)" % (ASR_MODEL_DIR, os.path.isdir(ASR_MODEL_DIR)))
        if os.path.isdir(ASR_MODEL_DIR):
            try:
                _vosk_model()
                print("  模型预热  : 完成，%d ms" % _MODEL_CACHE.get("vosk_load_ms", 0))
            except Exception as e:
                print("  模型预热  : 失败 %r（识别请求会返回 500 并说明原因）" % (e,))
        else:
            print("  ! 模型目录不存在：识别请求会 500。请先下载模型（docs/13 §2.2）")
    print("  TTS       : %s (voice hint=%s)" % (TTS_BACKEND, TTS_VOICE))
    print("=" * 70)
    srv = EngineServer((HOST, PORT), H)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
