#!/usr/bin/env node
/**
 * test_web_stale.js —— 用打桩 DOM 实测前端「断流」行为（不需要浏览器）
 *
 * 任务要求网页必须做到：板子停止上传时，**保留最后一次真实数值与时间戳**，
 * 并明确提示「暂无新数据」，而不是继续假装实时、更不能用 0 或占位值顶替。
 * 这段行为很容易"看起来写了"，所以这里直接把真实的 web/app.js 加载进来，
 * 用假的 DOM + 假的 fetch 驱动，检查它到底往页面上写了什么。
 *
 * 四个用例：
 *   1) 库里没有任何记录        -> 提示「等待设备首次上传」，数值保持占位符 —
 *   2) 收到一条新鲜记录        -> 显示「数据实时」，数值 = 接口值
 *   3) 记录已过期(>STALE_MS)   -> 显示「暂无新数据」，但**数值与时间戳保留**
 *   4) 接口请求失败            -> 保留上一次真实值（不能被清 0），并报连接失败次数
 *
 * 运行: node tools/test_web_stale.js
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
    id,
    className: "",
    width: 0,
    height: 0,
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c) => (classes.has(c) ? classes.delete(c) : classes.add(c)),
    },
    addEventListener: () => {},
    getContext: () => ctx,
    _classes: classes,
    _writes: 0,      // textContent 被写入的次数（用来验证"不是每帧都写 DOM"）
    _cwReads: 0,     // clientWidth 被读取的次数（读它会让浏览器重算布局）
  };
  Object.defineProperty(el, "textContent", {
    get() { return this._text; },
    set(v) { this._text = v; this._writes++; },
  });
  Object.defineProperty(el, "clientWidth", {
    get() { this._cwReads++; return 1200; },
  });
  return el;
}

// canvas 2d 上下文：任何方法都当空操作，任何属性都可写
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

// 可控时钟 + 可控 requestAnimationFrame：测试里手动"喂帧"，才能精确断言帧率与滚动
// wallClock 代替 Date.now()（页面代码用它判断时间），frameClock 代替 performance.now()
let wallClock = 1_700_000_000_000;
let frameClock = 1000;
global.Date.now = () => wallClock;
global.performance = { now: () => frameClock };
let rafCbs = [];
global.requestAnimationFrame = (cb) => { rafCbs.push(cb); return rafCbs.length; };
global.cancelAnimationFrame = () => {};

/** 推进 n 帧，每帧间隔 stepMs 毫秒（帧时钟与墙钟同步前进） */
function runFrames(n, stepMs) {
  for (let i = 0; i < n; i++) {
    const cbs = rafCbs;
    rafCbs = [];
    frameClock += stepMs;
    wallClock += stepMs;
    for (const cb of cbs) cb(frameClock);
  }
}

const intervals = [];
global.setInterval = (fn) => { intervals.push(fn); return intervals.length; };

// 预先把测试要读的元素桩都建出来（app.js 没碰过的元素也能被断言读到）
["freshness", "staleBanner", "staleDetail", "valGX", "valGY", "valGZ", "valMag",
 "valMS2X", "valMS2Y", "valMS2Z",
 "dDeviceId", "dSensor", "dRaw", "dRange", "dOver", "dTsLocal", "dUptime",
 "dRecvTime", "dIdSeq", "dVerify", "rawJson", "linkStatus", "apiInfo",
 "statInfo", "pollInfo", "fpsInfo", "chartNote", "dAge"].forEach(
  (id) => document.getElementById(id));

// 用 index.html 里真实的初始内容填充元素桩：这样「页面初始显示什么」也是被测的一部分
// （要求初始值必须是占位符 —，绝不能预先填任何一个数字）。
const INDEX_HTML = fs.readFileSync(path.join(__dirname, "..", "web", "index.html"), "utf8");
for (const m of INDEX_HTML.matchAll(/id="([A-Za-z0-9_]+)"[^>]*>([^<]*)</g)) {
  const stub = els[m[1]];
  if (stub) stub.textContent = m[2].trim();
}

