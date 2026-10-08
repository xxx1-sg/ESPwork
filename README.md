# ESP32-S3-EYE 真实物理传感器采集 → VPS 后端 → Web 实时可视化

一套完整可跑通的端到端系统：**板载物理传感器真实读数 → 开发板独立 Wi-Fi 上传 → 后端持久化 → 网页实时展示**。

> ### 关于「真实数据」的硬性约定
> 本工程**没有任何随机数、写死值或假测试数据**参与数据链路：
> - 板端只从 QMA6100P 芯片寄存器读取物理测量值（`firmware/src/imu_qma6100p.c`）；
> - 传感器读失败时，固件**如实报错并跳过本次上传**，不会用任何构造值顶替；
> - 后端缺 `raw` 字段直接 400 拒收，**不会代为生成/补齐数据**；
> - 网页每个数字都来自 API，页面代码里不存在任何传感器数值常量；
> - 数据库里为空时，接口如实返回 404，页面显示「等待设备首次上传」。
> - 后端还额外保存每一条上传的**原始报文 + SHA256**，可逐字节回溯证明未篡改。

---

## 0. 当前状态（已在真实硬件上跑通并验证）

| 环节 | 状态 | 证据 |
|---|---|---|
| 板端读取真实 IMU（QMA6100P，I2C 0x12 / CHIP_ID 0x90） | ✅ 实机运行 | 串口 `[DATA] … raw=(…) g=(…) ms2=(…) \|a\|=0.94~0.99g` |
| 开发板独立 Wi-Fi 联网（不依赖 USB） | ✅ 实机运行 | `获取 IP 成功: 10.1.42.183 (SSID="YOUR_WIFI_SSID")` |
| SNTP 对时 + 本地时间戳 | ✅ 实机运行 | `SNTP 对时成功, 设备本地时间: 2026-10-08 13:43:57 +0800` |
| 高密度采集：**10Hz 采样**（100ms 一个点） | ✅ 实测 10.01 Hz（15 分钟 9000 点） | 采样间隔中位 **100.0ms**，97% 落在 90~110ms（`evidence/final_10hz_suite.log`） |
| 攒包上传：1 秒一个包，**一包 10 个点** | ✅ 实测 | 包间隔中位 **1.01s**，包大小分布恒为 `[10]` 点/包 |
| 采集与上传**任务解耦**（网络慢不再拖慢采样） | ✅ 实机运行 | `sampler_task`(core1) / `uploader_task`(core0) + 两个队列 + 静态包池 |
| 定时 POST 到后端（**真实网络**，非 USB 桥接） | ✅ 实机运行 | `[POST] seq=N..M n=10 http=201 resp={…}`；后端记录来源 IP = 板子 IP |
| 后端持久化（逐字节原文 + 解析字段） | ✅ 已入库 1.7 万条 | `raw_uploads` 与 `sensor_records` 条数一致；`verify_ok` **全部为 1** |
| 网页实时显示（数值全部来自接口） | ✅ 契约自检通过 | 30 个 DOM 齐全、15 个字段齐全、**0 处兜底假值** |
| 网页数据密度（10Hz 下不丢样本） | ✅ 实测 60s 窗口 **590 个真实采样点** | `evidence/final_10hz_suite.log`（`after_id` 增量拉取） |
| 网页渲染流畅度 | ✅ 实测 **61 FPS @60Hz / 120 FPS @120Hz** | `requestAnimationFrame` 每帧重绘，曲线沿真实时间轴连续滚动 |
| 每帧开销 | ✅ 已按帧节流 | 61 帧内 DOM 写入 0 次、布局读取 0 次（自检断言） |
| 断流行为（保留时间戳 + 提示「暂无新数据」） | ✅ 打桩 DOM 实测通过 | `node tools/test_web_stale.js` 全部用例通过 |
| 三方核对（串口 = 数据库 = 网页接口） | ✅ **逐采样点全部一致** | `evidence/verify_report.json`（键 = `boot_count+seq+sample_idx`） |
| 长时间稳定性 | ✅ **单次开机连续采集 15 分钟无重启** | `evidence/final_10hz_suite.log` 里 boot 808 运行 898.9s / 9000 点 |

复现命令与逐条对照数据见 **[docs/05_真实硬件采集记录样例.md](docs/05_真实硬件采集记录样例.md)**。

