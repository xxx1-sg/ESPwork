# -*- coding: utf-8 -*-
"""nl_assistant.py —— 第 4 周：受限工具语言助手（自然语言查询 / 请求采集）

设计原则（与整个工程的"不造假"红线一致）
----------------------------------------------------------------------------
1. **模型只做意图解析，不碰数据、不碰设备。**
   授权语言服务（OpenAI 兼容接口）的返回值只允许是
   {intent, device_hint, time_range_s, reason}，且 intent 必须在白名单里；
   任何越界字段 / 越界设备 / 越界数值都会被丢弃，解析失败就退回本地规则解析器。
   模型的密钥只从服务端环境变量读取，任何响应里都不会出现密钥。

2. **工具是受限的。**
   助手能调用的工具只有 7 个（见 TOOL_SPECS），每个都有明确的参数表和设备范围：
   设备必须已接入（devices 表里存在），否则直接 DEVICE_OUT_OF_SCOPE，
   绝不允许"传一个不存在的 device_id 让后端去猜"。
   读工具只读数据库；写工具只有一个 request_collect，且带节流与在途去重。

3. **"查看上次数据" ≠ "重新采集一次"。**
   前者只调用 get_latest_record / get_records，**不产生任何设备侧流量**
   （pending_commands 与 collect_requests 都不新增一行）；
   后者必须走 create_collect_request → 设备轮询取走指令 → 上传带 nonce 的观测，
   只有拿到 observation 才算"新记录已到"。

4. **没有设备完成证据，就不许说"已采集成功"。**
   回答模板按 claim 分档：只有 claim == "new_record" 才允许出现成功措辞；
   其余情况（在途 / 超时 / 设备离线 / 没有基线）一律如实说"尚未确认"。
   `_guard_answer()` 还会兜底剔除任何坐标样式文本（本工程不提供位置能力）。

5. **回答必须带来源、时间、状态。**
   每条回答的 evidence 里都有 source / observed_at_utc / age_ms / status，
   前端直接渲染这些字段，不自己算时间、不自己编状态。
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

# ===========================================================================
# 配置
# ===========================================================================
MAX_QUERY_CHARS = 200
MAX_HISTORY_LIMIT = 200
COLLECT_WAIT_MAX_MS = 20000          # 单次自然语言采集最多等多久（客户端可调小，不可调大）
COLLECT_MIN_INTERVAL_MS = int(os.environ.get("NL_COLLECT_MIN_INTERVAL_MS", "5000"))
STALE_AFTER_MS = int(os.environ.get("NL_STALE_AFTER_MS", "60000"))
DEFAULT_WAIT_MS = int(os.environ.get("NL_COLLECT_WAIT_MS", "8000"))

# 只有这些意图能被识别；模型返回别的一律拒绝
ALLOWED_INTENTS = (
    "query_latest", "query_history", "device_status", "collect_status",
    "request_collect", "location", "unknown",
)

INTENT_LABELS = {
    "query_latest": "读取已有记录（最新一条）",
    "query_history": "读取已有记录（时间范围）",
    "device_status": "读取设备状态",
    "collect_status": "查询上一次采集的结果",
    "request_collect": "请求一次新采集",
    "location": "位置查询（本工程不具备该能力）",
    "unknown": "未识别",
}

# 工具白名单。kind=read 只读数据库；kind=act 会产生设备侧流量。
TOOL_SPECS = [
    {
        "name": "list_devices",
        "kind": "read",
        "desc": "列出已接入设备（device_id、最后上报时间、在线状态、待下发指令数）",
        "params": {},
        "side_effect": "无（只读 devices / sensor_records）",
    },
    {
        "name": "get_device_status",
        "kind": "read",
        "desc": "读取单台设备的在线状态与最后上报时间",
        "params": {"device_id": "string，必须在已接入设备范围内"},
        "side_effect": "无",
    },
    {
        "name": "get_latest_record",
        "kind": "read",
        "desc": "读取某设备最新一条已存记录（不触发采集）",
        "params": {"device_id": "string，必须在已接入设备范围内"},
        "side_effect": "无（不排指令、不写库）",
    },
    {
        "name": "get_records",
        "kind": "read",
        "desc": "读取某设备最近 N 条记录（N ≤ 200）",
        "params": {"device_id": "string", "limit": "int 1..200"},
        "side_effect": "无",
    },
    {
        "name": "request_collect",
        "kind": "act",
        "desc": "请求设备立即采集一次（会产生一条 collect_now 指令，等待设备回报）",
        "params": {"device_id": "string", "client_request_key": "string，幂等键（由服务端生成）"},
        "side_effect": "向 pending_commands 排一条 collect_now；设备下次轮询取走",
        "limits": "同一设备 %.0f 秒内最多一次；已有在途请求时复用，不重复下发"
                  % (COLLECT_MIN_INTERVAL_MS / 1000.0),
    },
    {
        "name": "list_collect_requests",
        "kind": "read",
        "desc": "列出某设备最近的采集请求（用于回答『采集完成了吗』）",
        "params": {"device_id": "string", "limit": "int 1..20"},
        "side_effect": "无",
    },
    {
        "name": "get_collect_status",
        "kind": "read",
        "desc": "查询采集请求状态与绑定观测（判断新旧与完成证据的唯一依据）",
        "params": {"request_id": "string"},
        "side_effect": "无",
    },
]
_SPEC_BY_NAME = {t["name"]: t for t in TOOL_SPECS}

OPEN_REQUEST_STATES = ("submitted", "acked", "collecting")
FINAL_REQUEST_STATES = ("completed", "failed", "timeout", "rejected", "cancelled")

# ===========================================================================
# 运行时绑定（由 server.py 注入，避免模块反向 import 主程序）
# ===========================================================================
_RT = None
_bind_lock = threading.Lock()
_last_collect = {}          # device_id -> monotonic 秒
_rate_lock = threading.Lock()


def bind(**kwargs):
    """注入后端能力。必须是可调用对象，name 见下方 _rt 使用处。"""
    global _RT
    need = ("list_devices", "latest_record", "records", "create_collect",
            "get_collect", "list_requests", "server_time")
    missing = [k for k in need if not callable(kwargs.get(k))]
    if missing:
        raise RuntimeError("nl_assistant.bind 缺少回调: %s" % ",".join(missing))
    with _bind_lock:
        _RT = kwargs


def _rt():
    if _RT is None:
        raise RuntimeError("nl_assistant 未绑定后端能力（server.py 未调用 bind）")
    return _RT


def _now_iso():
    return _rt()["server_time"]()


# ===========================================================================
# 时间与文案工具
# ===========================================================================
def _parse_iso(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _fmt_local(iso):
    """UTC ISO → 北京时间（+08:00），仅用于给人看的文案。"""
    dt = _parse_iso(iso)
    if dt is None:
        return iso or "—"
    return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _age_ms(iso):
    dt = _parse_iso(iso)
    if dt is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds() * 1000))


def _age_text(ms):
    if ms is None:
        return "未知"
    s = ms / 1000.0
    if s < 1:
        return "不到 1 秒前"
    if s < 60:
        return "%.0f 秒前" % s
    if s < 3600:
        return "%.0f 分钟前" % (s / 60.0)
    if s < 86400:
        return "%.1f 小时前" % (s / 3600.0)
    return "%.1f 天前" % (s / 86400.0)


def _freshness(ms):
    if ms is None:
        return "unknown"
    if ms < 10000:
        return "live"
    if ms < STALE_AFTER_MS:
        return "recent"
    return "stale"


def _g(v, nd=4):
    try:
        return ("%." + str(nd) + "f") % float(v)
    except (TypeError, ValueError):
        return "—"


def _error(status, code, message, retryable=False, **details):
    err = {"code": code, "message": message, "retryable": bool(retryable)}
    if details:
        err["details"] = details
    return status, {"ok": False, "server_time_utc": _now_iso(), "error": err}


def _ok(data):
    return 200, {"ok": True, "server_time_utc": _now_iso(), "data": data}


def _llm_cfg():
    base = (os.environ.get("NL_LLM_BASE_URL") or "").rstrip("/")
    key = os.environ.get("NL_LLM_API_KEY") or ""
    model = os.environ.get("NL_LLM_MODEL") or ""
    enabled_env = (os.environ.get("NL_LLM_ENABLED") or "auto").lower()
    configured = bool(base and key and model)
    enabled = configured and enabled_env not in ("0", "false", "off", "no")
    return {
        "configured": configured,
        "enabled": enabled,
        "base_url": base,
        "model": model or None,
        "timeout_s": float(os.environ.get("NL_LLM_TIMEOUT_S", "8")),
        "has_key": bool(key),       # 只暴露布尔，绝不回传密钥
    }


# ===========================================================================
# 意图解析：本地规则（默认、可离线）
# ===========================================================================
RE_COLLECT = re.compile(
    r"(重新采集|重新采|再采集|再采一次|再采|采集一次|采集一下|采一次|采一下|"
    r"现在采集|立即采集|马上采集|立刻采集|重新测|再测一次|采集最新|刷新采集|来一次采集)")
RE_DANGER = re.compile(
    r"(清空|删库|删除|删掉|清除|重置|改写|修改|写入|补数据|重启设备|关机|刷机|格式化)")
RE_COLLECT_ASK = re.compile(
    r"((采集|采|抓拍|拍照)(的)?(完成|好|到)了?吗|采到没|采集状态|刚才那(次|个)采集|上一次采集的结果)")
RE_READ_STRONG = re.compile(
    r"(上次|上一次|上回|历史|已有|之前|刚才|刚采的|旧数据|最近一次|现有|原来)")
RE_LOCATION = re.compile(r"(坐标|经纬度|位置|定位|GPS|gps|在哪|哪里|什么地方)")
RE_STATUS = re.compile(
    r"(在线|离线|状态|连上了吗|连上没|有没有连|还连着|心跳|还活着|掉线)")
RE_READ = re.compile(
    r"(数据|记录|读数|数值|多少|查看|看看|看一下|显示|查询|采样|读一下)")
RE_HISTORY = re.compile(r"(历史|最近\s*\d+\s*(秒|分钟|分|小时|天|条)|今天|过去|这段时间)")
RE_TIME = re.compile(r"最近\s*(\d+)\s*(秒|分钟|分|小时|天)")


def _rule_parse(text):
    t = text.strip()
    notes = []
    intent = "unknown"
    if RE_DANGER.search(t):
        # ★ 写/删类动词必须先于"数据"这类弱关键词判定。
        #   否则「把数据库清空」会因为包含"数据"被误判成"查看记录"——把危险请求
        #   当成只读请求，是比答错更严重的问题。
        intent = "unknown"
        notes.append("检测到写/删类动作；助手没有对应工具，不会执行")
    elif RE_LOCATION.search(t):
        intent = "location"
        notes.append("命中位置类关键词；本工程数据包不含位置字段")
    elif RE_COLLECT_ASK.search(t):
        intent = "collect_status"
        notes.append("命中「上一次采集结果」措辞：只查请求状态，不重新下发")
    elif RE_COLLECT.search(t) and not RE_READ_STRONG.search(t):
        intent = "request_collect"
        notes.append("命中「动作」措辞：需要设备侧新数据")
    elif RE_STATUS.search(t) and not RE_READ_STRONG.search(t) and \
            not re.search(r"(数据|读数|记录|采样)", t):
        intent = "device_status"
        notes.append("命中设备状态关键词")
    elif RE_READ_STRONG.search(t) or RE_READ.search(t):
        if RE_HISTORY.search(t):
            intent = "query_history"
            notes.append("命中时间范围/历史类措辞")
        else:
            intent = "query_latest"
            notes.append("命中「查看」类措辞：只读已有记录")

    tr = None
    m = RE_TIME.search(t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        mul = {"秒": 1, "分钟": 60, "分": 60, "小时": 3600, "天": 86400}[unit]
        tr = n * mul

    limit = None
    m2 = re.search(r"(\d+)\s*条", t)
    if m2:
        limit = max(1, min(int(m2.group(1)), MAX_HISTORY_LIMIT))

    conf = 0.55
    if intent in ("request_collect", "location"):
        conf = 0.93
    elif intent == "query_latest" and RE_READ_STRONG.search(t):
        conf = 0.9
    elif intent == "query_history":
        conf = 0.85
    elif intent == "device_status":
        conf = 0.9

    return {
        "intent": intent,
        "device_hint": None,
        "time_range_s": tr,
        "limit": limit,
        "confidence": conf,
        "parser": "rule",
        "notes": notes,
        "raw": t,
    }


# ===========================================================================
# 意图解析：授权语言服务（可选，密钥只在服务端）
# ===========================================================================
_LLM_SYSTEM = (
    "你是意图解析器，只输出一个 JSON 对象，不要输出解释或 Markdown。\n"
    "字段：intent（必须是 query_latest/query_history/device_status/request_collect/location/unknown 之一）、"
    "device_hint（用户提到的设备名或字符串，未提到填 null）、"
    "time_range_s（用户说最近多久，换算成秒；未说填 null）、"
    "confidence（0~1）、reason（一句话）。\n"
    "规则：\n"
    "1) 「查看上次数据 / 看一下记录 / 最新读数 / 上次采集的数据」→ query_latest（只读，不产生新采集）。\n"
    "2) 「重新采集一次 / 再采一次 / 现在采一次 / 要新的数据」→ request_collect。\n"
    "2.5) 「采集完成了吗 / 上次采集的结果 / 采到没有」→ collect_status（只查状态，不重新采）。\n"
    "3) 提到位置、坐标、经纬度、在哪 → location。\n"
    "4) 不得编造设备 ID、数值、时间；无法判断填 unknown。\n"
    "5) 不要说\"已采集成功\"之类结论，你只负责分类。"
)


def _llm_parse(text, device_ids):
    cfg = _llm_cfg()
    if not cfg["enabled"]:
        return None, "LLM_DISABLED"
    url = cfg["base_url"] + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "temperature": 0,
        "max_tokens": 300,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user", "content":
                "已接入设备：%s\n用户原话：%s" % (", ".join(device_ids) or "（无）", text)},
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ.get("NL_LLM_API_KEY", ""),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout_s"]) as r:
            raw = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return None, "LLM_HTTP_ERROR: %s" % type(e).__name__
    try:
        obj = json.loads(raw)
        content = obj["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return None, "LLM_NOT_OBJECT"
    except (KeyError, IndexError, TypeError, ValueError):
        return None, "LLM_BAD_JSON"

    intent = parsed.get("intent")
    if intent not in ALLOWED_INTENTS:
        return None, "LLM_INTENT_OUT_OF_WHITELIST"
    hint = parsed.get("device_hint")
    if hint is not None and not isinstance(hint, str):
        hint = None
    tr = parsed.get("time_range_s")
    if not isinstance(tr, (int, float)) or tr <= 0 or tr > 30 * 86400:
        tr = None
    try:
        conf = float(parsed.get("confidence", 0.6))
    except (TypeError, ValueError):
        conf = 0.6
    conf = max(0.0, min(1.0, conf))
    return {
        "intent": intent,
        "device_hint": hint,
        "time_range_s": int(tr) if tr else None,
        "limit": None,
        "confidence": conf,
        "parser": "llm",
        "notes": [str(parsed.get("reason") or "")[:120]],
        "raw": text,
    }, None


# ===========================================================================
# 设备解析 / 范围校验
# ===========================================================================
def _device_brief(d):
    return {
        "device_id": d.get("device_id"),
        "last_seen_utc": d.get("last_seen_utc") or d.get("last_seen"),
        "last_status": d.get("last_status"),
        "pending_commands": d.get("pending_commands"),
        "records": d.get("n"),
    }


def _normalize_devices(body):
    if not isinstance(body, dict):
        return []
    return body.get("devices") or []


def _resolve_device(text, ctx_device, devices, hint=None):
    """返回 (device_id, problem, candidates)。

    problem ∈ (None, 'out_of_scope', 'ambiguous')
    """
    ids = [d["device_id"] for d in devices]
    if ctx_device:
        hit = [i for i in ids if i.lower() == str(ctx_device).lower()]
        if hit:
            return hit[0], None, []
        return None, "out_of_scope", ids

    low = (text or "").lower()
    # 文本里点名了一台"像设备但没接入"的 ID（如 esp32s3eye-HACK）→ 直接越界，
    # 不能因为其余设备存在就当成歧义去反问。
    foreign = []
    for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-]{4,}", text or ""):
        if tok.lower().startswith("esp32") and tok.lower() not in [i.lower() for i in ids]:
            foreign.append(tok)
    if foreign:
        return None, "out_of_scope", foreign + ids

    mentioned = []
    for i in ids:
        if i.lower() in low:
            mentioned.append(i)
    if not mentioned:
        for i in ids:
            tail = i.split("-")[-1].lower()
            if len(tail) >= 4 and tail in low:
                mentioned.append(i)
    if hint:
        h = hint.lower()
        for i in ids:
            if i.lower() == h or (len(h) >= 4 and (h in i.lower() or i.split("-")[-1].lower() in h)):
                mentioned.append(i)
    mentioned = sorted(set(mentioned), key=mentioned.index)

    if len(mentioned) == 1:
        return mentioned[0], None, []
    if len(mentioned) > 1:
        return None, "ambiguous", mentioned
    if len(ids) == 1:
        return ids[0], None, []
    if not ids:
        return None, "no_device", []
    return None, "ambiguous", ids


def _find_device(devices, did):
    for d in devices:
        if d.get("device_id") == did:
            return d
    return None


# ===========================================================================
# 工具调用（统一留痕：名称、参数、耗时、结果摘要）
# ===========================================================================
def _digest(obj):
    """只留可核对的最小事实，避免把整条记录塞进 trace。"""
    if not isinstance(obj, dict):
        return None
    d = {}
    for k in ("id", "record_id", "request_id", "status", "received_at_utc",
              "completed_at_utc", "device_id", "count"):
        if k in obj and obj[k] is not None:
            d[k] = obj[k]
    return d or None


def _call(name, args, fn, trace):
    t0 = time.time()
    try:
        res = fn()
        ok = True
        payload = res[1] if isinstance(res, tuple) and len(res) == 2 else res
    except Exception as e:                                  # 工具内部异常 → 如实记录，不伪装成功
        ok = False
        payload = {"ok": False, "error": {"code": "TOOL_EXCEPTION",
                                          "message": "%s: %s" % (type(e).__name__, e)}}
    ms = int((time.time() - t0) * 1000)
    trace.append({
        "name": name,
        "args": args,
        "ok": bool(ok and (payload or {}).get("ok", True)),
        "ms": ms,
        "digest": _digest((payload.get("data") or payload) if isinstance(payload, dict) else None),
    })
    return payload


def _status_of(payload):
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"].get("status")
    return None


# ===========================================================================
# 回答兜底：不得出现"假成功"与"坐标"
# ===========================================================================
# ★ 判"坐标"不能只看"数字.数字,数字"，否则三轴加速度 (0.0049, 0.9951) 会被误判。
#   真坐标的整数部分通常 ≥ 5（北京 39.9/116.4），且小数位 ≥ 4。
RE_COORD_PAIR = re.compile(r"(\d{1,3}\.\d{4,})\s*[,，]\s*(\d{1,3}\.\d{4,})")
RE_COORD_DMS = re.compile(
    r"([NSns]\s*\d{1,3}\s*°)|(\d{1,3}\s*°\s*[NSEWnsew])|(经纬度\s*[:：])")
RE_SUCCESS = re.compile(r"(已采集成功|采集成功|已成功采集|成功采集到)")


def _has_coordinates(text):
    for m in RE_COORD_PAIR.finditer(text or ""):
        try:
            if float(m.group(1)) >= 5 and float(m.group(2)) >= 5:
                return True
        except ValueError:
            continue
    return bool(RE_COORD_DMS.search(text or ""))


def _guard_answer(answer, claim):
    """最后一道闸：只有拿到新观测才允许成功措辞；任何坐标样式一律剔除。"""
    flags = []
    if claim != "new_record" and RE_SUCCESS.search(answer):
        answer = RE_SUCCESS.sub("尚未确认采集完成", answer)
        flags.append("success_word_blocked")
    if _has_coordinates(answer):
        answer = ("本工程的数据包只含加速度、序号与时间戳，不含任何位置字段；"
                  "本助手不会生成或推断坐标。")
        flags.append("coordinates_stripped")
    return answer, flags


# ===========================================================================
# 各意图的回答组装
# ===========================================================================
def _err_dict(payload):
    """把工具返回的 error 统一成 dict。

    ★ 真实踩到的坑（第 6 周用"两台设备但都没上传过记录"的夹具测出来）：
      `query_latest()` 在没有记录时返回
          {"ok": False, "error": "数据库中还没有任何真实记录（设备尚未上传）"}
      —— `error` 是一个**字符串**（第 1 周遗留写法），而这里原来写的是
      `err = payload.get("error") or {}` 然后 `err.get("code")`，
      于是 AttributeError，整个 `/api/v1/nl/query` 直接 500。
      触发条件很常见：板子刚上电、刚进了 devices 表、还没上传第一条数据，
      用户此时问「查看上次数据」就会 500。
    """
    e = payload.get("error")
    if isinstance(e, dict):
        return e
    if isinstance(e, str):
        return {"code": "TOOL_ERROR", "message": e}
    return {}


def _answer_latest(text, did, dev, payload, trace):
    if not payload.get("ok"):
        err = _err_dict(payload)
        return {
            "status": "no_data", "claim": "no_data",
            "answer": "设备 %s 目前还没有任何已存记录（后端没有编造一条给你）。\n"
                      "如果你要现在的数据，请说「重新采集一次」。" % did,
            "error_code": err.get("code") or "NO_RECORD",
            "evidence": {"source": "sensor_records（空）", "device_id": did,
                         "observed_at_utc": None, "age_ms": None,
                         "freshness": "unknown", "device_command_sent": False},
        }

    rec = payload.get("record") or (payload.get("data") or {}).get("record")
    age = _age_ms(rec.get("received_at_utc")) if rec.get("received_at_utc") else None
    fresh = _freshness(age)
    mag = _g(rec.get("magnitude_g"))
    ax = rec.get("accel_g") or {}
    note = ""
    if fresh == "stale":
        note = "\n注意：这条记录距今 %s，属于旧数据，不代表设备此刻的状态（本次没有向设备要新数据）。" % _age_text(age)

    ans = (
        "「%s」→ 读取已有记录（只读，未向设备下发任何指令）。\n"
        "设备：%s\n"
        "采样时间：%s（设备时钟 local）/ 服务器接收：%s（%s）\n"
        "合矢量 |a| = %s g；三轴 g = (%s, %s, %s)\n"
        "来源：sensor_records.id=%s（received_at_utc=%s）\n"
        "状态：这是数据库中已经存在的一条记录，不是本次新采的。%s"
    ) % (text, did,
         rec.get("ts_local") or "—",
         _fmt_local(rec.get("received_at_utc")),
         _age_text(age), mag,
         _g(ax.get("x")), _g(ax.get("y")), _g(ax.get("z")),
         rec.get("id"), rec.get("received_at_utc"), note)

    return {
        "status": "answered", "claim": "old_record",
        "answer": ans,
        "evidence": {
            "source": "sensor_records.id=%s" % rec.get("id"),
            "device_id": did,
            "record_id": rec.get("id"),
            "observed_at_utc": rec.get("received_at_utc"),
            "received_at_utc": rec.get("received_at_utc"),
            "age_ms": age,
            "freshness": fresh,
            "device_command_sent": False,
            "collect_request_id": None,
            "collect_status": None,
        },
    }


def _answer_history(text, did, dev, parsed, payload, trace):
    if not payload.get("ok"):
        return {
            "status": "no_data", "claim": "no_data",
            "answer": "设备 %s 还没有任何已存记录。" % did,
            "evidence": {"source": "sensor_records（空）", "device_id": did,
                         "observed_at_utc": None, "age_ms": None,
                         "freshness": "unknown", "device_command_sent": False},
        }
    recs = payload.get("records") or (payload.get("data") or {}).get("records") or []
    if not recs:
        return {
            "status": "no_data", "claim": "no_data",
            "answer": "设备 %s 在数据库里没有符合条件的记录（没有补齐、没有插值）。" % did,
            "evidence": {"source": "sensor_records（0 条）", "device_id": did,
                         "observed_at_utc": None, "age_ms": None,
                         "freshness": "unknown", "device_command_sent": False},
        }

    now_ms = _age_ms(recs[-1].get("received_at_utc"))
    window = parsed.get("time_range_s")
    if window:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window)
        recs = [r for r in recs if (_parse_iso(r.get("received_at_utc")) or cutoff) >= cutoff]
        if not recs:
            return {
                "status": "no_data", "claim": "no_data",
                "answer": "最近 %d 秒内没有已存记录（设备可能没在上报）。本次没有触发采集。" % window,
                "evidence": {"source": "sensor_records（该窗口 0 条）", "device_id": did,
                             "observed_at_utc": None, "age_ms": None,
                             "freshness": "unknown", "device_command_sent": False},
            }

    first, last = recs[0], recs[-1]
    ages = [_age_ms(r.get("received_at_utc")) for r in recs]
    newest_age = age0 = ages[-1]
    ans = (
        "「%s」→ 读取已有记录（只读，未向设备下发任何指令）。\n"
        "设备：%s；共 %d 条记录。\n"
        "时间范围：%s → %s（北京时间）\n"
        "最新一条：sensor_records.id=%s，|a| = %s g，距今 %s。\n"
        "来源：sensor_records（id %s..%s）；没有插值、没有补点。\n"
        "状态：以上全部是数据库中已有的历史记录。"
    ) % (text, did, len(recs),
         _fmt_local(first.get("received_at_utc")), _fmt_local(last.get("received_at_utc")),
         last.get("id"), _g(last.get("magnitude_g")), _age_text(newest_age),
         first.get("id"), last.get("id"))
    return {
        "status": "answered", "claim": "old_record",
        "answer": ans,
        "evidence": {
            "source": "sensor_records.id=%s..%s" % (first.get("id"), last.get("id")),
            "device_id": did,
            "record_id": last.get("id"),
            "observed_at_utc": last.get("received_at_utc"),
            "received_at_utc": last.get("received_at_utc"),
            "age_ms": newest_age,
            "freshness": _freshness(newest_age),
            "count": len(recs),
            "device_command_sent": False,
            "collect_request_id": None,
            "collect_status": None,
        },
    }


def _answer_status(text, did, dev, payload, trace):
    d = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if did is None:
        devs = d.get("devices") or []
        lines = ["「%s」→ 读取设备状态（只读，未触发采集）。" % text]
        for x in devs:
            age = _age_ms(x.get("last_seen_utc"))
            lines.append("· %s：%s；最后上报 %s（%s）；待下发指令 %s 条" % (
                x.get("device_id"), x.get("last_status") or "未知",
                _fmt_local(x.get("last_seen_utc")), _age_text(age),
                x.get("pending_commands")))
        lines.append("来源：devices 表（last_seen_utc / last_status）与 sensor_records 最后接收时间。")
        return {
            "status": "answered", "claim": "none", "answer": "\n".join(lines),
            "evidence": {"source": "devices", "device_id": None,
                         "observed_at_utc": None, "age_ms": None,
                         "freshness": "unknown", "device_command_sent": False,
                         "collect_request_id": None, "collect_status": None},
        }

    d = dev or {}
    age = _age_ms(d.get("last_seen_utc"))
    online = (d.get("last_status") or "").lower()
    verdict = "在线" if online in ("online", "ok", "") and (age is not None and age < 60000) else "离线或状态未知"
    ans = (
        "「%s」→ 读取设备状态（只读，未触发采集）。\n"
        "设备：%s\n"
        "判定：%s\n"
        "最后上报：%s（距今 %s）\n"
        "设备自报状态：%s；待下发指令：%s 条\n"
        "来源：devices.last_seen_utc / devices.last_status；sensor_records 最后一条接收时间。"
    ) % (text, did, verdict, _fmt_local(d.get("last_seen_utc")), _age_text(age),
         d.get("last_status") or "未知", d.get("pending_commands"))
    if age is None or age >= 60000:
        ans += "\n注意：最后上报已超过 60 秒，此时请求采集很可能等不到设备回报。"
    return {
        "status": "answered", "claim": "none", "answer": ans,
        "evidence": {
            "source": "devices.device_id=%s" % did,
            "device_id": did,
            "observed_at_utc": d.get("last_seen_utc"),
            "received_at_utc": d.get("last_seen_utc"),
            "age_ms": age,
            "freshness": _freshness(age),
            "device_command_sent": False,
            "collect_request_id": None,
            "collect_status": None,
        },
    }


def _open_request(device_id):
    """查在途请求：避免用户连说两次「重新采集」就排两条指令。"""
    try:
        res = _rt()["list_requests"](device_id=device_id, limit=10)
        payload = res[1] if isinstance(res, tuple) and len(res) == 2 else res
    except Exception:
        return None
    data = (payload or {}).get("data") or {}
    for r in data.get("requests") or []:
        if r.get("status") in OPEN_REQUEST_STATES:
            age = _age_ms(r.get("submitted_at_utc"))
            if age is None or age < 120000:
                return r
    return None


def _rate_limited(device_id):
    now = time.monotonic()
    with _rate_lock:
        last = _last_collect.get(device_id)
        if last is not None:
            left = COLLECT_MIN_INTERVAL_MS - (now - last) * 1000.0
            if left > 0:
                return int(left)
    return 0


def _mark_collect(device_id):
    with _rate_lock:
        _last_collect[device_id] = time.monotonic()


def _collect_wait(rid, wait_ms, trace):
    """轮询请求状态直到终态或超时。返回最后一次 get_collect 的 data。"""
    deadline = time.monotonic() + wait_ms / 1000.0
    last = None
    polls = 0
    while True:
        payload = _call("get_collect_status", {"request_id": rid},
                        lambda: _rt()["get_collect"](rid), trace)
        polls += 1
        if payload.get("ok"):
            last = payload.get("data")
            if last.get("status") in FINAL_REQUEST_STATES:
                return last, polls
        if time.monotonic() >= deadline:
            return last, polls
        time.sleep(0.25)


def _answer_collect_status(text, did, dev, trace):
    """回答「采集完成了吗」——只查请求状态，绝不重新下发。"""
    payload = _call("list_collect_requests", {"device_id": did, "limit": 1},
                    lambda: _rt()["list_requests"](device_id=did, limit=1), trace)
    data = (payload or {}).get("data") or {}
    reqs = data.get("requests") or []
    if not reqs:
        return {
            "status": "no_data", "claim": "none",
            "answer": "「%s」：设备 %s 还没有任何采集请求记录。\n"
                      "如果你要一次新的采集，请说「重新采集一次」。" % (text, did),
            "evidence": {"source": "collect_requests（0 条）", "device_id": did,
                         "observed_at_utc": None, "age_ms": None,
                         "freshness": "unknown", "device_command_sent": False,
                         "collect_request_id": None, "collect_status": None},
        }

    rid = reqs[0].get("request_id")
    detail = _call("get_collect_status", {"request_id": rid},
                   lambda: _rt()["get_collect"](rid), trace)
    d = (detail or {}).get("data") or {}
    status = d.get("status") or reqs[0].get("status") or "unknown"
    obs = d.get("observation")
    dispatched = bool(d.get("dispatched_at_utc")) or status in ("acked", "collecting", "completed")
    base_ev = {
        "source": "collect_requests.request_id=%s" % rid,
        "device_id": did,
        "record_id": (obs or {}).get("record_id"),
        "observation_id": (obs or {}).get("observation_id"),
        "observed_at_utc": d.get("completed_at_utc") or d.get("submitted_at_utc"),
        "received_at_utc": d.get("completed_at_utc"),
        "age_ms": _age_ms(d.get("completed_at_utc")),
        "freshness": _freshness(_age_ms(d.get("completed_at_utc"))),
        "device_command_sent": dispatched,
        "collect_request_id": rid,
        "collect_status": status,
    }

    head = ("「%s」→ 查询最近一次采集请求（只读，不会重新下发）。\n"
            "设备：%s\n请求：request_id=%s\n提交：%s；下发：%s；回执：%s；完成：%s\n"
            "当前状态：%s\n") % (
        text, did, rid, _fmt_local(d.get("submitted_at_utc")),
        _fmt_local(d.get("dispatched_at_utc")), _fmt_local(d.get("acked_at_utc")),
        _fmt_local(d.get("completed_at_utc")), status)

    if status == "completed" and isinstance(obs, dict) and obs.get("record_id"):
        ans = head + ("设备回报完成：新观测 record_id=%s，|a| = %s g。\n"
                      "来源：observations.observation_id=%s。\n"
                      "状态：completed —— 这是那次请求产生的新记录，不是已有旧记录。") % (
            obs.get("record_id"), _g(obs.get("magnitude_g")), obs.get("observation_id"))
        claim = "new_record"
    elif status in ("submitted", "acked", "collecting"):
        ans = head + ("还没有拿到完成证据（设备%s），因此不能确认已采到新数据。\n"
                      "可以稍后再问一次，或打开 /api/v1/collect_requests/%s 看详情。") % (
            "尚未回执" if not d.get("acked_at_utc") else "已回执但尚未完成", rid)
        claim = "pending"
    else:
        ans = head + ("该请求已结束但未产生新观测，本次没有可用的新数据。\n"
                      "设备最后上报：%s（%s）。") % (
            _fmt_local((dev or {}).get("last_seen_utc")),
            _age_text(_age_ms((dev or {}).get("last_seen_utc"))))
        claim = "no_evidence"

    return {"status": "answered", "claim": claim, "answer": ans, "evidence": base_ev}


def _answer_collect(text, did, dev, parsed, wait_ms, trace, reused=None):
    # ---- 1) 在途请求优先复用（连说两次「重新采集」不应排两条指令）----
    pending = reused or _open_request(did)

    # ---- 2) 节流：仅对"真的新建指令"生效 ----
    left = _rate_limited(did)
    if left > 0 and not pending:
        return {
            "status": "rate_limited", "claim": "none",
            "answer": "刚刚已经为 %s 请求过一次采集，请 %d 秒后再试（避免连续下发指令把设备压垮）。" % (did, int(left / 1000) + 1),
            "error_code": "COLLECT_RATE_LIMITED",
            "retry_after_ms": left,
            "evidence": {"source": "nl 工具节流", "device_id": did,
                         "observed_at_utc": None, "age_ms": None,
                         "freshness": "unknown", "device_command_sent": False,
                         "collect_request_id": None, "collect_status": None},
        }

    # ---- 3) 新建指令 ----
    created_new = False
    if pending:
        rid = pending.get("request_id")
        reused_pending = True
        note = "检测到该设备已有在途采集请求，直接沿用它，没有重复下发指令。"
    else:
        key = "nl-" + uuid.uuid4().hex
        payload = _call("request_collect", {"device_id": did, "client_request_key": key},
                        lambda: _rt()["create_collect"]({
                            "device_id": did, "sensor": "qma6100p",
                            "requested_by": "nl-assistant",
                            "client_request_key": key,
                            "note": "自然语言请求：%s" % text,
                        }), trace)
        if not payload.get("ok"):
            err = _err_dict(payload)
            code = err.get("code") or "COLLECT_REJECTED"
            msg = err.get("message") or "后端拒绝了这次采集请求"
            return {
                "status": "rejected", "claim": "no_evidence",
                "answer": "没能发起采集：%s（错误码 %s）。\n没有向设备下发任何指令，也没有新数据产生。" % (msg, code),
                "error_code": code,
                "evidence": {"source": "collect_requests（未创建）", "device_id": did,
                             "observed_at_utc": None, "age_ms": None,
                             "freshness": "unknown", "device_command_sent": False,
                             "collect_request_id": None, "collect_status": None},
            }
        data = payload.get("data") or {}
        rid = data.get("request_id")
        reused_pending = False
        created_new = True
        note = "已向设备排入一条 collect_now 指令。"
        _mark_collect(did)

    # ---- 3) 等设备完成证据 ----
    st, polls = _collect_wait(rid, wait_ms, trace) if rid else (None, 0)
    st = st or {}
    status = st.get("status") or (pending or {}).get("status") or "submitted"
    obs = st.get("observation")
    dev_age = _age_ms((dev or {}).get("last_seen_utc"))

    # 设备完成证据 = 请求 completed + 绑定观测存在（record_id 必须真实）
    completed = (status == "completed" and isinstance(obs, dict) and obs.get("record_id"))

    head = "「%s」→ 请求一次新采集（会产生设备侧流量）。\n设备：%s\n" % (text, did)
    if created_new:
        head += "指令已下发：request_id=%s（提交 %s）。\n" % (
            rid, _fmt_local(st.get("submitted_at_utc") or (pending or {}).get("submitted_at_utc")))
    else:
        head += "沿用在途请求：request_id=%s。%s\n" % (rid, note)

    if completed:
        rec_time = obs.get("ts_local") or st.get("completed_at_utc")
        ans = head + (
            "设备回报完成：%s（北京时间）。\n"
            "新观测：record_id=%s，seq %s→%s，|a| = %s g。\n"
            "来源：collect_requests.request_id + observations.observation_id=%s + sensor_records.id=%s。\n"
            "状态：completed —— 这是**本次新产生**的记录，与「查看上次数据」返回的旧记录不是同一条。"
        ) % (_fmt_local(st.get("completed_at_utc")), obs.get("record_id"),
             obs.get("seq_first"), obs.get("seq_last"), _g(obs.get("magnitude_g")),
             obs.get("observation_id"), obs.get("record_id"))
        return {
            "status": "completed", "claim": "new_record", "answer": ans,
            "evidence": {
                "source": "observations.observation_id=%s" % obs.get("observation_id"),
                "device_id": did,
                "record_id": obs.get("record_id"),
                "observation_id": obs.get("observation_id"),
                "observed_at_utc": st.get("completed_at_utc"),
                "received_at_utc": st.get("completed_at_utc"),
                "age_ms": _age_ms(st.get("completed_at_utc")),
                "freshness": "live",
                "device_command_sent": True,
                "collect_request_id": rid,
                "collect_status": status,
                "reused_pending": reused_pending,
                "polls": polls,
            },
        }

    # 未完成：按"有没有设备回执"分档，措辞一律不下成功结论
    ack = st.get("ack") or {}
    if status in ("timeout", "failed", "rejected", "cancelled"):
        why = {
            "timeout": "等待设备回报超时",
            "failed": "设备执行失败",
            "rejected": "设备拒绝了本次指令",
            "cancelled": "请求已被取消",
        }.get(status, "请求未完成")
        ans = head + (
            "结果：%s（status=%s，deadline=%s）。\n"
            "没有收到绑定本次请求的新观测，因此**不能确认采集完成**，也不会有新记录。\n"
            "设备最后上报：%s（距今 %s）。\n"
            "建议：先确认设备在线（可问「设备在线吗」），再重新说「重新采集一次」。"
        ) % (why, status, _fmt_local(st.get("deadline_utc")),
             _fmt_local((dev or {}).get("last_seen_utc")), _age_text(dev_age))
        return {
            "status": status, "claim": "no_evidence", "answer": ans,
            "error_code": (st.get("failure_reason") or "COLLECT_NOT_COMPLETED"),
            "evidence": {
                "source": "collect_requests.request_id=%s" % rid,
                "device_id": did,
                "record_id": None,
                "observed_at_utc": st.get("deadline_utc"),
                "received_at_utc": None,
                "age_ms": dev_age,
                "freshness": _freshness(dev_age),
                "device_command_sent": True,
                "collect_request_id": rid,
                "collect_status": status,
                "reused_pending": reused_pending,
                "polls": polls,
                "ack": ack or None,
            },
        }

    # 还在途（submitted/acked/collecting）
    ack_txt = ("设备已回执 ack_type=%s（%s）" % (ack.get("ack_type"), _fmt_local(ack.get("ack_at_utc")))
               if ack else "设备尚未回执")
    ans = head + (
        "当前状态：%s（%s）。\n"
        "已等待约 %.1f 秒，仍未拿到完成证据；因此**不能确认已采到新数据**。\n"
        "设备最后上报：%s（距今 %s）。\n"
        "你可以稍后问「采集完成了吗」，或直接打开请求详情：/api/v1/collect_requests/%s"
    ) % (status, ack_txt, wait_ms / 1000.0,
         _fmt_local((dev or {}).get("last_seen_utc")), _age_text(dev_age), rid)
    return {
        "status": "pending", "claim": "pending", "answer": ans,
        "error_code": "COLLECT_PENDING",
        "evidence": {
            "source": "collect_requests.request_id=%s" % rid,
            "device_id": did,
            "record_id": None,
            "observed_at_utc": st.get("submitted_at_utc"),
            "received_at_utc": None,
            "age_ms": dev_age,
            "freshness": _freshness(dev_age),
            "device_command_sent": True,
            "collect_request_id": rid,
            "collect_status": status,
            "reused_pending": reused_pending,
            "polls": polls,
            "ack": ack or None,
        },
    }


def _answer_location(text):
    return {
        "status": "insufficient_evidence", "claim": "no_evidence",
        "error_code": "INSUFFICIENT_LOCATION_EVIDENCE",
        "answer": (
            "「%s」：这个问题我没法回答。\n"
            "本工程的数据包字段只有 raw/accel、seq、boot_count、时间戳与设备 ID，"
            "**不含任何位置、经纬度、GPS 或基站信息**；后端也没有位置表。\n"
            "缺少位置证据时我不会推断或生成坐标。\n"
            "如果你要找的是设备本身，可以问「设备在线吗」看它最后一次上报时间。"
        ) % text,
        "evidence": {"source": "无（数据模型不含位置字段）", "device_id": None,
                     "observed_at_utc": None, "age_ms": None,
                     "freshness": "unknown", "device_command_sent": False,
                     "collect_request_id": None, "collect_status": None},
    }


# ===========================================================================
# 对外入口
# ===========================================================================
def tools_info():
    cfg = _llm_cfg()
    return _ok({
        "tools": TOOL_SPECS,
        "policy": {
            "device_scope": "只允许已接入 devices 表内的设备；越界直接 DEVICE_OUT_OF_SCOPE",
            "read_side_effects": "读工具不写库、不排指令",
            "collect_limits": {
                "min_interval_ms": COLLECT_MIN_INTERVAL_MS,
                "max_wait_ms": COLLECT_WAIT_MAX_MS,
                "dedupe_open_requests": True,
            },
            "success_claim_rule": "只有 collect_requests.status=completed 且存在绑定观测(record_id) 才允许说采集完成",
            "location_supported": False,
            "max_query_chars": MAX_QUERY_CHARS,
        },
        "parser": {
            "mode": "llm+rule" if cfg["enabled"] else "rule",
            "llm": {
                "configured": cfg["configured"],   # 是否配了服务端密钥
                "enabled": cfg["enabled"],
                "model": cfg["model"],             # 只暴露模型名，不暴露密钥
                "key_visible": False,
            },
        },
        "intents": [{"intent": k, "label": v} for k, v in INTENT_LABELS.items()],
    })


def handle_query(payload):
    trace = []
    if not isinstance(payload, dict):
        return _error(400, "BAD_JSON", "报文必须是 JSON 对象")

    text = (payload.get("q") or payload.get("query") or "").strip()
    if not text:
        return _error(400, "MISSING_FIELD", "缺少必填字段 q（要说的那句话）")
    if len(text) > MAX_QUERY_CHARS:
        return _error(400, "QUERY_TOO_LONG",
                      "一句话最多 %d 个字符" % MAX_QUERY_CHARS)
    ctx_device = payload.get("device_id")
    if ctx_device is not None and not isinstance(ctx_device, str):
        return _error(400, "BAD_PARAM", "device_id 必须是字符串")

    # 等待时长只影响"这次回答等多久"，不能被用来掩盖设备不响应；上限硬编码。
    try:
        wait_ms = int(payload.get("wait_ms", DEFAULT_WAIT_MS))
    except (TypeError, ValueError):
        wait_ms = DEFAULT_WAIT_MS
    wait_ms = max(0, min(wait_ms, COLLECT_WAIT_MAX_MS))

    # ---- 工具 1：设备清单（同时也是设备范围校验的依据）----
    devices_payload = _call("list_devices", {}, lambda: _rt()["list_devices"](), trace)
    devices = _normalize_devices(devices_payload)
    device_ids = [d.get("device_id") for d in devices]

    # ---- 意图解析：优先语言服务，失败/越界则退回规则 ----
    parsed, llm_err = (None, None)
    if payload.get("parser") != "rule":
        parsed, llm_err = _llm_parse(text, device_ids)
    if parsed is None:
        parsed = _rule_parse(text)
        if llm_err and llm_err != "LLM_DISABLED":
            parsed["notes"].append("语言服务不可用，已退回本地规则解析：%s" % llm_err)
            parsed["parser"] = "rule_fallback"

    intent = parsed["intent"]
    did, problem, candidates = _resolve_device(
        text, ctx_device, devices, parsed.get("device_hint"))

    base = {
        "trace_id": "nl-" + uuid.uuid4().hex[:12],
        "query": text,
        "intent": intent,
        "intent_label": INTENT_LABELS.get(intent, "未识别"),
        "params": {
            "device_id": did,
            "time_range_s": parsed.get("time_range_s"),
            "limit": parsed.get("limit"),
            "wait_ms": wait_ms if intent == "request_collect" else None,
        },
        "parser": parsed.get("parser"),
        "confidence": parsed.get("confidence"),
        "notes": parsed.get("notes"),
        "tool_calls": trace,
        "limits": {
            "location_supported": False,
            "max_wait_ms": COLLECT_WAIT_MAX_MS,
            "device_scope_size": len(devices),
        },
    }

    # ---- 位置：数据模型不具备该能力，直接如实说明 ----
    if intent == "location":
        r = _answer_location(text)
        base.update(r)
        base["answer"], base["guard"] = _guard_answer(base["answer"], base["claim"])
        base["tool_calls"] = trace
        return _ok(base)

    # ---- 未识别：给出可用说法，而不是硬猜 ----
    if intent == "unknown":
        danger = bool(RE_DANGER.search(text))
        base.update({
            "status": "clarify", "claim": "none",
            "answer": ("这句话我不能执行：助手只有查询与「请求采集一次」两类工具，"
                       "**没有任何删除、修改、重置或重启设备的能力**。\n"
                       "本次没有写库、没有向设备下发指令。" if danger else
                       "我没听明白这句话。我只会两类事：\n"
                      "① 查看已有记录（例如「查看上次数据」「看看最近 10 分钟的数据」）；\n"
                      "② 请求设备采集一次（例如「重新采集一次」）。\n"
                      "或问「设备在线吗」。"),
            "ambiguity": {
                "needed": True, "kind": "intent",
                "question": "你是想「查看已有记录」，还是「请求设备重新采集一次」？",
                "options": ["查看上次数据", "重新采集一次", "设备在线吗"],
            },
            "evidence": {"source": "无（未调用业务工具）", "device_id": did,
                         "observed_at_utc": None, "age_ms": None,
                         "freshness": "unknown", "device_command_sent": False,
                         "collect_request_id": None, "collect_status": None},
        })
        return _ok(base)

    # ---- 设备范围校验 ----
    if problem == "out_of_scope":
        bad = ctx_device or (candidates[0] if candidates else None) or parsed.get("device_hint") or "（未知）"
        return _error(
            403, "DEVICE_OUT_OF_SCOPE",
            "设备 %s 不在已接入范围内，助手不会向它发起任何操作" % bad,
            allowed_devices=device_ids,
        )
    if problem == "no_device":
        base.update({
            "status": "no_data", "claim": "no_evidence",
            "answer": "后端还没有任何已接入设备（设备尚未轮询过）。请先给板子上电并让它连上后端。",
            "evidence": {"source": "devices（空）", "device_id": None,
                         "observed_at_utc": None, "age_ms": None,
                         "freshness": "unknown", "device_command_sent": False,
                         "collect_request_id": None, "collect_status": None},
        })
        return _ok(base)
    if problem == "ambiguous":
        cands = [d for d in devices if d.get("device_id") in (candidates or device_ids)]
        q = ("你想操作哪台设备？当前接入了 %d 台，我不能替你猜。" % len(cands))
        base.update({
            "status": "clarify", "claim": "none",
            "answer": q + "\n（本次没有向任何设备下发指令，也没有读取记录。）",
            "ambiguity": {
                "needed": True, "kind": "device",
                "question": q,
                "candidates": [_device_brief(d) for d in cands],
                "options": ["查看 %s 上次的数据" % c["device_id"] for c in cands[:3]]
                           + (["重新采集一次（%s）" % cands[0]["device_id"]] if cands else []),
            },
            "evidence": {"source": "无（歧义未消解，未调用业务工具）", "device_id": None,
                         "observed_at_utc": None, "age_ms": None,
                         "freshness": "unknown", "device_command_sent": False,
                         "collect_request_id": None, "collect_status": None},
        })
        return _ok(base)

    dev = _find_device(devices, did)

    # ---- 只读意图 ----
    if intent in ("query_latest", "query_history", "device_status", "collect_status"):
        if intent == "query_latest":
            p = _call("get_latest_record", {"device_id": did},
                      lambda: _rt()["latest_record"](did), trace)
            r = _answer_latest(text, did, dev, p, trace)
        elif intent == "query_history":
            lim = parsed.get("limit") or (60 if parsed.get("time_range_s") else 20)
            lim = max(1, min(int(lim), MAX_HISTORY_LIMIT))
            p = _call("get_records", {"device_id": did, "limit": lim},
                      lambda: _rt()["records"](did, lim), trace)
            r = _answer_history(text, did, dev, parsed, p, trace)
        elif intent == "collect_status":
            r = _answer_collect_status(text, did, dev, trace)
        else:
            p = _call("get_device_status", {"device_id": did},
                      lambda: _rt()["list_devices"](), trace)
            r = _answer_status(text, did, dev, p, trace)
        base.update(r)
        base["answer"], base["guard"] = _guard_answer(base["answer"], base["claim"])
        base["tool_calls"] = trace
        return _ok(base)

    # ---- 动作意图：请求一次新采集 ----
    if intent == "request_collect":
        r = _answer_collect(text, did, dev, parsed, wait_ms, trace)
        base.update(r)
        base["answer"], base["guard"] = _guard_answer(base["answer"], base["claim"])
        base["tool_calls"] = trace
        if base.get("status") == "rate_limited":
            return 429, {"ok": False, "server_time_utc": _now_iso(),
                         "error": {"code": "COLLECT_RATE_LIMITED",
                                   "message": base["answer"],
                                   "retryable": True,
                                   "retry_after_ms": base.get("retry_after_ms"),
                                   "data": base}}
        return _ok(base)

    return _error(400, "UNSUPPORTED_INTENT", "暂不支持该意图")