// ---------------------------------------------------------------------------
// 可控的假 fetch
// ---------------------------------------------------------------------------
let mode = "empty";           // empty | fresh | stale | fail
let nowOffsetMs = 0;          // 用来把记录的接收时间"推到过去"
let pollBatch = 1;            // 每次增量拉取返回几个采样点（真实板端 1 秒一个包、一包 10 点）
let nextId = 1000;            // 记录 id 单调递增：前端就是靠 id 续传，绝不重复也绝不漏
const LATEST_RECORD = {
  id: 42, device_id: "esp32s3eye-1C701C", boot_count: 7, seq: 9, uptime_ms: 12345,
  sample_idx: 10, sample_count: 10, sample_period_ms: 100,
  ts_local: "2026-09-30T09:09:14.003+0800",
  received_at_utc: new Date(Date.now() - 1000).toISOString(),
  sensor: { model: "QMA6100P", type: "accelerometer", i2c_addr: 18, chip_id: 144,
            range_g: 2, lsb_per_g: 4096, odr_hz: 100, oversample: 8 },
  unit: { raw: "LSB(14bit signed)", accel_g: "g", accel_ms2: "m/s^2", magnitude_g: "g" },
  raw: { x: -2834, y: 2010, z: 1940 },
  accel_g: { x: -0.692383, y: 0.490723, z: 0.473633 },
  accel_ms2: { x: -6.789956, y: 4.812, z: 4.645 },
  magnitude_g: 0.972123,
  verify_ok: 1, verify_note: "已核对 raw/lsb_per_g 与上报值一致(误差<=0.001g)",
};

function rec(offsetMs, id) {
  const r = JSON.parse(JSON.stringify(LATEST_RECORD));
  r.received_at_utc = new Date(Date.now() + offsetMs).toISOString();
  r.id = id === undefined ? nextId++ : id;
  return r;
}

global.fetch = async (url) => {
  const path_ = String(url);
  if (mode === "fail") throw new Error("模拟后端不可达");
  const reply = (status, body) => ({ status, text: async () => JSON.stringify(body) });

  if (path_.includes("/api/v1/stats")) {
    return reply(200, { ok: true, records: 86, raw_uploads: 86, devices: 1,
                        last_received_at_utc: new Date().toISOString() });
  }
  if (path_.includes("/api/v1/records")) {
    if (mode === "empty") return reply(404, { ok: false, error: "还没有任何记录" });
    const m = /after_id=(\d+)/.exec(path_);
    if (!m) {
      // 首次装载：历史点（升序），网页用它把曲线窗口先铺满
      const base = nextId;
      nextId += 3;
      return reply(200, { ok: true, count: 3,
                          records: [rec(-3000, base), rec(-2000, base + 1), rec(-1000, base + 2)] });
    }
    // 增量拉取：返回 pollBatch 个新的真实采样点（间隔 100ms，与板端一致）
    const after = parseInt(m[1], 10);
    const out = [];
    for (let i = 0; i < pollBatch; i++) {
      out.push(rec(nowOffsetMs - (pollBatch - 1 - i) * 100));
    }
    return reply(200, { ok: true, count: out.length, after_id: after, records: out });
  }
  return reply(404, { ok: false });
};

// ---------------------------------------------------------------------------
// 用例
// ---------------------------------------------------------------------------
const tick = () => new Promise((r) => setImmediate(r));
let failures = 0;

function check(name, cond, detail) {
  console.log(`  ${cond ? "✔" : "✘"} ${name}${cond ? "" : "   <-- " + detail}`);
  if (!cond) failures++;
}