> 📌 **当前实际运行方式（如实说明）**：本工程**当前跑的是「开发板 → 本机后端」的同网段直连**
> —— 板子走 Wi-Fi、后端跑在 `10.1.41.182:8000`，两边在同一个可路由网络里，**全程走真实网络**
> （不是 USB 桥接），RTT 约 50~60ms。
> 之所以从「Cloudflare 临时隧道 + HTTPS」换成直连：10Hz 高密度采集下，隧道单次请求要约 0.88s，
> 且临时隧道地址每次重启都变、本机 DNS 还被投毒过（`api.trycloudflare.com` 被解析到错误 IP），
> 会频繁打断长时间采集。**协议层代码一行没删**：把 `APP_SERVER_URL` 换回 `https://…`
> 就回到「真实 TLS 校验 + 公网」的 VPS 链路（`APP_TLS_TRUST_MODE 1` + 内嵌 CA 逻辑原样保留）。
> `backend/deploy/` 里的 systemd / nginx / 一键部署脚本**尚未在真实 VPS 上执行过**（本次没有可用 VPS），
> 部署到 VPS 请按 [docs/03](docs/03_VPS部署.md) 操作。

> ⚠️ 已知硬件注意事项：本机 USB 口供电偏弱时，板子会在 Wi-Fi 射频启动瞬间掉电重启
> （表现为约 1 次/秒反复重启，串口输出断在 `wifi_init` 之后）。固件已把 Wi-Fi 最大发射功率
> 降到 8dBm 以降低峰值电流（`APP_WIFI_MAX_TX_POWER_QDBM`），实测可在该 USB 口稳定运行；
> 换用供电充足的口/线后可调回 78（19.5dBm）。

---

## 1. 系统架构

```
┌──────────────────────────────┐        ┌───────────────────────────┐        ┌──────────────────────┐
│  ESP32-S3-EYE（实体开发板）   │        │  VPS / 本机后端           │        │  Web 前端            │
│                              │        │  backend/server.py        │        │  web/                │
│  板载 QMA6100P 加速度计       │  Wi-Fi │                           │  HTTP  │  index.html          │
│  I2C: SDA=GPIO4 SCL=GPIO5    │ ─────► │  POST /api/v1/ingest      │ ◄───── │  app.js              │
│  地址 0x12, CHIP_ID 0x90     │ 1包/秒 │    ├ 原始报文原样入库      │ 0.8s   │  style.css           │
│                              │ 10点/包│    └ 每点一行入库          │ 增量   │                      │
│  sampler_task  10Hz 采集      │        │  GET  /api/v1/latest …    │ 轮询   │  断流时保留时间戳      │
│      │  指针队列（静态包池）   │        │  GET  /records?after_id=  │        │  并提示「暂无新数据」  │
│  uploader_task 攒包 + POST    │        │  SQLite 持久化            │        │                      │
│  USB 口：只用于烧录/看日志    │        │  （不伪造、不补齐）        │        │                      │
└──────────────────────────────┘        └───────────────────────────┘        └──────────────────────┘
        ▲ 串口只做调试                      ▲ 原样保存，不伪造
        └── 三方核对：串口实测 = 数据库记录 = 网页数值（键 = boot+seq+包内序号）
```

> **为什么要拆成两个任务**：一开始是"采集→上传→再采集"的单循环，结果隧道一次超时（10 秒）
> 加上重试就把采样卡住 33 秒，数据出现 62 秒空洞。改成 **10Hz 采集任务 + 独立上传任务**后，
> 网络再慢也只影响上传队列（丢包时明确打印 `ESP_LOGW` 说明丢弃原因），**采样节奏始终 100ms 不变**。

---

## 2. 目录结构

