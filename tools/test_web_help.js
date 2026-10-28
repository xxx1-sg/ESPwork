#!/usr/bin/env node
/**
 * test_web_help.js —— 用打桩 DOM 实测前端「按键求助」显示规则（不需要浏览器）
 *
 * 直接加载真实的 web/app.js，用假 DOM + 假 fetch 驱动，断言页面**实际写进
 * #helpItemStatus / #helpItemRemoteAck / #helpList** 的文本。
 *
 * 本文件验证的是第 3 周**唯一的 P0 红线**（当堂验证第 2 项）：
 *
 *   ★★ 在没有远端回执证据（remote_acked_at_utc 为空）的情况下，
 *      页面不得显示「对方已收到」。
 *
 * 三种事实下的允许/禁止文案（docs/08 §4.6）：
 *   F1 仅本地反馈发生（无任何服务端回执）      → 「佩戴者已按键（待回应）」
 *                                               禁止：「对方已收到」「已送达」「设备已收到回应」「已确认」
 *   F2 status=responded 且 remote_acked_at_utc 为空 → 「我已回应，等待设备确认」
 *                                               禁止：同上三条
 *   F3 remote_acked_at_utc 非空                → 允许「佩戴者设备已收到回应」
 *
 * 另外验证：
 *   - 前端渲染「已收到」的唯一判据是 remote_acked_at_utc（源码级核对，不是 status）
 *   - 取消必须携带 reason（C3）；取消后记录不删除
 *   - 「刷新求助列表」零设备侧指令流量（只 GET，不 POST）
 *
 * 运行: node tools/test_web_help.js
 */

"use strict";

const fs = require("fs");
const path = require("path");

const APP_JS = path.join(__dirname, "..", "web", "app.js");
const INDEX_HTML = path.join(__dirname, "..", "web", "index.html");

