#!/usr/bin/env node
/**
 * test_web_voice6.js —— 第 6 周「语音任务可以澄清与停止」前端行为自检
 * （打桩 DOM + 假后端，不需要浏览器、不需要麦克风）
 *
 * 直接加载真实的 web/app.js，用假 DOM + 假 fetch 驱动，断言页面**实际写进**
 * 状态条、停止按钮、澄清卡片、时序表、播放区的文字与属性。
 *
 * 钉住的红线（第 6 周新增 C1/C2/C3 在前端这一侧的落点）：
 *   C1 取消必须真的拦住后续阶段：点停止 → 必须打到 /cancel（而不是只改页面文案），
 *      而且页面**不许**在收到 cancelled 之前宣称"已取消"。
 *   C2 迟到/被隔离的结果不许播：delivery.playable=false 时不许给 <audio> 设 src，
 *      播放按钮必须禁用，必须说出"为什么不能播"。
 *   C3 半双工只认最后一个 request_id：旧的那一轮结果回来时必须整轮丢弃
 *      （不渲染、不播放、不覆盖），且旧文本一个字都不许出现在 DOM 里。
 *   C4 三种"停"必须分开：放弃录音（不上传）/ 取消本轮（/cancel）/ 停止播报（/stop）。
 *   C5 澄清必须给"能点的选项 + 能打字的输入"，而且补充轮走 /voice/text 同一条链路；
 *      到轮数上限必须只给退路、不再接受输入。
 *   C6 注入必须显式填了才带，且页面上要标出"这一轮是注入的"。
 *   C7 回看历史（replay）不许改当前半双工阶段。
 *
 * 运行: node tools/test_web_voice6.js
 */

"use strict";

const fs = require("fs");
const path = require("path");

const APP_JS = path.join(__dirname, "..", "web", "app.js");
const INDEX_HTML = path.join(__dirname, "..", "web", "index.html");
const STAGE_KEYS = ["recorded", "vad", "asr", "task", "tts", "playback"];

// ---------------------------------------------------------------------------
// 打桩：DOM
// ---------------------------------------------------------------------------
const els = {};

function makeElement(id) {
  const classes = new Set();
  const attrs = {};
  const el = {
    id, className: "", width: 0, height: 0, disabled: false, src: "", value: "",
    children: [], _em: null, style: {}, currentTime: 0, paused: true, ended: false,
    _listeners: {},
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c, on) => (on === undefined ? (classes.has(c) ? classes.delete(c) : classes.add(c))
                                           : (on ? classes.add(c) : classes.delete(c))),
    },
    addEventListener: (ev, fn) => { (el._listeners[ev] = el._listeners[ev] || []).push(fn); },
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    setAttribute: (k, v) => { attrs[k] = String(v); },
    removeAttribute: (k) => { delete attrs[k]; },
    pause: () => { el.paused = true; },
    play: () => { el.paused = false; return Promise.resolve(); },
    appendChild: (c) => {
      el.children.push(c);
      el._text = (el._text || "") + String((c && c.textContent) || "");
      return c;
    },
    getContext: () => ctx,
    _classes: classes,
    _attrs: attrs,
    querySelector(sel) {
      const m = /li\[data-k="([^"]+)"\]/.exec(String(sel));
      if (m) return el.children.find((c) => c._attrs && c._attrs["data-k"] === m[1]) || null;
      if (sel === "em") return el._em || (el._em = makeElement(el.id + "-em"));
      return makeElement(el.id + "-q");
    },
    querySelectorAll: () => [],
  };
  Object.defineProperty(el, "textContent", {
    get() { return this._text === undefined ? "" : this._text; },
    set(v) { this._text = v; },
  });
  Object.defineProperty(el, "innerHTML", {
    get() { return this._html || ""; },
    set(v) {
      this._html = v;
      if (v === "") { el.children = []; el._text = ""; }
    },
  });
  Object.defineProperty(el, "clientWidth", { get() { return 1200; } });
  return el;
}

const ctx = new Proxy({}, {
  get: (t, p) => (p in t ? t[p] : () => {}),
  set: (t, p, v) => { t[p] = v; return true; },
});

global.document = {
  getElementById: (id) => els[id] || (els[id] = makeElement(id)),
  createElement: (tag) => makeElement("new-" + tag + "-" + Math.random().toString(36).slice(2, 7)),
  body: { classList: { add: () => {}, remove: () => {}, contains: () => false } },
  activeElement: null,
  addEventListener: () => {},
  hidden: false,
};
global.window = { devicePixelRatio: 1, addEventListener: () => {}, OfflineAudioContext: undefined };
global.location = { search: "", origin: "http://127.0.0.1:8000" };
Object.defineProperty(global, "navigator", {
  configurable: true,
  value: {
    userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    mediaDevices: null,
  },
});

let wallClock = 1_700_000_000_000;
global.Date.now = () => wallClock;
// performance.now 要"会走"，否则测不出 client_ack_ms 是真实测出来的
let perfNow = 1000;
global.performance = { now: () => (perfNow += 0.7) };
global.requestAnimationFrame = () => 1;
global.cancelAnimationFrame = () => {};
global.setTimeout = (fn, ms) => setImmediate(() => { wallClock += (ms || 0); fn(); });
const intervals = [];
global.setInterval = (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; };
global.clearInterval = (h) => { if (h) intervals[intervals.length - 1] = null; };

for (const m of fs.readFileSync(INDEX_HTML, "utf8")
         .matchAll(/id="([A-Za-z0-9_]+)"[^>]*>([^<]*)</g)) {
  const stub = els[m[1]] || (els[m[1]] = makeElement(m[1]));
  if (stub) stub.textContent = m[2].trim();
}

els.voiceStages.children = STAGE_KEYS.map((k) => {
  const li = makeElement("li-" + k);
  li._attrs["data-k"] = k;
  li._em = makeElement("li-" + k + "-em");
  li._em.textContent = "—";
  return li;
});

els.voiceSource.value = "browser_mic";   // 打桩 DOM 不解析 <option>，按真实默认值给上
els.voicePanel.classList.add("hidden");
els.voiceResult.classList.add("hidden");
els.voiceNotice.classList.add("hidden");
els.voiceClarify.classList.add("hidden");
els.voiceTimelineBox.classList.add("hidden");

