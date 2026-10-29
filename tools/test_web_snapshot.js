#!/usr/bin/env node
/**
 * test_web_snapshot.js —— 用打桩 DOM 实测前端「按需环境快照」（不需要浏览器）
 *
 * 直接加载真实的 web/app.js，用假 DOM + 假 fetch 驱动，断言页面**实际写进**
 * #snapState / #snapImg(src 属性) / #snapPhTitle / #snapExplain / #snapHistory 的内容。
 *
 * 本周前端的核心不是"能不能显示一张图"，而是**什么时候不许显示图**：
 *   W1 点「查看环境」→ POST /api/v1/cam/snapshot，参数含 device_id/mode/trigger
 *   W2 ★ 成功一次之后，再请求一次但失败/超时 → 结果区必须**摘掉旧图**，
 *        并把占位文案换成"本次没有画面"。这是本周最容易做错、也最像"成功"的错。
 *   W3 画面不可用（occluded）时**要**显示图（它是遮挡的证据），但结论必须说"不可用"
 *   W4 回放必须标成回放，且文案里不许出现"采集成功"
 *   W5 历史列表把 真机 / 模拟 / 回放 分开标注；没有图像的记录写"无图像"
 *   W6 409（在途/设备没在上传）时直接渲染后端给的那条记录，不空转等待
 *   W7 源码级红线核对（摘图函数确实被调用、只有三种状态才设 src）
 *
 * 运行: node tools/test_web_snapshot.js
 */

"use strict";

const fs = require("fs");
const path = require("path");

const APP_JS = path.join(__dirname, "..", "web", "app.js");
const INDEX_HTML = path.join(__dirname, "..", "web", "index.html");
const APP_SRC = fs.readFileSync(APP_JS, "utf8");

