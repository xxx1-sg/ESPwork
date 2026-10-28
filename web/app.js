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
    nl:      "/api/v1/nl/query",  // 第 4 周：受限工具语言助手
    nlTools: "/api/v1/nl/tools",
    // ---- 第 5 周：语音链路 ----
    voiceTurn:    "/api/v1/voice/turn",     // POST body=WAV
    voiceTurns:   "/api/v1/voice/turns",
    voiceHealth:  "/api/v1/voice/health",
    // ---- 第 6 周：会话 / 取消 / 澄清 ----
    voiceText:    "/api/v1/voice/text",     // POST JSON（澄清时用键盘补充）
    voiceSession: "/api/v1/voice/session",  // POST 建会话；/<sid>/cancel|state|reset
    voiceSessions:"/api/v1/voice/sessions",
    voiceTimeline:"/api/v1/voice/timeline",
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
  // ⑥ 相机预览（第 4 周：板载 OV2640）
  //
  // 数据来源与"不造假"边界：
  //   · 画面：GET /api/v1/cam/frame 返回的**原始 JPEG**，浏览器直接当作图片显示，
  //     前端不做任何滤镜、不做插帧、不缓存旧帧冒充新帧；
  //   · 元数据（分辨率/序号/字节数/收到时间）全部来自 /api/v1/cam/status；
  //   · 没有帧（后端 404 / has_frame=false）时显示「还没有画面」，
  //     绝不画占位图或循环播放上一次的画面。
  //
  // 为什么用「状态轮询 + 按需取图」而不是直接给 <img> 定时换 src：
  //   换 src 会让浏览器每 400ms 重新下载一次整帧（即使设备没出新帧）；
  //   先看 frame_no 变了才取图，静止场景下能省掉绝大部分流量。
  // ==========================================================================
  const CAM = {
    status:  "/api/v1/cam/status",
    frame:   "/api/v1/cam/frame",
    capture: "/api/v1/cam/capture",
    photos:  "/api/v1/cam/photos",
  };

  const cam = {
    timer: null,          // 状态轮询定时器
    paused: false,        // 「暂停画面」：只停取新帧，不清空当前画面
    deviceId: null,       // 当前显示哪台设备（null = 自动挑第一台有帧的）
    devices: [],          // 后端报告的"有画面的设备"
    lastFrameNo: null,    // 后端帧号（判断"是否有新帧"的唯一依据）
    lastSeq: null,
    shownAt: [],          // 最近几次真正显示新帧的时刻（算网页刷新率）
    photoIds: [],         // 已渲染过的照片 id（用于拍照后等新照片）
    shotBusy: false,
    loading: false,
    placeholder: true,
  };

  const camEl = (id) => $(id);

  function camDeviceId() {
    if (state.deviceFilter) return state.deviceFilter;
    if (cam.deviceId && cam.devices.indexOf(cam.deviceId) >= 0) return cam.deviceId;
    return cam.devices[0] || null;
  }

  function camSetBadge(text, cls) {
    const b = camEl("camLiveBadge");
    if (!b) return;
    b.textContent = text;
    b.className = "cam-badge" + (cls ? " " + cls : "");
  }

  function camShowPlaceholder(title, sub) {
    cam.placeholder = true;
    const ph = camEl("camPlaceholder");
    const img = camEl("camImg");
    if (ph) ph.classList.remove("hidden");
    if (img) img.classList.add("hidden");
    const t = camEl("camPhTitle"), s = camEl("camPhSub");
    if (t) t.textContent = title;
    if (s) s.textContent = sub;
    camSetBadge("● 等待画面", "");
  }

  function camShowImage() {
    cam.placeholder = false;
    const ph = camEl("camPlaceholder");
    const img = camEl("camImg");
    if (ph) ph.classList.add("hidden");
    if (img) img.classList.remove("hidden");
  }

  function camSetMeta(f) {
    const dev = camEl("camDevice");
    if (dev) dev.textContent = f.device_id || camDeviceId() || "—";
    const size = camEl("camSize");
    if (size) size.textContent = (f.w && f.h) ? `${f.w}×${f.h}` : "—";
    const seq = camEl("camSeq");
    if (seq) seq.textContent = (f.seq === null || f.seq === undefined) ? "—" : String(f.seq);
    const bytes = camEl("camBytes");
    if (bytes) bytes.textContent = f.bytes ? `${(f.bytes / 1024).toFixed(1)} KB` : "—";
    const recv = camEl("camRecv");
    if (recv) recv.textContent = f.received_at_utc ? recvToLocalText(f.received_at_utc) : "—";
  }

  function camNoteFps() {
    const now = Date.now();
    cam.shownAt.push(now);
    if (cam.shownAt.length > 8) cam.shownAt.shift();
    const el = camEl("camFps");
    if (!el) return;
    if (cam.shownAt.length < 2) { el.textContent = "首帧"; return; }
    const span = (cam.shownAt[cam.shownAt.length - 1] - cam.shownAt[0]) / 1000;
    el.textContent = span > 0 ? `${((cam.shownAt.length - 1) / span).toFixed(1)} fps` : "—";
  }

  /** 真正把新帧显示出来（先预加载，再替换，避免切换瞬间白屏/闪烁）。 */
  function camDisplayFrame(dev, f) {
    const url = `${CAM.frame}?device_id=${encodeURIComponent(dev)}&t=${f.frame_no}`;
    const img = camEl("camImg");
    if (!img) return;

    const apply = () => {
      img.src = url;
      camShowImage();
      camSetBadge("● 实时画面", "live");
      cam.lastFrameNo = f.frame_no;
      cam.lastSeq = f.seq;
      camNoteFps();
    };

    // Node 自检环境没有 Image 构造器：退化为直接赋值（自检只断言接口行为，不渲染）
    if (typeof Image === "undefined") { apply(); return; }
    const pre = new Image();
    pre.onload = apply;
    pre.onerror = () => {
      camShowPlaceholder("画面加载失败", "后端返回了 JPEG，但浏览器没能解码。刷新页面或点「刷新照片」重试。");
    };
    pre.src = url;
  }

  async function camPoll() {
    if (cam.paused || cam.loading) return;
    cam.loading = true;
    try {
      // 第一次不知道哪台设备有画面：先不带 device_id 问全局，拿到列表再指定
      let dev = camDeviceId();
      let r = await getJSON(CAM.status + (dev ? `?device_id=${encodeURIComponent(dev)}` : ""));
      const b = r.body || {};

      if (Array.isArray(b.devices_with_frames)) cam.devices = b.devices_with_frames;

      // 选定的设备没帧，但别的设备有：自动跟到有画面的那台（只在"全部设备"模式下）
      if (!b.has_frame && !state.deviceFilter && cam.devices.length) {
        dev = cam.devices[0];
        cam.deviceId = dev;
        r = await getJSON(CAM.status + `?device_id=${encodeURIComponent(dev)}`);
      }

      const d = r.body || {};
      if (!d.has_frame || !d.frame) {
        camShowPlaceholder("还没有画面",
          state.deviceFilter
            ? `设备 ${state.deviceFilter} 尚未上传画面。确认板子已联网、固件已烧录摄像头版本。`
            : "正在等待开发板上传第一帧…（板子联网后约 1 秒出现）");
        camPlaceholderHint(d);
        return;
      }

      camSetMeta(d.frame);
      camSetBadge("● 实时画面", "live");
      if (d.frame.frame_no !== cam.lastFrameNo) {
        camDisplayFrame(d.frame.device_id || dev, d.frame);
      }
    } catch (e) {
      camShowPlaceholder("连不上后端", "无法获取摄像头状态（后端可能未启动）。本页不会显示任何模拟画面。");
    } finally {
      cam.loading = false;
    }
  }

  /** 取"最能说明问题"的摄像头自报状态：优先本设备，其次最近上报的那台。 */
  function camPickCamera(d) {
    if (d && d.camera) return d.camera;
    if (d && Array.isArray(d.cameras) && d.cameras.length) return d.cameras[0];
    return null;
  }

  function camPlaceholderHint(d) {
    const s = camEl("camPhSub");
    const t = camEl("camPhTitle");
    if (!s) return;

    // 设备启动时会自报摄像头状态：有它就能把"没开机/没联网"与"摄像头没接好"分开
    const c = camPickCamera(d);
    if (c && c.ready === false) {
      const err = c.error || "未知原因";
      if (t) t.textContent = "摄像头未就绪（设备本身在线）";
      s.textContent = `设备 ${c.device_id || "—"} 在线，但摄像头探测失败：${err}。`
        + (/NOT_SUPPORTED/i.test(err)
          ? "板上 I2C 总线只应答了加速度计（0x12），没有摄像头的 0x30 地址——"
            + "传感器侧没通电，或排线没连通（2V8 / 1V5 供电在板上常开，没有开关可关）。"
          : "")
        + "请检查主板 24P 摄像头排线（J4 座）：插到底、锁扣扣紧、方向正确。"
        + "插好后固件会在 30 秒内自动重新探测并出画面，不需要重启板子。";
      return;
    }
    if (c && c.ready === true) {
      if (t) t.textContent = "摄像头已就绪，等待第一帧";
      s.textContent = "设备已探测到 OV2640，正在等待画面上传（通常 1 秒内出现）。";
      return;
    }
    if (!d || !Array.isArray(d.devices_with_frames) || !d.devices_with_frames.length) {
      s.textContent = "后端当前没有任何设备的画面。请确认：① 固件已烧录第 4 周版本；"
                    + "② 串口出现「板载摄像头就绪：OV2640」；③ 板子与服务器网络互通。";
    }
  }

  async function camRefreshPhotos() {
    const dev = camDeviceId();
    const url = CAM.photos + `?limit=12` + (dev ? `&device_id=${encodeURIComponent(dev)}` : "");
    const box = camEl("camGallery");
    const cnt = camEl("camPhotoCount");
    try {
      const { body } = await getJSON(url);
      const list = (body && Array.isArray(body.photos)) ? body.photos : [];
      cam.photoIds = list.map((p) => p.photo_id);
      if (cnt) cnt.textContent = `${(body && body.count) || list.length} 张`;
      if (!box) return;
      if (!list.length) {
        box.innerHTML = '<div class="cam-empty" id="camGalleryEmpty">'
                      + '还没有照片。点上方「拍照」让开发板抓一张。</div>';
        return;
      }
      box.innerHTML = list.map((p) => {
        const t = p.created_at_utc ? new Date(p.created_at_utc) : null;
        const tText = (t && !Number.isNaN(t.getTime())) ? hhmmss(t) : "—";
        return `<div class="cam-card">
          <a href="${esc(p.url)}" target="_blank" rel="noopener" title="打开原图 ${esc(p.photo_id)}">
            <img src="${esc(p.url)}" alt="照片 ${esc(p.photo_id)}" loading="lazy" />
          </a>
          <div class="cam-cap"><span>${esc(tText)}</span><span>${p.width || "?"}×${p.height || "?"}</span></div>
        </div>`;
      }).join("");
    } catch (e) {
      if (cnt) cnt.textContent = "—";
    }
  }

  /** 等一张"之前没有的"照片出现（拍照请求是异步的：设备在下一帧上传时才收到）。 */
  async function camWaitForNewPhoto(beforeIds, timeoutMs) {
    const t0 = Date.now();
    const known = new Set(beforeIds || []);
    while (Date.now() - t0 < timeoutMs) {
      await new Promise((res) => setTimeout(res, 600));
      const dev = camDeviceId();
      const url = CAM.photos + `?limit=5` + (dev ? `&device_id=${encodeURIComponent(dev)}` : "");
      try {
        const { body } = await getJSON(url);
        const list = (body && body.photos) || [];
        const fresh = list.find((p) => !known.has(p.photo_id));
        if (fresh) return fresh;
      } catch (e) { /* 网络抖动：继续等，直到超时 */ }
    }
    return null;
  }

  async function camShoot() {
    if (cam.shotBusy) return;
    const dev = camDeviceId();
    const hint = camEl("camShotHint");
    const btn = camEl("camShot");
    if (!dev) {
      if (hint) hint.textContent = "还没有任何设备上传过画面，无法拍照。等画面出现后再点。";
      addLog("warn", "拍照失败：当前没有可用的摄像头设备");
      return;
    }

    cam.shotBusy = true;
    if (btn) btn.disabled = true;
    if (hint) hint.textContent = "已下发拍照请求，等待设备抓拍并上传原图（最长等待 20 秒）…";
    addLog("info", `请求设备 ${dev} 拍一张 1600×1200 照片`);

    try {
      const before = cam.photoIds.slice(0, 5);
      const { status, body } = await postJSON(CAM.capture, { device_id: dev });
      if (status >= 400 || !body || body.ok !== true) {
        // 后端的 error 可能是字符串，也可能是 {code, message} 结构（第 3 周起的约定），
        // 两种都要能读出人话，不能把 [object Object] 丢到界面上
        const e = body && body.error;
        const msg = (typeof e === "string") ? e
                  : (e && (e.message || e.code)) ? (e.message || e.code)
                  : `后端拒绝（HTTP ${status}）`;
        throw new Error(msg);
      }
      const photo = await camWaitForNewPhoto(before, 20000);
      if (!photo) {
        throw new Error("20 秒内没有收到照片（设备可能离线、摄像头不可用，或原图上传超时）");
      }
      if (hint) {
        hint.textContent = `已收到照片：${photo.width}×${photo.height}，`
                         + `${(photo.byte_len / 1024).toFixed(1)} KB，${recvToLocalText(photo.created_at_utc)}`;
      }
      addLog("ok", `拍照成功：${photo.photo_id}（${photo.width}×${photo.height}，${photo.byte_len} 字节）`);
      await camRefreshPhotos();
      // 新照片顺手在画面窗口里也显示一下（点开大图仍走原图链接）
      cam.lastFrameNo = null;   // 触发下一轮重新取预览帧，保持画面与状态同步
    } catch (e) {
      const msg = (e && e.message) ? e.message : String(e);
      if (hint) hint.textContent = `拍照失败：${msg}`;
      addLog("warn", `拍照失败：${msg}`);
    } finally {
      cam.shotBusy = false;
      if (btn) btn.disabled = false;
    }
  }

  function camStartPolling() {
    if (cam.timer) return;
    camPoll();
    camRefreshPhotos();
    cam.timer = setInterval(camPoll, 450);
  }

  function camStopPolling() {
    if (cam.timer) clearInterval(cam.timer);
    cam.timer = null;
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
      // 摄像头：换了设备就重新判断"哪台有画面"，不要沿用上一台设备的帧号
      cam.lastFrameNo = null;
      cam.lastSeq = null;
      cam.shownAt = [];
      cam.photoIds = [];
      if (cam.timer) camRefreshPhotos();
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

    // ③ 相机预览（打开时才轮询，收起即停：不在后台白耗带宽）
    $("btnCamera").addEventListener("click", () => {
      const open = togglePanel("cameraPanel", $("btnCamera"));
      if (open) {
        camStartPolling();
        $("toolHint").textContent = "相机预览：画面来自板载 OV2640，每一帧都是真实拍摄";
        addLog("info", "打开相机预览（开始从后端拉取板载摄像头画面）");
      } else {
        camStopPolling();
        $("toolHint").textContent = "相机预览已收起（画面轮询已停止）";
      }
    });
    $("camToggle").addEventListener("click", () => {
      cam.paused = !cam.paused;
      const b = $("camToggle");
      if (b) {
        b.textContent = cam.paused ? "继续画面" : "暂停画面";
        b.classList.toggle("is-on", cam.paused);
      }
      if (cam.paused) {
        camSetBadge("❚❚ 已暂停", "paused");
      } else {
        camPoll();
      }
      addLog("info", cam.paused
        ? "相机预览暂停：保留最后一张真实画面，不再拉取新帧"
        : "相机预览继续：恢复拉取新帧");
    });
    $("camShot").addEventListener("click", () => { camShoot(); });
    $("camRefresh").addEventListener("click", () => { camRefreshPhotos(); });

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
      camStopPolling();
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
  // 第 3 周：按键求助闭环（查看者列表 / 回应 / 取消）
  //
  // ★★★ 本区块的最高红线（当堂验证第 2 项）：
  //     **没有远端回执证据时，页面不得显示「对方已收到」。**
  //
  //     唯一判据是 `ev.remote_acked_at_utc != null`（后端只有设备取走 help_ack
  //     并回执之后才会写它）。绝不允许：
  //       - 依据 `status === "responded"` 推断已送达；
  //       - 依据本地上传成功、或任何计时器"等一会儿就变绿"。
  //     三种事实的允许/禁止文案见 docs/08 §4.6。
  // ==========================================================================
  const HELP = {
    list: "/api/v1/help_events",
    response: (id) => `/api/v1/help_events/${encodeURIComponent(id)}/response`,
    cancel:   (id) => `/api/v1/help_events/${encodeURIComponent(id)}/cancel`,
    detail:   (id) => `/api/v1/help_events/${encodeURIComponent(id)}`,
  };

  /**
   * 求助状态 → 中文徽标/颜色。
   * 判据与 docs/06 §2.2 的 8 值枚举一一对应；**没有同义词**（不许出现"已读/完成"等）。
   */
  const HELP_STATUS_TEXT = {
    created:     { text: "已创建",           cls: "pill-wait" },
    uploaded:    { text: "已上传",           cls: "pill-info" },
    acked:       { text: "设备接收回执",     cls: "pill-info" },
    viewed:      { text: "查看者已查看",     cls: "pill-info" },
    responded:   { text: "查看者已回应",     cls: "pill-warn" },
    cancelled:   { text: "已撤销",           cls: "pill-wait" },
    unreachable: { text: "尚未送达",         cls: "pill-bad" },
    expired:     { text: "已过期",           cls: "pill-wait" },
  };

  /** 本地反馈事实 → 中文；取值只有契约里的三个，越界就如实显示原值。 */
  const HELP_FB_TEXT = {
    "buzzer+led": "蜂鸣 + 灯光",
    "led":        "仅灯光",
    "none":       "无（器件不可用）",
  };

  const HELP_TRIGGER_TEXT = { button: "实体按键", touch: "触摸" };

  const hstate = {
    selected: null,      // 当前选中的求助事件（对象）
    inflight: false,     // 防连点
    lastList: [],
  };

  function helpStatusPill(st) {
    const m = HELP_STATUS_TEXT[st];
    if (!m) return `<span class="pill pill-wait">—（未知状态）</span>`;
    return `<span class="pill ${m.cls}">${m.text}</span>`;
  }

  /**
   * ★★★ 核心函数：决定"远端回执"区域到底显示什么。
   *
   * 规则（docs/08 §4.6 的 F1/F2/F3）：
   *   F3  remote_acked_at_utc != null  → 「佩戴者设备已收到回应」（唯一允许说"已收到"的分支）
   *   F2  未 ack 但 status=="responded" → 「我已回应，等待设备确认」
   *   F1  仅本地反馈/已上传            → 返回空串（区域内什么都不显示）
   *
   * ★ 注意分支顺序与判据：F3 **只**看 remote_acked_at_utc，与 status 无关。
   */
  function helpRemoteAckText(ev) {
    if (!ev) return "";
    // F3：唯一判据 —— 设备确实回执了 help_ack
    if (ev.remote_acked_at_utc) {
      return `✓ 佩戴者设备已收到回应（设备回执 ${hhmmss3(new Date(ev.remote_acked_at_utc))}）`;
    }
    // F2：查看者已回应，但设备还没确认 —— 绝不能说"已收到"
    if (ev.status === "responded") {
      return "已记录我的回应，正在等待设备确认（设备取走 help_ack 并回执后才算送达）。";
    }
    // F1：只有本地反馈/已上传 —— 这个区域保持为空
    return "";
  }

  /**
   * 单项状态文案（对应 #helpItemStatus）。
   *
   * ★ 同样严格遵守红线：只有 F3 才允许出现"已收到"字样。
   */
  function helpItemStatusText(ev) {
    if (!ev) return "尚未选择求助事件";
    const hasAck = !!ev.remote_acked_at_utc;
    if (hasAck) {
      return "佩戴者设备已收到回应";
    }
    if (ev.status === "cancelled") {
      return `已撤销（原因：${ev.cancel_reason || "—"}）`;
    }
    if (ev.status === "responded") {
      return "我已回应，等待设备确认";
    }
    if (ev.status === "unreachable") {
      return "尚未送达（设备离线，事件还没到设备）";
    }
    // F1：事件已在后端可见，但还没有任何查看者/设备回执
    return "佩戴者已按键（待回应）";
  }

  /** 渲染选中事件的详情区。 */
  function renderHelpDetail(ev) {
    const stEl = $("helpItemStatus");
    const ackEl = $("helpItemRemoteAck");
    const metaEl = $("helpItemMeta");
    const detEl = $("helpItemDetail");
    const fbEl = $("helpLocalFeedback");
    const fbNoteEl = $("helpLocalFeedbackNote");
    if (!stEl) return;

    if (!ev) {
      stEl.textContent = "尚未选择求助事件";
      stEl.className = "pill pill-wait";
      if (ackEl) ackEl.textContent = "";
      if (metaEl) metaEl.textContent = "—";
      if (fbEl) { fbEl.textContent = "—"; fbEl.className = "pill pill-wait"; }
      if (detEl) detEl.textContent =
        "佩戴者按键后，这里显示该次求助的完整链路：本地反馈 → 上传 → 回应 → 设备确认。";
      return;
    }

    // ---- 状态徽标 ----
    const txt = helpItemStatusText(ev);
    stEl.textContent = txt;
    const hasAck = !!ev.remote_acked_at_utc;
    stEl.className = "pill " + (hasAck ? "pill-ok"
      : (ev.status === "cancelled" ? "pill-wait"
        : (ev.status === "responded" ? "pill-warn" : "pill-info")));

    // ---- ★ "远端回执"专属区域：只有 F3 有内容，其余必须为空 ----
    if (ackEl) ackEl.textContent = helpRemoteAckText(ev);

    // ---- 时间线：触发 → 上传 → 查看 → 回应 → 设备确认 ----
    const parts = [];
    if (ev.trigger_at_utc) parts.push(`触发 ${ev.trigger_at_utc.slice(11, 23)}`);
    else if (Number.isFinite(ev.trigger_up_ms)) parts.push(`触发 up_ms=${ev.trigger_up_ms}（未对时）`);
    if (ev.received_at_utc) parts.push(`上传 ${hhmmss3(new Date(ev.received_at_utc))}`);
    if (ev.viewed_at_utc) parts.push(`查看 ${hhmmss3(new Date(ev.viewed_at_utc))}`);
    if (ev.responded_at_utc) parts.push(`回应 ${hhmmss3(new Date(ev.responded_at_utc))}`);
    if (ev.remote_acked_at_utc) parts.push(`设备确认 ${hhmmss3(new Date(ev.remote_acked_at_utc))}`);
    if (ev.cancelled_at_utc) parts.push(`撤销 ${hhmmss3(new Date(ev.cancelled_at_utc))}`);
    if (metaEl) metaEl.textContent = parts.length ? parts.join(" → ") : "—";

    // ---- 本地反馈事实（设备如实标注，页面照原样显示）----
    if (fbEl) {
      fbEl.textContent = HELP_FB_TEXT[ev.local_feedback] || String(ev.local_feedback || "—");
      fbEl.className = "pill " + (ev.local_feedback && ev.local_feedback !== "none"
        ? "pill-ok" : "pill-warn");
    }
    if (fbNoteEl) {
      let note = "该字段由设备如实标注实际发生的反馈，后端与页面都不得代为推断。";
      if (ev.pending_local) {
        note += " 本条属于<b>断网期间产生、联网后补传</b>（pending_local=true）：" +
          "本地反馈在断网时就已经发生了，上传是之后补的。";
      }
      if (ev.age_ms != null) note += ` 距触发已 ${ageText(ev.age_ms)}。`;
      fbNoteEl.innerHTML = note;
    }

    // ---- 详情正文：解释"现在处于哪一步、还缺什么证据" ----
    let html = "";
    if (hasAck) {
      html = "设备已取走 <code>help_ack</code> 并回执，因此 <code>remote_acked_at_utc</code> 已写入 —— " +
        "此时页面才被允许显示「佩戴者设备已收到回应」。";
    } else if (ev.status === "responded") {
      html = "查看者已回应（<b>这是服务端事件</b>），后端也已把 <code>help_ack</code> 排入设备指令队列。" +
        "但<b>入队 ≠ 送达</b>：在设备轮询取走并回执之前，<code>remote_acked_at_utc</code> 仍是空，" +
        "所以页面不会声称对方设备已经知道。";
    } else if (ev.status === "cancelled") {
      html = "本次求助已被撤销（记录不删除，全程留痕）。后端已下发 <code>clear_alert</code>，" +
        "设备收到后停止蜂鸣并把指示灯复位到待机色。";
    } else if (ev.status === "unreachable") {
      html = "后端已感知设备离线，事件暂时无法送达设备，因此显示「尚未送达」。";
    } else {
      html = "事件已上传到后端（<b>VPS 接收</b>成立），列表里可见。" +
        "但这仅说明「佩戴者按下了键」——<b>还没有任何查看者回应</b>，" +
        "所以既没有回应、也没有设备回执。";
    }
    html += `<div class="help-id-row">help_id <code>${esc(ev.help_id)}</code>` +
      ` · 幂等键 <code>${esc(ev.client_request_key || "—")}</code>` +
      ` · 设备 <code>${esc(ev.device_id || "—")}</code>` +
      ` · boot <code>${fmt(ev.boot_count, 0)}</code></div>`;
    if (detEl) detEl.innerHTML = html;

    // ---- 按钮可用性：终态不可再回应/取消 ----
    const terminal = (ev.status === "cancelled" || ev.status === "expired");
    if ($("helpRespondBtn")) $("helpRespondBtn").disabled = terminal;
    if ($("helpCancelBtn")) $("helpCancelBtn").disabled = terminal;
  }

  /** 拉取求助列表并渲染。 */
  async function loadHelpList() {
    const tbody = $("helpList");
    if (!tbody) return;
    const dev = state.deviceFilter || ($("deviceSelect") && $("deviceSelect").value) || "";
    const q = dev ? `?device_id=${encodeURIComponent(dev)}&limit=50` : "?limit=50";
    try {
      const { status, body } = await getJSON(HELP.list + q);
      if (status !== 200 || !body || !body.ok) {
        tbody.innerHTML = `<tr><td colspan="7" class="req-empty">求助列表读取失败（HTTP ${status}）</td></tr>`;
        if ($("helpCountChip")) $("helpCountChip").textContent = "读取失败";
        return;
      }
      const rows = body.data ? (body.data.help_events || []) : (body.help_events || []);
      hstate.lastList = rows;
      if ($("helpCountChip")) $("helpCountChip").textContent = `${rows.length} 条`;

      if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="7" class="req-empty">还没有求助事件</td></tr>`;
        renderHelpDetail(null);
        return;
      }
      tbody.innerHTML = rows.map((r) => {
        const m = HELP_STATUS_TEXT[r.status];
        const pill = m ? `<span class="pill ${m.cls}">${m.text}</span>`
                       : `<span class="pill pill-wait">—（未知状态）</span>`;
        const when = r.trigger_at_utc
          ? r.trigger_at_utc.slice(11, 19)
          : (Number.isFinite(r.trigger_up_ms) ? `up_ms ${r.trigger_up_ms}` : "—");
        const fb = HELP_FB_TEXT[r.local_feedback] || String(r.local_feedback || "—");
        // ★ 列表里的"远端回执"列同样只看 remote_acked_at_utc：
        //   非空才显示已收到，否则显示"—"，绝不用 status 推断。
        const ack = r.remote_acked_at_utc
          ? `<span class="pill pill-ok">已收到</span>`
          : "—";
        const age = (r.age_ms != null) ? ageText(r.age_ms) : "—";
        return `<tr data-hid="${esc(r.help_id)}">
          <td>${esc(when)}</td>
          <td><code class="req-rid" title="${esc(r.help_id)}">${esc(String(r.help_id).slice(0, 8))}…</code></td>
          <td>${pill}</td>
          <td>${esc(HELP_TRIGGER_TEXT[r.trigger_type] || r.trigger_type || "—")}</td>
          <td>${esc(fb)}</td>
          <td>${esc(age)}</td>
          <td>${ack}</td>
        </tr>`;
      }).join("");

      tbody.querySelectorAll("tr[data-hid]").forEach((tr) => {
        tr.addEventListener("click", () => selectHelpRow(tr.getAttribute("data-hid")));
      });

      // 保持当前选中项同步（后端状态可能已推进，例如设备刚回执）
      if (hstate.selected) {
        const fresh = rows.find((x) => x.help_id === hstate.selected.help_id);
        if (fresh) {
          hstate.selected = fresh;
          renderHelpDetail(fresh);
        }
      }
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="7" class="req-empty">求助列表读取异常</td></tr>`;
    }
  }

  /**
   * 选中某条求助 → 拉一次详情（后端会记录 viewed）。
   * ★ 用列表里已有的对象先渲染，再等详情回来覆盖 —— 避免点击后界面空白。
   */
  async function selectHelpRow(helpId) {
    const fromList = hstate.lastList.find((x) => x.help_id === helpId);
    hstate.selected = fromList || { help_id: helpId };
    renderHelpDetail(hstate.selected);
    try {
      const { status, body } = await getJSON(HELP.detail(helpId));
      if (status === 200 && body && body.ok && body.data) {
        hstate.selected = body.data;
        renderHelpDetail(body.data);
        addLog("info", `选中求助 ${String(helpId).slice(0, 8)}…（已记为 viewed）`);
      }
    } catch (e) { /* 详情拉取失败时保留列表里的信息，不编造 */ }
  }

  /** 点「我看到了」：只写回应 + 下发 help_ack；**不会**因此显示"已收到"。 */
  async function respondHelp() {
    const ev = hstate.selected;
    if (!ev || hstate.inflight) return;
    hstate.inflight = true;
    if ($("helpRespondBtn")) $("helpRespondBtn").disabled = true;
    try {
      const actor = "viewer-" + (state.deviceFilter || "web");
      const { status, body } = await postJSON(HELP.response(ev.help_id), {
        actor, message: "查看者已看到求助",
      });
      if (status === 200 && body && body.ok) {
        const d = body.data || {};
        addLog("ok", `已对 ${ev.help_id.slice(0, 8)}… 回应（status=${d.status}）；` +
          (d.remote_acked_at_utc
            ? "设备已确认"
            : "remote_acked_at_utc 仍为空 —— 设备尚未确认，页面不会声称对方已经知道"));
        await selectHelpRow(ev.help_id);
        await loadHelpList();
      } else {
        const code = (body && body.error && body.error.code) || "未知";
        addLog("bad", `回应失败：${code}（HTTP ${status}）`);
        $("helpItemDetail").innerHTML =
          `回应失败：<b>${esc(code)}</b> ${esc((body && body.error && body.error.message) || "")}`;
      }
    } catch (e) {
      addLog("bad", `回应异常：${(e && e.message) ? e.message : e}`);
    } finally {
      hstate.inflight = false;
      if ($("helpRespondBtn")) $("helpRespondBtn").disabled = false;
    }
  }

  /** 点「取消求助」：原因必填（C3），取消后记录不删除（C3），终态再取消幂等（C4）。 */
  async function cancelHelp() {
    const ev = hstate.selected;
    if (!ev || hstate.inflight) return;
    // C3：cancel_reason 必填 —— 空原因后端会 400，这里先拦一道并说明原因
    const reason = window.prompt("取消原因（必填，会原样记录在 help_events.cancel_reason）", "误触");
    if (reason === null) return;                     // 用户主动放弃
    if (!String(reason).trim()) {
      $("helpItemDetail").innerHTML =
        "取消原因必填（C3：<code>cancel_reason</code> 不允许为空，后端会回 <code>400 MISSING_FIELD</code>）。";
      addLog("warn", "取消原因留空，已中止本次取消（后端也会拒收）");
      return;
    }
    hstate.inflight = true;
    if ($("helpCancelBtn")) $("helpCancelBtn").disabled = true;
    try {
      const actor = "wearer-" + (state.deviceFilter || "web");
      const { status, body } = await postJSON(HELP.cancel(ev.help_id), { actor, reason: String(reason) });
      if (status === 200 && body && body.ok) {
        const d = body.data || {};
        addLog("ok", `已取消 ${ev.help_id.slice(0, 8)}…（${d.status}` +
          `${d.deduplicated ? " · 幂等重复" : ""}）· 已下发 ${d.device_command || "—"}`);
        await selectHelpRow(ev.help_id);
        await loadHelpList();
      } else {
        const code = (body && body.error && body.error.code) || "未知";
        addLog("bad", `取消失败：${code}（HTTP ${status}）`);
        $("helpItemDetail").innerHTML =
          `取消失败：<b>${esc(code)}</b> ${esc((body && body.error && body.error.message) || "")}`;
      }
    } catch (e) {
      addLog("bad", `取消异常：${(e && e.message) ? e.message : e}`);
    } finally {
      hstate.inflight = false;
      if ($("helpCancelBtn")) $("helpCancelBtn").disabled = false;
    }
  }

  function updateHelpChip() {
    const el = $("helpChip");
    if (!el) return;
    const devId = state.deviceFilter || ($("deviceSelect") && $("deviceSelect").value) || "";
    el.textContent = devId ? `目标设备 ${devId}` : "未选择设备";
  }

  function bindHelpUI() {
    if (!$("helpPanel")) return;      // 页面被裁剪时不影响其它功能
    // ★ 单独一个「刷新求助列表」按钮，**不**挂到 refreshStoredData 上：
    //   第 2 周的自检用例断言"点刷新已存数据不得产生任何 collect 流量"，
    //   这里保持同样的洁癖 —— 刷新求助列表也只读，不下发任何指令。
    $("helpRefreshBtn").addEventListener("click", () => {
      loadHelpList();
      updateHelpChip();
    });
    $("helpRespondBtn").addEventListener("click", respondHelp);
    $("helpCancelBtn").addEventListener("click", cancelHelp);

    const sel = $("deviceSelect");
    if (sel) sel.addEventListener("change", () => { loadHelpList(); updateHelpChip(); });

    updateHelpChip();
    loadHelpList();
    renderHelpDetail(null);
  }

  // ==========================================================================
  // 第 4 周：受限工具语言助手（自然语言查询 / 请求采集）
  //
  // ★ 前端不做意图判断、不算时间、不编状态：
  //   只把「用户原话 + 当前设备上下文」发给后端，渲染后端返回的
  //   intent / answer / evidence / tool_calls / ambiguity。
  // ★ 文案红线：只有 claim === "new_record" 时后端才会给出完成措辞；
  //   前端原样显示，绝不把 status==="acked"/"submitted" 偷偷改成"已采集"。
  // ==========================================================================
  const nlState = { busy: false, last: null, ctx: null };

  function nlEsc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function nlFmtTime(iso) {
    if (!iso) return "—";
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return String(iso);
    try {
      return new Date(t).toLocaleString("zh-CN", { hour12: false });
    } catch (e) {
      return String(iso);
    }
  }

  function updateNlCtx() {
    const el = $("nlCtx");
    if (!el) return;
    const dev = (state.deviceFilter || "").trim();
    nlState.ctx = dev || null;
    el.textContent = dev ? ("设备上下文：" + dev) : "全部设备：请在句子里点名设备";
  }

  function nlSetBusy(b) {
    nlState.busy = !!b;
    const btn = $("nlSend");
    if (btn) {
      btn.disabled = !!b;
      btn.textContent = b ? "处理中…" : "发送";
    }
  }

  function nlRender(d, error) {
    const st = $("nlStatus"), ans = $("nlAnswer");
    const ev = $("nlEvidence"), tl = $("nlTools");
    if (!st || !ans) return;
    st.classList.remove("is-bad");
    ans.classList.remove("is-clarify");
    if (ev) { ev.classList.add("hidden"); ev.innerHTML = ""; }
    if (tl) { tl.classList.add("hidden"); tl.innerHTML = ""; }

    // ---- 失败：错误码与允许范围原样展示，不猜原因 ----
    if (error) {
      st.classList.add("is-bad");
      st.innerHTML = "请求未被接受 · 错误码 <b>" + nlEsc(error.code || "ERROR") + "</b> · " +
                     nlEsc(error.message || "");
      const allow = error.details && error.details.allowed_devices;
      ans.textContent = (error.message ? error.message + "\n" : "") +
        (allow && allow.length
          ? "允许的设备：" + allow.join("、") + "\n本次没有向任何设备下发指令。"
          : "本次没有向任何设备下发指令。");
      return;
    }
    if (!d) return;
    nlState.last = d;

    const conf = Number.isFinite(d.confidence) ? Math.round(d.confidence * 100) : null;
    const parserTxt = d.parser === "llm" ? "授权语言服务"
      : d.parser === "rule_fallback" ? "语言服务异常 → 本地规则" : "本地规则";
    st.innerHTML =
      "意图：<b>" + nlEsc(d.intent_label || d.intent || "—") + "</b>" +
      " · 解析：" + nlEsc(parserTxt) +
      (conf != null ? " · 置信度 " + conf + "%" : "") +
      (d.status === "clarify" ? " · <b>需要澄清</b>（未执行任何操作）" : "") +
      (d.trace_id ? " · trace " + nlEsc(d.trace_id) : "");

    ans.textContent = d.answer || "";
    if (d.status === "clarify") ans.classList.add("is-clarify");

    // ---- 证据：来源 / 时间 / 状态 / 是否下发 ----
    const e = d.evidence || {};
    const freshness = { live: "实时", recent: "较新", stale: "陈旧", unknown: "未知" }[e.freshness] || "—";
    const chips = [];
    chips.push('<span class="nl-ev"><i>来源</i>' + nlEsc(e.source || "—") + "</span>");
    chips.push('<span class="nl-ev"><i>时间</i>' +
      nlEsc(nlFmtTime(e.received_at_utc || e.observed_at_utc)) + "</span>");
    chips.push('<span class="nl-ev"><i>状态</i>' +
      nlEsc(e.collect_status || d.status || "—") + "</span>");
    if (d.claim !== "none") {
      chips.push('<span class="nl-ev"><i>数据新鲜度</i>' + freshness + "</span>");
    }
    chips.push('<span class="nl-ev ' + (e.device_command_sent ? "is-act" : "") + '"><i>设备指令</i>' +
      (e.device_command_sent ? "已下发" : "未下发（只读）") + "</span>");
    if (e.collect_request_id) {
      chips.push('<span class="nl-ev"><i>请求</i>' + nlEsc(String(e.collect_request_id).slice(0, 8)) + "…</span>");
    }
    if (e.age_ms != null) {
      const s = Math.round(e.age_ms / 1000);
      chips.push('<span class="nl-ev"><i>数据年龄</i>' +
        (s < 60 ? s + " 秒" : (s < 3600 ? Math.round(s / 60) + " 分钟" : (s / 3600).toFixed(1) + " 小时")) + "</span>");
    }

    // ---- 歧义：给出可选说法（点了才发，不会自动替用户选）----
    const amb = d.ambiguity;
    if (amb && amb.needed) {
      const opts = (amb.candidates && amb.candidates.length)
        ? amb.candidates.map((c) => ({
            label: c.device_id + (c.last_status ? "（" + c.last_status + "）" : ""),
            q: "查看 " + c.device_id + " 上次的数据",
            dev: c.device_id,
          }))
        : (amb.options || []).map((t) => ({ label: t, q: t, dev: null }));
      chips.push('<span class="nl-ev"><i>需要澄清</i>' + nlEsc(amb.question || "") + "</span>");
      for (const o of opts.slice(0, 4)) {
        chips.push('<button type="button" class="nl-choice" data-q="' + nlEsc(o.q) +
          '" data-dev="' + nlEsc(o.dev || "") + '">' + nlEsc(o.label) + "</button>");
      }
    }

    if (ev) {
      ev.innerHTML = chips.join("");
      ev.classList.remove("hidden");
    }

    // ---- 工具轨迹：让"它到底做了什么"可核对 ----
    const calls = d.tool_calls || [];
    if (tl && calls.length) {
      tl.innerHTML = calls.map((c) => {
        const ok = c.ok !== false;
        const dig = c.digest ? " · " + JSON.stringify(c.digest) : "";
        return '<div><span class="' + (ok ? "t-ok" : "t-bad") + '">' + (ok ? "✓" : "✗") +
          "</span> " + nlEsc(c.name) + " " + nlEsc(JSON.stringify(c.args || {})) +
          " · " + (c.ms != null ? c.ms + "ms" : "—") + nlEsc(dig) + "</div>";
      }).join("");
      tl.classList.remove("hidden");
    }
  }

  async function nlAsk(q, deviceOverride) {
    const text = String(q == null ? "" : q).trim();
    const st = $("nlStatus"), ans = $("nlAnswer");
    if (!text) {
      if (st) { st.classList.remove("is-bad"); st.textContent = "请先说一句话，例如「查看上次数据」。"; }
      return;
    }
    if (nlState.busy) return;

    const body = { q: text, wait_ms: 6000 };
    const dev = (deviceOverride !== undefined && deviceOverride !== null)
      ? deviceOverride : (state.deviceFilter || "");
    if (dev) body.device_id = dev;

    nlSetBusy(true);
    if (st) {
      st.classList.remove("is-bad");
      st.textContent = "已发送：「" + text + "」" +
        (dev ? "（设备 " + dev + "）" : "（未指定设备）") +
        (/采集|采一次|再采/.test(text) ? " · 等待设备回报，最长 6 秒…" : " · 读取数据库中…");
    }
    try {
      const r = await fetch(API.nl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      // 与页面其它请求一致：先取文本再解析，避免非 JSON 响应把异常吞掉
      const txt = await r.text();
      let j = null;
      try { j = JSON.parse(txt); } catch (e2) { j = null; }
      if (!j || j.ok !== true) {
        const err = (j && j.error) || { code: "HTTP_" + r.status, message: "后端返回了无法识别的结果" };
        nlRender(null, err);
        addLog("warn", "语言助手请求失败：" + (err.code || "ERROR"));
        return;
      }
      nlRender(j.data);
      addLog("info", "语言助手：" + text + " → " + (j.data.intent || "?") + "/" + (j.data.claim || "?"));
      const hint = $("toolHint");
      if (hint) hint.textContent = "语言助手已作答（" + (j.data.intent_label || j.data.intent) + "）";
    } catch (e) {
      nlRender(null, {
        code: "NETWORK_ERROR",
        message: "请求没有到达后端，无法确认任何结果（既不能确认采集完成，也不能确认失败）。",
      });
      addLog("bad", "语言助手网络失败：" + String(e && e.message ? e.message : e));
    } finally {
      nlSetBusy(false);
      updateNlCtx();
    }
  }

  function bindNlUI() {
    const btn = $("btnNl");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const open = togglePanel("nlPanel", btn);
      if (open) updateNlCtx();
      $("toolHint").textContent = open
        ? "语言助手：查看上次数据 = 只读；重新采集一次 = 真下发并等设备回报"
        : "语言助手已收起";
    });
    $("closeNl").addEventListener("click", () => {
      setPanel("nlPanel", false);
      btn.classList.remove("is-on");
    });
    $("nlClear").addEventListener("click", () => {
      $("nlInput").value = "";
      $("nlAnswer").textContent = "";
      $("nlEvidence").innerHTML = "";
      $("nlEvidence").classList.add("hidden");
      $("nlTools").innerHTML = "";
      $("nlTools").classList.add("hidden");
      $("nlStatus").classList.remove("is-bad");
      $("nlStatus").textContent = "已清空。助手只做两类事：读取已有记录（只读）或请求设备采集一次（会产生设备侧流量）。";
      nlState.last = null;
    });
    $("nlSend").addEventListener("click", () => nlAsk($("nlInput").value));
    $("nlInput").addEventListener("keydown", (e) => {
      if (e && e.key === "Enter") nlAsk($("nlInput").value);
    });
    // 顶部设备下拉变化时同步上下文（与页面其它面板共用同一个选择）
    $("deviceSelect").addEventListener("change", () => updateNlCtx());
    $("nlExamples").addEventListener("click", (e) => {
      const t = (e && e.target && e.target.textContent) ? e.target.textContent.trim() : "";
      if (!t) return;
      $("nlInput").value = t;
      nlAsk(t);
    });
    $("nlEvidence").addEventListener("click", (e) => {
      const el = e && e.target;
      if (!el || !el.getAttribute) return;
      const q = el.getAttribute("data-q");
      if (!q) return;
      const dev = el.getAttribute("data-dev") || null;
      $("nlInput").value = q;
      nlAsk(q, dev);
    });
  }

  window.__nlDebug = {
    render: nlRender,
    ask: nlAsk,
    state: () => nlState,
    fmtTime: nlFmtTime,
    setBusy: nlSetBusy,
  };

  // ==========================================================================
  // 第 5 周：语音输入 → 识别 → 任务 → 播报
  // --------------------------------------------------------------------------
  // ★ 页面只做三件事：采音频（16k/16bit/mono WAV）、把服务端给的六段状态画出来、
  //   播服务端合成的音频。**不猜任何东西**：
  //     · 识别文本只来自 v.transcript，识别失败时这块区域必须为空，不能拿"上一句"顶上；
  //     · 环节状态只来自 v.stages，页面不自己点亮第六格；
  //     · 播放只上报「播放器开始播放」这一个事实，绝不替用户宣称"听到了"。
  // ==========================================================================
  const VOICE = {
    TARGET_RATE: 16000,
    MAX_MS: 40000,          // 与后端 MAX_DURATION_MS 一致
    MIN_MS: 400,            // 太短的录音后端也会拒，前端先提示一次
    busy: false,
    recording: false,
    last: null,             // 最近一次 turn 的完整数据（供对照用）
    ctx: null,
    stream: null,
    node: null,
    srcNode: null,
    chunks: [],
    startedAt: 0,
    timer: null,
    ctxRate: 16000,
    meterRms: 0,
    // ---- 第 6 周：会话与半双工状态 ----
    sid: null,              // 会话 id（短期上下文：在途任务 / 待澄清问题 / 轮次）
    seq: 0,                 // 本地轮次计数，只用于生成 request_id
    phase: "idle",          // idle | listening | thinking | speaking | clarifying
    inflight: null,         // 当前**唯一**有播报资格的 request_id
    inflightKind: null,     // audio | text
    inflightTurnId: null,
    sentAt: 0,              // 本轮提交时刻（算 client_ack_ms 用）
    dropped: null,          // 最近一次被丢弃的 request_id（迟到结果）
    session: null,          // 服务端返回的会话快照
  };

  // ---- 第 6 周：半双工状态文案 -------------------------------------------------
  // ★ 文案只描述"本地此刻在做什么"，不替服务端宣称结果：
  //   thinking 说"处理中"而不是"正在执行任务"，speaking 说"播报中"而不是"人已经听见了"。
  const PHASE_TEXT = {
    idle: "空闲", listening: "听你说话", thinking: "处理中",
    speaking: "播报中", clarifying: "等待澄清",
  };
  const PHASE_TALK = {
    idle: "点击开始说话",
    listening: "正在录音 · 点击结束",
    thinking: "再说一句（会顶掉在途那一轮）",
    speaking: "再说一句（先停掉播报）",
    clarifying: "重新说一遍",
  };
  const PHASE_STOP = {
    idle: "■ 停止",
    listening: "■ 放弃这段录音",
    thinking: "■ 取消这一轮",
    speaking: "■ 停止播报",
    clarifying: "■ 放弃这个话题",
  };

  const STAGE_LABELS = {
    recorded: "录音", vad: "静音检测", asr: "语音识别",
    task: "任务执行", tts: "语音合成", playback: "播放",
  };

  function voiceEl(id) { return $(id); }

  function voiceSetBusy(b) {
    VOICE.busy = !!b;
    // ★ 第 6 周改动：**不再**在"处理中"禁用说话按钮。
    //   否则用户就没有办法用"再说一句"把在途的那一轮顶掉 —— 而"顶掉"正是半双工的
    //   另一半语义。取而代之的是：录音按钮按 phase 换文案，另有一个专门的「停止」按钮。
    const cmp = voiceEl("voiceCompare");
    if (cmp) cmp.disabled = !!b;
  }

  // ---------------------------------------------------------------- 半双工状态
  /**
   * 设置本地阶段。★ 这里只改"本地此刻在做什么"，不推断任何服务端结论。
   * @param {string} p idle|listening|thinking|speaking|clarifying
   */
  function voiceSetPhase(p, why) {
    VOICE.phase = p;
    const chip = voiceEl("voiceStateChip");
    if (chip) {
      chip.className = "vstate is-" + p;
      chip.textContent = PHASE_TEXT[p] || p;
      chip.title = why || "";
    }
    const talk = voiceEl("voiceTalk");
    if (talk) talk.classList.toggle("is-rec", p === "listening");
    const lb = voiceEl("voiceTalkLabel");
    if (lb) lb.textContent = PHASE_TALK[p] || PHASE_TALK.idle;
    const stop = voiceEl("voiceStopAll");
    if (stop) {
      stop.textContent = PHASE_STOP[p] || PHASE_STOP.idle;
      stop.disabled = (p === "idle");
      // 出声/在途时按钮更显眼：它此刻是唯一能"打断"的东西
      stop.classList.toggle("is-urgent", p === "thinking" || p === "speaking");
    }
    const panel = voiceEl("voicePanel");
    if (panel) panel.setAttribute("data-phase", p);
  }

  function voiceNewRequestId() {
    VOICE.seq += 1;
    return "rq_" + Date.now().toString(36) + "_" + VOICE.seq + "_"
      + Math.random().toString(36).slice(2, 7);
  }

  function voiceDevice() {
    return (state.deviceFilter || "").trim() || null;
  }

  /** 建会话（只建一次）。失败不阻断提交，但必须说清"这一轮少了什么能力"。 */
  async function voiceEnsureSession() {
    if (VOICE.sid) return VOICE.sid;
    try {
      const r = await fetch(API.voiceSession, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ device_id: voiceDevice(), note: "网页语音面板（半双工）" }),
      });
      const j = await r.json();
      if (j && j.ok && j.data && j.data.session_id) {
        VOICE.sid = j.data.session_id;
        VOICE.seq = 0;
        voiceRenderSession(j.data);
        addLog("info", "语音会话已建立：" + VOICE.sid);
        return VOICE.sid;
      }
      voiceNotice("warn", "会话没能建立（" + ((j && j.error && j.error.message) || "未知原因")
        + "）。这一轮仍然可以提交，但没有会话就没有取消、没有多轮澄清、"
        + "也无法把迟到的结果隔离到 410 —— 那三件事都挂在会话上。");
    } catch (e) {
      voiceNotice("warn", "会话没能建立（" + String(e && e.message ? e.message : e)
        + "）。这一轮仍然可以提交，只是少了取消 / 澄清 / 迟到隔离。");
    }
    return null;
  }

  function voiceRenderSession(s) {
    if (!s) return;
    VOICE.session = s;
    const sid = voiceEl("voiceSid");
    if (sid) {
      sid.textContent = s.session_id ? String(s.session_id).slice(-8) : "—";
      sid.title = s.session_id || "";
    }
    const inf = voiceEl("voiceInflight");
    if (inf) {
      inf.textContent = s.active_request_id ? String(s.active_request_id).slice(-10) : "无";
      inf.title = s.active_request_id || "";
    }
    const sq = voiceEl("voiceTurnSeq");
    if (sq) sq.textContent = (s.turn_seq == null ? "—" : String(s.turn_seq));
    const cr = voiceEl("voiceClarifyRound2");
    if (cr) {
      const c = s.clarify;
      cr.textContent = c && !c.exhausted
        ? (c.round + "/" + (c.max_rounds || 2)) : "无";
    }
    if (s.stale && s.stale_note) addLog("warn", "会话：" + s.stale_note);
  }

  async function voiceRefreshSession() {
    if (!VOICE.sid) return;
    try {
      const r = await fetch(API.voiceSession + "/" + encodeURIComponent(VOICE.sid),
        { cache: "no-store" });
      const j = await r.json();
      if (j && j.ok) voiceRenderSession(j.data);
    } catch (e) { /* 读不到不影响任何判断，只是不刷新那三个数字 */ }
  }

  /** 新建会话：旧的待澄清问题与在途标记都丢，历史记录不受影响。 */
  async function voiceNewSession() {
    const old = VOICE.sid;
    VOICE.sid = null;
    try { if (voiceEl("voiceAudio")) voiceEl("voiceAudio").pause(); } catch (e) {}
    VOICE.inflight = null;
    voiceHideClarify();
    voiceSetPhase("idle");
    await voiceEnsureSession();
    voiceNotice("info", "已新建会话" + (old ? "（旧会话 " + String(old).slice(-8)
      + " 的待澄清问题已不会影响新的一轮）" : "") + "。历史记录照旧可查。");
  }

  // ---------------------------------------------------------------- 停止 / 取消
  /**
   * 唯一的「停止」入口。按当前 phase 决定停什么 ——
   * ★ 三种停法的语义**在服务端是不同的**，页面上不许混为一谈：
   *   放弃录音 = 这段音频根本没上传（服务端不知道它存在过）
   *   取消本轮 = 任务不要了（后续阶段一行都不执行；已发生的动作收不回）
   *   停止播报 = 任务保留、文字保留，只是把声音关掉
   */
  function voiceStopAll() {
    const p = VOICE.phase;
    if (p === "listening") return void voiceAbortRecord();
    if (p === "speaking") return void voiceStopSpeech();
    if (p === "thinking") return void voiceCancelInflight("user_stop");
    if (p === "clarifying") return void voiceAbandonClarify();
    voiceNotice("info", "当前是空闲状态，没有可停止的东西。空闲时按停止什么都不会发生 —— 这是对的。");
    return undefined;
  }

  function voiceAbortRecord() {
    if (!VOICE.recording) return;
    const ms = performance.now() - VOICE.startedAt;
    voiceCleanup();
    const t = voiceEl("voiceTimer");
    if (t) t.textContent = (ms / 1000).toFixed(1) + " s";
    voiceMeterReset();
    voiceSetPhase("idle");
    voiceNotice("info", "这段录音已作废，**没有上传**。服务端从来没有收到它，"
      + "所以记录里不会留下这一条 —— 「没发生」比「记一条取消」更准确。");
    addLog("warn", "录音被放弃：" + Math.round(ms) + " ms，未上传");
  }

  async function voiceCancelInflight(reason) {
    const sid = VOICE.sid;
    const req = VOICE.inflight;
    if (!req) {
      voiceNotice("info", "当前没有在途的那一轮可取消。");
      voiceSetPhase("idle");
      return;
    }
    // ★ 只把本地阶段切回空闲；**不**宣称"已取消"。
    //   真正作数的是服务端在下一个阶段边界发现取消登记（取消 vs 抢先完成是有竞态的）。
    voiceSetPhase("idle", "已登记取消：" + req);
    const ps = voiceEl("voicePlayState");
    if (ps) ps.textContent = "本轮已请求取消";
    try {
      const r = await fetch(API.voiceSession + "/" + encodeURIComponent(sid || "")
        + "/cancel", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: req, reason: reason || "user_stop",
                               requested_by: "browser" }),
      });
      const j = await r.json();
      if (!j || j.ok !== true) {
        voiceNotice("bad", "取消请求被拒绝：" + ((j && j.error && j.error.message) || ("HTTP " + r.status))
          + " 这一轮可能已经跑完了，页面上的结果以服务端记录为准。");
        return;
      }
      const d = j.data || {};
      voiceNotice("info", (d.note || "")
        + (d.was_active
          ? " 如果它已经在跑任务段，取消只能拦住后面的阶段 —— 已经发生的动作收不回来。"
          : "")
        + " 正在等待在途那一步发现自己被取消，结果到了会被标成 cancelled 并隔离（不可播放）。");
      addLog("warn", "已登记取消 request_id=" + String(req).slice(-10)
        + " · was_active=" + d.was_active);
      flashStop("取消已登记");
    } catch (e) {
      voiceNotice("bad", "取消请求没有送达后端：" + String(e && e.message ? e.message : e)
        + "。所以不能声称已取消 —— 这一轮可能照常跑完，结果到了会如实显示。");
    }
  }

  async function voiceAbandonClarify() {
    voiceHideClarify();
    voiceSetPhase("idle");
    if (!VOICE.sid) return;
    try {
      await fetch(API.voiceSession + "/" + encodeURIComponent(VOICE.sid) + "/cancel", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "user_stop", requested_by: "browser" }),
      });
    } catch (e) { /* 没有在途任务时会返回 409，属于正常 */ }
    try {
      const r = await fetch(API.voiceSession + "/" + encodeURIComponent(VOICE.sid) + "/reset", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "user_discard_clarify" }),
      });
      const j = await r.json();
      if (j && j.ok) {
        voiceNotice("info", "这次澄清已经放弃，问题被清掉了。下一句话按新话题处理，"
          + "不会被当成对上一句的回答。");
        addLog("warn", "放弃澄清：" + String(VOICE.sid).slice(-8));
      }
    } catch (e) { /* 忽略：本地已经可以说"放弃了" */ }
  }

  async function voiceStopSpeech() {
    const audio = voiceEl("voiceAudio");
    const v = VOICE.last;
    const playedMs = audio ? Math.round((audio.currentTime || 0) * 1000) : null;
    try { if (audio) audio.pause(); } catch (e) {}
    const ps = voiceEl("voicePlayState");
    if (ps) ps.textContent = "已停止播报（已播出的那一段收不回来）";
    voiceSetPhase("idle", "播报已停止");
    if (!v || !v.turn_id) return;
    try {
      const r = await fetch(API.voiceTurn + "/" + encodeURIComponent(v.turn_id) + "/stop", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ played_ms: playedMs, reason: "user_stop",
                               reported_by: "browser" }),
      });
      const j = await r.json();
      if (!j || j.ok !== true) {
        const code = (j && j.error && j.error.code) || ("HTTP_" + r.status);
        voiceNotice("info", "停止播报只记在本地：后端没能记下这次停止（" + code
          + "）。任务与文字回答都还在，不受影响。");
        return;
      }
      voiceNotice("info", (j.data && j.data.note)
        || "播报已停止；任务与文字回答都保留。");
      addLog("warn", "停止播报：" + v.turn_id.slice(0, 10) + "… @ " + playedMs + " ms");
      voiceRefreshSession();
    } catch (e) {
      voiceNotice("info", "停止播报只记在本地（后端没连上）。声音确实已经停了，"
        + "但「停止于第几毫秒」这件事没有落库。");
    }
  }

  /** 停按钮上的短促反馈：证明"点击被处理了"，不是占位动画。 */
  function flashStop(text) {
    const stop = voiceEl("voiceStopAll");
    if (!stop) return;
    const old = stop.textContent;
    stop.textContent = "✓ " + text;
    setTimeout(() => { if (stop.textContent === "✓ " + text) stop.textContent = old; }, 1200);
  }

  // ---------------------------------------------------------------- 录音
  function voiceMeterReset() {
    VOICE.meterRms = 0;
    const bar = voiceEl("voiceMeterBar");
    if (bar) bar.style.width = "0%";
  }

  function voiceTick() {
    const ms = performance.now() - VOICE.startedAt;
    const t = voiceEl("voiceTimer");
    if (t) t.textContent = (ms / 1000).toFixed(1) + " s";
    if (ms >= VOICE.MAX_MS) {
      addLog("warn", "录音已达上限 " + (VOICE.MAX_MS / 1000) + " 秒，自动结束");
      voiceStopAndSend();
    }
  }

  async function voiceStart() {
    if (VOICE.recording) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      voiceNotice("bad", "这个浏览器不支持麦克风采集（缺少 getUserMedia）。"
        + "可以改用下方「选择 WAV 文件」走备用路径，或直接在「语言助手」里打字。");
      return;
    }
    // ★ 说话前先把正在播的声音关掉：否则麦克风会把播报内容录进去（自激）。
    //   这里是本地动作，不向服务端宣称"已停止播报"——那是另一个按钮的事。
    const pa = voiceEl("voiceAudio");
    if (pa && !pa.paused) {
      try { pa.pause(); } catch (e) {}
      const ps = voiceEl("voicePlayState");
      if (ps) ps.textContent = "播报被新的录音打断（本次没有上报「停止」）";
    }
    try {
      // ★ 请求 16 kHz：本课约定的语音输入格式。若浏览器不接受，会用实际采样率
      //   采集再在本地重采样到 16 kHz（下面 encode 时按 VOICE.ctxRate 处理）。
      VOICE.ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: VOICE.TARGET_RATE });
      VOICE.ctxRate = VOICE.ctx.sampleRate || VOICE.TARGET_RATE;
      VOICE.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      VOICE.srcNode = VOICE.ctx.createMediaStreamSource(VOICE.stream);
      VOICE.node = VOICE.ctx.createScriptProcessor(4096, 1, 1);
      VOICE.chunks = [];
      VOICE.node.onaudioprocess = (e) => {
        if (!VOICE.recording) return;
        const buf = e.inputBuffer.getChannelData(0);
        VOICE.chunks.push(new Float32Array(buf));
        let s = 0;
        for (let i = 0; i < buf.length; i++) s += buf[i] * buf[i];
        const rms = Math.sqrt(s / Math.max(1, buf.length));
        VOICE.meterRms = rms;
        const bar = voiceEl("voiceMeterBar");
        if (bar) bar.style.width = Math.min(100, Math.round(rms * 400)) + "%";
      };
      VOICE.srcNode.connect(VOICE.node);
      VOICE.node.connect(VOICE.ctx.destination);   // 必须连才能触发回调；输出缓冲全 0，不会啸叫

      VOICE.recording = true;
      VOICE.startedAt = performance.now();
      voiceSetPhase("listening");
      voiceEl("voiceTimer").textContent = "0.0 s";
      voiceEl("voiceFmt").textContent =
        "采集 " + VOICE.ctxRate + " Hz → 上传 16 kHz · 16 bit · 单声道";
      VOICE.timer = setInterval(voiceTick, 100);
      voiceNoticeHide();
      // 前端上报"我开始听了"（只允许 listening/playback_ended 两种本地事实）
      if (VOICE.sid) {
        fetch(API.voiceSession + "/" + encodeURIComponent(VOICE.sid) + "/state", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ state: "listening", by: "browser" }),
        }).catch(() => {});
      }
      addLog("info", "开始录音（采集率 " + VOICE.ctxRate + " Hz）");
    } catch (e) {
      const name = (e && e.name) || "Error";
      const msg = (e && e.message) || String(e);
      let hint = msg;
      if (name === "NotAllowedError") hint = "麦克风权限被拒绝。请在浏览器地址栏左侧允许麦克风后重试；这不影响文字入口。";
      else if (name === "NotFoundError") hint = "没有找到可用的输入设备（麦克风未连接）。可直接改用文字入口。";
      else if (name === "NotReadableError") hint = "麦克风被其它程序占用（例如会议软件）。关掉占用它的程序后重试。";
      voiceNotice("bad", "无法打开麦克风：" + hint);
      addLog("bad", "录音启动失败：" + name + " " + msg);
      voiceCleanup();
      voiceSetPhase("idle");
    }
  }

  function voiceCleanup() {
    VOICE.recording = false;
    if (VOICE.timer) { clearInterval(VOICE.timer); VOICE.timer = null; }
    try { if (VOICE.node) { VOICE.node.onaudioprocess = null; VOICE.node.disconnect(); } } catch (e) {}
    try { if (VOICE.srcNode) VOICE.srcNode.disconnect(); } catch (e) {}
    try { if (VOICE.stream) VOICE.stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
    try { if (VOICE.ctx && VOICE.ctx.state !== "closed") VOICE.ctx.close(); } catch (e) {}
    VOICE.node = null; VOICE.srcNode = null; VOICE.stream = null; VOICE.ctx = null;
    const btn = voiceEl("voiceTalk");
    if (btn) btn.classList.remove("is-rec");
    // ★ 这里不再直接写按钮文案：文案由 voiceSetPhase 统一管，
    //   否则"CLEANUP 时写一句固定的『点击开始说话』"会把"处理中"覆盖成"空闲"。
  }

  /** Float32（任意采样率）→ Int16 @16k。线性插值，和板端/后端用的是同一套朴素做法。 */
  function voiceResample16k(f32, srcRate) {
    if (srcRate === VOICE.TARGET_RATE) {
      const out = new Int16Array(f32.length);
      for (let i = 0; i < f32.length; i++) {
        const v = Math.max(-1, Math.min(1, f32[i]));
        out[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
      }
      return out;
    }
    const ratio = VOICE.TARGET_RATE / srcRate;
    const n = Math.floor(f32.length * ratio);
    const out = new Int16Array(n);
    for (let i = 0; i < n; i++) {
      const x = i / ratio;
      const i0 = Math.floor(x);
      const i1 = Math.min(f32.length - 1, i0 + 1);
      const f = x - i0;
      const v = Math.max(-1, Math.min(1, f32[i0] * (1 - f) + f32[i1] * f));
      out[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }
    return out;
  }

  /** 写标准 44 字节 RIFF 头。后端会逐字段校验，这里写错会被明确拒收。 */
  function voiceEncodeWav(int16, rate) {
    const dataBytes = int16.length * 2;
    const buf = new ArrayBuffer(44 + dataBytes);
    const v = new DataView(buf);
    const str = (off, s) => { for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i)); };
    str(0, "RIFF"); v.setUint32(4, 36 + dataBytes, true); str(8, "WAVE");
    str(12, "fmt "); v.setUint32(16, 16, true);
    v.setUint16(20, 1, true);          // PCM
    v.setUint16(22, 1, true);          // 单声道
    v.setUint32(24, rate, true);
    v.setUint32(28, rate * 2, true);   // byte rate
    v.setUint16(32, 2, true);          // block align
    v.setUint16(34, 16, true);         // 16 bit
    str(36, "data"); v.setUint32(40, dataBytes, true);
    new Int16Array(buf, 44).set(int16);
    return new Blob([buf], { type: "audio/wav" });
  }

  function voiceMergeChunks() {
    const total = VOICE.chunks.reduce((a, c) => a + c.length, 0);
    const f32 = new Float32Array(total);
    let off = 0;
    for (const c of VOICE.chunks) { f32.set(c, off); off += c.length; }
    return f32;
  }

  async function voiceStopAndSend() {
    if (!VOICE.recording) return;
    const ms = performance.now() - VOICE.startedAt;
    const f32 = voiceMergeChunks();
    const rate = VOICE.ctxRate;
    const rmsLocal = VOICE.meterRms;
    voiceCleanup();

    const t = voiceEl("voiceTimer");
    if (t) t.textContent = (ms / 1000).toFixed(1) + " s";

    if (ms < VOICE.MIN_MS) {
      voiceNotice("warn", "这段录音只有 " + Math.round(ms) + " 毫秒，太短了。"
        + "按住或点击后，说完一整句再点结束。");
      return;
    }
    const int16 = voiceResample16k(f32, rate);
    const blob = voiceEncodeWav(int16, VOICE.TARGET_RATE);
    // 前端也算一次 RMS，仅用于"明显没说话"的即时提示；
    // 真正的判定以后端实测为准（页面上显示的 RMS 一律用后端返回的值）。
    const secs = (blob.size - 44) / 2 / VOICE.TARGET_RATE;
    addLog("info", "录音结束：约 " + secs.toFixed(2) + " 秒 / " + blob.size + " 字节 / 本地 RMS≈"
      + rmsLocal.toFixed(4));
    await voiceSend(blob);
  }

  // ---------------------------------------------------------------- 上传
  function voiceCaptureLocation() {
    const ua = navigator.userAgent || "";
    let browser = "浏览器";
    if (/Edg\//.test(ua)) browser = "Edge";
    else if (/Chrome\//.test(ua)) browser = "Chrome";
    else if (/Firefox\//.test(ua)) browser = "Firefox";
    else if (/Safari\//.test(ua)) browser = "Safari";
    let os = "未知系统";
    if (/Windows/.test(ua)) os = "Windows";
    else if (/Mac OS X/.test(ua)) os = "macOS";
    else if (/Android/.test(ua)) os = "Android";
    else if (/iPhone|iPad/.test(ua)) os = "iOS";
    else if (/Linux/.test(ua)) os = "Linux";
    return "browser:" + browser + "/" + os + " · 本机 PC（页面所在的这台机器）";
  }

  /** 教师演示用注入参数：只在"确实填了非 0/非空"时才带上，避免污染正常记录。 */
  function voiceInjectParams() {
    const out = {};
    const d = voiceEl("voiceInjectDelay");
    const t = voiceEl("voiceInjectTask");
    const x = voiceEl("voiceInjectText");
    const dv = d ? String(d.value || "").trim() : "";
    const tv = t ? String(t.value || "").trim() : "";
    const xv = x ? String(x.value || "").trim() : "";
    if (dv && Number(dv) > 0) out.inject_delay_ms = String(Math.round(Number(dv)));
    if (tv && Number(tv) > 0) out.inject_task_ms = String(Math.round(Number(tv)));
    if (xv) out.inject_asr_text = xv;
    return out;
  }

  /**
   * 统一的提交通道（语音轮 / 文字补充轮）—— 第 6 周的关键结构。
   *
   * ★ 两条输入方式必须走**同一个函数**，否则"取消 / 迟到隔离 / 澄清 / 时序"
   *   就要实现两遍，两遍迟早分叉。这与第 4 周 V2「语音与文字同源」是同一条理由。
   *
   * @param {"audio"|"text"} kind
   * @param {Blob|string} payload  音频 Blob 或补充文字
   * @param {Object} opt           { sourceOverride, bytes }
   */
  async function voiceSubmit(kind, payload, opt) {
    opt = opt || {};
    const sid = await voiceEnsureSession();
    const req = voiceNewRequestId();
    // ★ 半双工的唯一判据：页面只承认**最后一个** request_id。
    VOICE.inflight = req;
    VOICE.inflightKind = kind;
    VOICE.inflightTurnId = null;
    VOICE.sentAt = performance.now();
    VOICE.dropped = null;

    voiceSetBusy(true);
    voiceSetPhase("thinking", kind === "text" ? "补充轮已提交" : "已提交，等待识别");
    voiceResetStages();
    voiceHideClarify();
    if (kind === "audio") {
      // ★ 不知道就别写 0：这一格是"事实区"，宁可写"未知"也不能写一个假数字
      const nBytes = opt.bytes || (payload && payload.size) || null;
      voiceStageSet("recorded", "ok",
                    nBytes != null ? ("已采集 " + nBytes + " 字节") : "已采集（字节数未知）");
      voiceStageSet("vad", "run", "检测中…");
      voiceFinalText("已提交，等待识别…");
    } else {
      // ★ 键盘补充轮没有音频：这三格必须显式标"跳过"，而不是留空让人以为是没跑完
      voiceStageSet("recorded", "skipped", "补充轮：没有音频");
      voiceStageSet("vad", "skipped", "补充轮：无音频，跳过静音检测");
      voiceStageSet("asr", "skipped", "补充轮：没有调用识别引擎（asr.engine=keyboard）");
      voiceFinalText("补充内容已提交，等待处理…");
    }
    voiceNoticeHide();
    // 前端本地提示是**立刻**出现的（不等服务端）。把这个真实的本地耗时如实上报，
    // 服务端只做记录，不据此推断任何结论。
    const ackMs = Math.max(0, Math.round(performance.now() - VOICE.sentAt));

    let url, init;
    const inject = voiceInjectParams();
    if (kind === "audio") {
      const src = opt.sourceOverride
        || (voiceEl("voiceSource") ? voiceEl("voiceSource").value : "browser_mic");
      const q = new URLSearchParams();
      q.set("audio_source", src);
      q.set("capture_location", voiceCaptureLocation());
      q.set("playback_location", "browser <audio> · 本机 PC（页面所在的这台机器）");
      q.set("wait_ms", "6000");
      const dev = voiceDevice();
      if (dev) q.set("device_id", dev);
      if (sid) q.set("session_id", sid);
      q.set("request_id", req);
      q.set("client_ack_ms", String(ackMs));
      for (const k of Object.keys(inject)) q.set(k, inject[k]);
      url = API.voiceTurn + "?" + q.toString();
      init = { method: "POST", headers: { "Content-Type": "audio/wav" }, body: payload };
    } else {
      url = API.voiceText;
      init = {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.assign({
          text: payload,
          session_id: sid,
          request_id: req,
          device_id: voiceDevice(),
          wait_ms: 6000,
          client_ack_ms: ackMs,
          capture_location: "keyboard:浏览器输入框 · 本机 PC（页面所在的这台机器）",
          playback_location: "browser <audio> · 本机 PC（页面所在的这台机器）",
        }, inject)),
      };
    }
    addLog("info", (kind === "audio" ? "提交语音轮" : "提交文字补充轮")
      + " request_id=" + req.slice(-12) + " · 本地提示 " + ackMs + " ms");

    try {
      const r = await fetch(url, init);
      const txt = await r.text();
      let j = null;
      try { j = JSON.parse(txt); } catch (e2) { j = null; }
      // ★★ C3：迟到结果隔离（前端这一侧）。只认最后一个 request_id。
      if (VOICE.inflight !== req) { voiceDropLate(req, j, r.status); return; }
      VOICE.inflight = null;
      if (!j || j.ok !== true) {
        const err = (j && j.error) || { code: "HTTP_" + r.status, message: "后端返回了无法识别的结果" };
        voiceRenderError(err, r.status);
        return;
      }
      voiceRender(j.data);
      voiceRefreshSession();
    } catch (e) {
      if (VOICE.inflight !== req) { voiceDropLate(req, null, 0); return; }
      VOICE.inflight = null;
      voiceRenderError({
        code: "NETWORK_ERROR",
        message: "请求没有到达后端，无法确认任何结果。"
          + "这次既不能说识别成功，也不能说识别失败。可以重试，或直接改用文字入口。",
      }, 0);
      addLog("bad", "语音请求网络失败：" + String(e && e.message ? e.message : e));
    } finally {
      voiceSetBusy(false);
      voiceMeterReset();
    }
  }

  async function voiceSend(blob, sourceOverride) {
    return voiceSubmit("audio", blob, { sourceOverride: sourceOverride, bytes: blob && blob.size });
  }

  async function voiceSendText(text) {
    const t = String(text || "").trim();
    if (!t) { voiceNotice("warn", "补充内容不能为空。"); return; }
    if (t.length > 200) {
      voiceNotice("warn", "补充内容最多 200 字（这次 " + t.length + " 字）。"
        + "澄清只要说清「哪一个」即可，不用把整句重说。");
      return;
    }
    return voiceSubmit("text", t, {});
  }

  /** 迟到结果：不渲染、不播放、不覆盖 —— 只留一条可核对的说明。 */
  function voiceDropLate(req, j, status) {
    VOICE.dropped = req;
    const state = (j && j.ok && j.data) ? j.data.final_state : ("HTTP " + status);
    const msg = "上一轮的结果（request_id …" + String(req).slice(-10) + "，终局 "
      + state + "）已被丢弃：它比新的那一轮晚到，按半双工规则不再播报、也不覆盖当前结果。"
      + "这是设计行为，不是失败。";
    voiceNotice("info", msg);
    addLog("warn", "迟到结果被丢弃：" + req + " · " + state);
    // 阶段不许被这条迟到的消息改写（新的那一轮可能还在 thinking）
  }

  // ---------------------------------------------------------------- 渲染
  function voiceResetStages() {
    const ol = voiceEl("voiceStages");
    if (!ol) return;
    for (const li of ol.children) {
      li.className = "";
      const em = li.querySelector("em");
      if (em) em.textContent = "—";
    }
    const res = voiceEl("voiceResult");
    if (res) res.classList.add("hidden");
  }

  function voiceStageSet(key, status, detail) {
    const ol = voiceEl("voiceStages");
    if (!ol) return;
    const li = ol.querySelector('li[data-k="' + key + '"]');
    if (!li) return;
    const cls = { ok: "is-ok", failed: "is-bad", timeout: "is-bad", unavailable: "is-bad",
                  empty: "is-bad", skipped: "is-skip", pending: "is-wait", run: "is-run" }[status] || "";
    li.className = cls;
    const em = li.querySelector("em");
    if (em) {
      const map = { ok: "完成", failed: "失败", timeout: "超时", unavailable: "不可用",
                    empty: "空结果", skipped: "跳过", pending: "等待播放", run: "进行中" };
      em.textContent = map[status] || status;
      if (detail) li.title = detail;
    }
  }

  function voiceFinalText(t) {
    const el = voiceEl("voiceFinal");
    if (el) el.textContent = t;
  }

  const FINAL_TEXT = {
    done: "链路完成：录音 → 识别 → 任务 → 合成 都成功了。播放见下方「播放回答」。",
    no_speech: "没有检测到有效语音。这一轮没有产生任何识别文本，也没有执行任务。",
    asr_failed: "识别失败。没有文本，没有执行任务。",
    asr_timeout: "识别超时。没有文本，没有执行任务。",
    asr_unavailable: "识别服务不可用。文字入口不受影响。",
    task_failed: "识别成功，但任务执行失败。识别文本仍然保留。",
    tts_failed: "任务完成，但语音合成失败。文字回答仍然完整。",
    // ---- 第 6 周新增的终局 ----
    running: "这一轮还在跑（服务端只写了预入库那一行）。这一条是被「看见」的中间态，不是结果。",
    cancelled: "这一轮被取消了。取消发生在哪个阶段、任务段有没有真的跑过，见下面「任务执行」与提示。",
    superseded: "这一轮被新的一轮顶掉了。结果只作历史保留，不播报、不覆盖当前结果。",
    clarifying: "系统没听准，先问了一句 —— 这一轮还没有执行任何业务工具。",
    clarify_exhausted: "澄清到轮数上限仍未消解，系统主动停下了，没有猜、也没有执行任何工具。",
    internal_error: "语音链路内部异常。这一轮的结果不可信，请以服务端记录为准。",
  };

  function voiceNotice(level, msg) {
    const el = voiceEl("voiceNotice");
    if (!el) return;
    el.className = "voice-notice is-" + level;
    el.innerHTML = "";
    const mark = { bad: "✕", warn: "!", info: "i", ok: "✓" }[level] || "·";
    const b = document.createElement("b");
    b.textContent = mark;
    const span = document.createElement("span");
    span.textContent = msg;
    el.appendChild(b);
    el.appendChild(span);
    el.classList.remove("hidden");
  }

  function voiceNoticeHide() {
    const el = voiceEl("voiceNotice");
    if (el) el.classList.add("hidden");
  }

  function voiceRenderError(err, httpStatus) {
    voiceFinalText("请求未被接受（HTTP " + httpStatus + "）");
    const code = err.code || "ERROR";
    let extra = "";
    // 格式类错误：把"实际收到什么"和"要求什么"都摆出来，不让人猜
    if (err.details && err.details.expected) {
      const e = err.details.expected;
      extra = " 要求格式：" + e.sample_rate + " Hz / " + e.bits + " bit / 单声道 / " + e.container;
    }
    voiceNotice("bad", "错误码 " + code + " · " + (err.message || "") + extra
      + " 文字入口不受影响，可以直接在「语言助手」里输入。");
    voiceSetBusy(false);
    addLog("warn", "语音请求被拒：" + code);
  }

  /**
   * 渲染一轮的完整结果。
   * @param {Object} v 服务端 data
   * @param {boolean} [replay] true = 这是"回看历史"，不许改动当前半双工阶段
   */
  function voiceRender(v, replay) {
    VOICE.last = v;
    VOICE.inflightTurnId = v.turn_id || null;
    voiceSetBusy(false);
    const sg = {};
    for (const s of v.stages || []) sg[s.key] = s;
    for (const k of Object.keys(STAGE_LABELS)) {
      const s = sg[k];
      if (s) voiceStageSet(k, s.status, s.detail);
    }
    voiceFinalText(FINAL_TEXT[v.final_state] || ("终局：" + v.final_state));

    // ---- 半双工阶段：只由**终局**决定，回看历史时不改 ----
    if (!replay) {
      const playable = !!(v.delivery && v.delivery.playable && v.reply_audio_url);
      if (v.final_state === "done") voiceSetPhase(playable ? "speaking" : "idle", "任务完成");
      else if (v.final_state === "clarifying") voiceSetPhase("clarifying", "等你说清一句");
      else voiceSetPhase("idle", "终局：" + v.final_state);
    }
    voiceRenderSessionOf(v);
    voiceRenderTiming(v);
    voiceRenderClarify(v, replay);

    // ---- 失败提示：必须说清"发生了什么 + 下一步" ----
    if (v.error) {
      const lvl = (v.final_state === "tts_failed") ? "warn"
        : (v.final_state === "no_speech" ? "warn"
          : (v.final_state === "cancelled" || v.final_state === "superseded"
            || v.final_state === "clarify_exhausted") ? "info" : "bad");
      let msg = v.error.message || "";
      if (v.final_state === "no_speech" || v.final_state === "asr_failed"
          || v.final_state === "asr_timeout" || v.final_state === "asr_unavailable") {
        msg += " 这次没有得到识别文本，所以没有执行任何任务 —— 不会被「猜」成某条指令。";
        msg += " 你可以重说一遍，或直接在「语言助手」里打字（文字入口与语音入口用的是同一套解析）。";
      }
      voiceNotice(lvl, msg);
    } else if (v.final_state === "clarifying") {
      voiceNoticeHide();
    } else {
      voiceNoticeHide();
    }

    // ---- 结果区：只有真的识别到文本才显示（红线 V1） ----
    const res = voiceEl("voiceResult");
    const hasText = typeof v.transcript === "string" && v.transcript.length > 0;
    if (!hasText) {
      // ★ 必须把上一次的文本清掉，而不只是把结果区藏起来。
      //   只调 classList.add("hidden") 的话，DOM 里仍留着上一轮的识别文本：
      //   用读屏软件、或把面板重新展开、或有人直接看 DOM，都会以为"这一轮也识别出了这句话"。
      //   tools/test_web_voice.js 的 [3][4] 两节专门钉住这一点。
      const trEl = voiceEl("voiceTranscript");
      if (trEl) trEl.textContent = "";
      if (voiceEl("voiceAnswer")) voiceEl("voiceAnswer").textContent = "";
      if (voiceEl("voiceAsrMeta")) voiceEl("voiceAsrMeta").textContent = "";
      if (voiceEl("voiceIntent")) { voiceEl("voiceIntent").textContent = "—"; voiceEl("voiceIntent").className = "pill pill-wait"; }
      if (voiceEl("voiceClaim")) { voiceEl("voiceClaim").textContent = "—"; voiceEl("voiceClaim").className = "pill pill-wait"; }
      if (voiceEl("voiceSourceTag")) voiceEl("voiceSourceTag").textContent = "来源：—";
      if (res) res.classList.add("hidden");
      // 播放器也一并复位，避免点了却播的是上一轮的音频
      const pbtn = voiceEl("voicePlay");
      const paudio = voiceEl("voiceAudio");
      if (pbtn) pbtn.disabled = true;
      if (paudio) paudio.removeAttribute("src");
      const pstate = voiceEl("voicePlayState");
      if (pstate) pstate.textContent = "本轮没有可播放的音频";
      // 仍然显示运行位置（来源与位置是必交内容，失败时也要在）
      voiceRenderRunLoc(v);
      voiceAddHistory(v);
      loadVoiceHistory();
      return;
    }
    if (res) res.classList.remove("hidden");

    const tr = voiceEl("voiceTranscript");
    if (tr) tr.textContent = v.transcript;      // textContent：不做任何 HTML 解析
    const am = voiceEl("voiceAsrMeta");
    if (am) {
      const a = v.asr || {};
      const au = v.audio || {};
      am.textContent = "引擎 " + (a.engine || "—") + " / " + (a.model || "—")
        + " · 识别耗时 " + (a.ms != null ? a.ms + " ms" : "—")
        + " · 音频 " + (au.duration_ms != null ? au.duration_ms + " ms" : "—")
        + " · RMS " + (au.rms != null ? au.rms : "—");
    }

    const it = voiceEl("voiceIntent");
    const cl = voiceEl("voiceClaim");
    const srcTag = voiceEl("voiceSourceTag");
    const t = v.task || {};
    if (it) {
      it.textContent = t.intent ? ("意图：" + t.intent) : "意图：—";
      it.className = "pill " + (t.status === "ok" ? "pill-ok" : "pill-bad");
    }
    if (cl) {
      cl.textContent = t.claim ? ("claim：" + t.claim) : "claim：—";
      cl.className = "pill " + (t.claim === "new_record" ? "pill-ok" : "pill-wait");
    }
    if (srcTag) srcTag.textContent = "来源：" + (t.source || "—");

    const ans = voiceEl("voiceAnswer");
    if (ans) ans.textContent = t.answer || "（这一轮没有文字回答）";

    // ---- 播放：后端给了音频、且这一轮**有交付资格**才启用（C2） ----
    const playBtn = voiceEl("voicePlay");
    const audio = voiceEl("voiceAudio");
    const ps = voiceEl("voicePlayState");
    const delivery = v.delivery || {};
    if (playBtn && audio) {
      if (v.reply_audio_url) {
        playBtn.disabled = false;
        audio.src = v.reply_audio_url;
        if (ps) ps.textContent = "未播放";
      } else if (v.reply_audio_quarantined) {
        // ★ 音频其实合成出来了，但这一轮（取消/被顶掉）没有播报资格。
        //   后端给的就是 410 —— 所以这里连 src 都不设，避免"点了没声"的假象。
        playBtn.disabled = true;
        audio.removeAttribute("src");
        if (ps) ps.textContent = "这一轮播报已被隔离（不可播放）";
      } else {
        playBtn.disabled = true;
        audio.removeAttribute("src");
        if (ps) ps.textContent = "这一轮没有播报音频";
      }
    }
    const mineBtn = voiceEl("voicePlayMine");
    const mineAudio = voiceEl("voiceMineAudio");
    if (mineBtn && mineAudio && v.audio_url) {
      mineBtn.disabled = false;
      mineAudio.src = v.audio_url;
    }
    // 播报长度说明：语音只念要点是**设计选择**，必须写清楚，
    // 否则用户会以为"语音漏掉了内容 / 和文字不一致"。
    const tn = voiceEl("voiceTtsNote");
    if (tn) {
      const ttv = v.tts || {};
      if (ttv.status === "ok" && ttv.truncated) {
        tn.textContent = "语音只念回答的前几句（原文前缀，未改写）；完整回答共 "
          + (ttv.full_chars || "?") + " 字，请看上面的文字。念出的是 "
          + (ttv.reply_text || "").length + " 字。";
      } else if (ttv.status === "ok") {
        tn.textContent = "本题回答较短，语音念的是完整回答。";
      } else {
        tn.textContent = "";
      }
    }

    voiceRenderRunLoc(v);
    const co = voiceEl("voiceCompareOut");
    if (co) { co.classList.add("hidden"); co.innerHTML = ""; }
    voiceAddHistory(v);
    loadVoiceHistory();
  }

  // ------------------------------------------------- 第 6 周：会话 / 时序 / 澄清
  function voiceMs(x) {
    if (x == null || x === "") return "—";
    const n = Number(x);
    return isFinite(n) ? (n.toLocaleString("zh-CN") + " ms") : String(x);
  }

  function voiceRenderSessionOf(v) {
    const s = v.session || {};
    const sidEl = voiceEl("voiceSid");
    if (sidEl && s.session_id) {
      sidEl.textContent = String(s.session_id).slice(-8);
      sidEl.title = s.session_id;
    }
    const sq = voiceEl("voiceTurnSeq");
    if (sq && s.turn_seq != null) sq.textContent = String(s.turn_seq);
    const inf = voiceEl("voiceInflight");
    if (inf) {
      // ★ 在途以**本地**为准：服务端在收尾时会把 active 清空，
      //   但此刻用户眼里"这一轮还在跑"，所以这里显示本地记着的那个 request_id。
      inf.textContent = VOICE.inflight ? ("…" + String(VOICE.inflight).slice(-8)) : "无";
      inf.title = VOICE.inflight || "";
    }
    const cr = voiceEl("voiceClarifyRound2");
    if (cr) {
      const c = (v.clarify || {}).asked;
      cr.textContent = (c && v.final_state === "clarifying")
        ? (c.round + "/" + (c.max_rounds || 2)) : "无";
    }
  }

  /** 时序表：每一行都是服务端记录的字段，页面只做格式化，不做任何推算。 */
  function voiceRenderTiming(v) {
    const tb = voiceEl("voiceTiming");
    const nt = voiceEl("voiceTimingNote");
    if (!tb) return;
    const t = v.timing || {};
    const isText = v.audio_source === "text_input";
    const rows = [
      ["t0（服务端收到完整输入）", t.t0_utc ? nlFmtTime(t.t0_utc) : "—",
       "起点。录多久、点下去到发出这一段，不算进链路"],
      ["首次反馈（有了文本）", voiceMs(t.first_feedback_ms),
       isText ? "键盘补充轮：输入的瞬间就有文本" : "用户第一次拿到「我听到了」"],
      ["任务完成", voiceMs(t.task_done_ms), "业务工具返回（只读，或已真实下发采集）"],
      ["播报就绪", voiceMs(t.tts_ready_ms), "WAV 已合成，可以播"],
      ["前端本地提示", voiceMs(t.client_ack_ms), "浏览器自己报的，不是服务端实测"],
      ["合计", voiceMs(t.total_ms), "服务端处理总时长"],
    ];
    const c = v.cancel || {};
    if (c.requested_at_utc || c.seen_at_utc) {
      rows.push(["取消登记 → 被发现", (c.requested_at_utc ? nlFmtTime(c.requested_at_utc) : "—")
        + " → " + (c.seen_at_utc ? nlFmtTime(c.seen_at_utc) : "尚未发现"),
        "发现于阶段「" + (c.stage || "—") + "」，任务段" + (c.action_taken ? "已跑过（收不回）" : "一行未执行")]);
    }
    const sp = v.stop || {};
    if (sp.stopped_at_utc) {
      rows.push(["播报被停止", nlFmtTime(sp.stopped_at_utc),
        "停在第 " + (sp.played_ms == null ? "?" : sp.played_ms) + " ms（已播出的收不回）"]);
    }
    tb.innerHTML = "";
    for (const [k, val, why] of rows) {
      const tr = document.createElement("tr");
      const td1 = document.createElement("th"); td1.textContent = k;
      const td2 = document.createElement("td"); td2.textContent = val;
      const td3 = document.createElement("td"); td3.textContent = why;
      tr.appendChild(td1); tr.appendChild(td2); tr.appendChild(td3);
      tb.appendChild(tr);
    }
    if (nt) {
      const bits = [];
      const d = v.delivery || {};
      if (d.state) {
        bits.push("交付资格：playable=" + (d.playable ? "true" : "false")
          + "（" + d.state + "）" + (d.playable ? "" : " —— " + (d.why || "")));
      }
      if (v.inject) {
        bits.push("★ 这一轮用了注入：延迟 " + (v.inject.delay_ms || 0) + " ms / 任务 "
          + (v.inject.task_ms || 0) + " ms"
          + (v.inject.asr_text ? " / 识别文本被替换为「" + v.inject.asr_text + "」" : "")
          + "。这些**不是**引擎实测结果，记录里已标 injected。");
      }
      nt.textContent = bits.join(" ");
    }
  }

  function voiceHideClarify() {
    const box = voiceEl("voiceClarify");
    if (box) box.classList.add("hidden");
    const inp = voiceEl("voiceClarifyInput");
    if (inp) inp.value = "";
  }

  /**
   * 澄清卡片。三种形态必须分开，不能混成一句"请重说"：
   *   正在问  → 给候选项 + 打字框
   *   到上限  → 只给退路说明，不给输入（不能无限问下去）
   *   已消解  → 说明"按你补充的什么执行了"，不给输入
   */
  function voiceRenderClarify(v, replay) {
    const box = voiceEl("voiceClarify");
    if (!box) return;
    const c = v.clarify || {};
    const asked = c.asked;
    const asking = !!asked && v.final_state === "clarifying";
    const exhausted = (v.final_state === "clarify_exhausted");
    if (!asking && !exhausted && !c.how) { voiceHideClarify(); return; }
    box.classList.remove("hidden");

    const roundEl = voiceEl("voiceClarifyRound");
    const qEl = voiceEl("voiceClarifyQ");
    const opts = voiceEl("voiceClarifyOpts");
    const inp = voiceEl("voiceClarifyInput");
    const send = voiceEl("voiceClarifySend");
    const note = voiceEl("voiceClarifyNote");
    if (opts) opts.innerHTML = "";
    const setInput = (on) => {
      const dis = !on || !!replay;
      if (inp) { inp.disabled = dis; if (dis) inp.value = ""; }
      if (send) send.disabled = dis;
      if (inp) inp.placeholder = dis
        ? (replay ? "历史记录：不能对过去的问题作答" : "这一轮不再接受补充")
        : "也可以直接打字回答，例如「第二个」/「查看上次数据」";
    };

    if (asking) {
      if (roundEl) roundEl.textContent = "第 " + (asked.round || 1) + "/"
        + (asked.max_rounds || 2) + " 轮";
      if (qEl) qEl.textContent = asked.question || "你能再说清楚一点吗？";
      // 设备候选（如果有）只作为**事实**列出来，不预选、不排序暗示
      const cands = asked.candidates || [];
      if (opts) {
        cands.forEach((cd, i) => {
          const tag = document.createElement("span");
          tag.className = "vcl-cand";
          tag.textContent = (i + 1) + ". " + (cd.device_id || "—")
            + (cd.last_seen_utc ? " · 最后上报 " + nlFmtTime(cd.last_seen_utc) : "");
          opts.appendChild(tag);
        });
        (asked.options || []).forEach((opt) => {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "vcl-opt";
          b.textContent = opt;                     // textContent：不解析 HTML
          b.addEventListener("click", () => {
            voiceNotice("info", "已把你选的「" + opt + "」作为补充提交 —— 走的是同一条链路，"
              + "只是输入方式换成了键盘。");
            voiceSendText(opt);
          });
          opts.appendChild(b);
        });
      }
      if (note) {
        note.textContent = "这是第 " + (asked.round || 1) + " 轮反问，共 "
          + (asked.max_rounds || 2) + " 轮。你的回答会先被**纯规则**消解（序号/意图关键词/"
          + "设备名），消解成功才重放原句去执行 —— 系统不会替你猜。"
          + " 到目前为止这一轮**没有调用任何业务工具**：没读库、没向设备下发指令。"
          + (asked.repeated ? " 上一轮的补充没能消解，所以再问一次。" : "");
      }
      setInput(true);
      return;
    }

    if (exhausted) {
      if (roundEl) roundEl.textContent = "已到轮数上限";
      if (qEl) qEl.textContent = (asked && asked.question) || "已经问过两轮了。";
      if (note) {
        note.textContent = (asked && asked.fallback)
          || ((v.error && v.error.message) || "澄清到上限，已停下。")
          + " 这一轮**没有**调用任何业务工具。";
      }
      setInput(false);
      return;
    }

    // 已消解（这一轮是"回答上面的问题"的那一轮）
    if (roundEl) roundEl.textContent = "已消解";
    if (qEl) qEl.textContent = "按你的补充执行：" + (c.resolved_with || "—");
    if (note) {
      note.textContent = "消解方式：" + (c.how || "—")
        + "（纯规则匹配，不是模型自由发挥）。原句是「" + (c.base_query || "—") + "」。";
    }
    setInput(false);
  }

  // ---------------------------------------------------------------- 事件时序
  async function voiceLoadTimeline() {
    const box = voiceEl("voiceTimelineBox");
    const rowsEl = voiceEl("voiceTimelineRows");
    const meta = voiceEl("voiceTimelineMeta");
    if (!box || !rowsEl) return;
    box.classList.remove("hidden");
    rowsEl.textContent = "读取中…";
    const q = new URLSearchParams({ limit: "80" });
    if (VOICE.sid) q.set("session_id", VOICE.sid);
    try {
      const r = await fetch(API.voiceTimeline + "?" + q.toString(), { cache: "no-store" });
      const j = await r.json();
      if (!j || j.ok !== true) { rowsEl.textContent = "读取失败"; return; }
      const list = ((j.data || {}).events) || [];
      if (meta) {
        meta.textContent = (VOICE.sid ? ("会话 " + String(VOICE.sid).slice(-8)) : "全部会话")
          + " · " + list.length + " 条事件";
      }
      rowsEl.innerHTML = "";
      if (!list.length) {
        rowsEl.textContent = "还没有事件。说一句话之后再看。";
        return;
      }
      for (const ev of list) {
        const row = document.createElement("div");
        row.className = "vtl-row is-" + String(ev.event || "");
        const a = document.createElement("span"); a.className = "vtl-t";
        a.textContent = nlFmtTime(ev.at_utc);
        const b = document.createElement("b"); b.className = "vtl-k";
        b.textContent = ev.event || "—";
        const c = document.createElement("span"); c.className = "vtl-r";
        c.textContent = ev.request_id ? String(ev.request_id).slice(-10) : "—";
        const d = document.createElement("span"); d.className = "vtl-d";
        d.textContent = ev.detail ? JSON.stringify(ev.detail) : "";
        row.appendChild(a); row.appendChild(b); row.appendChild(c); row.appendChild(d);
        rowsEl.appendChild(row);
      }
    } catch (e) {
      rowsEl.textContent = "读取失败：" + String(e && e.message ? e.message : e);
    }
  }

  function voiceRenderRunLoc(v) {
    const dl = voiceEl("voiceRunLoc");
    if (!dl) return;
    const rl = v.run_location || {};
    const rows = [
      ["录音", (v.audio_source_label || v.audio_source) + " · " + (rl.capture || "—")],
      ["识别", rl.asr || "—"],
      ["任务", rl.task || "—"],
      ["合成", rl.tts || "—"],
      ["播放", rl.playback || "—"],
    ];
    dl.innerHTML = "";
    for (const [k, val] of rows) {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = val;
      dl.appendChild(dt);
      dl.appendChild(dd);
    }
  }

  // ---------------------------------------------------------------- 播放
  function voiceBindPlay() {
    const audio = voiceEl("voiceAudio");
    const btn = voiceEl("voicePlay");
    const ps = voiceEl("voicePlayState");
    if (btn && audio) {
      btn.addEventListener("click", () => {
        if (!audio.getAttribute("src")) return;
        audio.currentTime = 0;
        const p = audio.play();
        if (p && p.catch) {
          p.catch((e) => {
            voiceNotice("bad", "浏览器拒绝播放：" + (e && e.message ? e.message : e)
              + "。可能是没有用户交互、或没有可用的输出设备（耳机/扬声器）。"
              + "回答文字仍然完整可读。");
          });
        }
      });
      // ★ 只有真的开始播放才上报；上报的是"播放器开始播放"这一事实，
      //   不是"用户听到了"。后端也只记录这一个字段。
      audio.addEventListener("play", () => {
        if (ps) ps.textContent = "播放器已开始播放（是否真的听到只能由现场判断）";
        const v = VOICE.last;
        if (!v || !v.turn_id) return;
        // 正在播的就是这一轮 → 进"播报中"，停止按钮才有意义
        if (v.delivery && v.delivery.playable) voiceSetPhase("speaking", "正在播这一轮");
        fetch(API.voiceTurn + "/" + encodeURIComponent(v.turn_id) + "/played", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reported_by: "browser:" + (navigator.userAgent.indexOf("Edg") >= 0 ? "Edge" : "Chrome") }),
        }).catch(() => {});
      });
      audio.addEventListener("ended", () => {
        if (ps) ps.textContent = "播放结束（播放器侧事实）";
        // ★ 播放结束 ≠ 用户听到，只是"这一轮没有声音在放了"
        if (VOICE.phase === "speaking") voiceSetPhase("idle", "播放结束（播放器侧事实）");
        // 前端能上报的第二个本地事实：playback_ended
        if (VOICE.sid) {
          fetch(API.voiceSession + "/" + encodeURIComponent(VOICE.sid) + "/state", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ state: "playback_ended", by: "browser" }),
          }).catch(() => {});
        }
      });
      audio.addEventListener("pause", () => {
        if (VOICE.phase === "speaking" && !audio.ended) {
          if (ps) ps.textContent = "已暂停（本次没有上报「停止播报」）";
        }
      });
      audio.addEventListener("error", () => {
        if (ps) ps.textContent = "音频加载失败";
        voiceNotice("warn", "播报音频没能加载（可能是文件已被清理）。文字回答仍然完整可读。");
      });
    }
    const mine = voiceEl("voicePlayMine");
    const mineAudio = voiceEl("voiceMineAudio");
    if (mine && mineAudio) {
      mine.addEventListener("click", () => {
        if (!mineAudio.getAttribute("src")) return;
        mineAudio.currentTime = 0;
        const p = mineAudio.play();
        if (p && p.catch) p.catch(() => {});
      });
    }
  }

  // ---------------------------------------------------------------- 对照
  async function voiceCompare() {
    const v = VOICE.last;
    if (!v || !v.transcript) {
      voiceNotice("warn", "这一轮没有识别文本，无法对照。");
      return;
    }
    const out = voiceEl("voiceCompareOut");
    if (out) {
      out.classList.remove("hidden");
      out.textContent = "正在把「" + v.transcript + "」送进文字入口…";
    }
    const body = { q: v.transcript, wait_ms: 6000 };
    const dev = (state.deviceFilter || "").trim();
    if (dev) body.device_id = dev;
    try {
      const r = await fetch(API.nl, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const txt = await r.text();
      let j = null;
      try { j = JSON.parse(txt); } catch (e2) { j = null; }
      if (!j || j.ok !== true) {
        if (out) out.textContent = "文字入口返回失败：" + ((j && j.error && j.error.message) || ("HTTP " + r.status));
        return;
      }
      const d = j.data || {};
      const vv = v.task || {};
      const same = (a, b) => JSON.stringify(a == null ? null : a) === JSON.stringify(b == null ? null : b);
      const rows = [
        ["解析方式", "语音入口：识别文本 " + v.transcript, "文字入口：直接输入", ""],
        ["意图", vv.intent, d.intent, same(vv.intent, d.intent)],
        ["claim", vv.claim, d.claim, same(vv.claim, d.claim)],
        ["状态", vv.remote_status, d.status, same(vv.remote_status, d.status)],
        ["来源", vv.source, (d.evidence || {}).source, same(vv.source, (d.evidence || {}).source)],
        ["回答", vv.answer, d.answer, same(vv.answer, d.answer)],
      ];
      if (out) {
        out.innerHTML = "";
        const h = document.createElement("div");
        h.className = "vc-head";
        h.textContent = "同一句话，两条入口的结论对照";
        out.appendChild(h);
        for (const [k, a, b, eq] of rows) {
          const row = document.createElement("div");
          row.className = "vc-row" + (eq === true ? " is-same" : (eq === false ? " is-diff" : ""));
          const k1 = document.createElement("b"); k1.textContent = k;
          const a1 = document.createElement("span"); a1.textContent = String(a == null ? "—" : a).slice(0, 90);
          const b1 = document.createElement("span"); b1.textContent = String(b == null ? "—" : b).slice(0, 90);
          const m1 = document.createElement("i");
          m1.textContent = eq === true ? "一致" : (eq === false ? "不同" : "—");
          row.appendChild(k1); row.appendChild(a1); row.appendChild(b1); row.appendChild(m1);
          out.appendChild(row);
        }
        const allSame = rows.filter((r) => r[3] === true).length;
        const foo = document.createElement("div");
        foo.className = "vc-foot";
        foo.textContent = allSame === rows.length - 1
          ? "两条入口结论完全一致 —— 说明语音只是换了一种「输入方式」，任务语义没有变。"
          : "两条入口有差异（" + (rows.length - 1 - allSame) + " 项不同）。看上面标红的行，"
            + "通常是因为两轮之间设备状态变了（例如这期间来了新数据），不是解析不一致。";
        out.appendChild(foo);
      }
      addLog("info", "语音/文字入口对照完成");
    } catch (e) {
      if (out) out.textContent = "对照请求失败：" + String(e && e.message ? e.message : e);
    }
  }

  // ---------------------------------------------------------------- 引擎状态
  async function loadVoiceHealth() {
    const el = voiceEl("voiceEngine");
    try {
      const r = await fetch(API.voiceHealth, { cache: "no-store" });
      const j = await r.json();
      const d = j.data || {};
      const e = d.engine || {};
      if (!el) return;
      if (e.ok) {
        const a = e.asr || {};
        const t = e.tts || {};
        el.textContent = "引擎就绪 · 识别 " + (a.backend || "?") + "（离线）· 合成 " + (t.backend || "?")
          + " · " + (d.engine_url || "");
        el.className = "chip chip-ok";
      } else {
        el.textContent = "语音引擎未就绪：" + (e.error || "无法连接 " + (d.engine_url || ""));
        el.className = "chip chip-bad";
      }
    } catch (err) {
      if (el) { el.textContent = "语音引擎状态读取失败"; el.className = "chip chip-bad"; }
    }
  }

  // ---------------------------------------------------------------- 历史
  function voiceAddHistory(v) {
    // 由 loadVoiceHistory 统一刷新；这里只打日志，避免列表重复
    if (v && v.turn_id) addLog("info", "语音 turn " + v.turn_id.slice(0, 10) + "… → " + v.final_state);
  }

  async function loadVoiceHistory() {
    const box = voiceEl("voiceHistory");
    const chip = voiceEl("voiceHistoryChip");
    if (!box) return;
    try {
      const r = await fetch(API.voiceTurns + "?limit=8", { cache: "no-store" });
      const j = await r.json();
      const list = ((j.data || {}).turns) || [];
      if (chip) chip.textContent = "历史：" + list.length + " 条";
      box.innerHTML = "";
      if (!list.length) {
        const e = document.createElement("div");
        e.className = "voice-hist-empty";
        e.textContent = "还没有语音记录。点上面的按钮说一句话试试。";
        box.appendChild(e);
        return;
      }
      for (const t of list) {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "voice-hist-row is-" + String(t.final_state || "");
        const a = document.createElement("span"); a.className = "vh-time";
        a.textContent = nlFmtTime(t.created_at_utc);
        const b = document.createElement("span"); b.className = "vh-state";
        b.textContent = t.final_state || "—";
        const c = document.createElement("span"); c.className = "vh-text";
        c.textContent = t.transcript ? ("「" + t.transcript + "」") : "（无识别文本）";
        const d2 = document.createElement("span"); d2.className = "vh-src";
        d2.textContent = (t.audio_source_label || t.audio_source || "—");
        row.appendChild(a); row.appendChild(b); row.appendChild(c); row.appendChild(d2);
        row.addEventListener("click", () => voiceShowTurn(t.turn_id));
        box.appendChild(row);
      }
    } catch (e) {
      if (chip) chip.textContent = "历史读取失败";
    }
  }

  async function voiceShowTurn(tid) {
    try {
      const r = await fetch("/api/v1/voice/turn/" + encodeURIComponent(tid), { cache: "no-store" });
      const j = await r.json();
      if (!j.ok) { voiceNotice("bad", "读取这条记录失败：" + ((j.error || {}).message || "")); return; }
      // ★ replay=true：看历史不许改当前半双工阶段，否则"看了一条旧记录"会把
      //   正在处理的这一轮从「处理中」变成「播报中」——那是最典型的假状态。
      voiceRender(j.data, true);
      addLog("info", "已载入历史语音 " + tid.slice(0, 10) + "…（只读回看，不改当前状态）");
    } catch (e) {
      voiceNotice("bad", "读取这条记录失败：" + String(e && e.message ? e.message : e));
    }
  }

  // ---------------------------------------------------------------- 备用路径：文件
  async function voiceUseFile(file) {
    if (!file) return;
    try {
      voiceNotice("info", "正在解码「" + file.name + "」…");
      const ab = await file.arrayBuffer();
      // 在一个 16 kHz 的离线上下文里解码：规范要求重采样到上下文采样率
      const off = new (window.OfflineAudioContext || window.webkitOfflineAudioContext)(1, 16000, VOICE.TARGET_RATE);
      const decoded = await off.decodeAudioData(ab);
      const ch0 = decoded.getChannelData(0);
      let mono = ch0;
      if (decoded.numberOfChannels > 1) {
        // 多声道先真实混成单声道（平均值），不是随便取一路
        mono = new Float32Array(ch0.length);
        for (let c = 0; c < decoded.numberOfChannels; c++) {
          const d = decoded.getChannelData(c);
          for (let i = 0; i < mono.length; i++) mono[i] += d[i] / decoded.numberOfChannels;
        }
      }
      const int16 = voiceResample16k(mono, decoded.sampleRate);
      const blob = voiceEncodeWav(int16, VOICE.TARGET_RATE);
      voiceEl("voiceSource").value = "uploaded_file";
      voiceUpdateSrcNote();
      voiceNotice("info", "文件已转为 16 kHz/16 bit/单声道 WAV（" + blob.size + " 字节），正在提交识别…");
      addLog("info", "使用授权录音文件：" + file.name + "（" + decoded.sampleRate + " Hz / "
        + decoded.numberOfChannels + " 声道 → 16k mono）");
      await voiceSend(blob, "uploaded_file");
    } catch (e) {
      voiceNotice("bad", "这个文件解不开：" + (e && e.message ? e.message : e)
        + "。请换一段 WAV/MP3，或直接改用文字入口。");
      addLog("bad", "授权录音解码失败：" + String(e && e.message ? e.message : e));
    }
  }

  function voiceUpdateSrcNote() {
    const note = voiceEl("voiceSrcNote");
    const sel = voiceEl("voiceSource");
    if (!note || !sel) return;
    if (sel.value === "uploaded_file") {
      note.innerHTML = "当前来源：<b>上传的授权录音文件</b>（备用路径）。"
        + "录音不是现场采的，但识别、任务、合成都是真实的 —— 记录里会如实标注来源。";
    } else {
      note.innerHTML = "录音发生在<b>本机浏览器</b>；识别与合成跑在<b>本机语音引擎</b>。"
        + "运行位置会随每条记录一起保存。";
    }
  }

  function bindVoiceUI() {
    const btn = voiceEl("btnVoice");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const open = togglePanel("voicePanel", btn);
      $("toolHint").textContent = open
        ? "语音助手：点一下开始说，再点一下结束；识别失败时文字入口照常可用"
        : "语音助手已收起";
      if (open) {
        loadVoiceHealth(); loadVoiceHistory(); updateVoiceCtx();
        voiceSetPhase(VOICE.phase);     // 把按钮文案/状态条按当前阶段重新刷一遍
        voiceEnsureSession();           // 打开面板就把会话准备好，别等到第一次提交
      }
    });
    const closeBtn = voiceEl("closeVoice");
    if (closeBtn) closeBtn.addEventListener("click", () => {
      setPanel("voicePanel", false); btn.classList.remove("is-on");
    });
    const talk = voiceEl("voiceTalk");
    if (talk) talk.addEventListener("click", () => {
      if (VOICE.recording) voiceStopAndSend(); else voiceStart();
    });
    // ---- 第 6 周：唯一的「停止」按钮 ----
    const stopBtn = voiceEl("voiceStopAll");
    if (stopBtn) stopBtn.addEventListener("click", voiceStopAll);
    const newSess = voiceEl("voiceNewSession");
    if (newSess) newSess.addEventListener("click", voiceNewSession);
    const tlBtn = voiceEl("voiceTimelineBtn");
    if (tlBtn) tlBtn.addEventListener("click", voiceLoadTimeline);
    const tlClose = voiceEl("voiceTimelineClose");
    if (tlClose) tlClose.addEventListener("click", () => {
      const b = voiceEl("voiceTimelineBox");
      if (b) b.classList.add("hidden");
    });
    // ---- 第 6 周：澄清卡片 ----
    const cSend = voiceEl("voiceClarifySend");
    if (cSend) cSend.addEventListener("click", () => {
      const inp = voiceEl("voiceClarifyInput");
      voiceSendText(inp ? inp.value : "");
    });
    const cInp = voiceEl("voiceClarifyInput");
    if (cInp) cInp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); voiceSendText(cInp.value); }
    });
    const clr = voiceEl("voiceClear");
    if (clr) clr.addEventListener("click", () => {
      voiceResetStages();
      voiceNoticeHide();
      voiceHideClarify();
      voiceFinalText("还没有提交过语音。");
      const res = voiceEl("voiceResult");
      if (res) res.classList.add("hidden");
      VOICE.last = null;
      voiceEl("voiceTimer").textContent = "0.0 s";
      voiceMeterReset();
      // ★ 清空只是"清屏幕"，不改会话、不取消在途任务 —— 否则用户会以为点了清空就取消了。
      voiceSetPhase(VOICE.recording ? "listening" : "idle");
    });
    const sel = voiceEl("voiceSource");
    if (sel) sel.addEventListener("change", voiceUpdateSrcNote);
    const file = voiceEl("voiceFile");
    if (file) file.addEventListener("change", (e) => {
      const f = e.target && e.target.files && e.target.files[0];
      if (f) voiceUseFile(f);
      file.value = "";
    });
    const cmp = voiceEl("voiceCompare");
    if (cmp) cmp.addEventListener("click", voiceCompare);
    const hist = voiceEl("voiceRefreshHist");
    if (hist) hist.addEventListener("click", loadVoiceHistory);
    $("deviceSelect").addEventListener("change", () => updateVoiceCtx());
    voiceBindPlay();
    voiceUpdateSrcNote();
    voiceSetPhase("idle");

    // 键盘：按住空格说话（在面板内且不在输入框里时生效）
    document.addEventListener("keydown", (e) => {
      if (e.code !== "Space" || e.repeat) return;
      const panel = voiceEl("voicePanel");
      if (!panel || panel.classList.contains("hidden")) return;
      const tag = (document.activeElement && document.activeElement.tagName) || "";
      if (/INPUT|TEXTAREA|SELECT|BUTTON/.test(tag)) return;
      e.preventDefault();
      voiceStart();
    });
    document.addEventListener("keyup", (e) => {
      if (e.code !== "Space") return;
      if (!VOICE.recording) return;
      e.preventDefault();
      voiceStopAndSend();
    });
  }

  function updateVoiceCtx() {
    const el = voiceEl("voiceCtx");
    if (!el) return;
    const dev = (state.deviceFilter || "").trim();
    el.textContent = dev ? ("设备上下文：" + dev) : "全部设备：请在句子里点名设备";
  }

  window.__voiceDebug = {
    render: voiceRender,
    error: voiceRenderError,
    stageSet: voiceStageSet,
    reset: voiceResetStages,
    notice: voiceNotice,
    finalText: voiceFinalText,
    state: () => VOICE,
    encodeWav: voiceEncodeWav,
    compare: voiceCompare,
    FINAL_TEXT,
    // ---- 第 6 周：半双工 / 取消 / 澄清 ----
    phase: () => VOICE.phase,
    setPhase: voiceSetPhase,
    PHASE_TEXT,
    stopAll: voiceStopAll,
    cancel: voiceCancelInflight,
    stopSpeech: voiceStopSpeech,
    abortRecord: voiceAbortRecord,
    abandonClarify: voiceAbandonClarify,
    sendText: voiceSendText,
    submit: voiceSubmit,
    dropLate: voiceDropLate,
    ensureSession: voiceEnsureSession,
    newSession: voiceNewSession,
    refreshSession: voiceRefreshSession,
    sid: () => VOICE.sid,
    inflight: () => VOICE.inflight,
    renderTiming: voiceRenderTiming,
    renderClarify: voiceRenderClarify,
    hideClarify: voiceHideClarify,
    timeline: voiceLoadTimeline,
    injectParams: voiceInjectParams,
  };

  // 暴露给自检脚本（与 window.__imuDebug 同一套路，便于断言三种事实下的文案）
  window.__helpDebug = {
    statusText:   helpItemStatusText,
    remoteAckText: helpRemoteAckText,
    selected:     () => hstate.selected,
    reload:       loadHelpList,
    select:       selectHelpRow,     // 供自检模拟"点某一行"
    renderDetail: renderHelpDetail,  // 供自检直接渲染给定事件对象
  };

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
  bindHelpUI();
  bindNlUI();
  bindVoiceUI();
  renderLogs();

  loadDevices();
  loadVoiceHealth();
  seedHistory().then(() => {
    pollNew();
    setInterval(pollNew, POLL_MS);          // ★ 必须是第一个 setInterval（自检脚本按序号触发轮询）
    requestAnimationFrame(renderLoop);      // ★ 开启平滑渲染循环（跟随显示器刷新率）
  });
})();