```
D:\ESP
├── firmware/                       # 板端工程（PlatformIO + ESP-IDF，带详细注释）
│   ├── platformio.ini
│   ├── sdkconfig.defaults
│   ├── CMakeLists.txt
│   └── src/
│       ├── main.c                  # 主流程：sampler_task(10Hz) + uploader_task(攒包上传)
│       ├── imu_qma6100p.c/.h       # QMA6100P 真实读数驱动（寄存器级）
│       ├── uploader.c/.h           # Wi-Fi 联网 + SNTP 对时 + HTTP(S) POST
│       ├── app_config.h            # ★ Wi-Fi / 服务器地址 / 令牌 / 采样率 在这里改
│       └── CMakeLists.txt
├── backend/                        # 后端服务（零依赖，Python 标准库）
│   ├── server.py
│   ├── README.md                   # 接口文档
│   └── deploy/                     # systemd / nginx / 一键部署脚本
├── web/                            # 前端（原生 JS，无构建步骤）
│   ├── index.html  app.js  style.css
├── tools/                          # 辅助工具
│   ├── serial_watch.py             # ★ 抓串口（"开两次"策略，抗芯片复位重枚举）
│   ├── serial_capture.py           # 抓串口日志（简单版，留证据用）
│   ├── verify_three_way.py         # ★ 三方核对（键 = boot+seq+包内序号，含增量接口核对）
│   ├── analyze_db_rate.py          # ★ 用数据库当观察者统计真实密度（零干扰、不怕复位）
│   ├── analyze_rate.py             # ★ 从串口日志统计采样节奏（用板端 up= 单调时钟）
│   ├── live_web_probe.js           # ★ 真后端 + 真 app.js 的实时网页探针（10Hz 不丢样本）
│   ├── start_system.ps1            # ★ 一条命令起「后端 + 网页」（可选 -Tunnel 一起开穿透）
│   ├── decode_backtrace.py         # ★ 把 `Guru Meditation` 崩溃地址翻译成函数/行号
│   ├── check_web_contract.py       # ★ 网页↔接口契约自检（DOM/字段/有无假值）
│   ├── test_web_stale.js           # ★ 前端断流行为实测（打桩 DOM 跑真实 app.js）
│   ├── gen_ca_header.py            # ★ 抓取服务器真实 CA 链 → 生成固件内嵌证书
│   ├── switch_endpoint.py          # ★ 换后端地址一条命令（改 URL+重生成 CA+重烧）
│   ├── selftest_backend.py         # 后端自检（用临时库，不污染生产）
│   ├── fix_idf_venv.py             # 修复 ESP-IDF Python 虚拟环境（见 docs/02）
│   ├── fetch_url.py                # 通用下载器（本机 curl 被拦，用 Python 下载）
│   └── bin/cloudflared.exe         # 内网穿透（本机联调用，可选）
├── docs/                           # 文档
│   ├── 01_硬件接线与传感器说明.md
│   ├── 02_编译与烧录.md
│   ├── 03_VPS部署.md
│   ├── 04_三方核对验证.md
│   └── 05_真实硬件采集记录样例.md   # ★ 真实采集数据
└── evidence/                       # 实测证据（串口日志 / 数据库快照 / 核对报告）
```

---

## 3. 快速开始

### ① 部署后端

```bash
# VPS（推荐）：一键脚本
sudo bash backend/deploy/install_vps.sh "你的设备令牌"
# 本机测试（Windows，一条命令起后端+网页，并打印板子该填的地址）：
powershell -ExecutionPolicy Bypass -File tools\start_system.ps1
# 本机测试（通用）：
python backend/server.py
```

> 本机测试时后端要监听 `0.0.0.0`（默认已是），否则开发板连不进来。
> `start_system.ps1` 还会用本机真实内网 IP 打印出 `APP_SERVER_URL` 该填的那一行，
> 不需要自己 `ipconfig` 猜地址。

### ② 改固件配置

编辑 `firmware/src/app_config.h`：

```c
// 主用 2.4G 热点（ESP32-S3 不支持 5GHz！）
#define APP_WIFI_SSID           "你的2.4G热点"
#define APP_WIFI_PASSWORD       "密码"
// 备用热点：主用连不上会自动切过去（可留成同一个）
#define APP_WIFI_FALLBACK_SSID      "备用热点"
#define APP_WIFI_FALLBACK_PASSWORD  "备用密码"

// 后端地址：https:// 开头会自动启用真实 TLS 校验（见下面 APP_TLS_TRUST_MODE）
//   同网段直连（当前用的这条）: "http://10.1.41.182:8000/api/v1/ingest"
//   VPS / 隧道:                "https://你的域名/api/v1/ingest"
#define APP_SERVER_URL    "http://10.1.41.182:8000/api/v1/ingest"
#define APP_DEVICE_TOKEN  "你的设备令牌"      // 与后端 SENSOR_TOKEN 一致

// 采集密度：100ms 采一个点（10Hz），攒够 10 个点（1 秒）发一次 HTTPS
#define APP_SAMPLE_PERIOD_MS  100     // 采样周期
#define APP_BATCH_SIZE        10      // 一包几个点（= 上传周期 1000ms / 采样周期 100ms）
#define APP_MAX_MERGE         30      // 网络慢时最多把几个包合成一次请求（只影响请求数，不丢点）

// HTTPS 校验方式：0=用 IDF 内置根证书包；1=用工程内嵌的真实 CA 链；
//                 2=不校验（★仅本机排错，生产严禁）
#define APP_TLS_TRUST_MODE  1
```

> **采集密度与上传频率是两件事**：板子 10Hz 采样，但只 1Hz 发请求（一包 10 个点）。
> 这样既有 10Hz 的数据密度，又不会把连接数打满。网络慢时上传任务会把积压的包
> 合并成一次请求（默认最多 30 个包），采样节奏不受影响。

