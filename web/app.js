/* ============================================================================
 * app.js —— 三轴姿态监测台 · 前端实时可视化逻辑（界面重构版）
 *
 * 绝对原则（本工程 README 的硬性约定，界面重构不改变它）：
 *   1) 本文件不产生任何传感器数值。页面上出现的每一个数字都直接来自后端 JSON。
 *   2) 数值只在「成功拿到 API 响应」时更新；接口失败时保留上一次真实数值与时间戳。
 *   3) 超过 STALE_MS 没有新数据 → 明确提示「暂无新数据」，而不是继续假装实时。
 *
 * 关于"派生值"的边界（页面上一共只有这几类，全部可从接口字段复算）：
 *   · pitch / roll / 夹角 = 对接口返回的 g 值做三角函数；
 *   · |a| 的 m/s² = 接口返回的 magnitude_g × 9.80665（与板端/后端完全同一个换算常数）；
 *   · 相对基线的 Δ = 当前接口值 − 基线时刻的接口值（只在曲线上显示，读数卡片永远显示绝对值）；
 *   · 球面落点方向 = 接口返回的 g 值归一化。
 *   除此之外页面不做任何插值、平滑、拟合或兜底填充；画布上的"缓动"只影响动画过渡，
 *   不参与任何数值显示（卡片里的数字始终精确等于接口返回值）。
 * ==========================================================================*/

