#!/usr/bin/env node
/**
 * test_web_nl.js —— 第 4 周「受限工具语言助手」前端行为自检（打桩 DOM，不需要浏览器）
 *
 * 直接加载真实的 web/app.js，用假 DOM + 假 fetch 驱动，断言页面**实际写进**
 * #nlStatus / #nlAnswer / #nlEvidence / #nlTools 的内容。
 *
 * 钉住的红线（与后端一致）：
 *   N1 「查看上次数据」的界面必须显示来源/时间/状态，且明确"未下发（只读）"。
 *   N2 「重新采集一次」只有在 claim=new_record 时才允许出现完成措辞；
 *      pending / timeout 时必须显示"不能确认"，前端不得自行补"成功"。
 *   N3 多设备歧义时必须反问，并给出可选设备按钮；不得自动替用户选。
 *   N4 403 越界必须显示错误码与允许设备，且明说"没有下发指令"。
 *   N5 位置类问题必须显示"数据模型不含位置"，页面不得出现坐标。
 *   N6 网络失败 → 无法确认任何结果，不得显示成功。
 *
 * 运行: node tools/test_web_nl.js
 */

"use strict";

const fs = require("fs");
const path = require("path");

const APP_JS = path.join(__dirname, "..", "web", "app.js");
const INDEX_HTML = path.join(__dirname, "..", "web", "index.html");