> 若目标域名的证书根不在 ESP-IDF 内置根证书包里（例如 Cloudflare 隧道用的
> Google Trust Services），保持 `APP_TLS_TRUST_MODE 1`，并运行
> `python tools/gen_ca_header.py` 重新生成内嵌 CA —— **不要**为了省事把校验关掉。
> 换服务器地址时可以直接用一条命令完成「改地址 + 重生成 CA + 重新编译烧录」：
>
> ```powershell
> python tools\switch_endpoint.py --url https://新域名/api/v1/ingest
> ```

### ③ 编译 + 烧录（USB 口仅作调试通道）

```powershell
cd firmware
..\.venv\Scripts\python.exe -m platformio run -t upload
..\.venv\Scripts\python.exe -m platformio device monitor
```

### ④ 看网页

浏览器打开后端地址（本机 `http://127.0.0.1:8000/`，VPS `https://你的域名/`）。

图表用 `requestAnimationFrame` **每帧重绘（约 60 FPS）**，横轴按真实时间定位，
所以曲线是连续左移的平滑滚动，而不是"来一条数据跳一格"。可用 URL 参数调整：

| 参数 | 默认 | 含义 |
|---|---|---|
| `poll` | 800 | 轮询后端间隔（ms）。用 `after_id` 增量拉取，10Hz 数据一个点都不会漏 |
| `stale` | 6000 | 超过多久没有新数据就提示「暂无新数据」 |
| `window` | 60 | 曲线时间窗口（秒）。10Hz 下这个窗口里约有 600 个真实采样点 |
| `history` | 700 | 页面初始化时拉取的历史点数（后端上限 2000） |
| `chunk` | 400 | 每次增量拉取的最大条数 |

例：`http://127.0.0.1:8000/?window=30&poll=500`

> 平滑只发生在**画布动画**上：卡片里的数字、时间戳、原始码值全部原样来自接口，
> 不做任何插值或滚动动画（自检脚本会断言"跑 61 帧后数值仍精确等于接口值"）。
> 页面切到后台标签页时会自动停止重绘，不白烧 CPU。

> **10Hz 下为什么必须用增量拉取**：以前每次轮询只取 `/api/v1/latest` 一条，
> 800ms 里新产生的 8 个点会被直接跳过（曲线一段一段地跳）。
> 现在改成 `GET /api/v1/records?after_id=<最后一条 id>&limit=400`：
> 后端返回 `id > after_id` 的记录（按 id 升序），网页按 id 去重后**逐点追加**，
> 一条不会重复、也一条不会丢。`tools/live_web_probe.js` 就是专门验证这件事的：
> 它拿真实后端驱动真实 `app.js`，断言"60 秒窗口里确实有 580+ 个真实采样点"。

> **帧率上限说明**：`requestAnimationFrame` 与显示器垂直同步对齐，所以帧率上限 = 显示器刷新率
> （60Hz 屏就是 60FPS，120Hz 屏自动 120FPS，不需要改任何代码）。页脚会实测并显示
> `显示器 ≈60Hz · 中位帧 16.7ms · 最慢 xx ms` —— 如果 FPS 明显低于这个上限，说明是掉帧
> （性能问题），而不是被上限卡住；正常应看到 FPS 与括号里的刷新率一致。

### ⑤ 三方核对

```powershell
# ① 抓串口（注意：打开串口会让芯片复位一次，属正常现象）
python tools\serial_watch.py --port COM5 --seconds 70 --out evidence\serial_run.log
# ② 三方核对：串口 = 数据库 = 接口；键 = boot_count + seq + sample_idx
python tools\verify_three_way.py --serial evidence\serial_run.log ^
       --db backend\data\sensor.db --api http://127.0.0.1:8000 --out evidence\verify_report.json
# ③ 网页↔接口契约 + 前端行为 + 实时探针（不需要浏览器）
python tools\check_web_contract.py --api http://127.0.0.1:8000
node tools\test_web_stale.js
node tools\live_web_probe.js --seconds 15
# ④ 数据密度（零干扰：只读数据库，不碰串口，所以不会打断板子）
python tools\analyze_db_rate.py --minutes 10
```

> 抓日志建议用 `serial_watch.py`：打开 USB-Serial-JTAG 串口会让芯片复位并重新枚举 USB，
> 普通工具容易因此丢掉开机日志或读不到数据，该脚本用「开两次」策略规避（见 docs/05 第 8.3 节）。
> **想看长时间稳定性就别抓串口**（每次抓都会复位板子）：用 `analyze_db_rate.py` 只读数据库，
> 它能直接告出"这段时间里有没有出现短命 boot"——重启循环一眼可见。

