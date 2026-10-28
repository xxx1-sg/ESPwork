#!/usr/bin/env node
/**
 * test_web_cam.js —— 用打桩 DOM 实测前端「相机预览 / 拍照」（不需要浏览器）
 *
 * 直接加载真实的 web/app.js，用假 DOM + 假 fetch 驱动，断言页面**实际写进**
 * #camPhTitle / #camPhSub / #camImg.src / #camGallery / #camShotHint 的内容。
 *
 * 验证的核心规则（与工程"不造假"原则一致）：
 *   C1 设备在线但摄像头探测失败 → 页面必须明说"摄像头未就绪 + 失败原因"，
 *      不能只说"还没有画面"（那会让用户以为网页坏了）。
 *   C2 没有帧时不得给 <img> 设置任何图片地址（不能拿旧帧/占位图冒充）。
 *   C3 有帧时 img.src 必须指向 /api/v1/cam/frame，且分辨率/序号来自接口。
 *   C4 点「拍照」必须 POST /api/v1/cam/capture，且只有在照片真的回来后才提示成功。
 *
 * 运行: node tools/test_web_cam.js
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
    // 真实 DOM 元素都有这三个（app.js 会用 setAttribute 写 data-phase / title 等）
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

// setTimeout 打桩：立即执行并推进假时钟（让"等照片"的轮询能跑完）
global.setTimeout = (fn, ms) => setImmediate(() => { wallClock += (ms || 0); fn(); });

const intervals = [];
global.setInterval = (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; };
global.clearInterval = (h) => { if (h) intervals[h - 1] = null; };

["index.html", INDEX_HTML].length; // noop，保持变量被使用
for (const m of fs.readFileSync(INDEX_HTML, "utf8")
         .matchAll(/id="([A-Za-z0-9_]+)"[^>]*>([^<]*)</g)) {
  const stub = els[m[1]] || (els[m[1]] = makeElement(m[1]));
  if (stub) stub.textContent = m[2].trim();
}

// 初始可见性必须与 index.html 一致，否则 togglePanel 的"开/关"判断会反
// （index.html 里 cameraPanel 带 hidden，camImg 也带 hidden）
els.cameraPanel.classList.add("hidden");
els.camImg.classList.add("hidden");

// ---------------------------------------------------------------------------
// 假后端
// ---------------------------------------------------------------------------
const calls = [];
let statusBody = null;
let photoList = [];
let captureOk = true;
let photoArrives = true;      // false = 模拟"点了拍照但设备不响应"

function jsonResponse(status, body) {
  return { status, text: async () => JSON.stringify(body) };
}

const RECORD = {
  id: 42, device_id: "esp32s3eye-1C701C", boot_count: 808, seq: 3203,
  sample_idx: 1, sample_count: 1, sample_period_ms: 100,
  ts_local: "2026-10-08T13:49:31.437+0800",
  received_at_utc: new Date(1_700_000_000_000).toISOString(),
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

  if (u.includes("/api/v1/cam/status")) {
    return jsonResponse(200, statusBody);
  }
  if (u.includes("/api/v1/cam/capture")) {
    if (!captureOk) {
      return jsonResponse(409, { ok: false, error: { code: "NO_FRAME", message: "该设备当前没有画面" } });
    }
    // 第一次查询时"照片还没到"，第二次才出现 —— 模拟真实异步
    if (photoArrives) {
      photoList = [{
        photo_id: "20261019T063000000Z-abc123",
        device_id: "esp32s3eye-1C701C",
        created_at_utc: new Date(wallClock).toISOString(),
        width: 1600, height: 1200, byte_len: 153600,
        sha256: "a".repeat(64), seq: 99,
        url: "/api/v1/cam/photo/20261019T063000000Z-abc123.jpg",
      }];
    }
    return jsonResponse(200, { ok: true, device_id: "esp32s3eye-1C701C", capture_ttl_s: 10 });
  }
  if (u.includes("/api/v1/cam/photos")) {
    return jsonResponse(200, { ok: true, count: photoList.length, photos: photoList });
  }

  // 既有端点（让 app.js 正常初始化）
  if (u.includes("/api/v1/records")) return jsonResponse(200, { ok: true, count: 1, records: [RECORD] });
  if (u.includes("/api/v1/latest")) return jsonResponse(200, { ok: true, record: RECORD });
  if (u.includes("/api/v1/devices")) {
    return jsonResponse(200, { ok: true, devices: [{
      device_id: "esp32s3eye-1C701C", n: 100, last_seen: RECORD.received_at_utc,
      model: "QMA6100P", last_seq: 3203, mac: "94:A9:90:1C:70:1C",
      last_status: "online", last_seen_utc: RECORD.received_at_utc,
      periodic_enabled: 1, pending_commands: 0,
    }] });
  }
  if (u.includes("/api/v1/stats")) return jsonResponse(200, { ok: true, records: 1, raw_uploads: 1, devices: 1 });
  if (u.includes("/api/v1/collect_requests")) return jsonResponse(200, { ok: true, data: { count: 0, requests: [] } });
  if (u.includes("/api/v1/help_events")) return jsonResponse(200, { ok: true, data: { count: 0, help_events: [] } });
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
const sleep = (ms) => new Promise((r) => setImmediate(r));
async function flush() { for (let i = 0; i < 30; i++) await sleep(0); }

async function click(id, ev) {
  const el = els[id];
  const ls = el && el._listeners && el._listeners.click;
  if (ls) ls.forEach((fn) => fn(ev || { target: el }));
  await flush();
}

function statusWithFrame() {
  return {
    ok: true, has_frame: true,
    device_id: "esp32s3eye-1C701C",
    frame: {
      device_id: "esp32s3eye-1C701C", seq: 321, w: 320, h: 240,
      bytes: 11264, frame_no: 7,
      received_at_utc: new Date(wallClock).toISOString(),
    },
    camera: { ready: true, pid: 38, error: "" },
    devices_with_frames: ["esp32s3eye-1C701C"],
    photo_count: 0, capture_pending: false,
  };
}

(async function main() {
  console.log("=".repeat(78));
  console.log("前端「相机预览 / 拍照」自检（打桩 DOM，不需要浏览器）");
  console.log("=".repeat(78));

  // ---- 场景 1：设备在线但摄像头探测失败 ----
  console.log("\n[1] 设备在线、摄像头未就绪 → 必须明说原因");
  statusBody = {
    ok: true, has_frame: false, device_id: "esp32s3eye-1C701C", frame: null,
    camera: { device_id: "esp32s3eye-1C701C", ready: false, pid: 0, error: "ESP_ERR_NOT_SUPPORTED" },
    devices_with_frames: [], photo_count: 0, capture_pending: false,
  };
  els.camImg.src = "";
  await click("btnCamera");
  await flush();
  check("打开面板即请求 /cam/status", calls.some((c) => c.url.includes("/api/v1/cam/status")));
  check("标题写明「摄像头未就绪」", String(els.camPhTitle.textContent).includes("摄像头未就绪"),
        "实际: " + els.camPhTitle.textContent);
  check("副文案带失败原因", String(els.camPhSub.textContent).includes("ESP_ERR_NOT_SUPPORTED"),
        "实际: " + String(els.camPhSub.textContent).slice(0, 90));
  check("没有帧时不给 img 设地址", !els.camImg.src, "实际: " + els.camImg.src);
  check("没有帧时 img 保持隐藏", els.camImg.classList.contains("hidden"));

  // ---- 场景 2：有帧 ----
  console.log("\n[2] 有帧 → 必须从 /cam/frame 取图，元数据来自接口");
  statusBody = statusWithFrame();
  intervals.filter(Boolean).forEach((iv) => iv.fn && iv.fn());
  await flush();
  check("img.src 指向 /api/v1/cam/frame", String(els.camImg.src).includes("/api/v1/cam/frame"),
        "实际: " + els.camImg.src);
  check("分辨率显示 320×240", String(els.camSize.textContent) === "320×240",
        "实际: " + els.camSize.textContent);
  check("帧序号显示 321", String(els.camSeq.textContent) === "321",
        "实际: " + els.camSeq.textContent);
  check("帧大小显示 11.0 KB", String(els.camBytes.textContent) === "11.0 KB",
        "实际: " + els.camBytes.textContent);
  check("有帧后 img 不再隐藏", !els.camImg.classList.contains("hidden"));
  check("徽标切换为实时画面", String(els.camLiveBadge.textContent).includes("实时"),
        "实际: " + els.camLiveBadge.textContent);

  // ---- 场景 3：拍照成功 ----
  console.log("\n[3] 点「拍照」→ POST /cam/capture，照片回来后才提示成功");
  const before = calls.length;
  await click("camShot");
  // 等待异步轮询（setTimeout 已打桩为立即执行，多 flush 几轮）
  for (let i = 0; i < 40; i++) await flush();
  const posted = calls.slice(before).filter((c) => c.method === "POST" && c.url.includes("/api/v1/cam/capture"));
  check("确实 POST 了 /cam/capture", posted.length === 1, "次数=" + posted.length);
  check("请求体带 device_id", posted[0] && String(posted[0].url).length > 0);
  check("提示已收到照片", String(els.camShotHint.textContent).includes("已收到照片"),
        "实际: " + els.camShotHint.textContent);
  check("画廊渲染了照片缩略图", String(els.camGallery.innerHTML).includes("/api/v1/cam/photo/"),
        "实际: " + String(els.camGallery.innerHTML).slice(0, 80));

  // ---- 场景 4：无帧设备点拍照 → 如实失败 ----
  console.log("\n[4] 没有画面时点拍照 → 不得谎称成功");
  statusBody = {
    ok: true, has_frame: false, device_id: "esp32s3eye-1C7011", frame: null,
    camera: null, devices_with_frames: [], photo_count: 0, capture_pending: false,
  };
  els.camShotHint.textContent = "";
  captureOk = false;
  photoArrives = false;
  await click("camShot");
  for (let i = 0; i < 20; i++) await flush();
  check("失败提示不含「已收到照片」", !String(els.camShotHint.textContent).includes("已收到照片"),
        "实际: " + els.camShotHint.textContent);
  check("失败提示含原因", /失败|没有画面|无法/.test(String(els.camShotHint.textContent)),
        "实际: " + els.camShotHint.textContent);

  // ---- 场景 5：全部设备模式（不带 device_id），后端只给 cameras[] ----
  //   这正是线上踩到的坑：状态接口没带 device_id 时只返回 camera=null，
  //   面板就退化成"还没有画面 + 请确认固件已烧录"，把真正的硬件原因藏起来了。
  console.log("\n[5] 全部设备模式、后端只给 cameras[] → 仍须说出真实原因");
  statusBody = {
    ok: true, has_frame: false, device_id: null, frame: null, camera: null,
    cameras: [{ device_id: "esp32s3eye-1C701C", ready: false, pid: 0,
                error: "ESP_ERR_NOT_SUPPORTED" }],
    devices_with_frames: [], photo_count: 0, capture_pending: false,
  };
  els.camPhTitle.textContent = "";
  els.camPhSub.textContent = "";
  await click("closeCamera");     // 关掉再打开 = 触发一次新的轮询
  await click("btnCamera");
  await flush();
  check("标题仍写明「摄像头未就绪」", String(els.camPhTitle.textContent).includes("摄像头未就绪"),
        "实际: " + els.camPhTitle.textContent);
  check("副文案带失败原因", String(els.camPhSub.textContent).includes("ESP_ERR_NOT_SUPPORTED"),
        "实际: " + String(els.camPhSub.textContent).slice(0, 90));
  check("副文案指出 0x30 不在总线上", String(els.camPhSub.textContent).includes("0x30"),
        "实际: " + String(els.camPhSub.textContent).slice(0, 90));
  check("不得退化成「请确认固件已烧录」", !String(els.camPhSub.textContent).includes("固件已烧录"),
        "实际: " + String(els.camPhSub.textContent).slice(0, 90));
  // 固件会自动重试探测：文案不能让用户白断电重启（旧文案让人"按 RST 重启"）
  check("文案给出「30 秒内自动恢复、不需要重启」",
        String(els.camPhSub.textContent).includes("30 秒")
        && String(els.camPhSub.textContent).includes("不需要重启"),
        "实际: " + String(els.camPhSub.textContent).slice(-40));

  console.log("\n" + "=".repeat(78));
  if (fails.length) {
    console.log("失败用例: " + fails.join(" | "));
    process.exit(1);
  }
  console.log("全部用例通过 ✔（相机预览/拍照符合「不造假」要求）");
  console.log("=".repeat(78));
  process.exit(0);
})();