// ---------------------------------------------------------------------------
// 打桩：DOM
// ---------------------------------------------------------------------------
const els = {};
function makeElement(id) {
  const classes = new Set();
  const attrs = {};
  const el = {
    id, className: "", width: 0, height: 0, disabled: false, src: "",
    _listeners: {},
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c, on) => (on === undefined ? (classes.has(c) ? classes.delete(c) : classes.add(c))
                                           : (on ? classes.add(c) : classes.delete(c))),
    },
    addEventListener: (ev, fn) => { (el._listeners[ev] = el._listeners[ev] || []).push(fn); },
    setAttribute: (k, v) => { attrs[k] = String(v); },
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    removeAttribute: (k) => { delete attrs[k]; },
    getContext: () => ctx,
    _classes: classes,
    querySelector: () => makeElement(id + "-child"),
    querySelectorAll: () => [],
  };
  Object.defineProperty(el, "textContent", {
    get() { return this._text; }, set(v) { this._text = v; },
  });
  Object.defineProperty(el, "innerHTML", {
    get() { return this._html; }, set(v) { this._html = v; },
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

// index.html 里已有的初始文案（与浏览器首屏一致）
for (const m of fs.readFileSync(INDEX_HTML, "utf8")
         .matchAll(/id="([A-Za-z0-9_]+)"[^>]*>([^<]*)</g)) {
  const stub = els[m[1]] || (els[m[1]] = makeElement(m[1]));
  if (stub) stub.textContent = m[2].trim();
}
els.cameraPanel.classList.add("hidden");
els.camImg.classList.add("hidden");
els.snapImg.classList.add("hidden");
els.snapCancel.classList.add("hidden");

// ---------------------------------------------------------------------------
// 假后端
// ---------------------------------------------------------------------------
const calls = [];
const DEV = "esp32s3eye-1C704C";
const T0 = "2026-10-29T06:00:00.000Z";

let statusBody = null;
let createResp = null;          // { status, body }
let snapStates = [];            // GET /snapshot/<id> 依次返回
let listBody = { ok: true, count: 0, snapshots: [] };
let replayResp = null;
let oneHit = 0;

function jsonResponse(status, body) {
  return { status, text: async () => JSON.stringify(body) };
}

function rec(over) {
  const base = {
    snapshot_id: "snp_20261029T060000-aaaaaa",
    device_id: DEV, state: "ok",
    trigger_kind: "user_request", source: "live",
    is_replay: false, simulated: false, counts_as_live: true,
    mode: "qvga",
    requested_at_utc: T0,
    delivered_at_utc: "2026-10-29T06:00:00.400Z",
    begin_at_utc: "2026-10-29T06:00:00.500Z",
    captured_at_utc: "2026-10-29T06:00:01.000Z",
    captured_is_est: true,
    received_at_utc: "2026-10-29T06:00:01.060Z",
    width: 320, height: 240, byte_len: 12000,
    sha256: "b".repeat(64),
    quality_verdict: "usable",
    quality_note: "画面可用：画面有正常明暗分布（平均亮度 120.5，标准差 39.1）",
    latency_capture_ms: 1000, latency_total_ms: 1060,
    error_code: null, error_message: null,
    late_at_utc: null, late_filename: null,
    replay_of_photo_id: null,
    has_image: true,
    image_url: "/api/v1/cam/snapshot/snp_20261029T060000-aaaaaa/image",
    quality: { mean_luma: 120.5, luma_std: 39.1 },
    explain: "采集成功：画面在 2026-10-29 06:00:01.000Z（估计值） 拍下，"
           + "2026-10-29 06:00:01.060Z 到达后端，端到端 1060 ms。",
  };
  return Object.assign(base, over || {});
}

function okRec(id) {
  return rec({
    snapshot_id: id,
    image_url: `/api/v1/cam/snapshot/${id}/image`,
  });
}

global.fetch = async (url, opts) => {
  const u = String(url);
  const method = (opts && opts.method) || "GET";
  calls.push({ url: u, method, body: opts && opts.body });

  // ---- 第 7 周：快照（注意顺序：snapshots 列表必须排在 snapshot/<id> 之前）----
  if (u.includes("/api/v1/cam/snapshots")) return jsonResponse(200, listBody);
  if (u.includes("/api/v1/cam/snapshot/summary")) {
    return jsonResponse(200, { ok: true, total: 0, live_ok: 0, live_occluded: 0,
                               simulated: 0, replayed: 0, by_state: {}, in_flight: [],
                               last_live_ok: null, settings: {} });
  }
  if (u.includes("/api/v1/cam/snapshot/replay")) return jsonResponse(200, replayResp);
  if (u.includes("/api/v1/cam/snapshot/prune")) {
    return jsonResponse(200, { ok: true, scanned: 5, pruned: 2, bytes_freed: 4096, items: [] });
  }
  if (/\/api\/v1\/cam\/snapshot\/[^/]+\/cancel$/.test(u)) {
    return jsonResponse(200, { ok: true, snapshot: rec({ state: "cancelled", has_image: false,
      image_url: null, explain: "这次请求已取消，本次没有画面。" }) });
  }
  if (/\/api\/v1\/cam\/snapshot\/snp_[^/?]+$/.test(u) && method === "GET") {
    const next = snapStates.length > 1 ? snapStates.shift() : snapStates[0];
    oneHit++;
    return jsonResponse(200, { ok: true, snapshot: next });
  }
  if (u.includes("/api/v1/cam/snapshot") && method === "POST") {
    return jsonResponse(createResp.status, createResp.body);
  }

  // ---- 既有端点（让 app.js 正常初始化、camDeviceId 有设备可选）----
  if (u.includes("/api/v1/cam/status")) return jsonResponse(200, statusBody);
  if (u.includes("/api/v1/cam/photos")) {
    return jsonResponse(200, { ok: true, count: 0, photos: [] });
  }
  if (u.includes("/api/v1/cam/capture")) {
    return jsonResponse(200, { ok: true, device_id: DEV, capture_ttl_s: 10 });
  }
  if (u.includes("/api/v1/records")) return jsonResponse(200, { ok: true, count: 0, records: [] });
  if (u.includes("/api/v1/latest")) return jsonResponse(404, { ok: false, error: "没有记录" });
  if (u.includes("/api/v1/devices")) {
    return jsonResponse(200, { ok: true, devices: [{ device_id: DEV, n: 10,
      last_seen: T0, model: "QMA6100P", last_seq: 9, mac: "94:A9:90:1C:70:4C",
      last_status: "online", last_seen_utc: T0, periodic_enabled: 1, pending_commands: 0 }] });
  }
  if (u.includes("/api/v1/stats")) return jsonResponse(200, { ok: true, records: 0, raw_uploads: 0, devices: 1 });
  if (u.includes("/api/v1/collect_requests")) return jsonResponse(200, { ok: true, data: { count: 0, requests: [] } });
  if (u.includes("/api/v1/help_events")) return jsonResponse(200, { ok: true, data: { count: 0, help_events: [] } });
  if (u.includes("/api/v1/nl/")) return jsonResponse(200, { ok: true, tools: [] });
  return jsonResponse(404, { ok: false, error: "not found" });
};

// ---------------------------------------------------------------------------
// 执行 app.js
// ---------------------------------------------------------------------------
new Function(APP_SRC)();

const fails = [];
const checks = [0];
function check(name, cond, detail) {
  checks[0]++;
  console.log((cond ? "  ✔ " : "  ✘ ") + name + (detail ? "  " + detail : ""));
  if (!cond) fails.push(name);
}
const sleep = (ms) => new Promise((r) => setImmediate(r));
async function flush() { for (let i = 0; i < 40; i++) await sleep(0); }

async function click(id, ev) {
  const el = els[id];
  const ls = el && el._listeners && el._listeners.click;
  if (ls) ls.forEach((fn) => fn(ev || { target: el }));
  await flush();
}

const snapSrc = () => els.snapImg.getAttribute("src");
const snapChip = () => String(els.snapState.textContent);

function statusWithFrame() {
  return {
    ok: true, has_frame: true, device_id: DEV,
    frame: { device_id: DEV, seq: 321, w: 320, h: 240, bytes: 11264, frame_no: 7,
             received_at_utc: T0 },
    camera: { device_id: DEV, ready: true, pid: 38, error: "" },
    devices_with_frames: [DEV], photo_count: 0, capture_pending: false,
  };
}

(async function main() {
  console.log("=".repeat(78));
  console.log("前端「按需环境快照」自检（打桩 DOM，不需要浏览器）");
  console.log("=".repeat(78));

  // 先让相机面板打开一次：这样 camDeviceId() 才有设备可选
  statusBody = statusWithFrame();
  await click("btnCamera");
  for (let i = 0; i < 20; i++) await flush();
  check("W0 前置：相机面板打开后拿得到设备", calls.some((c) => c.url.includes("/api/v1/cam/status")));

  // =========================================================================
  console.log("\n[W1] 点「查看环境」→ POST /api/v1/cam/snapshot");
  // =========================================================================
  snaps: {
    const sid = "snp_20261029T060000-aaaaaa";
    createResp = { status: 202, body: { ok: true, snapshot: rec({ state: "pending", has_image: false,
      image_url: null, captured_at_utc: null, received_at_utc: null, latency_total_ms: null,
      quality_verdict: null, quality_note: null,
      explain: "已登记这次请求，正在等设备上传下一帧——请求会随那一帧的响应下发（设备在线时通常 <500ms）。此刻还没有任何画面属于这次请求。" }) } };
    snapStates = [rec({ snapshot_id: sid, state: "requested", has_image: false, image_url: null,
                        captured_at_utc: null, received_at_utc: null, latency_total_ms: null,
                        explain: "请求已在 2026-10-29 06:00:00.400Z 下发给设备，等它取图并上传。" }),
                  okRec(sid)];
    const before = calls.length;
    await click("snapAsk");
    for (let i = 0; i < 40; i++) await flush();

    const posted = calls.slice(before).filter((c) => c.method === "POST" && /\/api\/v1\/cam\/snapshot$/.test(c.url));
    check("W1.1 确实 POST 了 /api/v1/cam/snapshot", posted.length === 1, "次数=" + posted.length);
    let payload = {};
    try { payload = JSON.parse(posted[0].body); } catch (e) { payload = {}; }
    check("W1.2 请求体带 device_id", payload.device_id === DEV, JSON.stringify(payload));
    check("W1.3 请求体带 mode=qvga", payload.mode === "qvga", payload.mode);
    check("W1.4 请求体带 trigger=user_request", payload.trigger === "user_request", payload.trigger);

    check("W1.5 状态徽标最终是「采集成功」", snapChip() === "采集成功", snapChip());
    check("W1.6 img 的 src 指向本次 snapshot 的 image 接口",
      String(snapSrc()).includes("/api/v1/cam/snapshot/" + sid + "/image"), snapSrc());
    check("W1.7 src 上带了 sid（cache-buster，防止拿旧缓存）",
      String(snapSrc()).includes("?sid=" + sid), snapSrc());
    check("W1.8 图像不再隐藏", !els.snapImg.classList.contains("hidden"));
    check("W1.9 占位层被隐藏", els.snapPlaceholder.classList.contains("hidden"));
    check("W1.10 结论文案原样来自后端（不自己编）",
      String(els.snapExplain.textContent).includes("采集成功"), els.snapExplain.textContent);
    check("W1.11 快门时刻显示成时间 + 标明是估计值",
      String(els.snapT1.textContent).includes("估计"), els.snapT1.textContent);
    check("W1.12 端到端时延来自接口", String(els.snapLat.textContent) === "1060 ms", els.snapLat.textContent);
    check("W1.13 画面质量行带判定与依据",
      String(els.snapQuality.textContent).includes("可用"), els.snapQuality.textContent);

    // =======================================================================
    console.log("\n[W2] ★ 上一次成功之后，这次超时 → 结果区必须摘掉旧图");
    // =======================================================================
    const sid2 = "snp_20261029T060100-bbbbbb";
    createResp = { status: 202, body: { ok: true, snapshot: rec({ snapshot_id: sid2, state: "pending",
      has_image: false, image_url: null, captured_at_utc: null, received_at_utc: null,
      latency_total_ms: null, quality_verdict: null, quality_note: null,
      explain: "已登记这次请求，正在等设备上传下一帧。" }) } };
    snapStates = [rec({ snapshot_id: sid2, state: "requested", has_image: false, image_url: null,
                        captured_at_utc: null, received_at_utc: null, latency_total_ms: null,
                        explain: "请求已下发，等设备取图。" }),
                  rec({ snapshot_id: sid2, state: "timeout", has_image: false, image_url: null,
                        captured_at_utc: null, received_at_utc: null, latency_total_ms: null,
                        quality_verdict: null, quality_note: null,
                        counts_as_live: false, error_code: "DEVICE_SLOW",
                        error_message: "请求已下发给设备，但 25 秒内没有送回图像",
                        explain: "采集超时：25 秒内设备没有送回图像，本次没有画面。系统不会把上一次成功的画面当作这次的结果。" })];
    await click("snapAsk");
    for (let i = 0; i < 40; i++) await flush();

    check("W2.1 徽标是「采集超时」", snapChip() === "采集超时", snapChip());
    check("W2.2 ★ img 的 src 属性被摘掉（不许留着上一次的图）",
      snapSrc() === null, "实际: " + snapSrc());
    check("W2.3 img 被隐藏", els.snapImg.classList.contains("hidden"));
    check("W2.4 页面内部也认为「当前没有图」",
      global.window.__snapDebug.state().imgSid === null,
      String(global.window.__snapDebug.state().imgSid));
    check("W2.5 占位标题是「本次没有画面」",
      String(els.snapPhTitle.textContent).includes("本次没有画面"), els.snapPhTitle.textContent);
    check("W2.6 占位副文案说明不会拿上一次的图冒充",
      String(els.snapPhSub.textContent).includes("不会"), els.snapPhSub.textContent);
    check("W2.7 结论文案点名「超时」", String(els.snapExplain.textContent).includes("超时"),
      els.snapExplain.textContent);

    // =======================================================================
    console.log("\n[W3] 画面不可用（occluded）→ 图要显示（它是证据），但结论说不算成功");
    // =======================================================================
    const sid3 = "snp_20261029T060200-cccccc";
    createResp = { status: 202, body: { ok: true, snapshot: rec({ snapshot_id: sid3, state: "pending",
      has_image: false, image_url: null, explain: "已登记。" }) } };
    snapStates = [rec({ snapshot_id: sid3, state: "occluded",
                        image_url: `/api/v1/cam/snapshot/${sid3}/image`,
                        quality_verdict: "occluded",
                        quality_note: "画面几乎没有任何明暗变化（块间标准差 0.0 < 3.0）",
                        quality: { mean_luma: 18.0, luma_std: 0.0 },
                        explain: "画面采到了，但质量判定为不可用：画面几乎没有任何明暗变化。"
                               + "这不算一次成功的环境查看 —— 请确认镜头没有被遮挡、对着的是要看的场景。" })];
    await click("snapAsk");
    for (let i = 0; i < 40; i++) await flush();
    check("W3.1 徽标是「画面不可用」", snapChip() === "画面不可用", snapChip());
    check("W3.2 ★ 遮挡的图仍然显示（证据不藏）",
      String(snapSrc()).includes(sid3), snapSrc());
    check("W3.3 质量行写明「不可用（疑似遮挡）」",
      String(els.snapQuality.textContent).includes("疑似遮挡"), els.snapQuality.textContent);
    check("W3.4 结论里明确「不算一次成功的环境查看」",
      String(els.snapExplain.textContent).includes("不算"), els.snapExplain.textContent);
    check("W3.5 徽标不是「采集成功」", snapChip() !== "采集成功");

    // =======================================================================
    console.log("\n[W4] 回放 → 必须标成回放，文案里不许出现「采集成功」");
    // =======================================================================
    snaps2: {
      const rid = "snp_20261029T060300-dddddd";
      replayResp = { ok: true, snapshot: rec({
        snapshot_id: rid, state: "replayed", is_replay: true, counts_as_live: false,
        trigger_kind: "replay", source: "replay",
        image_url: `/api/v1/cam/snapshot/${rid}/image`,
        replay_of_photo_id: "20261019T070403113435Z-6da002",
        captured_at_utc: "2026-10-19T07:04:03.113Z",
        quality_verdict: "usable",
        quality_note: "画面可用：画面有正常明暗分布",
        explain: "回放：这是 2026-10-19 07:04:03.113Z 采集的历史图像，不是本次采集。"
               + "它不计入「最近一次成功采集」。",
      }) };
      await click("snapReplay");
      for (let i = 0; i < 20; i++) await flush();
      check("W4.1 徽标是「回放」", snapChip() === "回放", snapChip());
      check("W4.2 结论里出现「不是本次采集」",
        String(els.snapExplain.textContent).includes("不是本次采集"), els.snapExplain.textContent);
      check("W4.3 ★ 结论里不出现「采集成功」",
        !String(els.snapExplain.textContent).includes("采集成功"), els.snapExplain.textContent);
      // 时刻按**本机时区**渲染（页面一直如此），所以期望值也按本机时区算
      const d0 = new Date("2026-10-19T07:04:03.113Z");
      const p2 = (n) => String(n).padStart(2, "0");
      const expT = `${p2(d0.getHours())}:${p2(d0.getMinutes())}:${p2(d0.getSeconds())}`
                 + `.${String(d0.getMilliseconds()).padStart(3, "0")}`;
      const expMD = `${p2(d0.getMonth() + 1)}-${p2(d0.getDate())}`;
      check("W4.4 快门时刻显示的是原始采集时刻（不是现在）",
        String(els.snapT1.textContent).startsWith(expT)
        || String(els.snapT1.textContent).startsWith(expMD + " " + expT),
        "期望包含 " + expT + "，实际 " + els.snapT1.textContent);
      // ★ 2026-10-19 不是"今天"，所以必须带上日期：否则 10 天前的原图时刻
      //   看起来像"今天 15:04"，而到达时刻是 14:56 —— 读起来像快门在未来。
      check("W4.4b 不是今天的时刻必须带日期（否则会被读成今天）",
        String(els.snapT1.textContent).startsWith(expMD + " "),
        "期望以 " + expMD + " 开头，实际 " + els.snapT1.textContent);
    }

    // =======================================================================
    console.log("\n[W5] 历史列表：真机 / 模拟 / 回放 分开标注");
    // =======================================================================
    listBody = { ok: true, count: 3, snapshots: [
      rec({ snapshot_id: "snp_live", state: "ok",
            image_url: "/api/v1/cam/snapshot/snp_live/image",
            explain: "采集成功：画面在 … 拍下" }),
      rec({ snapshot_id: "snp_sim", state: "ok", simulated: true, counts_as_live: false,
            image_url: "/api/v1/cam/snapshot/snp_sim/image",
            explain: "采集成功：这台设备的图像由协议模拟器送出（演练）" }),
      rec({ snapshot_id: "snp_rep", state: "replayed", is_replay: true, counts_as_live: false,
            image_url: "/api/v1/cam/snapshot/snp_rep/image",
            explain: "回放：这是历史图像，不是本次采集。" }),
      rec({ snapshot_id: "snp_to", state: "timeout", has_image: false, image_url: null,
            counts_as_live: false, explain: "采集超时：本次没有画面。" }),
    ] };
    await click("snapPrune");            // 清理按钮会顺带刷新历史
    for (let i = 0; i < 20; i++) await flush();
    const hist = String(els.snapHistory.innerHTML);
    // ★ 只数"行"本身：class="snap-row …" 里的子元素（snap-row-t / -kind / -note）
    //   也含这个前缀，用宽松匹配会把 4 行数成 16 个。
    check("W5.1 列表渲染了 4 行", (hist.match(/class="snap-row /g) || []).length === 4,
      (hist.match(/class="snap-row /g) || []).length);
    check("W5.2 标注了「真机」", hist.includes("真机"));
    check("W5.3 标注了「模拟」", hist.includes("模拟"));
    check("W5.4 标注了「回放」", hist.includes("回放"));
    check("W5.5 超时那条写「无图像」", hist.includes("无图像"));
    check("W5.6 有图的行给出「打开原图」链接", hist.includes("打开原图"));
    check("W5.7 计数显示为「4 条」", String(els.snapHistCount.textContent) === "4 条",
      els.snapHistCount.textContent);

    // =======================================================================
    console.log("\n[W6] 409（在途 / 设备没在上传）→ 直接渲染后端记录，不空转");
    // =======================================================================
    const sid6 = "snp_20261029T060400-eeeeee";
    createResp = { status: 409, body: { ok: false,
      error: { code: "SNAPSHOT_BUSY", message: "该设备还有一次采集在路上", retryable: true },
      snapshot: rec({ snapshot_id: sid6, state: "requested", has_image: false, image_url: null,
        captured_at_utc: null, latency_total_ms: null,
        explain: "请求已在 2026-10-29 06:00:00.400Z 下发给设备，等它取图并上传。" }) } };
    snapStates = [rec({ snapshot_id: sid6, state: "requested", has_image: false, image_url: null }),
                  okRec(sid6)];
    oneHit = 0;
    const before6 = calls.length;
    await click("snapAsk");
    for (let i = 0; i < 30; i++) await flush();
    check("W6.1 状态徽标显示后端的 state（已下发）", snapChip().includes("已下发"), snapChip());
    check("W6.2 ★ 没有进入轮询等待（只发了 1 次创建请求、0 次单条查询）",
      calls.slice(before6).filter((c) => c.url.includes("/api/v1/cam/snapshot") && c.method === "GET").length === 0,
      calls.slice(before6).map((c) => c.method + " " + c.url).join(" | "));
    check("W6.3 结果区没有图", snapSrc() === null, snapSrc());
    check("W6.4 取消按钮在这个状态下可见", !els.snapCancel.classList.contains("hidden"));

    // =======================================================================
    console.log("\n[W7] 取消 → 之后不再跟踪，按钮收起");
    // =======================================================================
    await click("snapCancel");
    for (let i = 0; i < 20; i++) await flush();
    check("W7.1 徽标变为「已取消」", snapChip() === "已取消", snapChip());
    check("W7.2 结果区仍无图", snapSrc() === null, snapSrc());
    check("W7.3 取消按钮被收起", els.snapCancel.classList.contains("hidden"));

    // =======================================================================
    console.log("\n[W8] 源码级红线核对（防止以后被顺手改掉）");
    // =======================================================================
    check("W8.1 ★ 发请求前先摘图（snapAsk 里调用 snapClearImage）",
      /snapClearImage\("正在采集…"/.test(APP_SRC));
    check("W8.2 ★ 只有 ok/occluded/replayed 才设置图像地址",
      /rec\.has_image && \(st === "ok" \|\| st === "occluded" \|\| st === "replayed"\)/.test(APP_SRC));
    check("W8.3 摘图用 removeAttribute（不是留着旧地址）",
      /img\.removeAttribute\("src"\)/.test(APP_SRC));
    check("W8.4 图像地址带 sid 做 cache-buster", /image_url\}\?sid=/.test(APP_SRC));
    check("W8.5 结论与质量文案来自后端字段（explain / quality_note）",
      /rec\.explain/.test(APP_SRC) && /rec\.quality_note/.test(APP_SRC));
    check("W8.6 三种「来源」在历史里分开标注",
      /is_replay \? "回放"/.test(APP_SRC) && /simulated \? "模拟"/.test(APP_SRC));
    check("W8.7 不把失败态渲染成成功文案",
      !/state === "timeout"[\s\S]{0,80}采集成功/.test(APP_SRC));
    check("W8.8 调试入口暴露了 ask/cancel/replay/show",
      !!(global.window.__snapDebug && global.window.__snapDebug.ask
         && global.window.__snapDebug.cancel && global.window.__snapDebug.replay
         && global.window.__snapDebug.show));

    console.log("\n" + "=".repeat(78));
    if (fails.length) {
      console.log("失败用例: " + fails.join(" | "));
      process.exit(1);
    }
    console.log("全部用例通过 ✔（" + checks[0] + " 项）—— 按需快照绝不把旧图当新结果");
    console.log("=".repeat(78));
    process.exit(0);
  }
})();