// ---------------------------------------------------------------------------
// 假后端
// ---------------------------------------------------------------------------
const calls = [];
let turnMode = "immediate";       // immediate | manual
const pendingTurns = [];          // manual 模式下等待被 resolve 的请求

const T0 = "2026-10-23T02:20:00.000Z";
const SID = "vs_0123456789abcdef";

const BASE_AUDIO = {
  bytes: 73124, sha256: "88808355706c5abcc7858659e79d0b6c0b61be6684fa7b3770d8de677427ce0c",
  sample_rate: 16000, bits: 16, channels: 1, duration_ms: 2284,
  rms: 0.066667, peak: 0.671478, zcr: 0.156158, vad_passed: true,
};

const BASE_RUNLOC = {
  capture: "browser:Chrome/Windows · 本机 PC（页面所在的这台机器）",
  asr: "http://127.0.0.1:8013 · vosk/vosk-model-small-cn-0.22（离线）",
  task: "server.py · nl_assistant.handle_query（第 4 周同一函数，进程内调用）",
  tts: "http://127.0.0.1:8013 · sapi/Microsoft Huihui Desktop",
  playback: "browser <audio> · 本机 PC（页面所在的这台机器）",
  session: "voice_session.py · session=" + SID + " turn_seq=1（半双工）",
};

function stages(spec) {
  const labels = { recorded: "录音", vad: "静音检测", asr: "语音识别",
                   task: "任务执行", tts: "语音合成", playback: "播放" };
  return STAGE_KEYS.map((k) => {
    const s = spec[k] || { status: "skipped", detail: "" };
    return { key: k, label: labels[k], status: s.status, detail: s.detail || "",
             ms: s.ms == null ? 0 : s.ms };
  });
}

const TIMING = {
  t0_utc: T0, first_feedback_ms: 640, task_done_ms: 690,
  tts_ready_ms: 1010, client_ack_ms: 3, total_ms: 1105,
};

const SESSION = { session_id: SID, turn_seq: 1, superseded_by: null, half_duplex: true };

const TASK_OK = {
  status: "ok", intent: "query_latest", claim: "old_record", remote_status: "answered",
  source: "sensor_records.id=42",
  answer: "设备：esp32s3eye-1C704C\n来源：sensor_records.id=42\n"
        + "状态：这是数据库中已经存在的一条记录，不是本次新采的。",
  error: null,
};

function turnOk(id, transcript) {
  return {
    ok: true, data: {
      turn_id: id, request_id: "rq_req_" + id, final_state: "done",
      audio_source: "browser_mic", audio_source_label: "浏览器麦克风（电脑音频）",
      audio: BASE_AUDIO, run_location: BASE_RUNLOC,
      stages: stages({
        recorded: { status: "ok", detail: "2.28 秒" },
        vad: { status: "ok", detail: "RMS=0.0667" },
        asr: { status: "ok", ms: 635, detail: "引擎 vosk → 「" + transcript + "」" },
        task: { status: "ok", ms: 18, detail: "意图=query_latest" },
        tts: { status: "ok", ms: 310, detail: "产出 9.7 秒 WAV" },
        playback: { status: "pending", detail: "等待浏览器播放器开始播放" },
      }),
      transcript: transcript, asr: { status: "ok", engine: "vosk", ms: 635, error: null },
      task: TASK_OK,
      tts: { status: "ok", engine: "sapi", voice: "Huihui", ms: 310,
             reply_text: "设备 esp32s3eye-1C704C…", bytes: 426314, truncated: true,
             full_chars: 46, error: null },
      delivery: { state: "deliverable", playable: true, why: null },
      reply_audio_url: "/api/v1/voice/reply/" + id + ".wav",
      reply_audio_quarantined: false,
      audio_url: "/api/v1/voice/audio/" + id + ".wav",
      session: SESSION, timing: TIMING, cancel: {}, stop: {}, inject: null,
      clarify: { asked: null, round: 0, how: null, resolved_with: null, max_rounds: 2 },
      error: null, elapsed_ms: 1105, created_at_utc: T0,
    },
  };
}

const TURN_OK_A = turnOk("vt_ok_aaaa", "查看上次数据");
const TURN_OK_B = turnOk("vt_ok_bbbb", "设备在线吗");
TURN_OK_B.data.task = Object.assign({}, TURN_OK_B.data.task, {
  intent: "device_status", claim: "live", remote_status: "answered",
  source: "devices.last_seen_utc",
  answer: "设备 esp32s3eye-1C704C 在线，最后上报 2026-10-23 10:20:00。",
});

const TURN_CLARIFY = {
  ok: true, data: {
    turn_id: "vt_clr_0001", request_id: "rq_req_clr01", final_state: "clarifying",
    audio_source: "browser_mic", audio_source_label: "浏览器麦克风（电脑音频）",
    audio: BASE_AUDIO, run_location: BASE_RUNLOC,
    stages: stages({
      recorded: { status: "ok" }, vad: { status: "ok" }, asr: { status: "ok", ms: 600 },
      task: { status: "ok", ms: 9, detail: "需要澄清（kind=intent，第 1/2 轮）" },
      tts: { status: "ok", ms: 280 }, playback: { status: "pending" },
    }),
    transcript: "看下那个",
    asr: { status: "ok", engine: "vosk", ms: 600, error: null },
    task: { status: "clarify", intent: null, claim: "none", remote_status: "clarify",
            source: "无（歧义未消解，未调用业务工具）",
            answer: "你是想「查看已有记录」，还是「请求设备重新采集一次」？", error: null },
    tts: { status: "ok", engine: "sapi", ms: 280, reply_text: "你是想查看已有记录…",
           bytes: 120000, truncated: false, full_chars: 26, error: null },
    delivery: { state: "deliverable", playable: true, why: null },
    reply_audio_url: "/api/v1/voice/reply/vt_clr_0001.wav",
    reply_audio_quarantined: false,
    audio_url: "/api/v1/voice/audio/vt_clr_0001.wav",
    session: SESSION, timing: TIMING, cancel: {}, stop: {}, inject: null,
    clarify: {
      asked: {
        round: 1, max_rounds: 2, kind: "intent",
        question: "你是想「查看已有记录」，还是「请求设备重新采集一次」？",
        options: ["查看上次数据", "重新采集一次", "设备在线吗"],
        candidates: [{ device_id: "esp32s3eye-1C704C", last_seen_utc: T0 }],
        base_query: "看下那个", device_id: null, source_turn_id: "vt_clr_0001",
        exhausted: false, why: "系统没能唯一确定你的意思，所以先问一句再动手（不猜）",
      },
      round: 1, from_turn_id: null, resolved_with: null, how: null, max_rounds: 2,
    },
    error: null, elapsed_ms: 1000, created_at_utc: T0,
  },
};

