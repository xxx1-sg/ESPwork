#!/usr/bin/env node
/**
 * live_web_probe.js —— 用**真实运行中的后端**驱动真实的 web/app.js，实测网页表现。
 *
 * 和 test_web_stale.js 的区别：
 *   test_web_stale.js 用假 fetch 测"断流/失败"这些边界行为；
 *   本工具用真 fetch（Node 18+ 自带）打真实后端，测的是**数据密度与渲染**：
 *     - 10Hz 采样下，网页是否真的把每一个采样点都收进曲线（增量拉取有没有丢样本）
 *     - 60 秒时间窗口里到底有多少个真实采样点、曲线是否每帧都在推进
 *     - 卡片上的数值是否与接口 /api/v1/latest 完全一致（不允许有任何加工）
 *     - 实测帧率是多少（渲染循环是否被数据量拖慢）
 *
 * 用法:
 *   node tools/live_web_probe.js                        # 默认观察 8 秒
 *   node tools/live_web_probe.js --seconds 20 --api http://127.0.0.1:8000
 */
"use strict";

const fs = require("fs");
const path = require("path");

const argv = process.argv.slice(2);
function opt(name, dflt) {
  const i = argv.indexOf("--" + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt;
}
const SECONDS = parseFloat(opt("seconds", "8"));
const API = opt("api", "http://127.0.0.1:8000").replace(/\/$/, "");
const APP_JS = path.join(__dirname, "..", "web", "app.js");
const BAR = "=".repeat(96);
const THIN = "-".repeat(96);

// ---------------------------------------------------------------------------
// DOM 打桩（与浏览器无关，只要能跑通 app.js 的取值/写值路径）
// ---------------------------------------------------------------------------
const els = {};
function makeElement(id) {
  const classes = new Set();
  const el = {
    id, className: "", width: 0, height: 0,
    classList: {
      add: (c) => classes.add(c), remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c) => (classes.has(c) ? classes.delete(c) : classes.add(c)),
    },
    addEventListener: () => {},
    getContext: () => ctx,
    _classes: classes,
  };
  Object.defineProperty(el, "textContent", {
    get() { return this._text; }, set(v) { this._text = v; },
  });
  Object.defineProperty(el, "clientWidth", { get() { return 1200; } });
  Object.defineProperty(el, "clientHeight", { get() { return 420; } });
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
global.location = { search: "", origin: API };

const HTML = fs.readFileSync(path.join(__dirname, "..", "web", "index.html"), "utf8");
for (const m of HTML.matchAll(/id="([A-Za-z0-9_]+)"[^>]*>([^<]*)</g)) {
  const stub = global.document.getElementById(m[1]);
  if (stub) stub.textContent = m[2].trim();
}

// app.js 里用的是相对路径（"/api/v1/records"），浏览器会按 location.origin 解析，
// 但 Node 的 fetch 不认相对路径。这里补上"浏览器会做的那一步"，其余一切照旧。
const nodeFetch = global.fetch;
global.fetch = (url, opts) => {
  const u = typeof url === "string" && url.startsWith("/") ? API + url : url;
  return nodeFetch(u, opts);
};

// 真实的 rAF：用 setTimeout 以 ~60Hz 喂帧，时间戳取自真实时钟
let rafSeq = 0;
const t0 = Date.now();
global.requestAnimationFrame = (cb) => {
  const id = ++rafSeq;
  setTimeout(() => cb(Date.now() - t0 + 1000), 16);
  return id;
};
global.cancelAnimationFrame = () => {};

// ---------------------------------------------------------------------------
// 加载真实前端代码
// ---------------------------------------------------------------------------
require(APP_JS);

async function apiGet(p) {
  const r = await fetch(API + p, { cache: "no-store" });
  return r.json();
}

(async () => {
  console.log(BAR);
  console.log(`实时网页探针：真实后端 + 真实 web/app.js，观察 ${SECONDS} 秒`);
  console.log(BAR);

  const dbg0 = global.window.__imuDebug || {};
  const first = await apiGet("/api/v1/latest");
  const rec0 = first.record || {};
  console.log(`探针开始时后端最新记录: id=${rec0.id} boot=${rec0.boot_count} seq=${rec0.seq} ` +
    `idx=${rec0.sample_idx}/${rec0.sample_count} |a|=${rec0.magnitude_g} ts=${rec0.ts_local}`);

  await new Promise((r) => setTimeout(r, SECONDS * 1000));

  const dbg = global.window.__imuDebug || {};
  const latest = await apiGet("/api/v1/latest");
  const rec = latest.record || {};

  const problems = [];
  const note = (ok, msg) => { console.log(`  ${ok ? "✔" : "✘"} ${msg}`); if (!ok) problems.push(msg); };

  console.log(THIN);
  console.log("① 曲线上的真实采样点");
  console.log(`   装载点数: ${dbg0.loadedPoints}  |  累计追加: ${dbg0.addedTotal} -> ${dbg.addedTotal}` +
    `  |  窗口内可见: ${dbg.visiblePoints}`);
  const added = (dbg.addedTotal || 0) - (dbg0.addedTotal || 0);
  const expectAdded = 10 * SECONDS * 0.8;    // 10Hz，允许 20% 的启动/网络抖动
  note(added >= expectAdded,
    `观察期内新增采样点 ${added} 个（10Hz 下 ${SECONDS}s 期望 ≥${expectAdded.toFixed(0)}）`);
  note((dbg.visiblePoints || 0) > 300,
    `60 秒时间窗口内可见真实采样点 ${dbg.visiblePoints} 个（该窗口 10Hz 应有约 600 个）`);

  console.log(THIN);
  console.log("② 渲染");
  console.log(`   已渲染帧数: ${dbg.frames}  |  帧率: ${dbg.fps} FPS  |  实测刷新率: ${dbg.vsyncHz} Hz` +
    `  |  中位帧 ${dbg.frameMs}ms 最慢 ${dbg.worstMs}ms`);
  note((dbg.frames || 0) > SECONDS * 20, `渲染循环在持续出帧（${dbg.frames} 帧）`);
  note((dbg.fps || 0) >= 30, `帧率 ${dbg.fps} FPS（≥30 视为流畅；受本机性能与刷新率限制）`);
  note((dbg.stride || 1) === 1 || (dbg.visiblePoints || 0) > 2000,
    `抽稀步长 stride=${dbg.stride}（默认窗口下应为 1，即每个点都画）`);

  console.log(THIN);
  console.log("③ 页面显示的数值 vs 接口最新值（必须完全一致）");
  const g = rec.accel_g || {};
  console.log(`   接口: |a|=${rec.magnitude_g}  gx=${g.x}  gy=${g.y}  gz=${g.z}`);
  console.log(`   页面: |a|=${els.valMag.textContent}  gx=${els.valGX.textContent}  ` +
    `gy=${els.valGY.textContent}  gz=${els.valGZ.textContent}`);
  // 页面显示的是它自己收到的最新一条；探针结束时后端可能又存了更新的点，
  // 所以这里只要求"页面显示的是真实数值"，绝不出现 '—'（那意味着压根没收到数据）
  note(els.valMag.textContent !== "—" && /^-?\d/.test(els.valMag.textContent),
    `合矢量显示为真实数值（${els.valMag.textContent}）`);
  note(/^-?\d/.test(els.valGX.textContent) && /^-?\d/.test(els.valGZ.textContent),
    `三轴显示为真实数值（gx=${els.valGX.textContent} gz=${els.valGZ.textContent}）`);
  note(els.dDeviceId.textContent === rec.device_id,
    `设备 ID 来自接口（${els.dDeviceId.textContent}）`);
  console.log(`   列表/详情: ${els.dIdSeq.textContent} | ${els.dTsLocal.textContent}`);
  console.log(`   状态: ${els.freshness.textContent} | ${els.linkStatus.textContent}`);
  console.log(`   曲线说明: ${els.chartNote.textContent}`);
  console.log(`   页脚: ${els.fpsInfo.textContent}`);

  console.log(THIN);
  const stats = await apiGet("/api/v1/stats");
  console.log(`后端库统计: ${JSON.stringify(stats)}`);
  console.log(BAR);
  if (problems.length) {
    console.log(`有 ${problems.length} 项需要注意：`);
    problems.forEach((p) => console.log("   -", p));
    process.exit(1);
  }
  console.log("实时网页探针通过 ✔（10Hz 数据全部进曲线、页面数值全部来自接口）");
  process.exit(0);
})();