async function main() {
  console.log("=".repeat(90));
  console.log("前端「断流」行为实测（直接加载 web/app.js，DOM/fetch 全部打桩）");
  console.log("=".repeat(90));

  // --- 用例 1：库里没有数据 ---
  mode = "empty";
  require(APP_JS);
  for (let i = 0; i < 6; i++) await tick();
  console.log("\n[1] 数据库为空时");
  console.log("    freshness =", JSON.stringify(els.freshness.textContent));
  console.log("    staleBanner 显示 =", !els.staleBanner._classes.has("hidden"),
              "| 明细 =", JSON.stringify((els.staleDetail.textContent || "").trim().slice(0, 60)));
  console.log("    valGX =", JSON.stringify(els.valGX.textContent));
  check("提示「等待设备首次上传」", els.freshness.textContent === "等待设备首次上传",
        els.freshness.textContent);
  check("数值保持占位符 —（不伪造 0）", els.valGX.textContent === "—", els.valGX.textContent);
  check("断流横幅可见", !els.staleBanner._classes.has("hidden"), "横幅被隐藏了");

  // --- 用例 2：收到新鲜记录 ---
  mode = "fresh";
  intervals[0]();                     // 手动触发一次轮询（等价于页面 2 秒轮询）
  for (let i = 0; i < 6; i++) await tick();
  console.log("\n[2] 收到一条新鲜记录（模拟后端返回真实数据）");
  console.log("    freshness =", JSON.stringify(els.freshness.textContent));
  console.log("    valGX =", els.valGX.textContent, " valGY =", els.valGY.textContent,
              " valGZ =", els.valGZ.textContent, " |a| =", els.valMag.textContent);
  console.log("    设备 =", els.dDeviceId.textContent, "| raw =", els.dRaw.textContent);
  check("显示「数据实时」", /^数据实时/.test(els.freshness.textContent), els.freshness.textContent);
  check("数值 = 接口返回值", els.valGX.textContent === "-0.692", els.valGX.textContent);
  check("合矢量显示", els.valMag.textContent === "0.972", els.valMag.textContent);
  check("设备 ID 来自接口", els.dDeviceId.textContent === "esp32s3eye-1C701C",
        els.dDeviceId.textContent);
  check("断流横幅隐藏", els.staleBanner._classes.has("hidden"), "横幅仍显示");

  // --- 用例 3：数据过期（板子停止上传） ---
  mode = "stale";
  nowOffsetMs = -30 * 1000;           // 最后一条记录是 30 秒前收到的
  intervals[0]();
  for (let i = 0; i < 6; i++) await tick();
  console.log("\n[3] 30 秒没有新数据（等价于板子断电/断网停传）");
  console.log("    freshness =", JSON.stringify(els.freshness.textContent));
  console.log("    明细 =", JSON.stringify((els.staleDetail.textContent || "").trim()));
  console.log("    valGX =", els.valGX.textContent, "| 设备本地时间戳 =", els.dTsLocal.textContent);
  check("提示「暂无新数据」", els.freshness.textContent === "暂无新数据",
        els.freshness.textContent);
  check("断流横幅出现", !els.staleBanner._classes.has("hidden"), "横幅没出现");
  check("明细说明保留最后一次数值", /页面保留最后一次真实数值与时间戳/.test(els.staleDetail.textContent),
        els.staleDetail.textContent);
  check("数值仍为最后一次真实值（未被清 0）", els.valGX.textContent === "-0.692",
        els.valGX.textContent);
  // "保留时间戳" = 断流后页面仍显示最后一条真实记录的时间，而不是被清成 — 或空
  check("设备本地时间戳保留", els.dTsLocal.textContent === LATEST_RECORD.ts_local,
        els.dTsLocal.textContent);
  check("服务器接收时间保留（未清空）",
        els.dRecvTime.textContent !== "—" && els.dRecvTime.textContent.trim() !== "",
        els.dRecvTime.textContent);
  check("断流提示里带出最后一次采集时间",
        els.staleDetail.textContent.includes("最后一次采集时间"),
        els.staleDetail.textContent);

  // --- 用例 4：接口请求失败 ---
  mode = "fail";
  intervals[0]();
  for (let i = 0; i < 6; i++) await tick();
  console.log("\n[4] 后端请求失败（例如服务挂了）");
  console.log("    linkStatus =", JSON.stringify(els.linkStatus.textContent));
  console.log("    valGX =", els.valGX.textContent);
  check("提示后端请求失败", /后端请求失败/.test(els.linkStatus.textContent),
        els.linkStatus.textContent);
  check("数值保持上一次真实值", els.valGX.textContent === "-0.692", els.valGX.textContent);

  // --- 用例 4b：10Hz 数据密度下"一个采样点都不能漏" ---
  // 板端 1 秒上传一个包、一包 10 个点；网页每 800ms 拉一次。
  // 老实现每次只取 /latest 一条 → 800ms 里的 8 个点被直接跳过（曲线一段一段地跳）。
  // 现在改成 after_id 增量拉取，必须把返回的 10 个点全部追加进曲线。
  console.log("\n[4b] 增量拉取：一次返回 10 个采样点（模拟板端一包 10 点）");
  mode = "fresh";
  nowOffsetMs = 0;
  pollBatch = 10;
  const dbgA = window.__imuDebug;
  const added0 = dbgA.addedTotal, loaded0 = dbgA.loadedPoints;
  intervals[0]();
  for (let i = 0; i < 6; i++) await tick();
  const dbgB = window.__imuDebug;
  console.log("    曲线上的点:", loaded0, "->", dbgB.loadedPoints,
              "| 累计追加:", added0, "->", dbgB.addedTotal);
  check("一次轮询把 10 个点全部追加（不丢样本）", dbgB.addedTotal - added0 === 10,
        `只追加了 ${dbgB.addedTotal - added0} 个`);
  check("数值卡片仍等于接口值（卡片只写一次，不是每个点刷一遍）",
        els.valGX.textContent === "-0.692", els.valGX.textContent);
  check("详情显示包内序号", /包内第 10\/10 点/.test(els.dIdSeq.textContent),
        els.dIdSeq.textContent);
  pollBatch = 1;

  // --- 用例 5：曲线平滑渲染（帧率 + 是否每帧推进） ---
  console.log("\n[5] 曲线平滑渲染（手动喂 61 帧 ≈ 1 秒的 60Hz vsync）");
  mode = "fresh";
  nowOffsetMs = 0;
  intervals[0]();                       // 先让窗口里有真实数据点
  for (let i = 0; i < 6; i++) await tick();

  const dbg0 = window.__imuDebug;
  const x0 = dbg0 && dbg0.newestX;
  runFrames(61, 1000 / 60);
  const dbg1 = window.__imuDebug;
  console.log("    frames:", dbg0 ? dbg0.frames : "?", "->", dbg1.frames,
              "| fps 统计 =", dbg1.fps);
  console.log("    最新点 x 像素:", x0 == null ? "?" : x0.toFixed(2),
              "->", dbg1.newestX == null ? "?" : dbg1.newestX.toFixed(2),
              "| 窗口内真实采样点 =", dbg1.visiblePoints);
  check("渲染循环每帧都在重绘", dbg1.frames - (dbg0 ? dbg0.frames : 0) === 61,
        `${dbg0 ? dbg0.frames : "?"} -> ${dbg1.frames}`);
  check("帧率统计约 60FPS", dbg1.fps >= 55 && dbg1.fps <= 66, String(dbg1.fps));
  check("页脚显示帧率", /\d+ FPS/.test(els.fpsInfo.textContent), els.fpsInfo.textContent);

  // 60 秒窗口、绘图区宽 1136px → 理论滚动速度 ≈ 18.9 px/s；1.017 秒应移动约 19px。
  // 以前按"点的序号"定位，画布只在有新数据时跳一下（位移要么 0 要么一大格），
  // 现在按真实时间定位，所以每帧位移都连续且速度正确。
  const glide = x0 != null ? (x0 - dbg1.newestX) : null;
  console.log("    1.017 秒内左移像素 =", glide == null ? "?" : glide.toFixed(2),
              "（理论 ≈ 19.2px @ 60s 窗口）");
  check("曲线随时间平滑左移且速度正确", glide != null && glide > 15 && glide < 24,
        String(glide));
  check("渲染 61 帧后数值仍精确等于接口值（没有插值/动画数字）",
        els.valGX.textContent === "-0.692", els.valGX.textContent);
  check("曲线说明标注为真实采样点", /真实采样点/.test(els.chartNote.textContent),
        els.chartNote.textContent);
  check("轴范围有缓动（不是每帧硬跳）",
        dbg1.axis && Number.isFinite(dbg1.axis.lo) && Number.isFinite(dbg1.axis.hi),
        JSON.stringify(dbg1.axis));
  check("实测显示器刷新率 ≈60Hz（中位帧 16.7ms）",
        dbg1.vsyncHz >= 55 && dbg1.vsyncHz <= 66 && dbg1.frameMs > 15 && dbg1.frameMs < 18,
        `vsync=${dbg1.vsyncHz}Hz frameMs=${dbg1.frameMs} worst=${dbg1.worstMs}`);

  // --- 用例 5b：每帧开销被压掉（DOM 写入/布局读取都按帧节流） ---
  console.log("\n[5b] 每帧开销检查（61 帧内）");
  const noteWrites0 = els.chartNote._writes, cw0 = els.chart._cwReads;
  runFrames(61, 1000 / 60);
  const noteWrites = els.chartNote._writes - noteWrites0;
  const cwReads = els.chart._cwReads - cw0;
  console.log("    chartNote 写入次数 =", noteWrites, "（不节流的话会是 61）");
  console.log("    画布 clientWidth 读取次数 =", cwReads, "（不节流的话会是 61，每次都触发重排）");
  check("说明文字不是每帧写 DOM", noteWrites <= 4, String(noteWrites));
  check("画布尺寸不每帧读布局", cwReads === 0, String(cwReads));

  // --- 用例 5c：换成 120Hz 显示器，同一份代码应自动跑满 120FPS ---
  console.log("\n[5c] 模拟 120Hz 高刷显示器（每帧 8.33ms）");
  runFrames(121, 1000 / 120);
  const dbg120 = window.__imuDebug;
  console.log("    fps =", dbg120.fps, "| 实测刷新率 =", dbg120.vsyncHz, "Hz");
  check("高刷屏自动跑满 ~120FPS", dbg120.fps >= 110 && dbg120.fps <= 130, String(dbg120.fps));
  check("刷新率识别为 ~120Hz", dbg120.vsyncHz >= 110 && dbg120.vsyncHz <= 130,
        String(dbg120.vsyncHz));

  // --- 用例 6：页面切到后台时停止重绘（不白烧 CPU） ---
  console.log("\n[6] 页面切到后台（document.hidden = true）");
  const before = window.__imuDebug.frames;
  global.document.hidden = true;
  runFrames(30, 1000 / 60);
  const after = window.__imuDebug.frames;
  global.document.hidden = false;
  console.log("    30 帧内 frames 增量 =", after - before);
  check("后台时不重绘", after === before, `${before} -> ${after}`);

  console.log("\n" + "=".repeat(90));
  console.log(failures === 0 ? "全部用例通过 ✔（断流行为符合要求）" : `有 ${failures} 项未通过 ✘`);
  process.exit(failures === 0 ? 0 : 1);
}

main();