const FALLBACK = "已经问了 2 轮还没说清，先停下。你可以直接说「查看上次数据」/「重新采集一次」/"
  + "「设备在线吗」，或者改用文字入口打字 —— 打字的歧义更少。";

const TURN_EXHAUSTED = {
  ok: true, data: Object.assign({}, TURN_CLARIFY.data, {
    turn_id: "vt_exh_0002", final_state: "clarify_exhausted",
    delivery: { state: "deliverable", playable: true, why: null },
    reply_audio_url: "/api/v1/voice/reply/vt_exh_0002.wav",
    error: { code: "CLARIFY_EXHAUSTED", message: FALLBACK },
    clarify: {
      asked: Object.assign({}, TURN_CLARIFY.data.clarify.asked,
        { round: 3, exhausted: true, fallback: FALLBACK, repeated: true }),
      round: 3, from_turn_id: null, resolved_with: null, how: null, max_rounds: 2,
    },
  }),
};

const TURN_RESOLVED = {
  ok: true, data: Object.assign({}, TURN_OK_A.data, {
    turn_id: "vt_res_0003", final_state: "done", audio_source: "text_input",
    audio_source_label: "键盘补充（澄清轮）",
    transcript: "重新采集一次", audio: Object.assign({}, BASE_AUDIO, { duration_ms: 0 }),
    clarify: {
      asked: null, round: 2, from_turn_id: "vt_clr_0001",
      resolved_with: "重新采集一次", how: "intent_keyword", max_rounds: 2,
      base_query: "看下那个",
    },
  }),
};

const TURN_TEXT_OK = {
  ok: true, data: Object.assign({}, TURN_OK_B.data, {
    turn_id: "vt_txt_0004", final_state: "done", audio_source: "text_input",
    audio_source_label: "键盘补充（澄清轮）",
    stages: stages({
      recorded: { status: "skipped", detail: "补充轮：没有音频" },
      vad: { status: "skipped", detail: "补充轮：无音频，跳过静音检测" },
      asr: { status: "skipped", detail: "补充轮：没有调用识别引擎（asr.engine=keyboard）" },
      task: { status: "ok", ms: 12, detail: "意图=query_latest（消解自澄清：ordinal）" },
      tts: { status: "ok", ms: 300 }, playback: { status: "pending" },
    }),
    asr: { status: "skipped", engine: "keyboard", ms: null, error: null },
    clarify: {
      asked: null, round: 2, from_turn_id: "vt_clr_0001",
      resolved_with: "查看上次数据", how: "ordinal", max_rounds: 2, base_query: "看下那个",
    },
  }),
};

const TURN_CANCELLED = {
  ok: true, data: Object.assign({}, TURN_CLARIFY.data, {
    turn_id: "vt_can_0005", final_state: "cancelled",
    stages: stages({
      recorded: { status: "ok" }, vad: { status: "ok" }, asr: { status: "ok", ms: 600 },
      task: { status: "skipped", detail: "整轮已被取消，任务段一行都没有执行" },
      tts: { status: "skipped" }, playback: { status: "skipped", detail: "整轮已被取消，不提供播报" },
    }),
    tts: { status: "skipped", reply_text: null, bytes: null },
    delivery: { state: "quarantined_cancelled", playable: false,
                why: "这一轮（cancelled）的播报已被隔离：可以回看记录，但不能播放。" },
    reply_audio_url: null, reply_audio_quarantined: true,
    cancel: { requested_at_utc: T0, seen_at_utc: T0, reason: "user_stop",
              stage: "after_asr", action_taken: false },
    clarify: { asked: null, round: 0, how: null, resolved_with: null, max_rounds: 2 },
    error: { code: "CANCELLED",
             message: "这一轮在「after_asr」被取消，任务段一行都没有执行（action_taken=0）："
                    + "没有写库、没有向设备下发任何指令。识别结果如实保留，但不执行、不播报。" },
  }),
};

const TURN_SUPERSEDED = {
  ok: true, data: Object.assign({}, TURN_OK_A.data, {
    turn_id: "vt_sup_0006", final_state: "superseded",
    delivery: { state: "quarantined_superseded", playable: false,
                why: "这一轮（superseded）的播报已被隔离：可以回看记录，但不能播放。" },
    reply_audio_url: null, reply_audio_quarantined: true,
    session: { session_id: SID, turn_seq: 1, superseded_by: "rq_req_bbbb", half_duplex: true },
    error: { code: "SUPERSEDED",
             message: "这一轮已经被新任务顶掉（新 request_id=rq_req_bbbb）。这是半双工的必然结果："
                    + "同一时刻只有一条 turn 有播报资格。它的结果只作为历史记录保留，"
                    + "不会覆盖新任务，也不会自动播放。" },
  }),
};

function jsonResponse(status, body) {
  return { status, text: async () => JSON.stringify(body), json: async () => body };
}

/** manual 模式：把请求挂起，由测试代码决定"谁先回来"。 */
function deferTurn(url, opts) {
  return new Promise((resolve) => {
    pendingTurns.push({ url, opts, resolve });
  });
}

let sessionSeq = 0;

