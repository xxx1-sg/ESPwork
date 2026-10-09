#!/usr/bin/env node
/**
 * test_web_collect.js —— 用打桩 DOM 实测前端「远程采集」状态展示（不需要浏览器）
 *
 * 直接加载真实的 web/app.js，用假 DOM + 假 fetch 驱动，断言页面**实际写进
 * #reqBadge / #reqDetail** 的文本。重点验证作业的两条红线：
 *
 *   1) 徽标必须与后端 status 一一对应，页面**不得自造状态**；
 *   2) `acked`（设备接收回执）**绝不能**显示成「完成」；
 *   3) `timeout` 的文案只能说"没等到"，**禁止**出现「设备故障 / 传感器损坏 / 硬件故障」；
 *   4) `failed` + SENSOR_ERROR 才说「设备明确拒绝：传感器读取失败」；
 *   5) 「刷新已存数据」不得调用任何 collect_* 接口（零设备侧流量）；
 *   6) 「采集最新数据」必须 POST /api/v1/collect_requests 并开始 1s 轮询详情。
 *
 * 运行: node tools/test_web_collect.js
 */

"use strict";

const fs = require("fs");
const path = require("path");

const APP_JS = path.join(__dirname, "..", "web", "app.js");

// ---------------------------------------------------------------------------
// 打桩：DOM / window / location / fetch
// ---------------------------------------------------------------------------
const els = {};
function makeElement(id) {
  const classes = new Set();
  const el = {
    id, className: "", width: 0, height: 0,
    _listeners: {},
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c) => (classes.has(c) ? classes.delete(c) : classes.add(c)),
    },
    addEventListener: (ev, fn) => { (el._listeners[ev] = el._listeners[ev] || []).push(fn); },
    getContext: () => ctx,
    _classes: classes,
    querySelector: () => makeElement(id + "-child"),
    querySelectorAll: () => [],
  };
  Object.defineProperty(el, "textContent", {
    get() { return this._text; },
    set(v) { this._text = v; },
  });
  Object.defineProperty(el, "innerHTML", {
    get() { return this._html; },
    set(v) { this._html = v; },
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
  hidden: false,
};
global.window = { devicePixelRatio: 1, addEventListener: () => {} };
global.location = { search: "", origin: "http://127.0.0.1:8000" };

let wallClock = 1_700_000_000_000;
global.Date.now = () => wallClock;
global.performance = { now: () => 1000 };
global.requestAnimationFrame = () => 1;
global.cancelAnimationFrame = () => {};

const intervals = [];
global.setInterval = (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; };
global.clearInterval = (h) => { intervals[h - 1] = null; };

// 预建测试要读的元素
["freshness", "valGX", "valGY", "valGZ", "valMag", "valMS2X", "valMS2Y", "valMS2Z",
 "dDeviceId", "dRaw", "rawJson", "linkStatus", "apiInfo", "statInfo", "pollInfo",
 "fpsInfo", "chartNote", "dAge", "toolHint", "logList", "logCount",
 "reqBadge", "reqDetail", "reqTimeline", "reqObservation", "reqList",
 "collectBtn", "refreshBtn", "pauseBtn", "reqListRefresh", "deviceSelect",
 "collectDeviceChip"].forEach((id) => document.getElementById(id));

// 用 index.html 的真实初始文本填充（这样"初始显示什么"也是被测的一部分）
const INDEX_HTML = fs.readFileSync(path.join(__dirname, "..", "web", "index.html"), "utf8");
for (const m of INDEX_HTML.matchAll(/id="([A-Za-z0-9_]+)"[^>]*>([^<]*)</g)) {
  const stub = els[m[1]];
  if (stub) stub.textContent = m[2].trim();
}

// ---------------------------------------------------------------------------
// 假 fetch：记录每一次调用，便于断言"刷新不碰 collect_*"
// ---------------------------------------------------------------------------
const calls = [];
let reqStatus = "submitted";     // 详情接口当前返回的 status
let createResponse = null;       // 创建接口的返回值
let detailPayload = null;        // 详情接口完整 data

const RECORD = {
  id: 42, device_id: "esp32s3eye-1C701C", boot_count: 808, seq: 3203,
  sample_idx: 1, sample_count: 1, sample_period_ms: 100,
  ts_local: "2026-10-08T13:49:31.437+0800",
  received_at_utc: new Date(Date.now() - 500).toISOString(),
  sensor: { model: "QMA6100P", lsb_per_g: 4096, range_g: 2 },
  unit: { raw: "LSB", accel_g: "g", accel_ms2: "m/s^2" },
  raw: { x: -3088, y: 1902, z: 1349 },
  accel_g: { x: -0.753906, y: 0.464355, z: 0.329346 },
  accel_ms2: { x: -7.393, y: 4.553, z: 3.229 },
  magnitude_g: 0.945112, verify_ok: 1,
};

function jsonResponse(status, body) {
  return {
    status,
    text: async () => JSON.stringify(body),
  };
}

global.fetch = async (url, opts) => {
  const u = String(url);
  const method = (opts && opts.method) || "GET";
  calls.push({ url: u, method });

  if (u.includes("/api/v1/collect_requests") && method === "POST") {
    return jsonResponse(201, createResponse || {
      ok: true, server_time_utc: new Date().toISOString(),
      data: {
        request_id: "b7f1c2e0-9a44-4c1f-8f2a-0d5e6c7b8a91",
        status: "submitted",
        submitted_at_utc: new Date().toISOString(),
        timeout_ms: 15000,
        deadline_utc: new Date(Date.now() + 15000).toISOString(),
        baseline: { boot_count: 808, seq_last: 3202, uptime_ms: 320000 },
        poll_url: "/api/v1/collect_requests/b7f1c2e0-9a44-4c1f-8f2a-0d5e6c7b8a91",
        deduplicated: false,
      },
    });
  }
  if (u.includes("/api/v1/collect_requests/")) {
    const d = detailPayload || {
      request_id: "b7f1c2e0-9a44-4c1f-8f2a-0d5e6c7b8a91",
      device_id: "esp32s3eye-1C701C", sensor: "qma6100p", status: reqStatus,
      submitted_at_utc: new Date().toISOString(),
      dispatched_at_utc: new Date().toISOString(),
      acked_at_utc: null, completed_at_utc: null,
      deadline_utc: new Date(Date.now() + 15000).toISOString(),
      timeout_ms: 15000, ack: null, observation: null,
      baseline: { boot_count: 808, seq_last: 3202, uptime_ms: 320000 },
      checks: null, rejections: [], elapsed_ms: {},
      device: { last_status: "online", last_seen_utc: new Date().toISOString(),
                periodic_enabled: 1 },
      failure_reason: null, actions: ["cancel"],
    };
    return jsonResponse(200, { ok: true, data: d });
  }
  if (u.includes("/api/v1/collect_requests?") || u.endsWith("/api/v1/collect_requests")) {
    return jsonResponse(200, { ok: true, data: { count: 0, requests: [] } });
  }
  if (u.includes("/api/v1/records")) {
    return jsonResponse(200, { ok: true, count: 1, records: [RECORD] });
  }
  if (u.includes("/api/v1/devices")) {
    return jsonResponse(200, { ok: true, devices: [{
      device_id: "esp32s3eye-1C701C", n: 100, last_seen: RECORD.received_at_utc,
      model: "QMA6100P", last_seq: 3203, mac: "94:A9:90:1C:70:1C",
      last_status: "online", last_seen_utc: RECORD.received_at_utc,
      periodic_enabled: 1, pending_commands: 0,
    }] });
  }
  if (u.includes("/api/v1/stats")) {
    return jsonResponse(200, { ok: true, records: 1, raw_uploads: 1, devices: 1,
                               requests_total: 1, requests_completed: 1,
                               requests_timeout: 0, rejected_observations_total: 0 });
  }
  return jsonResponse(404, { ok: false, error: "not found" });
};

// ---------------------------------------------------------------------------
// 加载真实的 web/app.js
// ---------------------------------------------------------------------------
const src = fs.readFileSync(APP_JS, "utf8");
// eslint-disable-next-line no-new-func
new Function(src)();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let failed = 0;
function check(name, cond, extra) {
  console.log((cond ? "  ✔ " : "  ✘ ") + name + (extra ? "  " + extra : ""));
  if (!cond) failed++;
}

function badge() { return String(els.reqBadge.textContent || "").trim(); }
function detail() { return String(els.reqDetail.innerHTML || els.reqDetail.textContent || ""); }
function fire(id, ev) {
  const stub = els[id];
  const ls = stub && stub._listeners && stub._listeners[ev];
  if (ls) ls.forEach((fn) => fn({ target: stub }));
}

async function main() {
  console.log("=".repeat(78));
  console.log("前端「远程采集」状态展示实测（打桩 DOM + 真实 web/app.js）");
  console.log("=".repeat(78));

  // 让 app.js 完成初始化（它会在 seedHistory 之后挂 setInterval）
  await sleep(60);

  // ---- 用例 1：初始状态不得出现任何业务状态词 ----
  console.log("\n[1] 初始状态");
  check("初始徽标不是'完成'等业务状态", !badge().includes("完成"), `badge="${badge()}"`);

  // ---- 用例 2：刷新已存数据 = 只读接口、零 collect_* ----
  console.log("\n[2] 「刷新已存数据」的网络行为（必须零设备侧流量）");
  calls.length = 0;
  fire("refreshBtn", "click");
  await sleep(120);
  const collectCalls = calls.filter((c) => c.url.includes("collect_requests"));
  check("刷新期间没有任何 collect_requests 调用", collectCalls.length === 0,
        `calls=${JSON.stringify(collectCalls)}`);
  check("刷新确实调用了 GET /records（读已存数据）",
        calls.some((c) => c.url.includes("/api/v1/records")));

  // ---- 用例 3：采集最新数据 = POST collect_requests + 轮询 ----
  console.log("\n[3] 「采集最新数据」的请求行为");
  els.deviceSelect.value = "esp32s3eye-1C701C";
  calls.length = 0;
  fire("collectBtn", "click");
  await sleep(80);
  const created = calls.filter((c) => c.method === "POST" && c.url.endsWith("/api/v1/collect_requests"));
  check("点击后 POST /api/v1/collect_requests", created.length >= 1,
        `calls=${JSON.stringify(calls.map((c) => c.method + " " + c.url))}`);
  const polling = calls.filter((c) => c.url.includes("/api/v1/collect_requests/"));
  check("开始轮询请求详情（GET /collect_requests/<id>）", polling.length >= 1);
  check("轮询用的是创建响应给的 poll_url",
        polling.some((c) => c.url.includes("b7f1c2e0-9a44-4c1f-8f2a-0d5e6c7b8a91")));

  // ---- 用例 4：六种状态的徽标映射 ----
  console.log("\n[4] 六种后端 status → 徽标文案（一一对应，不自造状态）");
  const CASES = [
    ["submitted",  "已提交",         []],
    ["acked",      "设备接收回执",   []],
    ["collecting", "设备采集中",     []],
    ["completed",  "完成",           []],
    ["failed",     "失败",           []],
    ["timeout",    "超时",           ["设备故障", "传感器损坏", "硬件故障"]],
  ];

  /**
   * 判断文本是否**断言**了硬件故障。
   * ★ 关键：页面写「这不等于设备故障」是**正确**的（明确否认），
   *   不能因为出现"设备故障"这四个字就判失败 —— 那样会逼着实现
   *   连"否认故障"都不敢写。这里排掉否定语境后再判断。
   */
  function assertsHardwareFault(text) {
    const negated = text
      .replace(/不等于设备故障/g, "")
      .replace(/不代表设备故障/g, "")
      .replace(/无法据此判定硬件状态/g, "")
      .replace(/这不等于[^。]*/g, "");
    return ["设备故障", "传感器损坏", "硬件故障"].filter((w) => negated.includes(w));
  }

  for (const [st, want, forbidden] of CASES) {
    detailPayload = {
      request_id: "b7f1c2e0-9a44-4c1f-8f2a-0d5e6c7b8a91",
      device_id: "esp32s3eye-1C701C", sensor: "qma6100p", status: st,
      submitted_at_utc: new Date().toISOString(),
      dispatched_at_utc: new Date().toISOString(),
      acked_at_utc: st === "submitted" ? null : new Date().toISOString(),
      completed_at_utc: st === "completed" ? new Date().toISOString() : null,
      deadline_utc: new Date(Date.now() + 15000).toISOString(),
      timeout_ms: 15000,
      ack: st === "submitted" ? null
           : { ack_type: st === "failed" ? "rejected" : "received",
               ack_at_utc: new Date().toISOString(), attempt: 1 },
      observation: st === "completed" ? {
        observation_id: 42, record_id: 10720, upload_id: 2454,
        boot_count: 808, seq_first: 3203, seq_last: 3203, sample_count: 1,
        up_ms: 320120, ts_local: RECORD.ts_local,
        magnitude_g: RECORD.magnitude_g, raw: RECORD.raw,
        accel_g: RECORD.accel_g, accel_ms2: RECORD.accel_ms2,
        freshness: "new_monotonic", verify_ok: 1,
      } : null,
      baseline: { boot_count: 808, seq_last: 3202, uptime_ms: 320000 },
      checks: st === "completed"
        ? { l1_identity: true, l2_monotonic: true, l3_time: true } : null,
      rejections: [], elapsed_ms: { submit_to_ack: 620, submit_to_complete: 1400 },
      device: { last_status: "online", last_seen_utc: new Date().toISOString(),
                periodic_enabled: 1 },
      failure_reason: st === "failed" ? "SENSOR_ERROR" : null,
      actions: ["cancel"],
    };
    // 触发一次轮询渲染
    calls.length = 0;
    fire("collectBtn", "click");
    await sleep(80);
    const b = badge();
    const d = detail();
    check(`status=${st} → 徽标「${want}」`, b.includes(want), `实际="${b}"`);
    if (forbidden.length) {
      const hits = assertsHardwareFault(d);
      check(`status=${st} 的文案未断言硬件故障`, hits.length === 0,
            hits.length ? `实际含：${hits.join("、")}` : "");
    }
  }

  // ---- 用例 5：acked 时绝不能把"完成"当成状态来显示 ----
  console.log("\n[5] 红线：设备回执 ≠ 完成");
  detailPayload.status = "acked";
  detailPayload.completed_at_utc = null;
  detailPayload.observation = null;
  fire("collectBtn", "click");
  await sleep(80);
  check("acked 徽标 = 设备接收回执（不是'完成'）",
        badge() === "设备接收回执", `实际="${badge()}"`);
  check("acked 文案明确说明'还不算采集完成'",
        detail().includes("还不算采集完成"), `实际：${detail().slice(0, 160)}`);
  check("acked 时没有观测数值区（还没有观测）",
        !detail().includes("本次新观测"));
  check("acked 时观测容器保持隐藏",
        els.reqObservation._classes.has("hidden"));

  // ---- 用例 6：timeout 文案禁止出现"故障"字样 ----
  console.log("\n[6] 红线：超时 ≠ 硬件故障（页面文案）");
  detailPayload.status = "timeout";
  detailPayload.device = { last_status: "offline", online: false,
                           last_seen_utc: new Date(Date.now() - 60000).toISOString(),
                           periodic_enabled: 1 };
  detailPayload.failure_reason = null;
  fire("collectBtn", "click");
  await sleep(80);
  const td = detail();
  check("timeout 徽标 = 超时", badge().includes("超时"), `实际="${badge()}"`);
  check("离线超时 → 文案含「设备当前离线」", td.includes("设备当前离线"));
  check("离线超时 → 明确说明无法据此判定硬件状态",
        td.includes("无法据此判定硬件状态"));
  check("离线超时 → 未断言硬件故障", assertsHardwareFault(td).length === 0,
        assertsHardwareFault(td).join("、"));

  // 在线但超时 → 文案应说明"在线但未回报"
  detailPayload.device = { last_status: "online", online: true,
                           last_seen_utc: new Date().toISOString(),
                           periodic_enabled: 1 };
  fire("collectBtn", "click");
  await sleep(80);
  check("在线超时 → 文案为「设备在线，但未在 15s 内回报」",
        detail().includes("设备在线，但未在 15s 内回报"),
        `实际：${detail().slice(0, 160)}`);
  check("在线超时 → 未断言硬件故障", assertsHardwareFault(detail()).length === 0,
        assertsHardwareFault(detail()).join("、"));

  // ---- 用例 7：failed + SENSOR_ERROR 才说硬件问题 ----
  console.log("\n[7] 硬件问题的唯一说法来源：设备明确拒绝");
  detailPayload.status = "failed";
  detailPayload.device.last_status = "online";
  detailPayload.failure_reason = "SENSOR_ERROR";
  fire("collectBtn", "click");
  await sleep(80);
  check("failed 徽标 = 失败", badge().includes("失败"), `实际="${badge()}"`);
  check("SENSOR_ERROR 文案含「设备明确拒绝」且含「传感器读取失败」",
        detail().includes("设备明确拒绝") && detail().includes("传感器读取失败"),
        `实际：${detail().slice(0, 200)}`);

  // ---- 用例 8：拒收留痕要显示出来 ----
  console.log("\n[8] 被拒收的观测必须显式呈现（旧记录不可改写为本次完成）");
  detailPayload.status = "submitter";
  detailPayload.status = "submitted";
  detailPayload.rejections = [{
    rejected_id: 6, reason_code: "STALE_OBSERVATION",
    reason_detail: "观测 seq_last=3202 未超过基线 3202，判定为旧值",
    received_at_utc: new Date().toISOString(),
  }];
  fire("collectBtn", "click");
  await sleep(80);
  const rd = detail();
  check("显示被拒收条数与原因码", rd.includes("STALE_OBSERVATION") && rd.includes("被拒收"),
        `实际：${rd.slice(0, 200)}`);

  // ---- 用例 9：终态后停止轮询 ----
  console.log("\n[9] 完成的请求：显示观测数值，并停止轮询（不空打后端）");
  detailPayload.status = "completed";
  detailPayload.completed_at_utc = new Date().toISOString();
  detailPayload.rejections = [];
  detailPayload.observation = {
    observation_id: 42, record_id: 10720, upload_id: 2454, boot_count: 808,
    seq_first: 3203, seq_last: 3203, sample_count: 1, up_ms: 320120,
    ts_local: RECORD.ts_local, magnitude_g: RECORD.magnitude_g, raw: RECORD.raw,
    accel_g: RECORD.accel_g, accel_ms2: RECORD.accel_ms2,
    freshness: "new_monotonic", verify_ok: 1,
  };
  detailPayload.checks = { l1_identity: true, l2_monotonic: true, l3_time: true };

  // 先进入 submitted（这样轮询不会立刻停），再切到 completed 触发一次终态判定
  detailPayload.status = "submitted";
  fire("collectBtn", "click");
  await sleep(80);
  detailPayload.status = "completed";

  // 驱动一次 1s 轮询：应该渲染完成并停止后续轮询
  const tickFns = () => intervals.filter(Boolean).map((i) => i.fn);
  calls.length = 0;
  for (const fn of tickFns()) { try { fn(); } catch (e) { /* 忽略 */ } }
  await sleep(150);
  check("完成时显示观测数值区（本次新观测）",
        String(els.reqObservation.innerHTML || "").includes("本次新观测"),
        `实际：${String(els.reqObservation.innerHTML).slice(0, 120)}`);
  check("完成文案说明三层校验（L1/L2/L3）",
        detail().includes("L1") && detail().includes("L3"));
  check("完成时显示 magnitude_g 真实数值",
        String(els.reqObservation.innerHTML || "").includes("0.945112"),
        `观测区：${String(els.reqObservation.innerHTML).slice(0, 140)}`);

  // 终态之后：再驱动多轮 1s 轮询，详情接口调用次数必须为 0
  calls.length = 0;
  for (let i = 0; i < 3; i++) {
    for (const fn of tickFns()) { try { fn(); } catch (e) { /* 忽略 */ } }
    await sleep(60);
  }
  const afterTicks = calls.filter((c) => c.url.includes("/collect_requests/"));
  check("终态后 1s 轮询停止（详情接口调用数 = 0）", afterTicks.length === 0,
        `仍调用 ${afterTicks.length} 次`);

  console.log("\n" + "=".repeat(78));
  console.log(failed === 0 ? "全部用例通过 ✔（远程采集状态展示符合要求）"
                           : `${failed} 项失败 ✘`);
  console.log("=".repeat(78));
  process.exit(failed === 0 ? 0 : 1);
}

main().catch((e) => { console.error(e); process.exit(1); });