---

## 4. 数据包字段（开发板每次上报）

板子 **1 秒发一个包**，包里是这一秒采到的 **10 个真实采样点**（每点 100ms 一个）。
每个采样点**自带 `seq` 与包内序号 `idx`**，所以不管是"一包 10 点"还是"网络慢时几个包合并成一次请求"，
每个点都能与串口 `[DATA]` 行、数据库行一一对上（这也是三方核对的键）。

包级字段：

| 字段 | 含义 |
|---|---|
| `device_id` | 设备唯一 ID，由芯片 eFuse MAC 派生（重启不变） |
| `boot_count` | 第几次开机（重启后 `seq` 从 1 重新开始，所以核对必须带它） |
| `sample_period_ms` / `sample_count` | 采样间隔（100ms）/ 本包点数（10） |
| `seq_first` / `seq_last` | 本包第一个 / 最后一个采样点的全局序号 |
| `sensor.*` | 型号、I2C 地址、CHIP_ID、量程、灵敏度(LSB/g)、输出率、平均次数 |
| `unit.*` | 每个数值的物理单位声明 |
| `samples[]` | **本包的全部采样点**（下面每项都在数组里逐点给出） |

`samples[]` 里每个点：

| 字段 | 含义 |
|---|---|
| `seq` / `idx` | 采样点全局序号 / 包内第几个（1..N） |
| `up_ms` | 上电以来的单调时间（毫秒） |
| `ts_utc_ms` / `ts_local` | SNTP 对时后的 UTC 毫秒 / 带时区的本地时间字符串 |
| `raw.x/y/z` | **传感器原始测量码值**（14 位有符号） |
| `accel_g.x/y/z` | 换算后的重力倍数（单位 g） |
| `accel_ms2.x/y/z` | 换算后的国际单位制加速度（m/s²） |
| `magnitude_g` | 合矢量 √(x²+y²+z²)，静止时应 ≈ 1.000 g |

> 为兼容早期版本，包顶层**同时**给出了 `raw` / `accel_g` / `accel_ms2` / `magnitude_g`，
> 它们等于本包**最后一个**采样点的值；`seq` / `ts_local` / `uptime_ms` 同理。

换算关系（板端与后端完全一致）：`g = raw / lsb_per_g`，`m/s² = g × 9.80665`，
`lsb_per_g = 8192 / 量程(g)`。

---

## 5. 文档索引

| 文档 | 内容 |
|---|---|
| [docs/01](docs/01_硬件接线与传感器说明.md) | 传感器型号/引脚/寄存器表、USB 两口的区别、I2C 接线、可选外接传感器 |
| [docs/02](docs/02_编译与烧录.md) | 工具链安装、编译、烧录、串口查看、常见问题 |
| [docs/03](docs/03_VPS部署.md) | VPS 部署、HTTPS、运维、备份、本机穿透联调 |
| [docs/04](docs/04_三方核对验证.md) | 三方核对步骤与脚本、断流验证、对不上的排查 |
| [docs/05](docs/05_真实硬件采集记录样例.md) | **真实硬件采集记录样例**（含原始串口日志与数据库记录） |
| [docs/06](docs/06_第2-3周接口文档_远程采集与按键反馈.md) | **第 2–3 周接口文档**：远程采集请求模型、请求状态机、L1∧L2∧L3 新采集判定、按键求助闭环、新增表 DDL 与错误码 |
| [docs/07](docs/07_第2-3周开发任务清单.md) | **第 2–3 周开发任务清单**：按学时拆分的任务ID、交付物、DoD、依赖与关键路径、角色分工、备用路径 |
| [docs/08](docs/08_第2-3周测试用例.md) | **第 2–3 周测试用例**：接口/集成/系统用例、两条当堂验证专节、缺陷分级与可复制测试命令 |

---

## 6. 安全提示

- `app_config.h` 里的 Wi-Fi 密码与设备令牌是明文，**交付/开源前请替换**；
  生产环境请把令牌换成随机长字符串，并同步修改后端 `SENSOR_TOKEN`。
- 生产必须走 HTTPS，并保持 `APP_TLS_TRUST_MODE` 为 `0` 或 `1`（真实校验证书）；
  只有在**本机抓包排错**时才可临时设为 `2`，用完立刻改回。
- 后端 `SENSOR_PORT` 建议只监听 `127.0.0.1`，由 nginx 反代对外提供 80/443。