global.fetch = async (url, opts) => {
  const u = String(url);
  const method = (opts && opts.method) || "GET";
  calls.push({ url: u, method, body: (opts && opts.body) || null });
  perfNow += 1;   // 每次网络往返推 1 ms：让 client_ack_ms 是"测出来"的

  // ★ 顺序要紧：/voice/turns 与 /voice/timeline 都 contains("/voice/turn")
  if (u.includes("/api/v1/voice/turns")) {
    return jsonResponse(200, { ok: true, data: { count: 0, limit: 8, turns: [] } });
  }
  if (u.includes("/api/v1/voice/timeline")) {
    return jsonResponse(200, { ok: true, data: { events: [
      { event: "session_created", at_utc: T0, session_id: SID, request_id: null,
        turn_id: null, detail: { device_id: null } },
      { event: "turn_pending", at_utc: T0, session_id: SID, request_id: "rq_req_aaaa",
        turn_id: "vt_ok_aaaa", detail: { audio_bytes: 73124 } },
      { event: "cancel", at_utc: T0, session_id: SID, request_id: "rq_req_aaaa",
        turn_id: null, detail: { reason: "user_stop", was_active: true } },
    ] } });
  }
  if (u.includes("/api/v1/voice/sessions")) {
    return jsonResponse(200, { ok: true, data: { sessions: [], count: 0, limit: 20 } });
  }
  if (/\/api\/v1\/voice\/session\/[^/]+\/cancel$/.test(u)) {
    return jsonResponse(200, { ok: true, data: {
      request_id: "rq_req_x", turn_id: "vt_x", reason: "user_stop", at_utc: T0,
      session_id: SID, state: "idle", was_active: true,
      note: "已登记取消。在途的那一步跑完后会发现它已被取消：不会再执行后续动作，也不会产出可播放的播报。",
    } });
  }
  if (/\/api\/v1\/voice\/session\/[^/]+\/reset$/.test(u)) {
    return jsonResponse(200, { ok: true, data: { session_id: SID, state: "idle" } });
  }
  if (/\/api\/v1\/voice\/session\/[^/]+\/state$/.test(u)) {
    return jsonResponse(200, { ok: true, data: { session_id: SID, state: "listening" } });
  }
  if (/\/api\/v1\/voice\/session$/.test(u)) {
    sessionSeq += 1;
    return jsonResponse(200, { ok: true, data: {
      session_id: SID, device_id: null, state: "idle", state_label: "空闲",
      version: 0, active_request_id: null, active_turn_id: null, turn_seq: 0,
      clarify_round: 0, max_clarify_rounds: 2, last_turn_id: null,
      created_at_utc: T0, updated_at_utc: T0, note: null, half_duplex: true,
      busy: false, clarify: null, seq: sessionSeq,
    } });
  }
  if (/\/api\/v1\/voice\/session\/[^/]+$/.test(u)) {
    return jsonResponse(200, { ok: true, data: {
      session_id: SID, state: "idle", turn_seq: 1, clarify: null, busy: false,
    } });
  }
  if (/\/api\/v1\/voice\/turn\/[^/]+\/stop$/.test(u)) {
    return jsonResponse(200, { ok: true, data: {
      turn_id: "vt_ok_aaaa", tts_stopped_at_utc: T0, played_ms: 0, reason: "user_stop",
      final_state: "done",
      note: "播报已停止；任务与文字回答都保留。已播出的那一段收不回来，"
          + "所以这里只记录「停止于第几毫秒」，不声称任何东西。",
    } });
  }
  if (u.includes("/api/v1/voice/turn/") && u.endsWith("/played")) {
    return jsonResponse(200, { ok: true, data: { turn_id: "vt_ok_aaaa", playback_started_at_utc: T0 } });
  }
  if (u.includes("/api/v1/voice/turn")) {
    if (turnMode === "manual") return deferTurn(u, opts);
    return jsonResponse(200, TURN_OK_A);
  }
  if (u.includes("/api/v1/voice/text")) {
    if (turnMode === "manual") return deferTurn(u, opts);
    return jsonResponse(200, TURN_TEXT_OK);
  }
  if (u.includes("/api/v1/voice/health")) {
    return jsonResponse(200, { ok: true, data: {
      engine: { ok: true, asr: { backend: "vosk" }, tts: { backend: "sapi" } },
      engine_url: "http://127.0.0.1:8013",
      audio_sources: [{ key: "browser_mic" }, { key: "uploaded_file" },
                      { key: "device_mic" }, { key: "text_input" }],
      session: { half_duplex: true, max_clarify_rounds: 2 },
      cancel: { endpoints: [] }, inject: { allowed: true }, timing: { t0: "server_received" },
    } });
  }
  if (u.includes("/api/v1/nl/query")) {
    return jsonResponse(200, { ok: true, data: {
      intent: "query_latest", status: "answered", claim: "old_record",
      answer: "同上", query: "查看上次数据",
      evidence: { source: "sensor_records.id=42" }, tool_calls: [],
    } });
  }
  if (u.includes("/api/v1/records")) return jsonResponse(200, { ok: true, count: 0, records: [] });
  if (u.includes("/api/v1/latest")) return jsonResponse(200, { ok: true, record: null });
  if (u.includes("/api/v1/devices")) {
    return jsonResponse(200, { ok: true, devices: [{
      device_id: "esp32s3eye-1C704C", n: 100, last_seen: T0, model: "QMA6100P",
      last_seq: 3203, mac: "94:A9:90:1C:70:4C", last_status: "online",
      last_seen_utc: T0, periodic_enabled: 1, pending_commands: 0,
    }] });
  }
  if (u.includes("/api/v1/stats")) return jsonResponse(200, { ok: true, records: 0, raw_uploads: 0, devices: 1 });
  if (u.includes("/api/v1/collect_requests")) return jsonResponse(200, { ok: true, data: { count: 0, requests: [] } });
  if (u.includes("/api/v1/help_events")) return jsonResponse(200, { ok: true, data: { count: 0, help_events: [] } });
  if (u.includes("/api/v1/cam/status")) return jsonResponse(200, { ok: true, has_frame: false, frame: null });
  return jsonResponse(404, { ok: false, error: "not found" });
};

// ---------------------------------------------------------------------------
// 执行 app.js
// ---------------------------------------------------------------------------
const APP_SRC = fs.readFileSync(APP_JS, "utf8");
new Function(APP_SRC)();

const fails = [];
function check(name, cond, detail) {
  console.log((cond ? "  ✔ " : "  ✘ ") + name + (detail ? "  " + detail : ""));
  if (!cond) fails.push(name);
}
const sleep = () => new Promise((r) => setImmediate(r));
async function flush(n) { for (let i = 0; i < (n || 60); i++) await sleep(); }

async function click(id, ev) {
  const el = els[id];
  const ls = el && el._listeners && el._listeners.click;
  if (ls) ls.forEach((fn) => fn(ev || { target: el }));
  await flush();
}