// ---------------------------------------------------------------------------
// 打桩：DOM
// ---------------------------------------------------------------------------
const els = {};
function makeElement(id) {
  const classes = new Set();
  const attrs = {};
  const el = {
    id, className: "", width: 0, height: 0, disabled: false, src: "", value: "",
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
    getContext: () => ctx,
    _classes: classes,
    _attrs: attrs,
    querySelector: () => makeElement(id + "-child"),
    querySelectorAll: () => [],
  };
  Object.defineProperty(el, "textContent", {
    get() { return this._text; }, set(v) { this._text = v; },
  });
  Object.defineProperty(el, "innerHTML", {
    get() { return this._html || ""; }, set(v) { this._html = v; },
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
  body: { classList: { add: () => {}, remove: () => {}, contains: () => false } },
  // app.js 会在 document 上挂"按住空格说话"的键盘监听（第 5 周）。
  // 真实浏览器一定有 addEventListener；打桩 DOM 必须补上，否则 app.js 一启动就抛错。
  addEventListener: () => {},
  activeElement: null,
  hidden: false,
};
global.window = { devicePixelRatio: 1, addEventListener: () => {} };
global.location = { search: "", origin: "http://127.0.0.1:8000" };

let wallClock = 1_700_000_000_000;
global.Date.now = () => wallClock;
global.performance = { now: () => 1000 };
global.requestAnimationFrame = () => 1;
global.cancelAnimationFrame = () => {};
global.setTimeout = (fn, ms) => setImmediate(() => { wallClock += (ms || 0); fn(); });
const intervals = [];
global.setInterval = (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; };
global.clearInterval = (h) => { if (h) intervals[h - 1] = null; };

for (const m of fs.readFileSync(INDEX_HTML, "utf8")
         .matchAll(/id="([A-Za-z0-9_]+)"[^>]*>([^<]*)</g)) {
  const stub = els[m[1]] || (els[m[1]] = makeElement(m[1]));
  if (stub) stub.textContent = m[2].trim();
}
els.nlPanel.classList.add("hidden");
els.nlEvidence.classList.add("hidden");
els.nlTools.classList.add("hidden");

// ---------------------------------------------------------------------------
// 假后端
// ---------------------------------------------------------------------------
const calls = [];
let nlReply = null;           // 函数：body => {status, body}
const OLD_TIME = "2026-10-23T01:12:00.000Z";

const RECORD = {
  id: 42, device_id: "esp32s3eye-1C704C", boot_count: 808, seq: 3203,
  sample_idx: 1, sample_count: 1, sample_period_ms: 100,
  ts_local: "2026-10-23T09:12:00.000+0800",
  received_at_utc: OLD_TIME,
  sensor: { model: "QMA6100P", lsb_per_g: 4096, range_g: 2 },
  unit: { raw: "LSB", accel_g: "g", accel_ms2: "m/s^2" },
  raw: { x: -3088, y: 1902, z: 1349 },
  accel_g: { x: -0.753906, y: 0.464355, z: 0.329346 },
  accel_ms2: { x: -7.393, y: 4.553, z: 3.229 },
  magnitude_g: 0.945112, verify_ok: 1,
};

function jsonResponse(status, body) {
  return { status, text: async () => JSON.stringify(body) };
}

const FIX = {
  ambiguous: {
    ok: true, server_time_utc: OLD_TIME, data: {
      trace_id: "nl-abc123", query: "查看上次数据", intent: "query_latest",
      intent_label: "读取已有记录（最新一条）", parser: "rule", confidence: 0.9,
      status: "clarify", claim: "none",
      answer: "你想操作哪台设备？当前接入了 2 台，我不能替你猜。\n（本次没有向任何设备下发指令，也没有读取记录。）",
      ambiguity: {
        needed: true, kind: "device", question: "你想操作哪台设备？",
        candidates: [
          { device_id: "esp32s3eye-1C704C", last_status: "online" },
          { device_id: "esp32s3eye-1C701C", last_status: "offline" },
        ],
      },
      evidence: { source: "无（歧义未消解，未调用业务工具）", device_command_sent: false },
      tool_calls: [{ name: "list_devices", args: {}, ok: true, ms: 1 }],
    },
  },
  readOld: {
    ok: true, server_time_utc: OLD_TIME, data: {
      trace_id: "nl-old001", query: "查看上次数据", intent: "query_latest",
      intent_label: "读取已有记录（最新一条）", parser: "rule", confidence: 0.9,
      status: "answered", claim: "old_record",
      answer: "「查看上次数据」→ 读取已有记录（只读，未向设备下发任何指令）。\n设备：esp32s3eye-1C704C\n" +
              "采样时间：2026-10-23 09:12:00（北京时间）\n来源：sensor_records.id=42\n" +
              "注意：这条记录距今 30 分钟前，属于旧数据，不代表设备此刻的状态。",
      evidence: {
        source: "sensor_records.id=42", device_id: "esp32s3eye-1C704C",
        received_at_utc: OLD_TIME, observed_at_utc: OLD_TIME,
        age_ms: 1800000, freshness: "stale", device_command_sent: false,
      },
      tool_calls: [
        { name: "list_devices", args: {}, ok: true, ms: 1, digest: null },
        { name: "get_latest_record", args: { device_id: "esp32s3eye-1C704C" }, ok: true, ms: 2, digest: { id: 42 } },
      ],
    },
  },
  collectOk: {
    ok: true, server_time_utc: OLD_TIME, data: {
      trace_id: "nl-new001", query: "重新采集一次", intent: "request_collect",
      intent_label: "请求一次新采集", parser: "rule", confidence: 0.93,
      status: "completed", claim: "new_record",
      answer: "「重新采集一次」→ 请求一次新采集（会产生设备侧流量）。\n" +
              "设备回报完成。新观测：record_id=900，|a| = 1.0021 g。\n" +
              "状态：completed —— 这是**本次新产生**的记录，与「查看上次数据」返回的旧记录不是同一条。",
      evidence: {
        source: "observations.observation_id=77", device_id: "esp32s3eye-1C704C",
        record_id: 900, observation_id: 77, completed_at_utc: OLD_TIME,
        received_at_utc: OLD_TIME, age_ms: 120, freshness: "live",
        device_command_sent: true, collect_request_id: "req-1234-abcd",
        collect_status: "completed",
      },
      tool_calls: [
        { name: "list_devices", args: {}, ok: true, ms: 1 },
        { name: "request_collect", args: { device_id: "esp32s3eye-1C704C" }, ok: true, ms: 40 },
        { name: "get_collect_status", args: { request_id: "req-1234-abcd" }, ok: true, ms: 3 },
      ],
    },
  },
  collectPending: {
    ok: true, server_time_utc: OLD_TIME, data: {
      trace_id: "nl-pend01", query: "重新采集一次", intent: "request_collect",
      intent_label: "请求一次新采集", parser: "rule", confidence: 0.93,
      status: "pending", claim: "pending",
      answer: "指令已下发：request_id=req-9999。\n当前状态：submitted（设备尚未回执）。\n" +
              "已等待约 6.0 秒，仍未拿到完成证据；因此**不能确认已采到新数据**。",
      evidence: {
        source: "collect_requests.request_id=req-9999", device_id: "esp32s3eye-1C704C",
        received_at_utc: null, observed_at_utc: OLD_TIME, age_ms: 6000,
        freshness: "live", device_command_sent: true,
        collect_request_id: "req-9999", collect_status: "submitted",
      },
      tool_calls: [
        { name: "list_devices", args: {}, ok: true, ms: 1 },
        { name: "request_collect", args: { device_id: "esp32s3eye-1C704C" }, ok: true, ms: 35 },
        { name: "get_collect_status", args: { request_id: "req-9999" }, ok: true, ms: 4 },
      ],
    },
  },
  location: {
    ok: true, server_time_utc: OLD_TIME, data: {
      trace_id: "nl-loc001", query: "开发板在哪里", intent: "location",
      intent_label: "位置查询（本工程不具备该能力）", parser: "rule", confidence: 0.93,
      status: "insufficient_evidence", claim: "no_evidence",
      error_code: "INSUFFICIENT_LOCATION_EVIDENCE",
      answer: "「开发板在哪里」：这个问题我没法回答。\n本工程的数据包字段只有 raw/accel、seq、" +
              "boot_count、时间戳与设备 ID，**不含任何位置、经纬度、GPS 或基站信息**；\n" +
              "缺少位置证据时我不会推断或生成坐标。",
      evidence: { source: "无（数据模型不含位置字段）", device_command_sent: false },
      tool_calls: [{ name: "list_devices", args: {}, ok: true, ms: 1 }],
    },
  },
};

global.fetch = async (url, opts) => {
  const u = String(url);
  const method = (opts && opts.method) || "GET";
  calls.push({ url: u, method });

  if (u.includes("/api/v1/nl/query")) {
    const body = JSON.parse((opts && opts.body) || "{}");
    return nlReply(body);
  }

  // 既有端点：让 app.js 正常初始化
  if (u.includes("/api/v1/records")) return jsonResponse(200, { ok: true, count: 1, records: [RECORD] });
  if (u.includes("/api/v1/latest")) return jsonResponse(200, { ok: true, record: RECORD });
  if (u.includes("/api/v1/devices")) {
    return jsonResponse(200, { ok: true, devices: [{
      device_id: "esp32s3eye-1C704C", n: 100, last_seen: OLD_TIME,
      model: "QMA6100P", last_seq: 3203, mac: "94:A9:90:1C:70:4C",
      last_status: "online", last_seen_utc: OLD_TIME,
      periodic_enabled: 1, pending_commands: 0,
    }] });
  }
  if (u.includes("/api/v1/stats")) return jsonResponse(200, { ok: true, records: 1, raw_uploads: 1, devices: 1 });
  if (u.includes("/api/v1/collect_requests")) return jsonResponse(200, { ok: true, data: { count: 0, requests: [] } });
  if (u.includes("/api/v1/help_events")) return jsonResponse(200, { ok: true, data: { count: 0, help_events: [] } });
  if (u.includes("/api/v1/cam/status")) return jsonResponse(200, { ok: true, has_frame: false, frame: null });
  return jsonResponse(404, { ok: false, error: "not found" });
};

// ---------------------------------------------------------------------------
// 执行 app.js
// ---------------------------------------------------------------------------
new Function(fs.readFileSync(APP_JS, "utf8"))();

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

function answerText() { return String(els.nlAnswer.textContent || ""); }
function statusText() { return String(els.nlStatus.innerHTML || els.nlStatus.textContent || ""); }
function evidenceHtml() { return String(els.nlEvidence.innerHTML || ""); }
function toolsText() { return String(els.nlTools.innerHTML || ""); }

(async function main() {
  console.log("=".repeat(78));
  console.log("前端「受限工具语言助手」自检（打桩 DOM + 假后端）");
  console.log("=".repeat(78));

  const dbg = window.__nlDebug;
  check("app.js 暴露 __nlDebug", !!dbg && typeof dbg.ask === "function");

  // ---- 1. 打开面板 ----
  console.log("\n[1] 打开语言助手面板");
  await click("btnNl");
  check("面板展开", !els.nlPanel.classList.contains("hidden"));
  check("设备上下文提示（未选中设备）",
        String(els.nlCtx.textContent).includes("全部设备"), els.nlCtx.textContent);

  // ---- 2. 歧义 ----
  console.log("\n[2] 多设备未指明 → 反问，给候选按钮，不自动选");
  nlReply = () => jsonResponse(200, FIX.ambiguous);
  await dbg.ask("查看上次数据");
  await flush();
  check("答案里明确反问设备", answerText().includes("哪台设备"), answerText().slice(0, 60));
  check("状态行标出需要澄清", /需要澄清/.test(statusText()), statusText());
  check("给出 2 个候选设备按钮",
        (evidenceHtml().match(/class="nl-choice"/g) || []).length === 2, evidenceHtml().slice(0, 120));
  check("候选按钮带 data-dev（点了才发）",
        evidenceHtml().includes('data-dev="esp32s3eye-1C704C"'));
  check("歧义时未显示任何『已下发』字样", !evidenceHtml().includes("已下发"));

  // ---- 3. 查看上次数据（有设备上下文）----
  console.log("\n[3] 查看上次数据 → 只读，来源/时间/状态齐全");
  els.deviceSelect.value = "esp32s3eye-1C704C";
  // 触发顶部设备选择（app.js 已有监听）
  const devListeners = els.deviceSelect._listeners.change || [];
  devListeners.forEach((fn) => fn({ target: els.deviceSelect }));
  await flush();
  check("设备上下文同步到面板", String(els.nlCtx.textContent).includes("esp32s3eye-1C704C"),
        els.nlCtx.textContent);

  nlReply = (body) => {
    // 断言请求体带上了设备上下文
    check("请求体带上 device_id（来自页面上下文）", body.device_id === "esp32s3eye-1C704C",
          JSON.stringify(body));
    return jsonResponse(200, FIX.readOld);
  };
  await dbg.ask("查看上次数据");
  await flush();
  check("状态行显示意图=读取已有记录", /读取已有记录/.test(statusText()), statusText());
  check("答案保留旧时间", answerText().includes("2026-10-23 09:12:00"));
  check("答案说明是旧数据", answerText().includes("旧数据"));
  check("证据芯片显示来源 sensor_records.id=42", evidenceHtml().includes("sensor_records.id=42"));
  check("★ 证据芯片明确『未下发（只读）』", evidenceHtml().includes("未下发（只读）"), evidenceHtml());
  check("证据不出现『已下发』", !evidenceHtml().includes("已下发"));
  check("时间芯片来自接口时间戳", evidenceHtml().includes("时间"));
  check("工具轨迹含 get_latest_record", toolsText().includes("get_latest_record"), toolsText());

  // ---- 4. 重新采集：真完成 ----
  console.log("\n[4] 重新采集一次 → completed + new_record");
  nlReply = () => jsonResponse(200, FIX.collectOk);
  await dbg.ask("重新采集一次");
  await flush();
  check("状态行显示意图=请求一次新采集", /请求一次新采集/.test(statusText()), statusText());
  check("答案区分『本次新产生』与旧记录", answerText().includes("本次新产生") &&
        answerText().includes("旧记录"));
  check("证据芯片显示『已下发』", evidenceHtml().includes("已下发"), evidenceHtml());
  check("证据芯片显示 collect 状态 completed", evidenceHtml().includes("completed"));
  check("工具轨迹含 request_collect", toolsText().includes("request_collect"), toolsText());

  // ---- 5. 重新采集：设备不回报 ----
  console.log("\n[5] 设备不回报 → 页面不得补『成功』");
  nlReply = () => jsonResponse(200, FIX.collectPending);
  await dbg.ask("重新采集一次");
  await flush();
  check("答案明确『不能确认』", answerText().includes("不能确认"), answerText().slice(-60));
  check("★ 答案不含『采集成功』", !answerText().includes("采集成功"));
  check("★ 页面全文不含『已成功』", !answerText().includes("已成功"));
  check("证据显示已下发但状态 submitted",
        evidenceHtml().includes("已下发") && evidenceHtml().includes("submitted"));

  // ---- 6. 位置 ----
  console.log("\n[6] 位置问题 → 说明数据不足，页面不出现坐标");
  nlReply = () => jsonResponse(200, FIX.location);
  await dbg.ask("开发板在哪里");
  await flush();
  check("答案说明不含位置字段", answerText().includes("不含任何位置"));
  check("答案说明不会生成坐标", answerText().includes("不会推断或生成坐标"));
  check("页面无经纬度样式数字",
        !/\d{1,3}\.\d{4,}\s*[,，]\s*\d{1,3}\.\d{4,}/.test(answerText()));

  // ---- 7. 越界 403 ----
  console.log("\n[7] 未接入设备 → 403 原样显示，且明说未下发");
  nlReply = () => jsonResponse(403, {
    ok: false, error: {
      code: "DEVICE_OUT_OF_SCOPE",
      message: "设备 esp32s3eye-HACK 不在已接入范围内，助手不会向它发起任何操作",
      details: { allowed_devices: ["esp32s3eye-1C704C", "esp32s3eye-1C701C"] },
    },
  });
  await dbg.ask("查看 esp32s3eye-HACK 的数据");
  await flush();
  check("状态行显示错误码", statusText().includes("DEVICE_OUT_OF_SCOPE"), statusText());
  check("答案列出允许设备", answerText().includes("esp32s3eye-1C704C"));
  check("明说没有下发指令", answerText().includes("没有向任何设备下发指令"));
  check("错误态样式 is-bad", els.nlStatus.classList.contains("is-bad"));

  // ---- 8. 网络失败 ----
  console.log("\n[8] 网络失败 → 无法确认任何结果");
  const realFetch = global.fetch;
  global.fetch = async (url, opts) => {
    if (String(url).includes("/api/v1/nl/query")) throw new Error("boom");
    return realFetch(url, opts);
  };
  await dbg.ask("重新采集一次");
  await flush();
  global.fetch = realFetch;
  check("状态行显示 NETWORK_ERROR", statusText().includes("NETWORK_ERROR"), statusText());
  check("★ 网络失败时不得出现成功措辞",
        !answerText().includes("成功") && answerText().includes("无法确认"), answerText());

  // ---- 9. 清空 ----
  console.log("\n[9] 清空");
  await click("nlClear");
  check("答案被清空", answerText() === "");
  check("证据区隐藏", els.nlEvidence.classList.contains("hidden"));

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