(function () {
  "use strict";

  // ---------------------------------------------------------------- 可调参数
  // 用 URL 参数即可排查：index.html?poll=800&stale=6000&window=60&history=700
  const params   = new URLSearchParams(location.search);
  const POLL_MS  = parseInt(params.get("poll") || "800", 10);    // 增量拉取间隔
  const STALE_MS = parseInt(params.get("stale") || "6000", 10);  // 判定「断流」的阈值
  const WINDOW_S = parseFloat(params.get("window") || "60");     // 曲线时间窗口（秒）
  const HISTORY  = parseInt(params.get("history") || "700", 10); // 初始化装载的历史点数
  const CHUNK    = parseInt(params.get("chunk") || "400", 10);   // 一次增量最多拉多少条

  const API = {
    latest:  "/api/v1/latest",
    records: "/api/v1/records",   // 曲线数据（支持 after_id 增量拉取）
    devices: "/api/v1/devices",   // 设备分组下拉
    stats:   "/api/v1/stats",
  };

  const G_TO_MS2  = 9.80665;   // ★ 与板端 imu_qma6100p.c / 后端 server.py 同一个换算常数
  const CHART_H   = 336;       // 与 style.css 里 #chart 的高度一致
  const SPHERE_PX = 320;       // 与 style.css 里 #sphere 的尺寸一致
  const EASE_MS   = 130;       // 球面落点"纯视觉"缓动时间常数（不改动任何数值）

  // 球体观察视角（正交投影，只影响观感，不影响落点代表的物理方向）
  const VIEW_AZ = 0.62, VIEW_EL = 0.34;
  const CA = Math.cos(VIEW_AZ), SA = Math.sin(VIEW_AZ);
  const CB = Math.cos(VIEW_EL), SB = Math.sin(VIEW_EL);

  // ---------------------------------------------------------------- 配色
  // canvas 里的颜色不能走 CSS 变量，所以这里按主题各留一份（与 style.css 保持一致）
  const PALETTE = {
    light: {
      grid: "rgba(20,28,52,.07)", gridStrong: "rgba(20,28,52,.14)",
      text: "#8d97ad", crosshair: "rgba(20,28,52,.22)",
      x: "#e0525f", y: "#0fa87e", z: "#2f7ff0", mag: "#7c4dff",
      sphereEdge: "rgba(20,28,52,.12)", sphereFill: "rgba(91,108,240,.045)",
      wire: "rgba(20,28,52,.13)", wireBack: "rgba(20,28,52,.055)",
    },
    dark: {
      grid: "rgba(255,255,255,.07)", gridStrong: "rgba(255,255,255,.15)",
      text: "#79859f", crosshair: "rgba(255,255,255,.22)",
      x: "#ff7b85", y: "#2fd3a6", z: "#62a8ff", mag: "#b18cff",
      sphereEdge: "rgba(255,255,255,.12)", sphereFill: "rgba(125,140,255,.06)",
      wire: "rgba(255,255,255,.13)", wireBack: "rgba(255,255,255,.05)",
    },
  };

  // 球面经纬网（设备坐标系下的单位球采样点，只算一次；每帧只做投影）
  const RINGS = (function () {
    const rings = [];
    for (const latDeg of [-60, -30, 0, 30, 60]) {
      const la = latDeg * Math.PI / 180, pts = [];
      for (let i = 0; i <= 72; i++) {
        const lo = i / 72 * Math.PI * 2;
        pts.push([Math.cos(la) * Math.cos(lo), Math.cos(la) * Math.sin(lo), Math.sin(la)]);
      }
      rings.push(pts);
    }
    for (let k = 0; k < 6; k++) {
      const lo = k * Math.PI / 6, pts = [];
      for (let i = 0; i <= 72; i++) {
        const la = i / 72 * Math.PI * 2;
        pts.push([Math.cos(la) * Math.cos(lo), Math.cos(la) * Math.sin(lo), Math.sin(la)]);
      }
      rings.push(pts);
    }
    return rings;
  })();

  // ---------------------------------------------------------------- 页面状态
  const state = {
    // ── 数据（只保存「从 API 拿到的真实数据」） ──
    points: [],        // 曲线点：{t,id,seq,idx,cnt,gx,gy,gz,mag,mx,my,mz,mm}
    last: null,        // 最后一次成功拿到的记录对象
    lastId: null,      // 已渲染过的记录 id，避免重复追加
    lastRecvMs: null,  // 最后一次服务器接收时间的毫秒数
    lastDeviceId: null,// 最后一条记录属于哪台设备（仅用于显示，不参与过滤）
    deviceFilter: null,// 设备分组筛选（null = 全部设备）
    rangeG: null,      // 接口返回的量程（决定三轴进度条满量程）
    pollCount: 0,
    failCount: 0,
    addedTotal: 0,     // 累计追加到曲线上的真实采样点总数（自检用）
    pollEnabled: true, // 「实时监控」开关
    wasStale: false,

    // ── 视图（只影响显示，不影响数据） ──
    theme: "light",
    palette: PALETTE.light,
    unit: "g",              // g | ms2
    viewPaused: false,      // 暂停：冻结时间轴
    autoScroll: true,       // 自动滚动：关闭后横轴铺满已加载的全部真实采样点
    freezeRight: null,      // 冻结时的时间轴右边缘（毫秒）
    baseline: null,         // 基线快照（全部为接口值）
    baselineId: null,
    hoverPx: null,          // 鼠标在画布上的位置（CSS 像素）；null = 没有悬浮
    hoverPy: null,
    hoverIdx: -1,
    logs: [],
    logPanelOpen: false,
    openPanel: null,

    // ── 平滑渲染（不参与任何数值计算） ──
    skewMs: 0, axis: null, lastFrameTs: 0, frames: 0, fps: 0,
    fpsSince: 0, fpsFrames: 0,
    needResize: true, dpr: 1, cssW: 0,
    lastLoopTs: 0, deltas: [], frameMs: null, worstMs: 0, vsyncHz: 0,
    lastNoteAt: 0, lastNote: "", lastLabelAt: 0, labelL: "", labelR: "",
    sphereNeedSize: true, sphereDpr: 1, lastSphereDraw: 0, lastSphereTs: 0,
    sphereTarget: null, sphereDir: null,
  };

  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------- 小工具
  const fmt = (v, digits) => (v === null || v === undefined || Number.isNaN(Number(v)))
    ? "—" : Number(v).toFixed(digits);

  const isNum = (v) => typeof v === "number" && Number.isFinite(v);

  const pad2 = (n) => String(n).padStart(2, "0");
  const hhmmss = (d) => `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  const hhmmss3 = (d) => `${hhmmss(d)}.${String(d.getMilliseconds()).padStart(3, "0")}`;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  /** 服务器接收时间(UTC ISO) → 本机时区可读字符串 */
  function recvToLocalText(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ` +
           `${hhmmss3(d)} (本机时区)`;
  }

  function ageText(ms) {
    if (ms === null || ms === undefined) return "—";
    if (ms < 1000) return `${Math.round(ms)} ms 前`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)} 秒前`;
    return `${(ms / 60000).toFixed(1)} 分钟前`;
  }

  const toDeg = (rad) => rad * 180 / Math.PI;

  /**
   * 取 canvas 渐变对象的"安全"包装。
   * 某些环境（例如无头打桩的自检脚本）里 canvas 上下文是空壳，
   * createXxxGradient 会返回 undefined —— 这里统一兜住，避免整页抛异常。
   */
  function gradient(ctx, kind, args, fallback) {
    if (typeof ctx[kind] !== "function") return fallback;
    const g = ctx[kind].apply(ctx, args);
    return (g && typeof g.addColorStop === "function") ? g : fallback;
  }

  // ==========================================================================
  // 主题
  // ==========================================================================
  function applyTheme(name, quiet) {
    const t = (name === "dark") ? "dark" : "light";
    state.theme = t;
    state.palette = PALETTE[t] || PALETTE.light;
    const root = document.documentElement;
    if (root && typeof root.setAttribute === "function") root.setAttribute("data-theme", t);
    $("themeIcon").textContent = (t === "dark") ? "☀" : "☾";
    const btn = $("themeToggle");
    if (btn && typeof btn.setAttribute === "function") {
      btn.setAttribute("title", (t === "dark" ? "切换为浅色模式" : "切换为深色模式"));
    }
    state.axis = null;            // 换主题后坐标轴范围重算，曲线颜色立即跟随
    if (!quiet) addLog("info", `切换为${t === "dark" ? "深色" : "浅色"}模式`);
    try { localStorage.setItem("imu-theme", t); } catch (e) { /* 隐私模式等：忽略 */ }
  }

  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem("imu-theme"); } catch (e) { saved = null; }
    applyTheme(saved === "dark" ? "dark" : "light", true);
  }

  // ==========================================================================
  // 运行日志（本页面真实观察到的事件，非模拟）
  // ==========================================================================
  function addLog(kind, msg) {
    state.logs.push({ t: Date.now(), kind, msg });
    if (state.logs.length > 300) state.logs.splice(0, state.logs.length - 300);
    const badge = $("logCount");
    if (badge) badge.textContent = String(state.logs.length);
    if (state.logPanelOpen) renderLogs();
  }

  function renderLogs() {
    const list = $("logList");
    if (!list) return;
    if (!state.logs.length) {
      list.innerHTML = '<li class="log-empty">暂无事件：页面尚未观察到任何异常或操作</li>';
      return;
    }
    const rows = state.logs.slice(-120).reverse().map((l) => {
      const d = new Date(l.t);
      return `<li class="log-${esc(l.kind)}"><span class="log-t">${hhmmss3(d)}</span>` +
             `<span class="log-m">${esc(l.msg)}</span></li>`;
    });
    list.innerHTML = rows.join("");
  }

  // ==========================================================================
  // ① 姿态球（重力落点）
  // ==========================================================================
  /** 设备坐标系下的一点 → 屏幕坐标（正交投影；返回深度用于区分球体正/背面） */
  function makeProjector(cx, cy, R) {
    return function (x, y, z) {
      const x1 = x * CA - y * SA;
      const y1 = x * SA + y * CA;
      const up = z * CB - y1 * SB;
      return { X: cx + x1 * R, Y: cy - up * R, d: y1 * CB + z * SB };
    };
  }

  /** 只连"同一侧"的线段：球背面的网格更淡，正面的更实，看起来才有体积感 */
  function strokeSide(ctx, pts, front) {
    let pen = false;
    ctx.beginPath();
    for (let i = 0; i < pts.length; i++) {
      const p = pts[i];
      if ((p.d >= 0) !== front) { pen = false; continue; }
      if (!pen) { ctx.moveTo(p.X, p.Y); pen = true; } else { ctx.lineTo(p.X, p.Y); }
    }
    ctx.stroke();
  }

  function drawSphere(frameTs) {
    const cv = $("sphere");
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;

    if (state.sphereNeedSize) {
      const dpr = window.devicePixelRatio || 1;
      cv.width  = Math.floor(SPHERE_PX * dpr);
      cv.height = Math.floor(SPHERE_PX * dpr);
      state.sphereDpr = dpr;
      state.sphereNeedSize = false;
    }
    const dpr = state.sphereDpr || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, SPHERE_PX, SPHERE_PX);

    const pal = state.palette;
    const cx = SPHERE_PX / 2, cy = SPHERE_PX / 2;
    const R  = SPHERE_PX / 2 - 40;                 // 留出轴标签的余地

    // ---- 落点方向的"纯视觉"缓动（只影响过渡动画，不参与任何数值） ----
    if (state.sphereTarget) {
      const tgt = state.sphereTarget;
      if (!state.sphereDir) {
        state.sphereDir = { x: tgt.x, y: tgt.y, z: tgt.z };
      } else {
        const dt = state.lastSphereTs ? Math.max(1, Math.min(120, frameTs - state.lastSphereTs)) : 16;
        const k = 1 - Math.exp(-dt / EASE_MS);
        state.sphereDir.x += (tgt.x - state.sphereDir.x) * k;
        state.sphereDir.y += (tgt.y - state.sphereDir.y) * k;
        state.sphereDir.z += (tgt.z - state.sphereDir.z) * k;
      }
    }
    state.lastSphereTs = frameTs;

    // ---- 球体 ----
    const body = gradient(ctx, "createRadialGradient",
      [cx - R * 0.34, cy - R * 0.40, R * 0.08, cx, cy, R * 1.04], null);
    if (body) { body.addColorStop(0, pal.sphereFill); body.addColorStop(1, "rgba(0,0,0,0)"); }
    ctx.fillStyle = body || pal.sphereFill;
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = pal.sphereEdge; ctx.lineWidth = 1.2; ctx.stroke();

    // ---- 经纬网（背面淡、正面实） ----
    const project = makeProjector(cx, cy, R);
    const projected = RINGS.map((ring) => ring.map((p) => project(p[0], p[1], p[2])));
    ctx.lineWidth = 1;
    ctx.strokeStyle = pal.wireBack;
    for (const ring of projected) strokeSide(ctx, ring, false);
    ctx.strokeStyle = pal.wire;
    for (const ring of projected) strokeSide(ctx, ring, true);

    // ---- 设备三轴（X 红 / Y 绿 / Z 蓝） ----
    const AXES = [
      { v: [1, 0, 0], c: pal.x, name: "X" },
      { v: [0, 1, 0], c: pal.y, name: "Y" },
      { v: [0, 0, 1], c: pal.z, name: "Z" },
    ];
    for (const ax of AXES) {
      const o = project(0, 0, 0), tip = project(ax.v[0], ax.v[1], ax.v[2]);
      const end = project(ax.v[0] * 1.02, ax.v[1] * 1.02, ax.v[2] * 1.02);
      ctx.strokeStyle = ax.c;
      ctx.globalAlpha = tip.d >= 0 ? 1 : 0.34;      // 指向球背面的轴淡一点
      ctx.lineWidth = 1.8;
      ctx.beginPath(); ctx.moveTo(o.X, o.Y); ctx.lineTo(end.X, end.Y); ctx.stroke();
      ctx.beginPath(); ctx.arc(end.X, end.Y, 2.6, 0, Math.PI * 2); ctx.fillStyle = ax.c; ctx.fill();
      ctx.font = "600 11px ui-monospace, monospace";
      ctx.fillStyle = ax.c;
      const lp = project(ax.v[0] * 1.20, ax.v[1] * 1.20, ax.v[2] * 1.20);
      ctx.fillText(ax.name, lp.X - 4, lp.Y + 4);
      ctx.globalAlpha = 1;
    }

    // ---- 重力落点标记 ----
    const dir = state.sphereDir;
    if (!dir) {
      ctx.fillStyle = pal.text;
      ctx.font = "12px system-ui, sans-serif";
      ctx.fillText("等待数据…", 12, 20);
      return;
    }
    const P = project(dir.x, dir.y, dir.z);
    const O = project(0, 0, 0);

    // 从球心到落点的指向线
    ctx.strokeStyle = pal.mag;
    ctx.globalAlpha = 0.42;
    ctx.lineWidth = 1.2;
    if (typeof ctx.setLineDash === "function") ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.moveTo(O.X, O.Y); ctx.lineTo(P.X, P.Y); ctx.stroke();
    if (typeof ctx.setLineDash === "function") ctx.setLineDash([]);
    ctx.globalAlpha = 1;

    // 光晕
    const halo = gradient(ctx, "createRadialGradient",
      [P.X, P.Y, 1, P.X, P.Y, 20], null);
    if (halo) { halo.addColorStop(0, "rgba(124,77,255,.34)"); halo.addColorStop(1, "rgba(124,77,255,0)"); }
    if (halo) {
      ctx.fillStyle = halo;
      ctx.beginPath(); ctx.arc(P.X, P.Y, 20, 0, Math.PI * 2); ctx.fill();
    }

    // 十字准星 + 外环 + 实心落点
    const ringR = 9.5;
    ctx.strokeStyle = pal.mag;
    ctx.globalAlpha = 0.75;
    ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.arc(P.X, P.Y, ringR, 0, Math.PI * 2); ctx.stroke();
    ctx.globalAlpha = 0.55;
    ctx.lineWidth = 1;
    for (const a of [0, Math.PI / 2, Math.PI, Math.PI * 1.5]) {
      const dx = Math.cos(a), dy = Math.sin(a);
      ctx.beginPath();
      ctx.moveTo(P.X + dx * (ringR + 1.5), P.Y + dy * (ringR + 1.5));
      ctx.lineTo(P.X + dx * (ringR + 5.0), P.Y + dy * (ringR + 5.0));
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    ctx.fillStyle = pal.mag;
    ctx.beginPath(); ctx.arc(P.X, P.Y, 3.4, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#ffffff";
    ctx.beginPath(); ctx.arc(P.X, P.Y, 1.3, 0, Math.PI * 2); ctx.fill();

    // 落点旁边的实时读数（|a|，接口值）
    if (isNum(state.lastMagG)) {
      const leaderEnd = project(dir.x * 1.16, dir.y * 1.16, dir.z * 1.16);
      ctx.strokeStyle = pal.mag;
      ctx.globalAlpha = 0.45;
      ctx.beginPath(); ctx.moveTo(P.X, P.Y); ctx.lineTo(leaderEnd.X, leaderEnd.Y); ctx.stroke();
      ctx.globalAlpha = 1;
      const txt = `|a| ${state.lastMagG.toFixed(3)} g`;
      ctx.font = "600 11px ui-monospace, monospace";
      const tx = Math.max(6, Math.min(SPHERE_PX - 78, leaderEnd.X + (dir.x >= 0 ? 6 : -74)));
      const ty = Math.max(14, Math.min(SPHERE_PX - 6, leaderEnd.Y + 4));
      ctx.fillStyle = pal.text;
      ctx.fillText(txt, tx, ty);
    }
  }

  // ==========================================================================
  // ③ 实时趋势曲线
  // ==========================================================================
  function prunePoints(newestT) {
    const cutoff = newestT - WINDOW_S * 1000 * 1.2;
    let drop = 0;
    while (drop < state.points.length && state.points[drop].t < cutoff) drop++;
    if (drop) state.points.splice(0, drop);
  }

  /** 数值更新时闪一下（纯视觉提示；数值本身始终等于接口返回值） */
  function flashCards() {
    for (const id of ["cardX", "cardY", "cardZ", "cardMag"]) {
      const el = $(id);
      if (!el) continue;
      el.classList.remove("tick");
      void el.offsetWidth;          // 强制重排，让同一个动画能反复触发
      el.classList.add("tick");
    }
  }

  /** 视图右边缘（毫秒，服务器时间基准）：最新采样点之后再留一点余量，避免贴边 */
  function viewRightMs() {
    if (state.freezeRight !== null) return state.freezeRight;
    return Date.now() + state.skewMs + Math.max(1200, POLL_MS * 0.6);
  }

  /** 当前单位下的四条曲线 key 与颜色 */
  function seriesOf() {
    const pal = state.palette;
    if (state.unit === "ms2") {
      return [
        { key: "mx", color: pal.x }, { key: "my", color: pal.y },
        { key: "mz", color: pal.z }, { key: "mm", color: pal.mag },
      ];
    }
    return [
      { key: "gx", color: pal.x }, { key: "gy", color: pal.y },
      { key: "gz", color: pal.z }, { key: "mag", color: pal.mag },
    ];
  }

  /** 曲线上某点的取值：设置了基线就显示相对基线的 Δ（读数卡片不受影响） */
  function pointValue(p, key) {
    const v = p[key];
    if (!Number.isFinite(v)) return NaN;
    const base = state.baseline;
    if (!base) return v;
    const b = base[key];
    return Number.isFinite(b) ? v - b : v;
  }

  function drawChart(frameTs) {
    const cv = $("chart");
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;

    // ★ 画布尺寸只在"真的变了"的时候才量取/重设（每帧读 clientWidth 会强制重排）
    if (state.needResize || !state.cssW) {
      state.dpr  = window.devicePixelRatio || 1;
      state.cssW = cv.clientWidth || 1200;
      cv.width   = Math.floor(state.cssW * state.dpr);
      cv.height  = Math.floor(CHART_H * state.dpr);
      state.needResize = false;
      state.axis = null;              // 尺寸变化后轴范围重新起算
    }
    const dpr = state.dpr, cssW = state.cssW, cssH = CHART_H;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    ctx.lineJoin = "round";
    ctx.lineCap  = "round";

    const pal = state.palette;
    const series = seriesOf();
    const pts = state.points;

    if (pts.length < 2) {
      ctx.fillStyle = pal.text;
      ctx.font = "13px system-ui, sans-serif";
      ctx.fillText("等待数据…", 16, 30);
      return;
    }

    // ---- 时间轴 ----
    const lastT = pts[pts.length - 1].t;
    let tRight, tLeft;
    if (state.autoScroll) {
      tRight = viewRightMs();                       // 右边缘跟着真实时间走 → 连续左移
      tLeft  = tRight - WINDOW_S * 1000;
    } else {
      const firstT = pts[0].t;                      // 全览模式：横轴铺满已加载的真实采样点
      const pad = Math.max(1000, (lastT - firstT) * 0.02);
      tLeft  = firstT - pad;
      tRight = lastT + pad;
    }
    if (tRight - tLeft < 2000) tLeft = tRight - 2000;

    // ---- Y 轴范围：按窗口内的真实数据自适应（刻度只是网格线位置，不是测量值） ----
    let lo = Infinity, hi = -Infinity;
    for (const p of pts) {
      if (p.t < tLeft) continue;
      for (const s of series) {
        const v = pointValue(p, s.key);
        if (Number.isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
      }
    }
    if (!Number.isFinite(lo)) { lo = -1; hi = 1; }
    const padY = Math.max(0.15, (hi - lo) * 0.15);
    const tLo = lo - padY, tHi = hi + padY;

    if (!state.axis) {
      state.axis = { lo: tLo, hi: tHi };
    } else {
      const dt = state.lastFrameTs ? Math.max(1, Math.min(100, frameTs - state.lastFrameTs)) : 16;
      const k  = 1 - Math.exp(-dt / 120);           // 约 120ms 时间常数的指数缓动
      state.axis.lo += (tLo - state.axis.lo) * k;
      state.axis.hi += (tHi - state.axis.hi) * k;
    }
    lo = state.axis.lo; hi = state.axis.hi;

    const L = 56, R = 16, T = 16, B = 26;
    const plotW = cssW - L - R, plotH = cssH - T - B;
    const span   = tRight - tLeft;
    const xAt    = (t) => L + plotW * (t - tLeft) / span;   // ★ 按真实时间定位
    const yAt    = (v) => T + plotH * (1 - (v - lo) / (hi - lo));
    const clampX = (x) => Math.max(L, Math.min(L + plotW, x));

    // ---- 浅网格 + 刻度 ----
    ctx.strokeStyle = pal.grid;
    ctx.lineWidth = 1;
    ctx.font = "11px ui-monospace, monospace";
    const ticks = 5;
    for (let i = 0; i <= ticks; i++) {
      const v = lo + (hi - lo) * i / ticks;
      const y = yAt(v);
      ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(cssW - R, y); ctx.stroke();
      ctx.fillStyle = pal.text;
      ctx.fillText(v.toFixed(state.unit === "ms2" ? 1 : 2), 8, y + 4);
    }
    // 竖向网格（时间刻度）
    ctx.strokeStyle = pal.grid;
    for (let i = 1; i < 6; i++) {
      const x = L + plotW * i / 6;
      ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, T + plotH); ctx.stroke();
    }
    // ±1g（或 ±9.80665 m/s²）参考线
    ctx.strokeStyle = pal.gridStrong;
    if (typeof ctx.setLineDash === "function") ctx.setLineDash([4, 4]);
    const refs = state.unit === "ms2" ? [G_TO_MS2, -G_TO_MS2] : [1, -1];
    for (const ref of refs) {
      if (ref > lo && ref < hi) {
        const y = yAt(ref);
        ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(cssW - R, y); ctx.stroke();
      }
    }
    if (typeof ctx.setLineDash === "function") ctx.setLineDash([]);

    // ---- 只画窗口内的真实采样点（起始下标扫描，避免每帧 filter 分配数组） ----
    let iFirst = 0;
    while (iFirst < pts.length && pts[iFirst].t < tLeft) iFirst++;
    const nVis = pts.length - iFirst;

    // 点比像素还密时按步长抽稀：只挑"真实存在过的采样点"，绝不插值造点
    const stride = nVis > plotW ? Math.ceil(nVis / plotW) : 1;

    for (const s of series) {
      ctx.strokeStyle = s.color;
      ctx.lineWidth = (s.key === "mag" || s.key === "mm") ? 2.2 : 1.6;
      ctx.beginPath();
      let started = false;
      for (let i = iFirst; i < pts.length; i += stride) {
        const v = pointValue(pts[i], s.key);
        if (!Number.isFinite(v)) continue;
        const x = xAt(pts[i].t), y = yAt(v);
        if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
      }
      const lastP = pts[pts.length - 1];
      if (stride > 1 && lastP) {
        const v = pointValue(lastP, s.key);
        if (Number.isFinite(v)) ctx.lineTo(xAt(lastP.t), yAt(v));
      }
      ctx.stroke();
    }

    // ---- 最新一个真实采样点：亮点 + 到顶部的细虚线 ----
    const magKey = state.unit === "ms2" ? "mm" : "mag";
    const newest = nVis > 0 ? pts[pts.length - 1] : null;
    let newestX = null;
    if (newest) {
      const nx = clampX(xAt(newest.t));
      newestX = nx;
      if (typeof ctx.setLineDash === "function") ctx.setLineDash([3, 5]);
      ctx.strokeStyle = pal.crosshair;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(nx, T); ctx.lineTo(nx, T + plotH); ctx.stroke();
      if (typeof ctx.setLineDash === "function") ctx.setLineDash([]);
      ctx.fillStyle = pal.mag;
      ctx.beginPath(); ctx.arc(nx, yAt(pointValue(newest, magKey)), 3.2, 0, Math.PI * 2); ctx.fill();
    }

    // ---- 悬浮提示：显示离鼠标最近的那个真实采样点 ----
    if (state.hoverPx !== null) {
      let best = -1, bestD = Infinity;
      for (let i = iFirst; i < pts.length; i++) {
        const dx = Math.abs(xAt(pts[i].t) - state.hoverPx);
        if (dx < bestD) { bestD = dx; best = i; }
      }
      if (best >= 0 && bestD < 60) {
        const hp = pts[best];
        const hx = clampX(xAt(hp.t));
        ctx.strokeStyle = pal.crosshair;
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(hx, T); ctx.lineTo(hx, T + plotH); ctx.stroke();
        for (const s of series) {
          const v = pointValue(hp, s.key);
          if (!Number.isFinite(v)) continue;
          ctx.fillStyle = s.color;
          ctx.beginPath(); ctx.arc(hx, yAt(v), 2.8, 0, Math.PI * 2); ctx.fill();
        }
        showTip(hp, best);
      } else {
        hideTip();
        state.hoverIdx = -1;
      }
    }

    // ---- 时间刻度文字（每帧重画，但字符串本身算一次缓存 250ms） ----
    if (!state.labelL || frameTs - state.lastLabelAt > 250) {
      state.lastLabelAt = frameTs;
      state.labelL = hhmmss(new Date(tLeft));
      state.labelR = hhmmss(new Date(tRight));
    }
    ctx.fillStyle = pal.text;
    ctx.font = "11px ui-monospace, monospace";
    ctx.fillText(state.labelL, L, cssH - 8);
    ctx.fillText(state.labelR, cssW - R - 58, cssH - 8);
    if (state.unit === "ms2") {
      ctx.fillText("m/s²", 8, cssH - 8);
    } else {
      ctx.fillText("g", 8, cssH - 8);
    }

    // ---- 说明文字：DOM 写入每秒最多 2 次（每帧写 DOM 也是掉帧来源之一） ----
    // 第一次不受节流限制：即使浏览器把渲染循环节流了，窗口说明也会立刻出现
    if (!state.lastNote || frameTs - state.lastNoteAt > 500) {
      state.lastNoteAt = frameTs;
      const firstT = nVis > 0 ? pts[iFirst].t : tLeft;
      const lastTs = newest ? newest.t : tRight;
      const mode = state.autoScroll ? `窗口 ${WINDOW_S}s` : "全览";
      const txt = `${mode} 内 ${nVis} 个真实采样点（服务器接收时间 ` +
        `${hhmmss(new Date(firstT))} ~ ${hhmmss(new Date(lastTs))}）` +
        `${state.baseline ? " · 曲线为相对基线的 Δ" : ""}` +
        ` · 单位 ${state.unit === "ms2" ? "m/s²" : "g"} · 沿真实时间轴连续滚动`;
      if (txt !== state.lastNote) { state.lastNote = txt; $("chartNote").textContent = txt; }
    }

    state.lastFrameTs = frameTs;

    // 调试/自检出口：tools/test_web_stale.js 与 live_web_probe.js 会读它
    window.__imuDebug = {
      frames: state.frames, fps: state.fps, windowS: WINDOW_S, poll: POLL_MS,
      visiblePoints: nVis, newestX: newestX, stride: stride,
      loadedPoints: state.points.length, addedTotal: state.addedTotal,
      lastId: state.lastId,
      axis: { lo: lo, hi: hi },
      unit: state.unit, autoScroll: state.autoScroll, paused: state.viewPaused,
      baseline: state.baseline ? state.baselineId : null,
      pollEnabled: state.pollEnabled,
      frameMs: state.frameMs, worstMs: state.worstMs, vsyncHz: state.vsyncHz,
    };
  }

  // ---- 悬浮提示的 DOM 更新（跟随鼠标，显示最近的那个真实采样点） ----
  function showTip(p, idx) {
    const tip = $("chartTip");
    if (!tip) return;
    tip.classList.remove("hidden");        // 注意：CSS 里 .hidden 带 !important，必须靠类名切换
    if (state.hoverIdx !== idx) {
      const d = new Date(p.t);
      const series = seriesOf();
      const names = state.unit === "ms2"
        ? { mx: "X", my: "Y", mz: "Z", mm: "|a|" }
        : { gx: "X", gy: "Y", gz: "Z", mag: "|a|" };
      const sws = state.unit === "ms2"
        ? { mx: "sw-x", my: "sw-y", mz: "sw-z", mm: "sw-m" }
        : { gx: "sw-x", gy: "sw-y", gz: "sw-z", mag: "sw-m" };
      const unit = state.unit === "ms2" ? "m/s²" : "g";
      let html = `<span class="tip-t">${hhmmss3(d)}（服务器接收时间）</span>`;
      for (const s of series) {
        html += `<div class="tip-row"><span class="tip-k"><i class="sw ${sws[s.key]}"></i>` +
                `${names[s.key]}</span><b>${fmt(pointValue(p, s.key), state.unit === "ms2" ? 2 : 3)} ${unit}</b></div>`;
      }
      html += `<div class="tip-row"><span class="tip-k">记录 / seq</span><b>#${p.id} / ${p.seq}</b></div>`;
      html += `<div class="tip-row"><span class="tip-k">包内序号</span><b>${p.idx}/${p.cnt}</b></div>`;
      tip.innerHTML = html;
      state.hoverIdx = idx;
    }
    if (tip.style) {
      const w = state.cssW || 1200, tipW = 200;
      let left = state.hoverPx + 14;
      if (left + tipW > w - 4) left = state.hoverPx - tipW - 14;
      if (left < 4) left = 4;
      let top = (state.hoverPy === null ? 14 : state.hoverPy) + 14;
      if (top > CHART_H - 130) top = Math.max(6, top - 160);
      tip.style.left = left + "px";
      tip.style.top = top + "px";
    }
  }

  function hideTip() {
    const tip = $("chartTip");
    if (!tip) return;
    tip.classList.add("hidden");
  }

  // ==========================================================================
  // 平滑渲染循环（requestAnimationFrame）
  //   ★ 帧率上限由显示器刷新率决定（rAF 与 vsync 对齐）；这里额外统计
  //     "中位帧耗时 / 最慢帧"，用来判断有没有掉帧（而不是被上限卡住）。
  // ==========================================================================
  function renderLoop(ts) {
    requestAnimationFrame(renderLoop);        // 先排下一帧，保证节奏稳定
    if (document.hidden) return;              // 页面切到后台就不画，别白烧 CPU

    if (state.lastLoopTs) {
      const d = ts - state.lastLoopTs;
      if (d > 0 && d < 500) {
        state.deltas.push(d);
        if (state.deltas.length > 60) state.deltas.shift();
        if (d > state.worstMs) state.worstMs = d;
      }
    }
    state.lastLoopTs = ts;

    state.frames++;
    drawChart(ts);
    if (ts - state.lastSphereDraw >= 33) {     // 球体 30fps 足够，省 CPU
      state.lastSphereDraw = ts;
      drawSphere(ts);
    }

    if (!state.fpsSince) { state.fpsSince = ts; state.fpsFrames = 0; }
    state.fpsFrames++;
    if (ts - state.fpsSince >= 1000) {
      const n = state.fpsFrames;
      state.fps = Math.round(n * 1000 / (ts - state.fpsSince));
      state.fpsSince = ts;
      state.fpsFrames = 0;

      // 中位帧间隔 → 反推显示器刷新率上限（浏览器不直接暴露刷新率，只能实测）
      if (state.deltas.length >= 5) {
        const s = state.deltas.slice().sort((a, b) => a - b);
        state.frameMs = s[Math.floor(s.length / 2)];
        state.vsyncHz = Math.round(1000 / state.frameMs);
      }
      const f = $("fpsInfo");
      if (f) {
        const extra = state.frameMs
          ? ` · 显示器 ≈${state.vsyncHz}Hz · 中位帧 ${state.frameMs.toFixed(1)}ms · 最慢 ${state.worstMs.toFixed(1)}ms`
          : "";
        f.textContent = `渲染: ${state.fps} FPS${extra}（数值仍为真实采样值）`;
      }
      state.worstMs = 0;
    }
    if (window.__imuDebug) {
      window.__imuDebug.frames  = state.frames;
      window.__imuDebug.fps     = state.fps;
      window.__imuDebug.frameMs = state.frameMs;
      window.__imuDebug.worstMs = state.worstMs;
      window.__imuDebug.vsyncHz = state.vsyncHz;
    }
  }

  // ==========================================================================
  // 数据 → 界面
  // ==========================================================================
  /**
   * 把一个真实采样点追加到曲线（不做 DOM 写入、不碰数值卡片）。
   * 板端一个数据包含 10 个采样点，一批要追加 10 次，所以这里必须足够轻。
   */
  function appendPoint(rec) {
    const g = rec.accel_g || {};
    const si = rec.accel_ms2 || {};
    const mag = Number(rec.magnitude_g);
    const tRaw = Date.parse(rec.received_at_utc);
    const t = Number.isNaN(tRaw) ? Date.now() : tRaw;

    state.points.push({
      t,
      id: rec.id, seq: rec.seq, idx: rec.sample_idx, cnt: rec.sample_count,
      gx: Number(g.x), gy: Number(g.y), gz: Number(g.z), mag: mag,
      mx: Number(si.x), my: Number(si.y), mz: Number(si.z),
      // |a| 的 m/s² = 接口返回的 magnitude_g × 9.80665（与板端/后端同一常数，纯单位换算）
      mm: Number.isFinite(mag) ? mag * G_TO_MS2 : NaN,
    });

    // 服务器时钟与本机时钟的偏差：曲线的"左移"完全由真实时间驱动
    state.skewMs = t - Date.now();

    prunePoints(t);          // 只保留时间窗口内的点

    state.last = rec;
    state.lastId = rec.id;
    state.lastRecvMs = t;
    return t;
  }

  /** 设备时钟校对结论（从接口返回的本地时间串里读时区，不做任何推测） */
  function clockText(tsLocal) {
    if (!tsLocal) return "未对时（设备尚未取得网络时间）";
    const m = /([+-]\d{4})$/.exec(String(tsLocal));
    return m ? `已 SNTP 对时 · 时区偏移 ${m[1]}` : "已 SNTP 对时（接口未给出时区偏移）";
  }

  /** 时间偏移判定：绿=正常，橙=偏大，红=异常 / 板端时钟超前 */
  function offsetVerdict(ms) {
    if (ms < 0) return "板端时钟快于服务器";
    if (ms <= 2000) return "正常（含网络与入库耗时）";
    if (ms <= 6000) return "偏大，建议检查链路";
    return "异常偏大";
  }

  /** 三轴进度条：以 0 为中心的双向条（满量程取接口返回的 range_g） */
  function setBar(id, v, rangeG) {
    const el = $(id);
    if (!el) return;
    const half = (Number.isFinite(v) && Number.isFinite(rangeG) && rangeG > 0)
      ? Math.min(50, Math.abs(v) / rangeG * 50)
      : 0;
    if (el.style) {
      if (v >= 0) { el.style.left = "50%"; el.style.width = half + "%"; }
      else { el.style.left = (50 - half) + "%"; el.style.width = half + "%"; }
    }
  }

  function renderRecord(rec) {
    const raw = rec.raw || {};
    const g = rec.accel_g || {};
    const si = rec.accel_ms2 || {};
    const sensor = rec.sensor || {};

    // ---- 三轴实时读数（只来自接口） ----
    const gx = Number(g.x), gy = Number(g.y), gz = Number(g.z);
    $("valGX").textContent = fmt(g.x, 3);
    $("valGY").textContent = fmt(g.y, 3);
    $("valGZ").textContent = fmt(g.z, 3);
    $("valMag").textContent = fmt(rec.magnitude_g, 3);
    $("valMS2X").textContent = fmt(si.x, 2);
    $("valMS2Y").textContent = fmt(si.y, 2);
    $("valMS2Z").textContent = fmt(si.z, 2);

    state.rangeG = isNum(sensor.range_g) ? sensor.range_g : null;
    setBar("barX", gx, state.rangeG);
    setBar("barY", gy, state.rangeG);
    setBar("barZ", gz, state.rangeG);

    // ---- 姿态：pitch / roll / 与 Z 轴夹角（对接口 g 值做三角函数，可复算） ----
    const mag = Number(rec.magnitude_g);
    state.lastMagG = Number.isFinite(mag) ? mag : null;
    if (Number.isFinite(gx) && Number.isFinite(gy) && Number.isFinite(gz)) {
      const pitch = toDeg(Math.atan2(-gx, Math.sqrt(gy * gy + gz * gz)));
      const roll  = toDeg(Math.atan2(gy, gz));
      const n = Math.sqrt(gx * gx + gy * gy + gz * gz);
      $("attPitch").textContent = `${pitch.toFixed(1)}°`;
      $("attRoll").textContent  = `${roll.toFixed(1)}°`;
      $("attTiltZ").textContent = n > 1e-6 ? `${toDeg(Math.acos(Math.max(-1, Math.min(1, gz / n)))).toFixed(1)}°` : "—";
      $("attGvec").textContent = n > 1e-6
        ? `(${(gx / n).toFixed(3)}, ${(gy / n).toFixed(3)}, ${(gz / n).toFixed(3)})`
        : "—";
      state.sphereTarget = n > 1e-4 ? { x: gx / n, y: gy / n, z: gz / n } : null;
    } else {
      $("attPitch").textContent = "—";
      $("attRoll").textContent = "—";
      $("attTiltZ").textContent = "—";
      $("attGvec").textContent = "—";
      state.sphereTarget = null;
    }

    // ---- 姿态结论（判据 = |a| 与 1g 的偏差，全部由接口值算出） ----
    if (Number.isFinite(mag)) {
      const dev = Math.abs(mag - 1);
      if (dev <= 0.06) {
        $("attState").textContent = `静止 · |a| 偏差 ${dev.toFixed(3)} g`;
        $("attState").className = "chip chip-ok";
      } else if (dev <= 0.25) {
        $("attState").textContent = `轻微运动 · |a| 偏差 ${dev.toFixed(3)} g`;
        $("attState").className = "chip chip-warn";
      } else {
        $("attState").textContent = `运动中 · |a| 偏差 ${dev.toFixed(3)} g`;
        $("attState").className = "chip chip-bad";
      }
    } else {
      $("attState").textContent = "状态未知";
      $("attState").className = "chip";
    }

    $("sphereNote").textContent = state.sphereTarget
      ? "球面落点 = 重力方向在设备坐标系中的指向（由接口返回的 g 值归一化得到）。" +
        "落点做了纯视觉缓动（仅让画面过渡更顺），卡片里的数字始终精确等于接口返回值。"
      : "合矢量 ≈ 0 时重力方向无法定义，因此不显示落点（不做任何推测）。";

    // ---- 设备信息与时间同步 ----
    state.lastDeviceId = rec.device_id;
    $("dDeviceId").textContent = rec.device_id || "—";
    $("dMac").textContent = rec.mac ? String(rec.mac).toUpperCase() : "—";

    const i2c = (sensor.i2c_addr !== null && sensor.i2c_addr !== undefined)
      ? "0x" + Number(sensor.i2c_addr).toString(16).toUpperCase().padStart(2, "0") : "—";
    const chip = (sensor.chip_id !== null && sensor.chip_id !== undefined)
      ? "0x" + Number(sensor.chip_id).toString(16).toUpperCase().padStart(2, "0") : "—";
    $("dComm").textContent =
      `IMU 总线 I2C（地址 ${i2c} · CHIP_ID ${chip} · ODR ${sensor.odr_hz ?? "—"} Hz）｜` +
      `上报链路 Wi-Fi → HTTP POST /api/v1/ingest（后半句为固定说明，USB 仅作调试）`;

    $("dTsLocal").textContent = rec.ts_local || "（设备未对时）";
    $("dRecvTime").textContent = recvToLocalText(rec.received_at_utc);
    $("dUptime").textContent = (rec.uptime_ms !== null && rec.uptime_ms !== undefined)
      ? `${rec.uptime_ms} ms` : "—";
    $("dClock").textContent = clockText(rec.ts_local);

    // 时间偏移 = 服务器接收时间 − 板端 UTC 时间戳（优先用接口的 ts_utc_ms）
    const recvMs = Date.parse(rec.received_at_utc || "");
    let boardMs = isNum(rec.ts_utc_ms) ? rec.ts_utc_ms : NaN;
    if (Number.isNaN(boardMs) && rec.ts_local) {
      const p = Date.parse(rec.ts_local);
      if (!Number.isNaN(p)) boardMs = p;
    }
    const offEl = $("dOffset");
    if (!Number.isNaN(recvMs) && !Number.isNaN(boardMs)) {
      const off = recvMs - boardMs;
      const sign = off >= 0 ? "+" : "−";
      offEl.textContent = `${sign}${Math.abs(off)} ms · ${offsetVerdict(off)}`;
      offEl.className = off < 0 ? "off-bad" : (off <= 2000 ? "off-ok" : (off <= 6000 ? "off-warn" : "off-bad"));
      const chipEl = $("dSyncChip");
      chipEl.textContent = off < 0 ? "时钟需检查" : (off <= 2000 ? "时钟同步正常" : "时间偏移偏大");
      chipEl.className = "chip " + (off < 0 ? "chip-bad" : (off <= 2000 ? "chip-ok" : "chip-warn"));
    } else {
      offEl.textContent = "—";
      offEl.className = "";
      $("dSyncChip").textContent = "—";
      $("dSyncChip").className = "chip";
    }

    // ---- 数据来源与时间戳（三方核对） ----
    $("dSensor").textContent = `${sensor.model || "—"} (${sensor.type || "—"}) @${i2c} · CHIP_ID ${chip}`;
    $("dRaw").textContent =
      `x=${raw.x ?? "—"}  y=${raw.y ?? "—"}  z=${raw.z ?? "—"}  (14bit 有符号码值)`;
    $("dRange").textContent = `±${sensor.range_g ?? "—"} g / ${sensor.lsb_per_g ?? "—"} LSB per g`;
    $("dOver").textContent = sensor.oversample ? `${sensor.oversample} 次` : "—";
    // 一个包 10 个采样点：把包内序号也显示出来，方便与串口 [DATA] idx=K/N 对齐
    const idxTxt = (rec.sample_idx !== null && rec.sample_idx !== undefined)
      ? ` · 包内第 ${rec.sample_idx}/${rec.sample_count || "?"} 点` : "";
    $("dIdSeq").textContent =
      `#${rec.id ?? "—"} / seq=${rec.seq ?? "—"} / boot=${rec.boot_count ?? "—"}${idxTxt}`;
    // 胶囊只放简短结论（超长文本会把窄屏布局挤爆），完整说明放到卡片下方
    $("dVerify").textContent = rec.verify_ok === 1 ? "校验通过 ✔"
      : (rec.verify_ok === 0 ? "校验异常 ✘" : "未校验");
    $("dVerify").className = "chip " + (rec.verify_ok === 1 ? "chip-ok"
      : (rec.verify_ok === 0 ? "chip-bad" : ""));
    const vOk = (rec.verify_ok === null || rec.verify_ok === undefined) ? "—" : rec.verify_ok;
    $("dVerifyNote").textContent = rec.verify_note
      ? `后端自洽校验（verify_ok=${vOk}）：${rec.verify_note}`
      : `接口未给出校验结论（verify_ok=${vOk}），页面如实留空，不做推测。`;

    // 后端返回的原始 JSON（最新一条，逐字段原样）
    $("rawJson").textContent = JSON.stringify(rec, null, 2);

    flashCards();                    // 数值更新时轻微闪一下（纯视觉提示）
    drawChart(performance.now());    // 立刻画一帧，不必等到下一个动画帧
    drawSphere(performance.now());   // 球体也立刻重绘：不依赖渲染循环是否被浏览器节流
  }

  /** 数据新鲜度 + 断流提示 */
  function updateFreshness() {
    const linkOk = state.failCount === 0;
    $("linkStatus").textContent = linkOk ? "后端连接正常" : `后端请求失败 ${state.failCount} 次`;
    $("linkStatus").className = "pill " + (linkOk ? "pill-ok" : "pill-bad");

    const fresh = $("freshness");
    const banner = $("staleBanner");
    const dot = $("linkDot");

    if (state.lastRecvMs === null) {
      // 库里没有任何真实记录：如实说明，不编造数值
      fresh.textContent = "等待设备首次上传";
      fresh.className = "pill pill-wait";
      banner.classList.remove("hidden");
      $("staleDetail").textContent =
        " 后端数据库里还没有任何记录，请确认开发板已上电、已连上 Wi-Fi 并开始上传。";
      $("dAge").textContent = "—";
      dot.className = "dot " + (linkOk ? "dot-warn" : "dot-bad");
      return;
    }

    const age = Date.now() - state.lastRecvMs;
    $("dAge").textContent = ageText(age);

    if (age > STALE_MS) {
      fresh.textContent = "暂无新数据";
      fresh.className = "pill pill-bad";
      banner.classList.remove("hidden");
      $("staleDetail").textContent =
        ` 最后一次采集时间：${recvToLocalText(state.last.received_at_utc)}` +
        `（设备本地时间戳：${state.last.ts_local || "未对时"}），已停止更新 ${ageText(age)}。` +
        " 页面保留最后一次真实数值与时间戳。";
      document.body.classList.add("is-stale");   // 数值变灰，明确"这不是当前值"
      dot.className = "dot dot-warn";
      if (!state.wasStale) {
        state.wasStale = true;
        addLog("warn", `数据断流：已 ${ageText(age)}没有新的服务器接收时间，页面保留最后一次真实数值与时间戳`);
      }
    } else {
      fresh.textContent = `数据实时 (${ageText(age)})`;
      fresh.className = "pill pill-ok";
      banner.classList.add("hidden");
      document.body.classList.remove("is-stale");
      dot.className = "dot " + (linkOk ? "dot-ok" : "dot-bad");
      if (state.wasStale) {
        state.wasStale = false;
        addLog("ok", "数据已恢复：重新收到新的真实采样点");
      }
    }
  }

  // ==========================================================================
  // 数据获取
  // ==========================================================================
  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    const text = await r.text();
    let body = null;
    try { body = JSON.parse(text); } catch (e) { body = { ok: false, error: text.slice(0, 200) }; }
    return { status: r.status, body };
  }

  /** POST JSON（第 2 周新增：创建采集请求 / 暂停周期上报）。 */
  async function postJSON(url, obj) {
    const r = await fetch(url, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(obj),
    });
    const text = await r.text();
    let body = null;
    try { body = JSON.parse(text); } catch (e) { body = { ok: false, error: text.slice(0, 200) }; }
    return { status: r.status, body };
  }

  function recordsURL(extra) {
    const dev = state.deviceFilter ? `&device_id=${encodeURIComponent(state.deviceFilter)}` : "";
    return `${API.records}?${extra}${dev}`;
  }

  /**
   * 增量拉取：只取「比上次已收到的 id 更新」的记录。
   * 板端 10Hz 采样、1Hz 整包上传，如果每次只拉 /latest 一条，800ms 内新产生的
   * 8~10 个点会有 7~8 个被跳过。改成 after_id 增量拉取后，一个真实采样点都不漏。
   */
  async function pollNew() {
    if (!state.pollEnabled) { updateFreshness(); return; }
    state.pollCount++;
    try {
      const url = state.lastId === null
        ? recordsURL(`limit=${HISTORY}`)                                  // 首次：拉历史铺满窗口
        : recordsURL(`after_id=${state.lastId}&limit=${CHUNK}`);          // 之后：只拉新增
      const { status, body } = await getJSON(url);

      if (status === 200 && body && body.ok && Array.isArray(body.records)) {
        if (state.failCount > 0) addLog("ok", `后端请求恢复正常（此前失败 ${state.failCount} 次）`);
        state.failCount = 0;
        let newest = null, added = 0;
        for (const rec of body.records) {
          if (state.lastId !== null && rec.id <= state.lastId) continue;  // 去重
          appendPoint(rec);
          newest = rec;
          added++;
        }
        if (newest) {
          // 先累加计数，再渲染 —— window.__imuDebug 是在绘制里发布的
          state.addedTotal += added;
          renderRecord(newest);      // 一个批次只写一次 DOM（用最新那个点的数值）
        }
      } else if (status === 404) {
        // 数据库里确实没有数据：如实呈现，不伪造
        state.failCount = 0;
      } else {
        state.failCount++;
        if (state.failCount === 1) addLog("bad", `拉取记录失败：HTTP ${status}`);
      }
    } catch (e) {
      state.failCount++;
      if (state.failCount === 1) addLog("bad", `拉取记录异常：${(e && e.message) ? e.message : e}`);
    }
    updateFreshness();

    if (state.pollCount % 5 === 1) refreshStats();
  }

  async function seedHistory() {
    try {
      const { status, body } = await getJSON(recordsURL(`limit=${HISTORY}`));
      if (status === 200 && body && body.ok && body.records && body.records.length) {
        state.points = [];
        for (const r of body.records) appendPoint(r);
        const lastRec = body.records[body.records.length - 1];
        renderRecord(lastRec);
        $("apiInfo").textContent =
          `API: ${location.origin}${API.records}（同源，由后端直接提供页面）` +
          ` · 本次已装载 ${body.records.length} 个真实采样点` +
          `（每包 ${lastRec.sample_count || 1} 点 / ${lastRec.sample_period_ms || "?"}ms 一个点）`;
      }
    } catch (e) { /* 忽略：pollNew 会给出连接状态 */ }
  }

  async function refreshStats() {
    try {
      const { status, body } = await getJSON(API.stats);
      if (status === 200 && body && body.ok) {
        $("statInfo").textContent =
          `库统计: 记录 ${body.records} 条 / 原始报文 ${body.raw_uploads} 条 / 设备 ${body.devices} 台` +
          (body.last_received_at_utc ? ` · 最后接收 ${body.last_received_at_utc}` : "");
      }
    } catch (e) { /* 忽略 */ }
  }

  /** 设备分组下拉：列表来自后端真实上报过的设备（不去猜、不编造设备） */
  async function loadDevices() {
    try {
      const { status, body } = await getJSON(API.devices);
      if (status !== 200 || !body || !body.ok || !Array.isArray(body.devices)) return;
      const sel = $("deviceSelect");
      if (!sel) return;
      const total = body.devices.length;
      const opts = [`<option value="">全部设备（${total} 台）</option>`];
      for (const d of body.devices) {
        const mac = d.mac ? ` · ${String(d.mac).toUpperCase()}` : "";
        opts.push(`<option value="${esc(d.device_id)}">${esc(d.device_id)}${esc(mac)} · ${esc(d.n)} 条</option>`);
      }
      sel.innerHTML = opts.join("");
      if (state.deviceFilter) sel.value = state.deviceFilter;
      addLog("info", `设备列表已刷新：后端共有 ${total} 台设备上报过数据`);
    } catch (e) { /* 忽略：下拉保留"加载中"提示，不显示任何伪造设备 */ }
  }

  // ==========================================================================
  // 交互：快捷工具栏 / 抽屉
  // ==========================================================================
  function setPanel(id, open) {
    const el = $(id);
    if (!el) return;
    if (open) el.classList.remove("hidden"); else el.classList.add("hidden");
    state.openPanel = open ? id : null;
  }

  function togglePanel(id, btn) {
    const el = $(id);
    if (!el) return;
    const willOpen = el.classList.contains("hidden");
    setPanel(id, willOpen);
    if (btn) {
      if (willOpen) btn.classList.add("is-on"); else btn.classList.remove("is-on");
    }
    return willOpen;
  }

  function bindUI() {
    // 主题
    $("themeToggle").addEventListener("click", () => {
      applyTheme(state.theme === "dark" ? "light" : "dark", false);
    });

    // 设备分组
    $("deviceSelect").addEventListener("change", (e) => {
      const v = (e && e.target && e.target.value) ? String(e.target.value) : "";
      state.deviceFilter = v ? v : null;
      state.points = [];
      state.lastId = null;
      state.lastRecvMs = null;
      state.baseline = null;
      state.baselineId = null;
      state.wasStale = false;
      $("baseInfo").textContent = "基线：未设置";
      addLog("info", `切换设备分组：${v || "全部设备"}，按记录 id 重新装载曲线`);
      $("toolHint").textContent = `已切换设备分组：${v || "全部设备"}`;
      seedHistory().then(() => { loadDevices(); pollNew(); });
    });

    // ① 实时监控（暂停 / 继续拉取）
    $("btnLive").addEventListener("click", () => {
      state.pollEnabled = !state.pollEnabled;
      const b = $("btnLive");
      if (state.pollEnabled) {
        b.classList.add("is-on");
        $("toolHint").textContent = "已恢复实时监控：按记录 id 续传，暂停期间的点不会被丢掉";
        addLog("ok", "恢复实时监控（按 id 续传，暂停期间的采样点会补齐）");
      } else {
        b.classList.remove("is-on");
        $("toolHint").textContent = "已暂停拉取：页面保留最后一次真实数值与时间戳";
        addLog("warn", "暂停实时监控（停止拉取后端数据，页面数值保持最后一次真实值）");
      }
      updateFreshness();
    });

    // ② 数据导出
    $("btnExport").addEventListener("click", () => { exportCSV(); });

    // ③ 相机预览
    $("btnCamera").addEventListener("click", () => {
      const open = togglePanel("cameraPanel", $("btnCamera"));
      $("toolHint").textContent = open
        ? "相机预览：当前固件没有接入图像链路，这里只作如实说明"
        : "相机预览已收起";
    });

    // ④ 日志记录
    $("btnLog").addEventListener("click", () => {
      const open = togglePanel("logPanel", $("btnLog"));
      state.logPanelOpen = !!open;
      if (open) renderLogs();
      $("toolHint").textContent = open ? `日志记录：共 ${state.logs.length} 条` : "日志记录已收起";
    });
    $("closeLog").addEventListener("click", () => {
      setPanel("logPanel", false);
      state.logPanelOpen = false;
      $("btnLog").classList.remove("is-on");
    });
    $("btnLogClear").addEventListener("click", () => {
      state.logs = [];
      $("logCount").textContent = "0";
      renderLogs();
    });
    $("btnLogExport").addEventListener("click", () => { exportLogs(); });

    // ⑤ AI 自然语言交互
    $("btnAsk").addEventListener("click", () => {
      const open = togglePanel("aiPanel", $("btnAsk"));
      $("toolHint").textContent = open
        ? "AI 交互：本地规则解析，结果全部由已加载的真实采样点算出"
        : "AI 交互已收起";
    });
    $("closeAi").addEventListener("click", () => {
      setPanel("aiPanel", false);
      $("btnAsk").classList.remove("is-on");
    });
    $("closeCamera").addEventListener("click", () => {
      setPanel("cameraPanel", false);
      $("btnCamera").classList.remove("is-on");
    });
    $("aiSend").addEventListener("click", () => { sendAI(); });
    $("aiInput").addEventListener("keydown", (e) => { if (e && e.key === "Enter") sendAI(); });
    $("aiExamples").addEventListener("click", (e) => {
      const t = (e && e.target && e.target.textContent) ? e.target.textContent.trim() : "";
      if (!t) return;
      const inp = $("aiInput");
      if (inp) inp.value = t;
      sendAI();
    });

    // ── 图表控制 ──
    $("btnPause").addEventListener("click", () => {
      state.viewPaused = !state.viewPaused;
      if (state.viewPaused) {
        state.freezeRight = viewRightMs();
        $("btnPause").classList.add("is-on");
        $("btnPause").textContent = "继续";
        addLog("info", "图表已暂停：时间轴冻结（数据仍在按 id 续传，继续后一次性补齐）");
      } else {
        state.freezeRight = null;
        $("btnPause").classList.remove("is-on");
        $("btnPause").textContent = "暂停";
        addLog("info", "图表已继续：时间轴重新跟随服务器真实时间");
      }
    });

    $("btnBaseline").addEventListener("click", () => {
      if (state.baseline) {
        state.baseline = null;
        state.baselineId = null;
        $("btnBaseline").classList.remove("is-on");
        $("btnBaseline").textContent = "设基线";
        $("baseInfo").textContent = "基线：未设置";
        state.axis = null;
        addLog("info", "已清除基线：曲线恢复显示接口绝对值");
      } else {
        const last = state.points[state.points.length - 1];
        if (!last) {
          $("toolHint").textContent = "还没有真实采样点，无法设置基线（不编造基线值）";
          addLog("warn", "设基线失败：当前没有已加载的真实采样点");
          return;
        }
        state.baseline = {
          gx: last.gx, gy: last.gy, gz: last.gz, mag: last.mag,
          mx: last.mx, my: last.my, mz: last.mz, mm: last.mm,
        };
        state.baselineId = last.id;
        $("btnBaseline").classList.add("is-on");
        $("btnBaseline").textContent = "清除基线";
        $("baseInfo").textContent =
          `基线 #${last.id}（seq=${last.seq}）g=(${fmt(last.gx, 3)}, ${fmt(last.gy, 3)}, ${fmt(last.gz, 3)})` +
          ` · 曲线显示 Δ，读数卡片仍为绝对值`;
        state.axis = null;
        addLog("info", `以记录 #${last.id} 的接口值为基线（共 ${state.points.length} 个已加载采样点）`);
      }
    });

    $("btnAuto").addEventListener("click", () => {
      state.autoScroll = !state.autoScroll;
      const b = $("btnAuto");
      if (state.autoScroll) { b.classList.add("is-on"); state.freezeRight = state.viewPaused ? state.freezeRight : null; }
      else { b.classList.remove("is-on"); }
      state.axis = null;
      addLog("info", state.autoScroll
        ? "自动滚动已开启：横轴跟随真实时间"
        : "自动滚动已关闭：横轴铺满已加载的全部真实采样点");
    });

    $("unitG").addEventListener("click", () => { setUnit("g"); });
    $("unitMS2").addEventListener("click", () => { setUnit("ms2"); });

    $("toggleRaw").addEventListener("click", () => {
      const pre = $("rawJson");
      const open = pre.classList.contains("hidden");
      if (open) pre.classList.remove("hidden"); else pre.classList.add("hidden");
      state.rawOpen = open;
    });

    // ── 图表悬浮提示（显示最近的真实采样点，不插值） ──
    const cv = $("chart");
    cv.addEventListener("mousemove", (e) => {
      const rect = cv.getBoundingClientRect();
      state.hoverPx = e.clientX - rect.left;
      state.hoverPy = e.clientY - rect.top;
    });
    cv.addEventListener("mouseleave", () => {
      state.hoverPx = null;
      state.hoverPy = null;
      state.hoverIdx = -1;
      hideTip();
    });

    // 画布尺寸变化时才重新量取（避免每帧读布局）
    window.addEventListener("resize", markResize);
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(markResize).observe($("chart"));
    }
  }

  function setUnit(u) {
    if (state.unit === u) return;
    state.unit = u;
    $("unitG").className = "seg-btn" + (u === "g" ? " is-on" : "");
    $("unitMS2").className = "seg-btn" + (u === "ms2" ? " is-on" : "");
    state.axis = null;
    addLog("info", `图表单位切换为 ${u === "ms2" ? "m/s²" : "g"}（数值取自接口的对应字段）`);
  }

  function markResize() {
    state.needResize = true;
    state.sphereNeedSize = true;
  }

  // ==========================================================================
  // 数据导出 / 日志导出
  // ==========================================================================
  function downloadText(filename, text, mime) {
    if (typeof document.createElement !== "function" || typeof Blob === "undefined") {
      addLog("warn", "当前环境不支持浏览器下载，导出已取消（未生成任何文件）");
      $("toolHint").textContent = "导出取消：当前环境不支持浏览器下载";
      return false;
    }
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    return true;
  }

  function stamp() {
    const d = new Date();
    return `${d.getFullYear()}${pad2(d.getMonth() + 1)}${pad2(d.getDate())}_` +
           `${pad2(d.getHours())}${pad2(d.getMinutes())}${pad2(d.getSeconds())}`;
  }

  async function exportCSV() {
    const { status, body } = await getJSON(recordsURL("limit=2000"));
    if (status !== 200 || !body || !body.ok || !Array.isArray(body.records) || !body.records.length) {
      addLog("warn", `导出取消：接口没有返回可导出的真实记录（HTTP ${status}）`);
      $("toolHint").textContent = "导出失败：后端没有可导出的真实记录";
      return;
    }
    const head = ["id", "device_id", "mac", "boot_count", "seq", "sample_idx", "sample_count",
                  "sample_period_ms", "uptime_ms", "ts_local", "ts_utc_ms", "received_at_utc",
                  "raw_x", "raw_y", "raw_z", "gx", "gy", "gz", "ms2x", "ms2y", "ms2z",
                  "magnitude_g", "verify_ok"];
    const lines = [head.join(",")];
    for (const r of body.records) {
      const raw = r.raw || {}, g = r.accel_g || {}, m = r.accel_ms2 || {};
      lines.push([
        r.id, r.device_id, r.mac, r.boot_count, r.seq, r.sample_idx, r.sample_count,
        r.sample_period_ms, r.uptime_ms, `"${r.ts_local}"`, r.ts_utc_ms, `"${r.received_at_utc}"`,
        raw.x, raw.y, raw.z, g.x, g.y, g.z, m.x, m.y, m.z, r.magnitude_g, r.verify_ok,
      ].join(","));
    }
    const ok = downloadText(`imu_records_${stamp()}.csv`, "\uFEFF" + lines.join("\r\n"), "text/csv;charset=utf-8");
    if (ok) {
      addLog("ok", `已导出 ${body.records.length} 条真实记录为 CSV（含 raw / g / m-s² / 时间戳 / verify_ok）`);
      $("toolHint").textContent = `已导出 ${body.records.length} 条真实记录（CSV）`;
    }
  }

  function exportLogs() {
    if (!state.logs.length) {
      $("toolHint").textContent = "日志为空，无可导出内容";
      return;
    }
    const lines = state.logs.map((l) => `${new Date(l.t).toISOString()}\t${l.kind}\t${l.msg}`);
    const ok = downloadText(`imu_page_log_${stamp()}.txt`, lines.join("\r\n"), "text/plain;charset=utf-8");
    if (ok) {
      addLog("ok", `已导出本页面 ${state.logs.length} 条运行日志`);
      renderLogs();
    }
  }

  // ==========================================================================
  // ⑤ AI 自然语言交互（本地规则解析，结果全部由已加载的真实采样点算出）
  // ==========================================================================
  function pickAxis(q) {
    if (/合矢量|合力|模长|magnitude|\|a\||norm/i.test(q)) return "mag";
    // 「X 轴 / x轴 / X方向 / X axis」都认（允许字母与"轴"之间有空格，大小写不敏感）
    const m = /([xyz])\s*(?:轴|方向|axis)/i.exec(q);
    if (m) return m[1].toLowerCase();
    const s = /(?:轴|方向)\s*([xyz])/i.exec(q);
    if (s) return s[1].toLowerCase();
    // 兜底：只写了单个轴字母（避免误伤 0x12 这类十六进制，要求前后不是字母数字）
    const lone = /(?:^|[^a-z0-9])([xyz])(?:[^a-z0-9]|$)/i.exec(q);
    if (lone) return lone[1].toLowerCase();
    return null;
  }

  function pickRangeSeconds(q) {
    const m = /最近\s*(\d+(?:\.\d+)?)\s*(秒|s\b|分钟|min|分)/i.exec(q);
    if (!m) return null;
    const n = parseFloat(m[1]);
    if (!Number.isFinite(n)) return null;
    return /分|min/i.test(m[2]) ? n * 60 : n;
  }

  function answerAI(q) {
    const pts = state.points;
    if (!pts.length) {
      return "当前还没有加载到任何真实采样点，无法计算。\n" +
             "（本页面不会为了回答问题而编造数值；请等开发板上传数据后重试。）";
    }
    const rangeS = pickRangeSeconds(q);
    const newestT = pts[pts.length - 1].t;
    const from = rangeS === null ? null : newestT - rangeS * 1000;
    const sel = from === null ? pts : pts.filter((p) => p.t >= from);
    if (!sel.length) {
      return `「最近 ${rangeS} 秒」内没有已加载的真实采样点（当前曲线窗口 ${WINDOW_S}s）。` +
             `可以把曲线窗口调大（URL 参数 ?window=120）后重试。`;
    }

    const firstT = sel[0].t, lastT = sel[sel.length - 1].t;
    const prov = `\n\n数据来源：以上全部由接口返回的真实采样点算出 —— 样本 ${sel.length} 个，` +
                 `服务器接收时间 ${hhmmss3(new Date(firstT))} ~ ${hhmmss3(new Date(lastT))}` +
                 `（记录 #${sel[0].id} ~ #${sel[sel.length - 1].id}，未做插值或推测）。`;

    // 倾角：直接用最新一条接口值算
    if (/倾角|角度|姿态|倾斜/.test(q)) {
      const last = pts[pts.length - 1];
      const n = Math.sqrt(last.gx * last.gx + last.gy * last.gy + last.gz * last.gz);
      if (!(n > 1e-6)) return "最新一条记录的合矢量 ≈ 0，重力方向无法定义，因此无法给出倾角。" + prov;
      const pitch = toDeg(Math.atan2(-last.gx, Math.sqrt(last.gy * last.gy + last.gz * last.gz)));
      const roll  = toDeg(Math.atan2(last.gy, last.gz));
      const tiltZ = toDeg(Math.acos(Math.max(-1, Math.min(1, last.gz / n))));
      return `最新一条记录（#${last.id}，seq=${last.seq}）：\n` +
             `· 俯仰角 pitch = ${pitch.toFixed(1)}°\n` +
             `· 横滚角 roll = ${roll.toFixed(1)}°\n` +
             `· Z 轴与重力夹角 = ${tiltZ.toFixed(1)}°\n` +
             `· 合矢量 |a| = ${fmt(last.mag, 3)} g（m/s² 未在接口里给出合矢量值，故不换算显示）` + prov;
    }

    const axis = pickAxis(q);
    // ★ 注意：曲线点的字段名是 gx/gy/gz/mag（g 单位）与 mx/my/mz/mm（m/s²）
    const key  = axis === "x" ? "gx" : axis === "y" ? "gy" : axis === "z" ? "gz" : "mag";
    const mkey = axis === "x" ? "mx" : axis === "y" ? "my" : axis === "z" ? "mz" : "mm";
    const axisName = axis === "x" ? "X 轴" : axis === "y" ? "Y 轴" : axis === "z" ? "Z 轴" : "合矢量 |a|";

    if (/多少|点数|个数|几个|数量/.test(q)) {
      return `当前范围内共有 <b>${sel.length}</b> 个真实采样点（${axisName} 只是范围说明，不影响点数）。` +
             `\n曲线时间窗口 ${WINDOW_S}s；板端 10Hz 采样、1Hz 整包上传。` + prov;
    }

    const vals = [], times = [];
    for (const p of sel) {
      const v = p[key];
      if (Number.isFinite(v)) { vals.push(v); times.push(p.t); }
    }
    if (!vals.length) return `${axisName} 在所选范围内没有有效数值（接口未给出该字段），无法计算。` + prov;

    let sum = 0, mn = Infinity, mx = -Infinity, mnAt = 0, mxAt = 0;
    for (let i = 0; i < vals.length; i++) {
      const v = vals[i];
      sum += v;
      if (v < mn) { mn = v; mnAt = i; }
      if (v > mx) { mx = v; mxAt = i; }
    }
    const mean = sum / vals.length;
    let varSum = 0;
    for (const v of vals) { const d = v - mean; varSum += d * d; }
    const sd = Math.sqrt(varSum / vals.length);

    const msVals = [];
    if (axis && axis !== "mag") {
      for (const p of sel) { const v = p[mkey]; if (Number.isFinite(v)) msVals.push(v); }
    }
    let msSum = 0;
    for (const v of msVals) msSum += v;
    const msMean = msVals.length ? msSum / msVals.length : NaN;

    if (/峰峰|peak|p2p/i.test(q)) {
      let msTxt = "";
      if (msVals.length) {
        let mnM = Infinity, mxM = -Infinity;
        for (const v of msVals) { if (v < mnM) mnM = v; if (v > mxM) mxM = v; }
        msTxt = `\n同一范围 ${axisName} 的 m/s² 峰峰值 = ${(mxM - mnM).toFixed(3)} m/s²` +
                `（取自接口的 accel_ms2 字段）`;
      }
      return `${axisName} 峰峰值 = <b>${(mx - mn).toFixed(4)} g</b>` +
             `（最大 ${mx.toFixed(4)} g，最小 ${mn.toFixed(4)} g）` + msTxt + prov;
    }
    if (/标准差|std|sigma/i.test(q)) {
      return `${axisName} 标准差 = <b>${sd.toFixed(5)} g</b>（均值 ${mean.toFixed(4)} g，样本 ${vals.length} 个）` + prov;
    }
    if (/最大|max/i.test(q)) {
      return `${axisName} 最大值 = <b>${mx.toFixed(4)} g</b>（出现在 ${hhmmss3(new Date(times[mxAt]))}）` + prov;
    }
    if (/最小|min/i.test(q)) {
      return `${axisName} 最小值 = <b>${mn.toFixed(4)} g</b>（出现在 ${hhmmss3(new Date(times[mnAt]))}）` + prov;
    }
    // 默认给平均值
    return `${axisName} 平均值 = <b>${mean.toFixed(4)} g</b>` +
           (Number.isFinite(msMean) ? ` / <b>${msMean.toFixed(3)} m/s²</b>（两个数值分别取自接口的 accel_g 与 accel_ms2 字段）` : "") +
           `\n同一范围内的极值：最小 ${mn.toFixed(4)} g，最大 ${mx.toFixed(4)} g，标准差 ${sd.toFixed(5)} g。` + prov;
  }

  function sendAI() {
    const inp = $("aiInput");
    const q = (inp && inp.value) ? String(inp.value).trim() : "";
    if (!q) {
      $("aiAnswer").textContent = "请输入问题，例如「最近 30 秒 X 轴平均值」。";
      return;
    }
    $("aiAnswer").textContent = answerAI(q);
    addLog("info", `AI 问答：「${q}」`);
    $("toolHint").textContent = "AI 已作答（本地规则解析，样本全部为已加载的真实采样点）";
  }

  // ==========================================================================
  // 第 2 周：远程采集（刷新已存数据 vs 采集最新数据）
  // ==========================================================================
  /**
   * ★ 本区块只做两件事：把用户意图变成正确的接口调用；把后端返回的 status
   *   原样映射成徽标。**页面绝不自己造状态**，也绝不把 acked 显示成完成。
   */
  const COLLECT = {
    create: "/api/v1/collect_requests",
    list:   "/api/v1/collect_requests",
    pause:  "/api/v1/device/pause",
  };

  /** 请求状态 → 中文徽标/颜色。判据与 docs/06 §6.2 的表一一对应。 */
  const REQ_STATUS_TEXT = {
    submitted:  { text: "已提交",       cls: "pill-wait" },
    acked:      { text: "设备接收回执", cls: "pill-info" },
    collecting: { text: "设备采集中",   cls: "pill-info" },
    completed:  { text: "完成",         cls: "pill-ok" },
    failed:     { text: "失败",         cls: "pill-bad" },
    timeout:    { text: "超时",         cls: "pill-warn" },
    rejected:   { text: "请求被拒",     cls: "pill-bad" },
    cancelled:  { text: "已撤销",       cls: "pill-wait" },
  };

  // 采集相关的页面状态
  const cstate = {
    requestId: null,        // 当前正在跟踪的请求
    pollUrl: null,          // 用创建响应给的 poll_url 轮询（不自造路径）
    timer: null,            // 1s 轮询句柄
    clientKey: null,        // 幂等键：同一意图恒定，双击不产生第二条
    lastKeyAt: 0,
    paused: false,
    inflight: false,        // 防连点：提交期间不允许再次点击
  };

  function setBadge(st, extraText) {
    const el = $("reqBadge");
    if (!el) return;
    if (!st || !REQ_STATUS_TEXT[st]) {
      // 拿不到 status 时显示占位，**不猜**成任何业务状态
      el.textContent = "—（未知状态）";
      el.className = "pill pill-wait";
      return;
    }
    const m = REQ_STATUS_TEXT[st];
    el.textContent = extraText ? `${m.text}${extraText}` : m.text;
    el.className = `pill ${m.cls}`;
  }

  /**
   * 把后端 status 翻译成用户能看懂的一句话。
   * ★ 红线：timeout 只能说"没等到"，**禁止**说成"设备故障/传感器损坏"
   *   —— 超时是服务端计时结论，硬件故障必须有设备侧的 rejected/SENSOR_ERROR 证据。
   */
  function describeStatus(d, st) {
    if (st === "submitted") {
      return "请求已落库，等待设备下一次轮询取走指令（设备 1Hz 轮询，通常 1 秒内被取走）。";
    }
    if (st === "acked") {
      return "设备已回执「收到指令」。注意：<b>这只是收到，还不算采集完成</b>——观测数据尚未回传。";
    }
    if (st === "collecting") {
      return "设备已开始读取传感器，观测数据回传中（尚未完成）。";
    }
    if (st === "completed") {
      return "已完成：本次观测通过 L1 身份 ∧ L2 新旧性 ∧ L3 时间性 三层校验，数值来自设备<b>当下</b>的寄存器读。";
    }
    if (st === "failed") {
      const why = (d && d.failure_reason) || "未知";
      if (why === "SENSOR_ERROR") {
        return "失败：<b>设备明确拒绝——传感器读取失败</b>。这才是硬件问题的证据（设备自己说的）。";
      }
      return `失败：设备拒绝执行（${esc(why)}）。`;
    }
    if (st === "timeout") {
      // 在线/离线两种情形文案不同，但都不得断言"硬件故障"
      const online = d && d.device &&
        (d.device.online === true || d.device.last_status === "online");
      return online
        ? "超时：<b>设备在线，但未在 15s 内回报</b>（可能指令未执行或回报丢失）。这不等于设备故障。"
        : "超时：<b>设备当前离线</b>（可能关机或断网），本次请求超时。离线包含关机与断网两种，无法据此判定硬件状态。";
    }
    if (st === "cancelled") {
      const why = (d && d.failure_reason) || "";
      return why === "SUPERSEDED"
        ? "已撤销：这条请求被更新的一次采集取代（同类型指令只执行最新一条）。"
        : "已撤销：操作者取消了本次请求。";
    }
    return "状态：" + esc(String(st));
  }

  /** 渲染请求详情（时间线 + 说明 + 观测数值 + 拒收记录）。 */
  function renderRequestDetail(d) {
    const st = d.status;
    setBadge(st, d.deduplicated ? "（去重）" : "");

    // ---- 时间线：提交 → 下发 → 回执 → 完成 ----
    const parts = [];
    if (d.submitted_at_utc)  parts.push(`提交 ${hhmmss3(new Date(d.submitted_at_utc))}`);
    if (d.dispatched_at_utc) parts.push(`下发 ${hhmmss3(new Date(d.dispatched_at_utc))}`);
    if (d.acked_at_utc)      parts.push(`回执 ${hhmmss3(new Date(d.acked_at_utc))}`);
    if (d.completed_at_utc)  parts.push(`完成 ${hhmmss3(new Date(d.completed_at_utc))}`);
    const e = d.elapsed_ms || {};
    if (Number.isFinite(e.submit_to_complete)) parts.push(`总耗时 ${e.submit_to_complete}ms`);
    $("reqTimeline").textContent = parts.length ? parts.join(" → ") : "—";

    let html = describeStatus(d, st);

    // 被拒收的观测：必须显式呈现，这是"旧记录不可改写为本次完成"的可见证据
    if (d.rejections && d.rejections.length) {
      html += `<div class="req-reject"><b>⚠ 有 ${d.rejections.length} 条观测被拒收</b>` +
        `<span class="req-reject-note">被拒收的观测不会改变请求状态，只留痕。</span><ul>` +
        d.rejections.map((r) =>
          `<li><code>${esc(r.reason_code)}</code> · ${esc(r.reason_detail || "")}` +
          ` <small>${esc(r.received_at_utc || "")}</i></small></li>`).join("") +
        `</ul></div>`;
    }
    $("reqDetail").innerHTML = html;

    // ---- 观测数值（只有真的完成了才有）----
    const obs = d.observation;
    const box = $("reqObservation");
    if (!obs) {
      box.classList.add("hidden");
      box.innerHTML = "";
      return;
    }
    box.classList.remove("hidden");
    const raw = obs.raw || {};
    const ag = obs.accel_g || {};
    const am = obs.accel_ms2 || {};
    box.innerHTML = `
      <div class="req-obs-head">
        <b>本次新观测</b>
        <span class="req-obs-id">request_id <code>${esc(d.request_id)}</code></span>
      </div>
      <div class="req-obs-grid">
        <div><label>合矢量 |a|</label><b>${fmt(obs.magnitude_g, 6)}</b><small>g</small></div>
        <div><label>原始码值 raw</label><b>(${fmt(raw.x, 0)}, ${fmt(raw.y, 0)}, ${fmt(raw.z, 0)})</b><small>LSB</small></div>
        <div><label>accel_g</label><b>(${fmt(ag.x, 4)}, ${fmt(ag.y, 4)}, ${fmt(ag.z, 4)})</b><small>g</small></div>
        <div><label>accel_ms2</label><b>(${fmt(am.x, 3)}, ${fmt(am.y, 3)}, ${fmt(am.z, 3)})</b><small>m/s²</small></div>
        <div><label>seq_first → seq_last</label><b>${fmt(obs.seq_first, 0)} → ${fmt(obs.seq_last, 0)}</b><small>相对基线递增</small></div>
        <div><label>新鲜度 freshness</label><b>${obs.freshness === "new_after_reboot" ? "重启后新采" : "同次开机内新采"}</b><small>${esc(obs.freshness || "")}</small></div>
        <div><label>后端自洽校验</label><b>${obs.verify_ok === 1 ? "通过" : "见备注"}</b><small>verify_ok=${fmt(obs.verify_ok, 0)}</small></div>
        <div><label>落库引用</label><b>obs#${fmt(obs.observation_id, 0)} / rec#${fmt(obs.record_id, 0)} / upl#${fmt(obs.upload_id, 0)}</b><small>三表联查</small></div>
      </div>`;
  }

  /** 请求列表：动作执行追踪（连点两次应当看到两条）。 */
  async function loadRequestList() {
    const tbody = $("reqList");
    if (!tbody) return;
    const dev = state.deviceFilter ? `?device_id=${encodeURIComponent(state.deviceFilter)}&limit=50` : "?limit=50";
    try {
      const { status, body } = await getJSON(COLLECT.list + dev);
      if (status !== 200 || !body || !body.ok) {
        tbody.innerHTML = `<tr><td colspan="7" class="req-empty">列表读取失败（HTTP ${status}）</td></tr>`;
        return;
      }
      const rows = body.requests || [];
      if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="7" class="req-empty">还没有采集记录</td></tr>`;
        return;
      }
      tbody.innerHTML = rows.map((r) => {
        const m = REQ_STATUS_TEXT[r.status] || { text: r.status, cls: "pill-wait" };
        const obs = r.observation;
        const obsTxt = obs
          ? `|a|=${fmt(obs.magnitude_g, 4)}g · seq ${fmt(obs.seq_first, 0)}→${fmt(obs.seq_last, 0)}`
          : "—";
        const rej = r.rejections_count ? `<span class="req-rej-badge">${r.rejections_count}</span>` : "0";
        return `<tr data-rid="${esc(r.request_id)}">
          <td>${r.submitted_at_utc ? hhmmss3(new Date(r.submitted_at_utc)) : "—"}</td>
          <td><code class="req-rid" title="${esc(r.request_id)}">${esc(String(r.request_id).slice(0, 8))}…</code></td>
          <td><span class="pill ${m.cls}">${m.text}</span></td>
          <td>${r.acked_at_utc ? hhmmss3(new Date(r.acked_at_utc)) : "—"}</td>
          <td>${r.completed_at_utc ? hhmmss3(new Date(r.completed_at_utc)) : "—"}</td>
          <td>${rej}</td>
          <td class="req-obs-cell">${obsTxt}</td>
        </tr>`;
      }).join("");

      // 点列表任意一行 → 把详情切到那条请求（便于复盘"连点两次"各自的终局）
      tbody.querySelectorAll("tr[data-rid]").forEach((tr) => {
        tr.addEventListener("click", () => {
          const rid = tr.getAttribute("data-rid");
          cstate.requestId = rid;
          cstate.pollUrl = COLLECT.create + "/" + rid;
          pollRequestOnce();
          addLog("info", `追踪到请求 ${rid.slice(0, 8)}…（来自执行追踪列表）`);
        });
      });
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="7" class="req-empty">列表读取异常</td></tr>`;
    }
  }

  /** 轮询一次当前请求详情。 */
  async function pollRequestOnce() {
    if (!cstate.pollUrl) return null;
    try {
      const { status, body } = await getJSON(cstate.pollUrl);
      if (status === 404) {
        setBadge(null);
        $("reqDetail").textContent = "该请求不存在或已被清理。";
        stopRequestPoll();
        return null;
      }
      if (status !== 200 || !body || !body.ok) return null;
      const d = body.data;
      renderRequestDetail(d);
      // 终态 → 停止轮询（不再打后端）
      if (["completed", "failed", "timeout", "rejected", "cancelled"].includes(d.status)) {
        stopRequestPoll();
        loadRequestList();
      }
      return d;
    } catch (e) {
      return null;
    }
  }

  function startRequestPoll() {
    stopRequestPoll();
    cstate.timer = setInterval(pollRequestOnce, 1000);
  }

  function stopRequestPoll() {
    if (cstate.timer) {
      clearInterval(cstate.timer);
      cstate.timer = null;
    }
  }

  /**
   * 点「采集最新数据」：创建请求 → 跟踪状态。
   *
   * ★ 幂等键：2 秒内重复点击复用同一个 client_request_key，后端据此返回
   *   **同一个** request_id（deduplicated:true），所以"手抖双击"不会产生两条任务；
   *   超过 2 秒的刻意连点则生成两条独立请求，各自留记录。
   */
  async function createCollectRequest() {
    if (cstate.inflight) return;                      // 本地也防一手
    const devId = state.deviceFilter || ($("deviceSelect") && $("deviceSelect").value) || "";
    if (!devId) {
      $("reqDetail").innerHTML =
        "请先在右上角「设备分组」里选定一台设备：<b>采集是按设备下发的</b>，不指定设备无法创建任务。";
      addLog("warn", "未选择设备，采集请求未创建");
      return;
    }

    const now = Date.now();
    if (!cstate.clientKey || now - cstate.lastKeyAt > 2000) {
      // 新的一次意图 → 新 key；同一意图内的重复点击复用旧 key
      cstate.clientKey = `web-${devId}-${now}`;
      cstate.lastKeyAt = now;
    }

    cstate.inflight = true;
    $("collectBtn").disabled = true;
    try {
      const { status, body } = await postJSON(COLLECT.create, {
        device_id: devId,
        sensor: "qma6100p",
        requested_by: "web",
        client_request_key: cstate.clientKey,
        note: "网页点击采集最新数据",
      });

      if (status === 201 || status === 200) {
        const d = body.data;
        cstate.requestId = d.request_id;
        cstate.pollUrl = d.poll_url || (COLLECT.create + "/" + d.request_id);
        addLog("info",
          `${d.deduplicated ? "重复点击已合并到同一条请求" : "已创建采集请求"} ` +
          `${d.request_id}（超时 ${d.timeout_ms}ms，基线 seq=${d.baseline ? d.baseline.seq_last : "?"}）`);
        $("toolHint").textContent = "采集请求已提交，正在跟踪设备回执与观测…";
        await pollRequestOnce();
        startRequestPoll();
        loadRequestList();
      } else {
        // 失败原因如实呈现（例如 NO_BASELINE / NO_DEVICE），不吞掉
        const code = (body && body.error && body.error.code) || "未知";
        const msg  = (body && body.error && body.error.message) || "";
        setBadge(null);
        $("reqDetail").innerHTML =
          `<b>创建失败</b>：<code>${esc(code)}</code> ${esc(msg)}` +
          (code === "NO_BASELINE"
            ? "<br>设备还没有轮询过，后端拿不到基线快照。请等待设备首次轮询（上电后约 1 秒）再试。"
            : "");
        addLog("bad", `创建采集请求失败：${code} ${msg}（HTTP ${status}）`);
      }
    } catch (e) {
      addLog("bad", `创建采集请求异常：${(e && e.message) ? e.message : e}`);
    } finally {
      cstate.inflight = false;
      $("collectBtn").disabled = false;
    }
  }

  /**
   * 点「刷新已存数据」：只读库，绝不触碰任何 collect_* 接口。
   *
   * ★ 这里**故意不刷新**动作执行追踪列表：那个列表也属于 collect_requests 接口族，
   *   而「刷新已存数据」的语义是"只读已入库的传感记录、零设备侧流量"。
   *   要更新追踪列表请用列表自己的「刷新列表」按钮。
   *   （测试用例 TC-W2-01 会断言：点刷新后 collect_requests 调用数增量必须为 0。）
   */
  async function refreshStoredData() {
    addLog("info", "刷新已存数据：只调 GET /api/v1/records 与 /api/v1/latest（零设备侧流量）");
    $("toolHint").textContent = "正在刷新已存数据…";
    await seedHistory();
    await pollNew();
    await refreshStats();
    await loadDevices();
    $("toolHint").textContent = "已刷新已存数据（数据库内容，未向设备下发任何指令）";
  }

  /** 暂停/恢复周期上报。 */
  async function togglePauseReports() {
    const devId = state.deviceFilter || ($("deviceSelect") && $("deviceSelect").value) || "";
    if (!devId) {
      addLog("warn", "未选择设备，暂停/恢复未执行");
      return;
    }
    const enable = cstate.paused;      // 当前是暂停态 → 这次点就是恢复
    try {
      const { status, body } = await postJSON(COLLECT.pause, {
        device_id: devId,
        enable: enable,
        reason: "网页手动切换周期上报",
      });
      if (status === 200 && body.ok) {
        cstate.paused = !enable;
        const b = $("pauseBtn");
        b.classList.toggle("tool-off", cstate.paused);
        b.querySelector("span").textContent = cstate.paused ? "恢复周期上报" : "暂停周期上报";
        addLog("info",
          `${cstate.paused ? "已请求暂停" : "已请求恢复"}周期上报（指令已入队，等待设备轮询取走）`);
        $("toolHint").textContent = cstate.paused
          ? "已请求暂停周期上报：周期包应停止增长，但「采集最新数据」仍然可用"
          : "已请求恢复周期上报";
        loadDevices();
      } else {
        const code = (body && body.error && body.error.code) || "未知";
        addLog("bad", `暂停/恢复失败：${code}（HTTP ${status}）`);
      }
    } catch (e) {
      addLog("bad", `暂停/恢复异常：${(e && e.message) ? e.message : e}`);
    }
  }

  function bindCollectUI() {
    if (!$("collectBtn")) return;     // 页面被裁剪时不影响其它功能
    $("refreshBtn").addEventListener("click", refreshStoredData);
    $("collectBtn").addEventListener("click", createCollectRequest);
    $("pauseBtn").addEventListener("click", togglePauseReports);
    $("reqListRefresh").addEventListener("click", loadRequestList);

    // 设备分组变化时，重新拉一次该设备的追踪列表
    const sel = $("deviceSelect");
    if (sel) sel.addEventListener("change", () => { loadRequestList(); updateCollectChip(); });

    updateCollectChip();
    loadRequestList();
  }

  function updateCollectChip() {
    const el = $("collectDeviceChip");
    if (!el) return;
    const devId = state.deviceFilter || ($("deviceSelect") && $("deviceSelect").value) || "";
    el.textContent = devId ? `目标设备 ${devId}` : "未选择设备";
  }

  // ==========================================================================
  // 启动
  // ==========================================================================
  initTheme();

  $("pollInfo").textContent =
    `数据: 每 ${POLL_MS} ms 增量拉取一次（按 id 续传，一个采样点都不漏）· ` +
    `曲线窗口 ${WINDOW_S}s · 可用 ?poll= &window= &history= 调整，` +
    `超过 ${STALE_MS} ms 无新数据即提示断流`;

  // 默认展开原始 JSON：它是"最新一条"的逐字段原样，方便随时核对
  state.rawOpen = true;
  $("rawJson").classList.remove("hidden");

  bindUI();
  bindCollectUI();
  renderLogs();

  loadDevices();
  seedHistory().then(() => {
    pollNew();
    setInterval(pollNew, POLL_MS);          // ★ 必须是第一个 setInterval（自检脚本按序号触发轮询）
    requestAnimationFrame(renderLoop);      // ★ 开启平滑渲染循环（跟随显示器刷新率）
  });
})();