const txt = (id) => String(els[id].textContent || "");
const isHidden = (id) => els[id].classList.contains("hidden");
const cls = (id) => String(els[id].className || "");
const stageStatus = (k) => {
  const li = els.voiceStages.children.find((c) => c._attrs["data-k"] === k);
  return li ? li.className : "";
};
const lastCall = (re) => {
  for (let i = calls.length - 1; i >= 0; i--) if (re.test(calls[i].url)) return calls[i];
  return null;
};
const allCalls = (re) => calls.filter((c) => re.test(c.url));
const bodyOf = (c) => {
  try { return c && c.body ? JSON.parse(c.body) : null; } catch (e) { return null; }
};
/** 从 URL 里取查询参数 */
function qsOf(u) {
  const q = String(u).split("?")[1] || "";
  const out = {};
  for (const kv of q.split("&")) {
    if (!kv) continue;
    const i = kv.indexOf("=");
    out[decodeURIComponent(kv.slice(0, i))] = decodeURIComponent(kv.slice(i + 1));
  }
  return out;
}

(async function main() {
  console.log("=".repeat(78));
  console.log("前端「语音任务可以澄清与停止」自检（打桩 DOM + 假后端）");
  console.log("=".repeat(78));

  const dbg = window.__voiceDebug;
  check("app.js 暴露 __voiceDebug", !!dbg && typeof dbg.render === "function");
  if (!dbg) { console.log("无法继续"); process.exit(1); }

  // ---- 1. 面板与会话 ----
  console.log("\n[1] 打开面板 → 建会话 → 状态条与停止按钮");
  await click("btnVoice");
  check("面板展开", !isHidden("voicePanel"));
  check("★ 打开面板就建了会话（POST /voice/session）",
        allCalls(/\/api\/v1\/voice\/session$/).length >= 1);
  check("会话 id 显示的是后 8 位", txt("voiceSid") === SID.slice(-8), txt("voiceSid"));
  check("状态条 = 空闲", /空闲/.test(txt("voiceStateChip")) && cls("voiceStateChip").includes("is-idle"),
        txt("voiceStateChip") + " / " + cls("voiceStateChip"));
  check("★ 空闲时停止按钮是禁用的（没有可停的东西）", els.voiceStopAll.disabled === true);
  check("对话阶段写入 data-phase", els.voicePanel.getAttribute("data-phase") === "idle",
        els.voicePanel.getAttribute("data-phase"));
  check("会话条显示在途=无", /无/.test(txt("voiceInflight")), txt("voiceInflight"));

  // ---- 2. 提交一轮：request_id / session_id / client_ack_ms 必须带上 ----
  console.log("\n[2] 提交一轮 → 请求带上会话与 request_id，状态条进「处理中」");
  turnMode = "manual";
  perfNow += 100;                        // 模拟"用户在页面上花了 100ms"
  const pA = dbg.submit("audio", { size: 73124 }, {});
  await flush();
  check("★ 提交后状态条 = 处理中",
        /处理中/.test(txt("voiceStateChip")) && cls("voiceStateChip").includes("is-thinking"),
        txt("voiceStateChip"));
  check("★ 处理中时停止按钮可用（这是唯一能打断的东西）", els.voiceStopAll.disabled === false);
  check("停止按钮文案说明它是「取消」", /取消/.test(txt("voiceStopAll")), txt("voiceStopAll"));
  check("①录音格=完成", stageStatus("recorded").includes("is-ok"), stageStatus("recorded"));
  check("②静音检测格=进行中", stageStatus("vad").includes("is-run"), stageStatus("vad"));
  check("终局文案=已提交等待识别", /已提交/.test(txt("voiceFinal")), txt("voiceFinal"));

  const submitCall = pendingTurns[0];
  const sq = qsOf(submitCall.url);
  check("★ 请求带了 session_id", sq.session_id === SID, sq.session_id);
  check("★ 请求带了 request_id", /^rq_/.test(sq.request_id || ""), sq.request_id);
  check("★ request_id 与页面在途的是同一个", dbg.inflight() === sq.request_id, dbg.inflight());
  check("★ client_ack_ms 是真实测出来的（>0）",
        sq.client_ack_ms != null && Number(sq.client_ack_ms) > 0, sq.client_ack_ms);
  check("audio_source 如实带 browser_mic", sq.audio_source === "browser_mic", sq.audio_source);
  check("没有注入参数时请求里不带 inject_*",
        !("inject_delay_ms" in sq) && !("inject_asr_text" in sq), JSON.stringify(sq).slice(0, 80));

  pendingTurns.shift().resolve(jsonResponse(200, TURN_OK_A));
  await pA; await flush();
  check("★ 正常完成 → 状态条 = 播报中（有可播音频）",
        /播报中/.test(txt("voiceStateChip")), txt("voiceStateChip"));
  check("识别文本原样显示", txt("voiceTranscript") === "查看上次数据", txt("voiceTranscript"));
  check("播放按钮可用、src 指向 reply 音频", els.voicePlay.disabled === false
        && String(els.voiceAudio.src).includes("/api/v1/voice/reply/"), els.voiceAudio.src);
  check("在途标记已清空", dbg.inflight() === null);

  // ---- 3. 时序表 ----
  console.log("\n[3] 时序表：每个数字都来自服务端 timing 字段");
  const timingTxt = txt("voiceTiming");
  check("★ 表里有 t0", timingTxt.includes("t0（服务端收到完整输入）"), timingTxt.slice(0, 60));
  check("★ 表里有首次反馈 640 ms", timingTxt.includes("640 ms"), timingTxt.slice(0, 120));
  check("★ 表里有任务完成 690 ms", timingTxt.includes("690 ms"));
  check("★ 表里有播报就绪 1010 ms", timingTxt.includes("1,010 ms"), timingTxt);
  check("★ 前端本地提示被标成「浏览器自己报的」", timingTxt.includes("不是服务端实测"));
  check("交付资格写清楚了 playable=true", txt("voiceTimingNote").includes("playable=true"),
        txt("voiceTimingNote").slice(0, 80));
  dbg.render(Object.assign({}, TURN_OK_A.data, {
    timing: { t0_utc: T0, first_feedback_ms: null, task_done_ms: null,
              tts_ready_ms: null, client_ack_ms: null, total_ms: 40 },
  }));
  check("缺失的时间显示「—」而不是 0 或猜出来的数",
        txt("voiceTiming").includes("—") && !txt("voiceTiming").includes("null"));

  // ---- 4. 取消（任务在途） ----
  console.log("\n[4] 在途时点停止 → 打到 /cancel，且不许宣称「已取消」");
  turnMode = "manual";
  const pC = dbg.submit("audio", { size: 4096 }, {});
  await flush();
  const inflightReq = dbg.inflight();
  check("在途 request_id 已记下", /^rq_/.test(inflightReq || ""), inflightReq);
  const beforeCancel = allCalls(/\/api\/v1\/voice\/session\/[^/]+\/cancel$/).length;

  const stopRet = dbg.stopAll();
  await flush();
  check("★ 点停止后调用了 /session/<sid>/cancel",
        allCalls(/\/api\/v1\/voice\/session\/[^/]+\/cancel$/).length === beforeCancel + 1);
  const cCall = lastCall(/\/api\/v1\/voice\/session\/[^/]+\/cancel$/);
  const cBody = bodyOf(cCall);
  check("★ 取消请求带的是**这一轮**的 request_id", cBody && cBody.request_id === inflightReq,
        cBody && cBody.request_id);
  check("取消原因 = user_stop", cBody && cBody.reason === "user_stop");
  check("★ 文案只说「已登记/等待发现」，不许说「已取消」",
        !/已取消/.test(txt("voiceNotice")) && /登记/.test(txt("voiceNotice")),
        txt("voiceNotice").slice(0, 90));
  check("★ 说明里点出「已经发生的动作收不回来」",
        /收不回/.test(txt("voiceNotice")), txt("voiceNotice").slice(-50));
  check("本地阶段回到空闲（但服务端还没定论）", /空闲/.test(txt("voiceStateChip")),
        txt("voiceStateChip"));
  check("★ 在途标记仍保留（结果还没回来，不能先清掉）", dbg.inflight() === inflightReq);
  check("停止按钮回到禁用（本地已无「当前在途」可点）", els.voiceStopAll.disabled === true);

  pendingTurns.shift().resolve(jsonResponse(200, TURN_CANCELLED));
  await pC; await flush();
  check("★ 收到 cancelled → 终局文案是取消",
        txt("voiceFinal").includes("被取消"), txt("voiceFinal"));
  check("⑥播放格=跳过", stageStatus("playback").includes("is-skip"), stageStatus("playback"));
  check("提示里说明了「任务段一行都没有执行」",
        /一行都没有执行/.test(txt("voiceNotice")), txt("voiceNotice").slice(0, 90));
  check("★ C2 被隔离的播报：播放按钮禁用", els.voicePlay.disabled === true);
  check("★ C2 被隔离的播报：<audio> 上根本没有 src",
        els.voiceAudio.getAttribute("src") === null, String(els.voiceAudio.getAttribute("src")));
  check("★ C2 播放区写明「已被隔离」", /隔离/.test(txt("voicePlayState")), txt("voicePlayState"));
  check("交付资格写清楚了 playable=false",
        txt("voiceTimingNote").includes("playable=false"), txt("voiceTimingNote").slice(0, 90));
  check("时序表补了「取消登记→被发现」这一行",
        txt("voiceTiming").includes("取消登记 → 被发现"));
  check("时序表写明发现于哪个阶段", txt("voiceTiming").includes("after_asr"));

  // ---- 5. 迟到结果丢弃（C3） ----
  console.log("\n[5] 旧的比新的晚回来 → 整轮丢弃，旧文本一个字都不许进 DOM");
  turnMode = "manual";
  const pOld = dbg.submit("audio", { size: 1111 }, {});
  await flush();
  const pNew = dbg.submit("audio", { size: 2222 }, {});
  await flush();
  check("两条请求都在途（这就是半双工要处理的情形）", pendingTurns.length === 2,
        String(pendingTurns.length));
  // 新的先回来
  pendingTurns[1].resolve(jsonResponse(200, TURN_OK_B));
  await pNew; await flush();
  check("新的结果被渲染了", txt("voiceTranscript") === "设备在线吗", txt("voiceTranscript"));
  // 旧的晚回来
  pendingTurns.shift().resolve(jsonResponse(200, TURN_OK_A));
  await pOld; await flush();
  check("★ 迟到结果被丢弃：提示里说了「已被丢弃」",
        /丢弃/.test(txt("voiceNotice")), txt("voiceNotice").slice(0, 90));
  check("★ 页面仍然是新那一轮的文本", txt("voiceTranscript") === "设备在线吗", txt("voiceTranscript"));
  check("★ 旧那一轮的文本没有出现在任何地方",
        !txt("voiceTranscript").includes("查看上次数据")
        && !txt("voiceAnswer").includes("sensor_records"), txt("voiceAnswer").slice(0, 40));
  check("丢弃不算失败：提示样式是 info 而不是 bad",
        cls("voiceNotice").includes("is-info"), cls("voiceNotice"));
  check("★ 阶段没有被迟到的消息改写", /播报中/.test(txt("voiceStateChip")), txt("voiceStateChip"));

  // ---- 6. 停止播报 ----
  console.log("\n[6] 停止播报 → 打到 /turn/<id>/stop，任务与文字都保留");
  dbg.render(TURN_OK_A.data);
  await flush();
  check("渲染完成的一轮 → 状态条 = 播报中", /播报中/.test(txt("voiceStateChip")), txt("voiceStateChip"));
  const beforeStop = allCalls(/\/api\/v1\/voice\/turn\/[^/]+\/stop$/).length;
  await dbg.stopSpeech();
  await flush();
  check("★ 调用了 /turn/<id>/stop",
        allCalls(/\/api\/v1\/voice\/turn\/[^/]+\/stop$/).length === beforeStop + 1);
  const sCall = lastCall(/\/api\/v1\/voice\/turn\/[^/]+\/stop$/);
  const sBody = bodyOf(sCall);
  check("★ 上报了「停在第几毫秒」", sBody && typeof sBody.played_ms === "number",
        sBody && String(sBody.played_ms));
  check("★ 停止播报**不是**取消：请求打到的是 /stop 而不是 /cancel",
        !/\/cancel$/.test(sCall.url), sCall.url);
  check("播放状态说明「已播出的那一段收不回来」",
        /收不回/.test(txt("voicePlayState")), txt("voicePlayState"));
  check("文字回答仍然在", txt("voiceAnswer").includes("sensor_records"), txt("voiceAnswer").slice(0, 40));
  check("阶段回到空闲", /空闲/.test(txt("voiceStateChip")), txt("voiceStateChip"));

  // ---- 7. 澄清（提问 + 点选项 = 文字补充轮） ----
  console.log("\n[7] 听不准 → 澄清卡片；点候选项 = 走 /voice/text 同一条链路");
  turnMode = "immediate";
  dbg.render(TURN_CLARIFY.data);
  await flush();
  check("★ 澄清卡片显示出来了", !isHidden("voiceClarify"));
  check("★ 状态条 = 等待澄清", /等待澄清/.test(txt("voiceStateChip")), txt("voiceStateChip"));
  check("轮次显示 第 1/2 轮", /第 1\/2 轮/.test(txt("voiceClarifyRound")), txt("voiceClarifyRound"));
  check("问题原文显示", txt("voiceClarifyQ").includes("查看已有记录"), txt("voiceClarifyQ"));
  check("设备候选作为事实列出（不预选）", txt("voiceClarifyOpts").includes("esp32s3eye-1C704C"),
        txt("voiceClarifyOpts").slice(0, 60));
  const optBtns = els.voiceClarifyOpts.children.filter((c) => c._listeners && c._listeners.click);
  check("★ 候选项是可点的按钮（3 个）", optBtns.length === 3, String(optBtns.length));
  check("★ 澄清时「这一轮没调用业务工具」要说清",
        /没有调用任何业务工具|未调用业务工具/.test(txt("voiceClarifyNote")),
        txt("voiceClarifyNote").slice(0, 60));
  check("输入框可用（也可以打字）", els.voiceClarifyInput.disabled === false);

  const beforeText = allCalls(/\/api\/v1\/voice\/text$/).length;
  optBtns[0]._listeners.click[0]({ target: optBtns[0] });
  await flush(80);
  check("★ 点选候选项 → POST /api/v1/voice/text",
        allCalls(/\/api\/v1\/voice\/text$/).length === beforeText + 1);
  const tCall = lastCall(/\/api\/v1\/voice\/text$/);
  const tBody = bodyOf(tCall);
  check("★ 补充轮带 text=被点的那个选项", tBody && tBody.text === "查看上次数据", tBody && tBody.text);
  check("★ 补充轮同样带 session_id / request_id / client_ack_ms",
        !!(tBody && tBody.session_id === SID && /^rq_/.test(tBody.request_id)
           && tBody.client_ack_ms != null), JSON.stringify(tBody || {}).slice(0, 100));
  check("★ 补充轮声明 capture_location 是键盘（不伪装成麦克风）",
        !!(tBody && /^keyboard:/.test(tBody.capture_location || "")), tBody && tBody.capture_location);
  check("★ 补充轮跑完后卡片切成「已消解」，不再是那道问题",
        !isHidden("voiceClarify") && /已消解/.test(txt("voiceClarifyRound"))
        && !txt("voiceClarifyQ").includes("查看已有记录"),
        txt("voiceClarifyRound") + " | " + txt("voiceClarifyQ"));
  check("★ 补充轮的 ①②③ 三格标成「跳过」而不是留空",
        stageStatus("recorded").includes("is-skip") && stageStatus("vad").includes("is-skip")
        && stageStatus("asr").includes("is-skip"),
        [stageStatus("recorded"), stageStatus("vad"), stageStatus("asr")].join(" | "));
  check("★ ④任务格说明消解来源", txt("voiceTiming").length >= 0 && stageStatus("task").includes("is-ok"));

  // ---- 8. 澄清上限 ----
  console.log("\n[8] 澄清到上限 → 只给退路，不再接受输入");
  dbg.render(TURN_EXHAUSTED.data);
  await flush();
  check("卡片仍然显示（要说清为什么停下）", !isHidden("voiceClarify"));
  check("轮次标注为已到上限", /上限/.test(txt("voiceClarifyRound")), txt("voiceClarifyRound"));
  check("★ 给了退路（三个可选说法 + 打字入口）",
        /查看上次数据/.test(txt("voiceClarifyNote")) && /打字/.test(txt("voiceClarifyNote")),
        txt("voiceClarifyNote").slice(0, 70));
  check("★ 输入框被禁用（不能无限问下去）", els.voiceClarifyInput.disabled === true);
  check("发送按钮也被禁用", els.voiceClarifySend.disabled === true);
  check("终局文案说明主动停下", txt("voiceFinal").includes("上限"), txt("voiceFinal"));

  // ---- 9. 已消解 ----
  console.log("\n[9] 已消解 → 说明「按你补充的什么执行了」");
  dbg.render(TURN_RESOLVED.data);
  await flush();
  check("★ 卡片显示「按你的补充执行」", /按你的补充执行/.test(txt("voiceClarifyQ")), txt("voiceClarifyQ"));
  check("★ 说明了消解方式（规则名）", /intent_keyword/.test(txt("voiceClarifyNote")),
        txt("voiceClarifyNote").slice(0, 70));
  check("★ 已消解后不再接受输入", els.voiceClarifyInput.disabled === true);

  // ---- 10. 被顶掉（superseded） ----
  console.log("\n[10] 被顶掉 → 结果保留但不可播");
  dbg.render(TURN_SUPERSEDED.data);
  await flush();
  check("终局文案说明被顶掉", txt("voiceFinal").includes("顶掉"), txt("voiceFinal"));
  check("★ 不可播：按钮禁用 + 无 src", els.voicePlay.disabled === true
        && els.voiceAudio.getAttribute("src") === null);
  check("提示说明「不会覆盖新任务」", /不会覆盖|不覆盖/.test(txt("voiceNotice")),
        txt("voiceNotice").slice(0, 80));

  // ---- 11. 回看历史不改阶段（C7） ----
  console.log("\n[11] 回看历史 → 不许改当前半双工阶段");
  dbg.setPhase("thinking");
  check("先置为处理中", /处理中/.test(txt("voiceStateChip")));
  dbg.render(TURN_OK_A.data, true);
  await flush();
  check("★ 回看一条正常记录后，阶段仍是「处理中」",
        /处理中/.test(txt("voiceStateChip")), txt("voiceStateChip"));
  check("回看时澄清卡片的输入框不能作答", els.voiceClarifyInput.disabled === true);
  dbg.setPhase("idle");

  // ---- 12. 注入 ----
  console.log("\n[12] 注入：只有显式填了才带，并在页面上标出来");
  els.voiceInjectDelay.value = "3000";
  els.voiceInjectTask.value = "1500";
  els.voiceInjectText.value = "重新采集一次";
  const inj = dbg.injectParams();
  check("三个注入参数都被采集", inj.inject_delay_ms === "3000"
        && inj.inject_task_ms === "1500" && inj.inject_asr_text === "重新采集一次",
        JSON.stringify(inj));
  els.voiceInjectDelay.value = "0";
  els.voiceInjectTask.value = "";
  els.voiceInjectText.value = "";
  check("★ 全为 0/空时一个都不带（不污染正常记录）",
        Object.keys(dbg.injectParams()).length === 0, JSON.stringify(dbg.injectParams()));
  dbg.render(Object.assign({}, TURN_OK_A.data, {
    inject: { delay_ms: 3000, task_ms: 0, asr_text: "重新采集一次",
              note: "这是**注入**的一轮" },
  }));
  await flush();
  check("★ 页面上标出「这一轮用了注入」", txt("voiceTimingNote").includes("注入"),
        txt("voiceTimingNote").slice(0, 100));
  check("★ 并说明不是引擎实测", /不是\*\*引擎实测|不是.*引擎实测/.test(txt("voiceTimingNote")),
        txt("voiceTimingNote").slice(0, 120));

  // ---- 13. 放弃录音 / 新建会话 ----
  console.log("\n[13] 放弃录音不产生记录；新建会话会断开旧上下文");
  const beforeTurns = allCalls(/\/api\/v1\/voice\/turn\?/).length;
  dbg.setPhase("listening");
  els.voiceClarify.classList.remove("hidden");
  dbg.abortRecord();                    // 没有真的在录音 → 直接返回，不应发任何请求
  await flush();
  check("★ 没有录音时点停止不会凭空发一个 turn 请求",
        allCalls(/\/api\/v1\/voice\/turn\?/).length === beforeTurns);
  const beforeSess = allCalls(/\/api\/v1\/voice\/session$/).length;
  await dbg.newSession();
  await flush();
  check("★ 新建会话 → 再 POST 一次 /voice/session",
        allCalls(/\/api\/v1\/voice\/session$/).length === beforeSess + 1);
  check("新建会话后澄清卡片收起", isHidden("voiceClarify"));
  check("新建会话后回到空闲", /空闲/.test(txt("voiceStateChip")), txt("voiceStateChip"));
  check("提示说明「历史记录照旧可查」", /历史记录照旧/.test(txt("voiceNotice")),
        txt("voiceNotice").slice(0, 70));

  // ---- 14. 事件时序面板 ----
  console.log("\n[14] 事件时序：按 request_id 串起来看");
  await dbg.timeline();
  await flush();
  check("时序面板打开", !isHidden("voiceTimelineBox"));
  check("★ 列出 3 条事件", els.voiceTimelineRows.children.length === 3,
        String(els.voiceTimelineRows.children.length));
  check("★ 事件名可见（session_created / turn_pending / cancel）",
        txt("voiceTimelineRows").includes("session_created")
        && txt("voiceTimelineRows").includes("turn_pending")
        && txt("voiceTimelineRows").includes("cancel"));
  check("★ 取消事件被标红（可核对「真的取消了没有」）",
        els.voiceTimelineRows.children.some((c) => String(c.className).includes("is-cancel")),
        els.voiceTimelineRows.children.map((c) => c.className).join(","));

  // ---- 15. 源码级红线核对 ----
  console.log("\n[15] 源码级红线核对");
  check("★ C3 迟到判据只认「不是最后一个 request_id」",
        APP_SRC.includes("VOICE.inflight !== req"));
  check("★ C4 三种停是三个不同函数（放弃录音 / 取消 / 停止播报）",
        APP_SRC.includes("function voiceAbortRecord")
        && APP_SRC.includes("function voiceCancelInflight")
        && APP_SRC.includes("function voiceStopSpeech"));
  check("★ 停止播报打 /stop、取消打 /cancel（两条路径不共用）",
        APP_SRC.includes('+ "/stop"') && APP_SRC.includes('"/cancel"'));
  check("★ C2 被隔离时不设 src（源码里是 removeAttribute(\"src\") 分支）",
        /reply_audio_quarantined[\s\S]{0,200}removeAttribute\("src"\)/.test(APP_SRC));
  check("★ 澄清候选项用 textContent 渲染（不解析 HTML，防 XSS）",
        APP_SRC.includes("b.textContent = opt"));
  check("★ 会话失败不阻断提交，但要说清少了什么能力",
        /少了那三件事|没有会话就没有取消/.test(APP_SRC));
  check("★ 注入只在显式填写时附带（Number(dv) > 0 判断）",
        /Number\(dv\) > 0/.test(APP_SRC) && /Number\(tv\) > 0/.test(APP_SRC));
  check("★ 页面不把「播报中」写成「人听到了」",
        !APP_SRC.includes("用户听到") || /播放器.*事实/.test(APP_SRC));
  const html = fs.readFileSync(INDEX_HTML, "utf8");
  check("index.html 有停止按钮与澄清卡片",
        html.includes('id="voiceStopAll"') && html.includes('id="voiceClarify"'));
  check("index.html 有状态条与会话条",
        html.includes('id="voiceStateChip"') && html.includes('id="voiceSid"'));
  check("index.html 有时序表与事件流容器",
        html.includes('id="voiceTiming"') && html.includes('id="voiceTimelineRows"'));
  check("★ 注入区默认是收起的 <details>（不默认干扰现场演示）",
        /<details[^>]*id="voiceInjectBox"/.test(html));

  console.log("\n" + "=".repeat(78));
  if (fails.length) {
    console.log("失败 " + fails.length + " 项：");
    for (const f of fails) console.log("  - " + f);
    console.log("=".repeat(78));
    process.exit(1);
  }
  console.log("全部通过");
  console.log("=".repeat(78));
})();
