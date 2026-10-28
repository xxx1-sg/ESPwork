#!/usr/bin/env node
/**
 * test_web_voice.js —— 第 5 周「按键说话 + 语音播报」前端行为自检（打桩 DOM，不需要浏览器）
 *
 * 直接加载真实的 web/app.js，用假 DOM + 假 fetch 驱动，断言页面**实际写进**
 * 六段状态、提示区、结果区的内容。
 *
 * 钉住的红线：
 *   W1 六段状态的显示必须**只来自**后端 stages；页面不自己点亮任何一格。
 *   W2 识别失败 / 无声 / 超时 / 引擎不可用 → 结果区必须隐藏，
 *      页面上**不得出现任何"识别文本"**（不拿旧句子、不猜、不补占位）。
 *   W3 每种失败都要给出可理解的下一步，并指向"文字入口仍然可用"。
 *   W4 合成失败（tts_failed）时，识别文本与文字回答必须仍然完整显示，
 *      播放按钮必须禁用 —— 不能让人以为能听。
 *   W5 播放文案只能是"播放器开始播放"，不得出现"已听到"。
 *   W6 采样格式错误必须把"实际参数"和"要求格式"都显示出来。
 *   W7 语音/文字两条入口的对照表：一致标"一致"，不同标"不同"。
 *   W8 源码级核对：app.js 里不存在"识别失败就回退到某段文本"的兜底写法。
 *
 * 运行: node tools/test_web_voice.js
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
    children: [], _em: null, style: {},
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
    // 打桩 DOM 没有真实布局：appendChild 时把子节点的文本累积到父节点上，
    // 否则 voiceNotice / voiceHistory 这类"先清空再逐个 append"的写法读不到文字。
    appendChild: (c) => {
      el.children.push(c);
      el._text = (el._text || "") + String((c && c.textContent) || "");
      return c;
    },
    getContext: () => ctx,
    _classes: classes,
    _attrs: attrs,
    querySelector(sel) {
      // voiceStageSet 用 'li[data-k="xxx"]' 定位某一格
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
// Node 22 自带只读的 global.navigator，必须用 defineProperty 覆盖
Object.defineProperty(global, "navigator", {
  configurable: true,
  value: {
    userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    mediaDevices: null,   // 本自检不测真实采集；录音入口的失败分支由"没有 getUserMedia"覆盖
  },
});

let wallClock = 1_700_000_000_000;
global.Date.now = () => wallClock;
global.performance = { now: () => 1000 };
global.requestAnimationFrame = () => 1;
global.cancelAnimationFrame = () => {};
global.setTimeout = (fn, ms) => setImmediate(() => { wallClock += (ms || 0); fn(); });
const intervals = [];
global.setInterval = (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; };
global.clearInterval = (h) => { if (h) intervals[intervals.length - 1] = null; };

// 从 index.html 里把静态文案带进桩，尽量贴近真实初始状态
for (const m of fs.readFileSync(INDEX_HTML, "utf8")
         .matchAll(/id="([A-Za-z0-9_]+)"[^>]*>([^<]*)</g)) {
  const stub = els[m[1]] || (els[m[1]] = makeElement(m[1]));
  if (stub) stub.textContent = m[2].trim();
}

// 六段状态的 li 必须存在，否则 voiceStageSet 找不到目标
els.voiceStages.children = STAGE_KEYS.map((k) => {
  const li = makeElement("li-" + k);
  li._attrs["data-k"] = k;
  li._em = makeElement("li-" + k + "-em");
  li._em.textContent = "—";
  return li;
});

els.voicePanel.classList.add("hidden");
els.voiceResult.classList.add("hidden");
els.voiceNotice.classList.add("hidden");

// ---------------------------------------------------------------------------
// 假后端
// ---------------------------------------------------------------------------
const calls = [];
let voiceReply = null;      // (url, opts) => {status, body}
const T0 = "2026-10-23T02:20:00.000Z";

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

const TURN_OK = {
  ok: true, data: {
    turn_id: "vt_ok_0001", final_state: "done",
    audio_source: "browser_mic", audio_source_label: "浏览器麦克风（电脑音频）",
    audio: BASE_AUDIO, run_location: BASE_RUNLOC,
    stages: stages({
      recorded: { status: "ok", detail: "2.28 秒 · 16000 Hz/16 bit/1 声道" },
      vad: { status: "ok", rms: 0.066667, peak: 0.671478, detail: "RMS=0.0667" },
      asr: { status: "ok", ms: 635, detail: "引擎 vosk → “查看上次数据”" },
      task: { status: "ok", ms: 18, detail: "意图=query_latest" },
      tts: { status: "ok", ms: 310, detail: "产出 9.7 秒 WAV" },
      playback: { status: "pending", detail: "等待浏览器播放器开始播放" },
    }),
    transcript: "查看上次数据",
    asr: { status: "ok", engine: "vosk", model: "vosk-model-small-cn-0.22", ms: 635, error: null },
    task: { status: "ok", intent: "query_latest", claim: "old_record",
            remote_status: "answered", source: "sensor_records.id=42",
            answer: "设备：esp32s3eye-1C704C\n采样时间：2026-10-23 09:12:00\n来源：sensor_records.id=42\n"
                  + "状态：这是数据库中已经存在的一条记录，不是本次新采的。", error: null },
    tts: { status: "ok", engine: "sapi", voice: "Microsoft Huihui Desktop - Chinese (Simplified)",
           ms: 310, reply_text: "设备 esp32s3eye-1C704C，采样时间…", bytes: 426314, sha256: "d35168", error: null },
    reply_audio_url: "/api/v1/voice/reply/vt_ok_0001.wav",
    audio_url: "/api/v1/voice/audio/vt_ok_0001.wav",
    playback: { started_at_utc: null, reported_by: null,
                note: "播放是浏览器侧事实；服务端只记录上报，不推断“已听到”" },
    error: null, elapsed_ms: 2100, created_at_utc: T0,
  },
};

const TURN_SILENT = {
  ok: true, data: {
    turn_id: "vt_sil_0002", final_state: "no_speech",
    audio_source: "browser_mic", audio_source_label: "浏览器麦克风（电脑音频）",
    audio: Object.assign({}, BASE_AUDIO, { rms: 0.0, peak: 0.0, vad_passed: false }),
    run_location: BASE_RUNLOC,
    stages: stages({
      recorded: { status: "ok", detail: "1.50 秒" },
      vad: { status: "failed", detail: "判定为无有效语音，未提交识别" },
      asr: { status: "skipped", detail: "静音检测未通过" },
      task: { status: "skipped", detail: "没有识别文本" },
      tts: { status: "skipped", detail: "没有回答" },
      playback: { status: "skipped", detail: "没有音频" },
    }),
    transcript: null,
    asr: { status: "skipped", engine: null, model: null, ms: null, error: null },
    task: { status: "skipped", intent: null, claim: null, remote_status: null, source: null,
            answer: null, error: null },
    tts: { status: "skipped", engine: null, voice: null, ms: null, reply_text: null,
           bytes: null, sha256: null, error: null },
    reply_audio_url: null, audio_url: "/api/v1/voice/audio/vt_sil_0002.wav",
    playback: { started_at_utc: null, reported_by: null },
    error: { code: "NO_SPEECH",
             message: "没有检测到有效语音：这段录音的 RMS 为 0.0000（需要 ≥ 0.0060），"
                    + "峰值 0.0000（需要 ≥ 0.0200）。可能麦克风被静音、输入音量太小，或没有对着麦克风说话。" },
    elapsed_ms: 120, created_at_utc: T0,
  },
};

const TURN_ASR_FAIL = {
  ok: true, data: {
    turn_id: "vt_fail_0003", final_state: "asr_failed",
    audio_source: "browser_mic", audio_source_label: "浏览器麦克风（电脑音频）",
    audio: BASE_AUDIO, run_location: BASE_RUNLOC,
    stages: stages({
      recorded: { status: "ok" }, vad: { status: "ok" },
      asr: { status: "failed", detail: "引擎返回 HTTP 500：decoder crashed" },
      task: { status: "skipped" }, tts: { status: "skipped" }, playback: { status: "skipped" },
    }),
    transcript: null,
    asr: { status: "failed", engine: null, model: null, ms: 800,
           error: "引擎返回 HTTP 500：decoder crashed" },
    task: { status: "skipped", intent: null, claim: null, remote_status: null, source: null,
            answer: null, error: null },
    tts: { status: "skipped", reply_text: null, bytes: null },
    reply_audio_url: null, audio_url: "/api/v1/voice/audio/vt_fail_0003.wav",
    playback: {}, error: { code: "ASR_FAILED", message: "识别失败：引擎返回 HTTP 500：decoder crashed。" },
    elapsed_ms: 900, created_at_utc: T0,
  },
};

const TURN_TTS_FAIL = {
  ok: true, data: {
    turn_id: "vt_tts_0004", final_state: "tts_failed",
    audio_source: "uploaded_file", audio_source_label: "上传的授权录音文件",
    audio: BASE_AUDIO, run_location: BASE_RUNLOC,
    stages: stages({
      recorded: { status: "ok" }, vad: { status: "ok" },
      asr: { status: "ok", ms: 620 }, task: { status: "ok", ms: 20 },
      tts: { status: "failed", detail: "合成引擎返回 HTTP 500" },
      playback: { status: "skipped", detail: "没有可播放的音频" },
    }),
    transcript: "重新采集一次",
    asr: { status: "ok", engine: "vosk", model: "vosk-model-small-cn-0.22", ms: 620, error: null },
    task: { status: "ok", intent: "request_collect", claim: "pending",
            remote_status: "pending", source: "collect_requests.request_id=req-9",
            answer: "指令已下发：request_id=req-9。当前状态：submitted。\n不能确认已采到新数据。",
            error: null },
    tts: { status: "failed", engine: null, voice: null, ms: 40, reply_text: "指令已下发…",
           bytes: null, sha256: null, error: "合成引擎返回 HTTP 500：fake tts boom" },
    reply_audio_url: null, audio_url: "/api/v1/voice/audio/vt_tts_0004.wav",
    playback: {},
    error: { code: "TTS_FAILED",
             message: "语音合成没成功：合成引擎返回 HTTP 500：fake tts boom。识别文本与文字回答都不受影响，可以直接阅读回答。" },
    elapsed_ms: 1400, created_at_utc: T0,
  },
};

function jsonResponse(status, body) {
  return {
    status,
    text: async () => JSON.stringify(body),
    json: async () => body,
  };
}

global.fetch = async (url, opts) => {
  const u = String(url);
  const method = (opts && opts.method) || "GET";
  calls.push({ url: u, method });

  // ★ 顺序要紧：/api/v1/voice/turns 也 contains("/api/v1/voice/turn")，
  //   所以列表必须排在单条之前，否则列表请求会被当成"某一条 turn"返回。
  if (u.includes("/api/v1/voice/turns")) {
    return jsonResponse(200, { ok: true, data: { count: 2, limit: 8, turns: [
      { turn_id: "vt_ok_0001", final_state: "done", transcript: "查看上次数据",
        audio_source: "browser_mic", audio_source_label: "浏览器麦克风（电脑音频）", created_at_utc: T0 },
      { turn_id: "vt_sil_0002", final_state: "no_speech", transcript: null,
        audio_source: "browser_mic", audio_source_label: "浏览器麦克风（电脑音频）", created_at_utc: T0 },
    ] } });
  }
  if (u.includes("/api/v1/voice/turn/") && u.endsWith("/played")) {
    return jsonResponse(200, { ok: true, data: { turn_id: "vt_ok_0001", playback_started_at_utc: T0 } });
  }
  if (u.includes("/api/v1/voice/turn")) {
    return voiceReply ? voiceReply(u, opts) : jsonResponse(200, TURN_OK);
  }
  if (u.includes("/api/v1/voice/health")) {
    return jsonResponse(200, { ok: true, data: {
      engine: { ok: true, asr: { backend: "vosk", model_present: true, loaded: true }, tts: { backend: "sapi" } },
      engine_url: "http://127.0.0.1:8013",
      audio_sources: [{ key: "browser_mic" }, { key: "uploaded_file" }, { key: "device_mic" }],
      limits: { required_format: "16000 Hz / 16 bit / mono / PCM WAV" },
    } });
  }
  if (u.includes("/api/v1/nl/query")) {
    const body = JSON.parse((opts && opts.body) || "{}");
    return jsonResponse(200, { ok: true, data: {
      intent: "query_latest", status: "answered", claim: "old_record",
      answer: "设备：esp32s3eye-1C704C\n采样时间：2026-10-23 09:12:00\n来源：sensor_records.id=42\n"
            + "状态：这是数据库中已经存在的一条记录，不是本次新采的。",
      query: body.q,
      evidence: { source: "sensor_records.id=42", device_command_sent: false },
      tool_calls: [{ name: "get_latest_record", args: {}, ok: true, ms: 2 }],
    } });
  }

  // 既有端点：让 app.js 正常初始化
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
async function flush() { for (let i = 0; i < 40; i++) await sleep(); }

async function click(id, ev) {
  const el = els[id];
  const ls = el && el._listeners && el._listeners.click;
  if (ls) ls.forEach((fn) => fn(ev || { target: el }));
  await flush();
}

const txt = (id) => String(els[id].textContent || "");
const html = (id) => String(els[id].innerHTML || "");
const isHidden = (id) => els[id].classList.contains("hidden");
const stageStatus = (k) => {
  const li = els.voiceStages.children.find((c) => c._attrs["data-k"] === k);
  return li ? li.className : "";
};
const stageText = (k) => {
  const li = els.voiceStages.children.find((c) => c._attrs["data-k"] === k);
  return li && li._em ? String(li._em.textContent || "") : "";
};

(async function main() {
  console.log("=".repeat(78));
  console.log("前端「语音输入 → 任务 → 播报」自检（打桩 DOM + 假后端）");
  console.log("=".repeat(78));

  const dbg = window.__voiceDebug;
  check("app.js 暴露 __voiceDebug", !!dbg && typeof dbg.render === "function");

  // ---- 1. 打开面板 ----
  console.log("\n[1] 打开语音面板");
  await click("btnVoice");
  check("面板展开", !isHidden("voicePanel"));
  check("六段初始都是未开始", STAGE_KEYS.every((k) => stageText(k) === "—"),
        STAGE_KEYS.map(stageText).join("/"));
  check("引擎状态已读取并显示就绪", /引擎就绪/.test(txt("voiceEngine")), txt("voiceEngine"));
  check("设备上下文提示（未选中设备）", txt("voiceCtx").includes("全部设备"), txt("voiceCtx"));

  // ---- 2. 正常链路 ----
  console.log("\n[2] 正常 turn → 六段按后端 stages 显示");
  dbg.render(TURN_OK.data);
  await flush();
  check("① 录音格=完成", stageStatus("recorded").includes("is-ok"), stageStatus("recorded"));
  check("② 静音检测格=完成", stageStatus("vad").includes("is-ok"));
  check("③ 识别格=完成", stageStatus("asr").includes("is-ok"));
  check("④ 任务格=完成", stageStatus("task").includes("is-ok"));
  check("⑤ 合成格=完成", stageStatus("tts").includes("is-ok"));
  check("⑥ 播放格=等待播放（页面不自己点亮）",
        stageStatus("playback").includes("is-wait") && stageText("playback") === "等待播放",
        stageText("playback"));
  check("结果区显示", !isHidden("voiceResult"));
  check("★ 识别文本原样显示", txt("voiceTranscript") === "查看上次数据", txt("voiceTranscript"));
  check("意图显示 query_latest", txt("voiceIntent").includes("query_latest"), txt("voiceIntent"));
  check("claim 显示 old_record", txt("voiceClaim").includes("old_record"));
  check("来源标签来自接口", txt("voiceSourceTag").includes("sensor_records.id=42"), txt("voiceSourceTag"));
  check("回答文本原样显示", txt("voiceAnswer").includes("不是本次新采的"), txt("voiceAnswer").slice(0, 60));
  check("有播报音频时播放按钮可用", els.voicePlay.disabled === false);
  check("播放按钮绑定了音频地址", String(els.voiceAudio.src).includes("/api/v1/voice/reply/"),
        els.voiceAudio.src);
  check("播放状态初始为未播放", /未播放/.test(txt("voicePlayState")), txt("voicePlayState"));
  check("★ 播放文案不含『已听到』", !txt("voicePlayState").includes("已听到"));
  check("运行位置五段都显示", ["录音", "识别", "任务", "合成", "播放"].every((k) =>
        txt("voiceRunLoc").includes(k)), txt("voiceRunLoc").slice(0, 80));
  check("★ 运行位置说明了识别跑在本机引擎",
        txt("voiceRunLoc").includes("127.0.0.1:8013"));
  check("正常链路不显示失败提示", isHidden("voiceNotice"));

  // ---- 3. 无声 ----
  console.log("\n[3] 无声 → 结果区隐藏，页面不得出现任何识别文本");
  dbg.render(TURN_SILENT.data);
  await flush();
  check("② 静音检测格=失败", stageStatus("vad").includes("is-bad"), stageStatus("vad"));
  check("③ 识别格=跳过", stageStatus("asr").includes("is-skip"), stageStatus("asr"));
  check("★ 结果区隐藏", isHidden("voiceResult"));
  check("★ 页面上没有任何识别文本", txt("voiceTranscript") === "", "[" + txt("voiceTranscript") + "]");
  check("★ 提示区可见", !isHidden("voiceNotice"));
  check("提示说明没有检测到有效语音", txt("voiceNotice").includes("没有检测到有效语音"),
        txt("voiceNotice").slice(0, 60));
  check("提示给出实测 RMS 与阈值",
        txt("voiceNotice").includes("0.0000") && txt("voiceNotice").includes("0.006"),
        txt("voiceNotice").slice(0, 120));
  check("★ 提示指向文字入口", txt("voiceNotice").includes("语言助手"));
  check("终局文案说明没有执行任务", txt("voiceFinal").includes("没有执行任务"), txt("voiceFinal"));
  check("播放按钮被禁用（没有音频）", els.voicePlay.disabled === true);

  // ---- 4. 识别失败 ----
  console.log("\n[4] 识别失败 → 明确失败原因，不猜文本");
  dbg.render(TURN_ASR_FAIL.data);
  await flush();
  check("③ 识别格=失败", stageStatus("asr").includes("is-bad"), stageStatus("asr"));
  check("★ 结果区隐藏", isHidden("voiceResult"));
  check("★ 识别文本区为空", txt("voiceTranscript") === "");
  check("提示含错误原因（HTTP 500）", txt("voiceNotice").includes("HTTP 500"), txt("voiceNotice").slice(0, 80));
  check("★ 提示说明不会被『猜』成某条指令", txt("voiceNotice").includes("猜"), txt("voiceNotice").slice(-60));
  check("提示指向文字入口", txt("voiceNotice").includes("语言助手"));
  check("终局文案=识别失败", txt("voiceFinal").includes("识别失败"), txt("voiceFinal"));

  // ---- 5. 合成失败（文字仍在） ----
  console.log("\n[5] 合成失败 → 识别文本与回答仍完整，播放禁用");
  dbg.render(TURN_TTS_FAIL.data);
  await flush();
  check("⑤ 合成格=失败", stageStatus("tts").includes("is-bad"), stageStatus("tts"));
  check("★ 结果区仍然显示（识别是成功的）", !isHidden("voiceResult"));
  check("★ 识别文本仍然可见", txt("voiceTranscript") === "重新采集一次", txt("voiceTranscript"));
  check("★ 文字回答仍然可见", txt("voiceAnswer").includes("request_id=req-9"),
        txt("voiceAnswer").slice(0, 50));
  check("★ 提示说明文字回答不受影响", txt("voiceNotice").includes("不受影响"),
        txt("voiceNotice").slice(0, 80));
  check("提示级别是 warn（不是致命错）", els.voiceNotice.className.includes("is-warn"),
        els.voiceNotice.className);
  check("播放按钮禁用", els.voicePlay.disabled === true);
  check("播放状态说明本轮没有音频", txt("voicePlayState").includes("没有播报音频"),
        txt("voicePlayState"));

  // ---- 6. 格式错误（HTTP 400） ----
  console.log("\n[6] 采样格式被拒 → 实际参数与要求格式都显示");
  dbg.error({
    code: "AUDIO_FORMAT_INVALID",
    message: "只接受单声道，实际收到 44100 Hz / 16 bit / 2 声道 / format=1",
    details: { expected: { sample_rate: 16000, bits: 16, channels: 1,
                           container: "RIFF/WAVE PCM" } },
  }, 400);
  await flush();
  check("★ 提示里同时出现实际参数与要求格式",
        txt("voiceNotice").includes("44100") && txt("voiceNotice").includes("16000"),
        txt("voiceNotice").slice(0, 110));
  check("提示带错误码", txt("voiceNotice").includes("AUDIO_FORMAT_INVALID"));
  check("提示指向文字入口", txt("voiceNotice").includes("语言助手"));
  check("终局文案写明请求未被接受", txt("voiceFinal").includes("未被接受"), txt("voiceFinal"));
  check("错误态提示样式为 bad", els.voiceNotice.className.includes("is-bad"),
        els.voiceNotice.className);

  // ---- 7. 对照表 ----
  console.log("\n[7] 语音 / 文字两条入口对照");
  dbg.render(TURN_OK.data);
  await flush();
  await click("voiceCompare");
  await flush();
  const cmp = txt("voiceCompareOut");
  const cmpRows = els.voiceCompareOut.children.filter(
    (c) => String(c.className || "").includes("vc-row"));
  check("对照表已渲染（表头 + 6 行比对）", cmp.includes("两条入口的结论对照") && cmpRows.length === 6,
        "rows=" + cmpRows.length);
  check("★ 意图行被标记为一致", cmpRows.some((r) => r.className.includes("is-same")),
        cmpRows.map((r) => r.className).join(","));
  check("★ 不一致的行会被标红（本例应无）",
        cmpRows.every((r) => !r.className.includes("is-diff")),
        cmpRows.filter((r) => r.className.includes("is-diff")).length);
  check("给出总结（说明语音只是换了输入方式）", cmp.includes("输入方式") || cmp.includes("完全一致"),
        cmp.slice(-80));

  // ---- 8. 历史 ----
  console.log("\n[8] 历史记录渲染");
  await click("voiceRefreshHist");
  await flush();
  const hist = els.voiceHistory.children.map((c) => ({
    s: c.className, t: c.children.map((x) => String(x.textContent)).join(" | "),
  }));
  check("历史列表渲染了 2 条", hist.length === 2, hist.length);
  check("done 记录状态色为成功", hist[0] && hist[0].s.includes("is-done"), hist[0] && hist[0].s);
  check("无声记录状态色为警告", hist[1] && hist[1].s.includes("is-no_speech"), hist[1] && hist[1].s);
  check("无声记录文本显示『无识别文本』而不是空白",
        hist[1] && hist[1].t.includes("无识别文本"), hist[1] && hist[1].t);

  // ---- 9. 清空 ----
  console.log("\n[9] 清空");
  await click("voiceClear");
  check("结果区隐藏", isHidden("voiceResult"));
  check("提示区隐藏", isHidden("voiceNotice"));
  check("六段复位", STAGE_KEYS.every((k) => stageText(k) === "—"),
        STAGE_KEYS.map(stageText).join("/"));
  check("终局文案复位", txt("voiceFinal").includes("还没有提交过语音"), txt("voiceFinal"));

  // ---- 10. 源码级红线核对 ----
  console.log("\n[10] 源码级红线核对（app.js 里不能有兜底造文本的写法）");
  check("★ 不存在 'transcript || 默认文本' 这类兜底",
        !/transcript\s*\|\|\s*["'`][^"'`]{2,}/.test(APP_SRC),
        (APP_SRC.match(/transcript\s*\|\|[^\n]{0,40}/) || ["—"])[0]);
  check("★ 不存在把识别失败改写为上一句的逻辑",
        !/transcript\s*=\s*VOICE\.last/.test(APP_SRC));
  check("★ 源码里没有『已听到』这种断言", !APP_SRC.includes("已听到"));
  check("源码里对『播放』的措辞是『播放器』而不是『用户听到』",
        APP_SRC.includes("播放器已开始播放"));
  check("★ 结果区是否显示取决于 transcript 是否为空（单一判据）",
        /hasText\s*=\s*typeof\s+v\.transcript\s*===\s*"string"/.test(APP_SRC),
        (APP_SRC.match(/hasText[^\n]{0,70}/) || ["—"])[0]);
  check("录音失败分支覆盖了权限被拒/无设备两种原因",
        APP_SRC.includes("NotAllowedError") && APP_SRC.includes("NotFoundError"));

  console.log("\n" + "=".repeat(78));
  if (fails.length) {
    console.log("失败 " + fails.length + " 项：");
    fails.forEach((f) => console.log("  - " + f));
    process.exitCode = 1;
  } else {
    console.log("全部通过");
  }
  console.log("=".repeat(78));
})();