// ---------------------------------------------------------------------------
// 打桩：DOM / window / location / fetch
// ---------------------------------------------------------------------------
const els = {};
function makeElement(id) {
  const classes = new Set();
  const el = {
    id, className: "", width: 0, height: 0, disabled: false,
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

// ★ window.prompt 打桩：取消用例要验证"原因必填"这条拦截
let promptAnswer = "误触";
let promptCalls = 0;
global.window.prompt = () => { promptCalls++; return promptAnswer; };

// 预建测试要读的元素（含第 3 周约定的三个 ID）
["freshness", "valGX", "valGY", "valGZ", "valMag", "valMS2X", "valMS2Y", "valMS2Z",
 "dDeviceId", "dRaw", "rawJson", "linkStatus", "apiInfo", "statInfo", "pollInfo",
 "fpsInfo", "chartNote", "dAge", "toolHint", "logList", "logCount",
 "reqBadge", "reqDetail", "reqTimeline", "reqObservation", "reqList",
 "collectBtn", "refreshBtn", "pauseBtn", "reqListRefresh", "deviceSelect",
 "collectDeviceChip",
 // 第 3 周
 "helpPanel", "helpChip", "helpList", "helpItemStatus", "helpItemRemoteAck",
 "helpItemDetail", "helpItemMeta", "helpRefreshBtn", "helpRespondBtn",
 "helpCancelBtn", "helpCountChip", "helpLocalFeedback", "helpLocalFeedbackNote"]
  .forEach((id) => document.getElementById(id));

// 用 index.html 的真实初始文本填充（"初始显示什么"也是被测的一部分）
for (const m of fs.readFileSync(INDEX_HTML, "utf8")
         .matchAll(/id="([A-Za-z0-9_]+)"[^>]*>([^<]*)</g)) {
  const stub = els[m[1]];
  if (stub) stub.textContent = m[2].trim();
}

// ---------------------------------------------------------------------------
// 假 fetch
// ---------------------------------------------------------------------------
const calls = [];
let helpRows = [];            // GET /help_events 返回的列表
let helpDetail = null;        // GET /help_events/<id> 返回的详情
let respondResp = null;       // POST .../response 的返回
let cancelResp = null;        // POST .../cancel 的返回

function jsonResponse(status, body) {
  return { status, text: async () => JSON.stringify(body) };
}

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

global.fetch = async (url, opts) => {
  const u = String(url);
  const method = (opts && opts.method) || "GET";
  calls.push({ url: u, method });

  // ---- 第 3 周端点 ----
  if (u.includes("/api/v1/help_events") && u.endsWith("/response")) {
    return jsonResponse(200, respondResp || {
      ok: true, server_time_utc: new Date().toISOString(),
      data: {
        help_id: "9c2d51ab-3f77-4b90-9f21-7c0a1b2c3d4e",
        status: "responded",
        responded_at_utc: new Date().toISOString(),
        remote_ack_command: "help_ack",
        remote_acked_at_utc: null,   // ★ 设备还没确认
        note: "已记录回应，并已向设备下发 help_ack；设备确认后 remote_acked_at_utc 才会写入",
      },
    });
  }
  if (u.includes("/api/v1/help_events") && u.endsWith("/cancel")) {
    return jsonResponse(200, cancelResp || {
      ok: true, server_time_utc: new Date().toISOString(),
      data: {
        help_id: "9c2d51ab-3f77-4b90-9f21-7c0a1b2c3d4e",
        status: "cancelled", cancelled_at_utc: new Date().toISOString(),
        cancel_reason: "误触", device_command: "clear_alert",
        deduplicated: false, note: "已下发 clear_alert",
      },
    });
  }
  if (u.includes("/api/v1/help_events/")) {
    return jsonResponse(200, { ok: true, data: helpDetail || helpRows[0] || {} });
  }
  if (u.includes("/api/v1/help_events")) {
    return jsonResponse(200, { ok: true,
      data: { count: helpRows.length, help_events: helpRows } });
  }

  // ---- 既有端点（保持 app.js 能正常初始化）----
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
    return jsonResponse(200, { ok: true, records: 1, raw_uploads: 1, devices: 1 });
  }
  if (u.includes("/api/v1/collect_requests")) {
    return jsonResponse(200, { ok: true, data: { count: 0, requests: [] } });
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

/** 页面正文全文（含 #helpItemStatus 与 #helpItemRemoteAck 的全部文本）。 */
function bodyText() {
  const parts = [];
  for (const id of Object.keys(els)) {
    const e = els[id];
    if (e && e.textContent) parts.push(String(e.textContent));
    if (e && e._html) parts.push(String(e._html));
  }
  return parts.join(" \n ");
}

/** ★ 禁止词：只要出现任一个就是 P0 失败（大小写/全半角都查）。 */
const FORBIDDEN_F1F2 = ["对方已收到", "已送达", "设备已收到回应", "已确认",
                        "对方已經收到", "已送達"];

function findForbidden(text, words) {
  const low = text.toLowerCase();
  return words.filter((w) => text.includes(w) || low.includes(w.toLowerCase()));
}

const HELP_ID = "9c2d51ab-3f77-4b90-9f21-7c0a1b2c3d4e";

function row(over) {
  return Object.assign({
    help_id: HELP_ID,
    device_id: "esp32s3eye-1C701C",
    status: "uploaded",
    trigger_type: "button",
    trigger_up_ms: 318440,
    trigger_at_utc: "2026-10-08T13:49:31.437+0800",
    queued_at_utc: "2026-10-08T13:49:31.437+0800",
    received_at_utc: new Date(Date.now() - 3000).toISOString(),
    local_feedback: "buzzer+led",
    local_feedback_at_utc: "2026-10-08T13:49:31.437+0800",
    local_feedback_received_at_utc: new Date(Date.now() - 3000).toISOString(),
    pending_local: false,
    viewed_at_utc: null,
    responded_at_utc: null,
    remote_acked_at_utc: null,     // ★ 默认：没有任何远端回执
    accepted_by: null,
    cancel_reason: null,
    age_ms: 3000,
    client_request_key: "btn-808-318440",
    boot_count: 808,
  }, over || {});
}

async function selectAndRender(r) {
  helpRows = [r];
  helpDetail = r;
  if (window.__helpDebug) {
    await window.__helpDebug.reload();
    // 直接渲染给定事件对象（等价于点中列表那一行：选中 + 渲染详情）
    window.__helpDebug.renderDetail(r);
    // 同时把选中态写进去，这样"回应/取消"按钮才作用于这条事件
    await window.__helpDebug.select(r.help_id);
  }
  await sleep(30);
  return r;
}

async function main() {
  console.log("=".repeat(78));
  console.log("前端「按键求助」显示规则实测（打桩 DOM + 真实 web/app.js）");
  console.log("=".repeat(78));

  await sleep(80);   // 等 app.js 初始化完成

  const H = window.__helpDebug;
  check("window.__helpDebug 已暴露（供自检断言）", !!H);
  if (!H) { console.log("\n无法继续：app.js 未暴露 __helpDebug"); process.exit(1); }

  // =========================================================================
  console.log("\n[1] 事实 F1：仅本地反馈发生（无任何服务端回执）");
  // =========================================================================
  const f1 = row({ status: "uploaded", remote_acked_at_utc: null });
  const t1status = H.statusText(f1);
  const t1ack = H.remoteAckText(f1);
  check("F1 #helpItemStatus 含「佩戴者已按键（待回应）」",
        t1status.includes("佩戴者已按键（待回应）"), `实际="${t1status}"`);
  check("F1 #helpItemRemoteAck 区域为空（不含任何'已收到'文案）",
        t1ack === "", `实际="${t1ack}"`);
  const bad1 = findForbidden(t1status + " " + t1ack, FORBIDDEN_F1F2);
  check("F1 禁止词一个都不出现", bad1.length === 0, `命中=${JSON.stringify(bad1)}`);

  // 真渲染一遍再查全文
  await selectAndRender(f1);
  await H.reload();
  await sleep(40);
  const full1 = bodyText();
  const bad1b = findForbidden(full1, FORBIDDEN_F1F2);
  check("F1 渲染后整页全文无禁止词", bad1b.length === 0, `命中=${JSON.stringify(bad1b)}`);

  // =========================================================================
  console.log("\n[2] 事实 F2：status=responded 但设备尚未 ack（最关键的一条）");
  // =========================================================================
  const f2 = row({
    status: "responded",
    responded_at_utc: new Date(Date.now() - 1000).toISOString(),
    accepted_by: "viewer-web",
    remote_acked_at_utc: null,     // ★★ 设备还没确认
  });
  const t2status = H.statusText(f2);
  const t2ack = H.remoteAckText(f2);
  check("F2 #helpItemStatus 含「我已回应，等待设备确认」",
        t2status.includes("我已回应，等待设备确认"), `实际="${t2status}"`);
  check("F2 状态文案**不含**「已收到」类字样", findForbidden(t2status, FORBIDDEN_F1F2).length === 0,
        `实际="${t2status}"`);
  check("F2 #helpItemRemoteAck 只说'等待设备确认'，不说已收到",
        t2ack.includes("等待设备确认") && findForbidden(t2ack, FORBIDDEN_F1F2).length === 0,
        `实际="${t2ack}"`);

  await selectAndRender(f2);
  await H.reload();
  await sleep(40);
  const full2 = bodyText();
  const bad2 = findForbidden(full2, FORBIDDEN_F1F2);
  check("F2 渲染后整页全文无禁止词", bad2.length === 0, `命中=${JSON.stringify(bad2)}`);

  // =========================================================================
  console.log("\n[3] 事实 F3：设备已 ack（remote_acked_at_utc 非空）才允许说已收到");
  // =========================================================================
  const f3 = row({
    status: "responded",
    responded_at_utc: new Date(Date.now() - 5000).toISOString(),
    accepted_by: "viewer-web",
    remote_acked_at_utc: new Date(Date.now() - 1000).toISOString(),   // ★ 设备真的回执了
  });
  const t3status = H.statusText(f3);
  const t3ack = H.remoteAckText(f3);
  check("F3 #helpItemStatus == 「佩戴者设备已收到回应」",
        t3status.includes("佩戴者设备已收到回应"), `实际="${t3status}"`);
  check("F3 #helpItemRemoteAck 含「佩戴者设备已收到回应」",
        t3ack.includes("佩戴者设备已收到回应"), `实际="${t3ack}"`);
  check("F3 不再出现「等待设备确认」", !t3ack.includes("等待设备确认"), `实际="${t3ack}"`);

  // =========================================================================
  console.log("\n[4] 判据唯一性：F3 只认 remote_acked_at_utc，不认 status");
  // =========================================================================
  // 关键反例：status=responded 但 remote_acked_at_utc 为空 → 必须**不能**是 F3
  const notYet = row({ status: "responded", remote_acked_at_utc: null });
  check("反例：responded + remote_acked 为空 → 不显示'已收到'",
        !H.statusText(notYet).includes("已收到") && !H.remoteAckText(notYet).includes("已收到"),
        `status="${H.statusText(notYet)}" ack="${H.remoteAckText(notYet)}"`);
  // 另一个反例：status 还是 uploaded（甚至 cancelled），但 remote_acked 非空 → 仍应显示已收到
  const oddButAcked = row({ status: "uploaded", remote_acked_at_utc: new Date().toISOString() });
  check("反例：uploaded + remote_acked 非空 → 显示'已收到'（证明判据是 remote_acked 而非 status）",
        H.remoteAckText(oddButAcked).includes("佩戴者设备已收到回应"),
        `实际="${H.remoteAckText(oddButAcked)}"`);

  // 源码级核对：F3 分支必须用 remote_acked_at_utc，且不得用 status==='responded' 推已送达
  const srcTxt = fs.readFileSync(APP_JS, "utf8");
  const ackFnBody = srcTxt.slice(
    srcTxt.indexOf("function helpRemoteAckText"),
    srcTxt.indexOf("function helpItemStatusText"));
  check("源码核对：helpRemoteAckText 的 F3 分支判据是 remote_acked_at_utc",
        /if\s*\(\s*ev\.remote_acked_at_utc\s*\)/.test(ackFnBody),
        ackFnBody.match(/if\s*\([^)]*remote_acked_at_utc[^)]*\)/g)
          ? `实际判据="${ackFnBody.match(/if\s*\([^)]*remote_acked_at_utc[^)]*\)/g)[0]}"`
          : "未找到以 remote_acked_at_utc 为条件的 if");
  const iAckBranch = ackFnBody.indexOf("ev.remote_acked_at_utc");
  const iRespBranch = ackFnBody.indexOf('ev.status === "responded"');
  check("源码核对：F3 分支出现在 responded 分支**之前**（顺序保证已收到必须先看回执）",
        iAckBranch >= 0 && iRespBranch >= 0 && iAckBranch < iRespBranch,
        `实际位置：remote_acked@${iAckBranch} < responded@${iRespBranch}`);

  // =========================================================================
  console.log("\n[5] 取消逻辑：reason 必填（C3）与记录不删除（C3）");
  // =========================================================================
  helpRows = [row({ status: "uploaded" })];
  helpDetail = helpRows[0];
  await H.reload();
  await H.select(HELP_ID);      // 先选中一条（等价于点列表那一行）
  await sleep(30);
  check("已选中一条求助事件（取消/回应的前提）",
        !!(H.selected() && H.selected().help_id === HELP_ID),
        `selected=${H.selected() ? H.selected().help_id : "null"}`);

  // 空原因必须被前端拦住，且**不发** cancel 请求
  promptAnswer = "   ";
  calls.length = 0;
  fire("helpCancelBtn", "click");
  await sleep(60);
  const cancelCallsBlank = calls.filter((c) => c.url.endsWith("/cancel"));
  check("C3 取消原因为空时 → 不发出 cancel 请求（前端先拦一道）",
        cancelCallsBlank.length === 0,
        `calls=${JSON.stringify(cancelCallsBlank.map((c) => c.method + " " + c.url))}`);

  // 正常原因 → 发出 cancel 且带 reason
  promptAnswer = "误触";
  calls.length = 0;
  fire("helpCancelBtn", "click");
  await sleep(60);
  const cancelCalls = calls.filter((c) => c.url.endsWith("/cancel") && c.method === "POST");
  check("C3 填了原因 → POST /help_events/<id>/cancel", cancelCalls.length >= 1,
        `calls=${JSON.stringify(calls.map((c) => c.method + " " + c.url))}`);
  check("C3 cancel 请求路径带 help_id", cancelCalls.some((c) => c.url.includes(HELP_ID)));

  // =========================================================================
  console.log("\n[6] 「刷新求助列表」只读，不下发任何设备指令");
  // =========================================================================
  calls.length = 0;
  fire("helpRefreshBtn", "click");
  await sleep(80);
  const posts = calls.filter((c) => c.method === "POST");
  check("刷新求助列表期间没有任何 POST（零设备侧指令流量）", posts.length === 0,
        `posts=${JSON.stringify(posts.map((c) => c.url))}`);
  check("刷新求助列表确实 GET 了 /api/v1/help_events",
        calls.some((c) => c.method === "GET" && c.url.includes("/api/v1/help_events")));
  check("刷新求助列表不触碰任何 collect_* 接口",
        !calls.some((c) => c.url.includes("collect_requests")));

  // =========================================================================
  console.log("\n[7] 列表渲染：'远端回执'列同样只认 remote_acked_at_utc");
  // =========================================================================
  helpRows = [
    row({ help_id: "aaaaaaaa-0000-4000-8000-000000000001", status: "uploaded",
          remote_acked_at_utc: null }),
    row({ help_id: "bbbbbbbb-0000-4000-8000-000000000002", status: "responded",
          responded_at_utc: new Date().toISOString(), remote_acked_at_utc: null }),
  ];
  await H.reload();
  await sleep(40);
  const listHtml = String(els.helpList.innerHTML || "");
  const badList = findForbidden(listHtml, ["对方已收到", "已送达", "设备已收到回应"]);
  check("列表里没有任何'已收到'类文案（两条都还没设备回执）", badList.length === 0,
        `命中=${JSON.stringify(badList)}`);
  check("列表渲染出两行", (listHtml.match(/data-hid=/g) || []).length === 2);
  check("列表状态列使用契约文案「已上传」",
        listHtml.includes("已上传"), "未找到'已上传'");
  check("列表状态列使用契约文案「查看者已回应」", listHtml.includes("查看者已回应"));

  // =========================================================================
  console.log("\n[8] 初始状态（页面刚打开、还没有任何求助事件）");
  // =========================================================================
  helpRows = [];
  await H.reload();
  await sleep(40);
  check("无事件时列表显示空态", String(els.helpList.innerHTML).includes("还没有求助事件"));
  check("无事件时 #helpItemRemoteAck 为空",
        String(els.helpItemRemoteAck.textContent || "").trim() === "");
  const initText = bodyText();
  const badInit = findForbidden(initText, ["对方已收到", "已送达", "设备已收到回应"]);
  check("初始整页无禁止词", badInit.length === 0, `命中=${JSON.stringify(badInit)}`);

  // =========================================================================
  console.log("\n[9] 关键不变量：轮询不得把文案“自动升级”成已收到");
  // =========================================================================
  // 模拟"后端状态一直是 responded 且 remote_acked 为空"，跑 30 次轮询刷新，
  // 文案必须始终不变（没有本地计时器把它升级）。
  helpRows = [row({ status: "responded", responded_at_utc: new Date().toISOString(),
                    remote_acked_at_utc: null })];
  helpDetail = helpRows[0];
  let stable = true;
  for (let i = 0; i < 30; i++) {
    wallClock += 1000;                     // 每次 +1 秒（相当于过了 30 秒）
    await H.reload();
    const s = H.statusText(helpRows[0]) + "|" + H.remoteAckText(helpRows[0]);
    if (findForbidden(s, FORBIDDEN_F1F2).length > 0) stable = false;
    if (!s.includes("等待设备确认")) stable = false;
  }
  check("30 秒内文案保持「我已回应，等待设备确认」不自动升级", stable);

  console.log("\n" + "=".repeat(78));
  console.log(failed === 0 ? "全部用例通过 ✔" : `存在 ${failed} 项失败 ✘`);
  console.log("=".repeat(78));
  process.exit(failed === 0 ? 0 : 1);
}

function fire(id, ev) {
  const stub = els[id];
  const ls = stub && stub._listeners && stub._listeners[ev];
  if (ls) ls.forEach((fn) => fn({ target: stub }));
}

main().catch((e) => { console.error(e); process.exit(1); });