/**
 * @file app_config.h
 * @brief 板端全部可配置项集中在这里（Wi-Fi、服务器地址、采集参数）。
 *
 * ★ 说明：这些是「部署参数」，不是传感数据。传感数据只能由 imu_qma6100p.c
 *   从真实硬件寄存器读出，本工程任何地方都不允许出现伪造/随机的测量值。
 */
#pragma once

// ===========================================================================
// 1. Wi-Fi —— 开发板独立联网（正式上传通道）
//    注意: ESP32-S3 只支持 2.4GHz，手机热点请确认工作在 2.4G 频段。
//
//    现场排错记录（真实扫描结果）：板子能扫到 17 个 2.4GHz 热点，
//    但 "vivo X100" 不在其中 —— 说明该手机热点当时工作在 5GHz，
//    ESP32-S3 收不到。因此这里把主用网络改成本机实测可用的 2.4GHz 路由器，
//    并把手机热点降级为备用网络，固件会自动在两个网络间切换。
// ===========================================================================
#define APP_WIFI_SSID           "YOUR_WIFI_SSID"
#define APP_WIFI_PASSWORD       "YOUR_WIFI_PASSWORD"

// 备用网络（主用连不上时自动切换，连上后再切回来也没关系：优先级按顺序轮转）
#define APP_WIFI_FALLBACK_SSID      "YOUR_FALLBACK_SSID"
#define APP_WIFI_FALLBACK_PASSWORD  "YOUR_FALLBACK_PASSWORD"

// ── 本地私密覆盖（不进版本库）─────────────────────────────────────────────
// 真实的 SSID / 密码不要写进本文件（本文件会提交到公开仓库）。
// 做法：新建 firmware/src/app_config.local.h（已在 .gitignore 中），写上
//   #pragma once
//   #undef  APP_WIFI_SSID
//   #define APP_WIFI_SSID "你的真实SSID"
//   ...（其余同理）
// 编译时用 -DAPP_CONFIG_LOCAL_HEADER 或直接在下方取消注释启用。
#ifdef APP_CONFIG_USE_LOCAL
#include "app_config.local.h"
#endif

// Wi-Fi 连接失败时的重试间隔与最大次数（0 表示无限重试）
#define APP_WIFI_RETRY_MS   5000
#define APP_WIFI_MAX_RETRY  0

// 连续离线多久后切换到下一个候选网络（毫秒）
#define APP_WIFI_SWITCH_AFTER_MS 45000

// Wi-Fi 最大发射功率，单位 0.25dBm：78=19.5dBm(芯片默认最大)  40=10dBm  32=8dBm
// ★ 现场排错记录：板子曾出现约 1 次/秒的反复重启，串口输出总是断在
//   "wifi_init: tcp mss: 1440" 之后 —— 也就是 Wi-Fi 射频启动的瞬间。
//   这是典型的"供电能力不足 → 射频瞬间电流把电压拉低 → brownout 复位"。
//   把发射功率调小能显著降低峰值电流，让板子在供电偏弱的 USB 口上也能稳定跑
//   （代价是通信距离变短）。换了好电源/线之后可以调回 78。
#define APP_WIFI_MAX_TX_POWER_QDBM 32

// 开机时等待联网的最长时间；超时不阻塞采集，联网后会自动补上上传
#define APP_WIFI_CONNECT_TIMEOUT_MS 30000

// ===========================================================================
// 2. 后端接收接口 —— 开发板定时 POST 数据包到这里
//    - 正式部署: 填 VPS 的公网地址, 例如 https://your-vps.example.com/api/v1/ingest
//    - 本机联调: 填内网穿透/隧道地址, 例如 https://xxxx.trycloudflare.com/api/v1/ingest
//    - 同网段联调: 直接填开发板能访问到的内网地址, 例如 http://10.1.41.182:8000/api/v1/ingest
//    以 https:// 开头时自动启用 TLS 证书校验，具体校验方式见下面 APP_TLS_TRUST_MODE。
//
//    ★ 当前用的是"同网段直连"：开发板走 Wi-Fi、服务器跑在 10.1.41.182:8000 上，
//      两边在同一个可路由网络里，RTT 约 50~60ms（走 Cloudflare 临时隧道要 0.88s）。
//      这条路径**完全走真实网络**（不是 USB 桥接），也不需要任何内网穿透服务，
//      所以长时间跑数据最稳；数据密度调高到 10Hz 后尤其重要。
//      VPS 上线后，把下面这一行换回 https://你的域名/api/v1/ingest 即可，
//      其它代码一行都不用动（HTTPS 证书校验逻辑原样保留）。
//      隧道地址每次重启都会变；换地址可用 tools/switch_endpoint.py 一条命令完成
//      "改地址 + 重新生成内嵌 CA + 重新编译烧录"。
// ===========================================================================
#define APP_SERVER_URL      "http://10.1.41.182:8000/api/v1/ingest"

// ---- HTTPS 证书校验方式（三选一） ----
// 0 = 使用 ESP-IDF 内置根证书包（默认；适用于 Let's Encrypt 等常见 CA）
// 1 = 使用工程自带的 server_ca_pem.h（把服务器证书链里的 CA 内嵌进固件）
//     ★ 本工程当前用 1：隧道/部分 VPS 的证书由 Google Trust Services 签发，
//       IDF 内置根证书包里没有对应根，实机报
//       "esp-x509-crt-bundle: No matching trusted root certificate found"。
//       内嵌 CA 后依然是**真实校验**（不是把校验关掉）。
//       服务器域名变了请重新生成：见 docs/03 与 tools/gen_ca_header.py
// 2 = 不校验证书（★★ 仅供本机抓包排错，生产环境严禁使用 ★★）
#define APP_TLS_TRUST_MODE  1

// 与后端约定的设备令牌，后端用它拒绝来路不明的上传（防伪造/防误传）
//   ★ 现场排错记录：板子里烧录的固件实际发的是 "esp32s3eye-demo-token-2026"，
//     而源文件/后端默认都是 "CHANGE_ME_DEVICE_TOKEN"，两边不一致 → 后端对每一次
//     上传都回 401，数据一条也存不进去（网页表现为"检测不到实时数据"）。
//     这里统一成板子里真实在用的那个值；后端默认值、部署脚本、测试用例同步改成
//     同一个字符串，避免下次重新烧录后又对不上。
//     改令牌时请四处一起改：本文件、backend/server.py、backend/deploy/*、
//     docs 里的测试用例。
#define APP_DEVICE_TOKEN    "esp32s3eye-demo-token-2026"

// HTTP 超时与失败重试
#define APP_HTTP_TIMEOUT_MS 10000
#define APP_HTTP_RETRY      3

// ===========================================================================
// 2.5 第 2 周：远程采集指令通道（复用上面同一个 HTTP(S) 客户端与令牌）
//     ★ 不引入 MQTT / WebSocket / 新端口：设备处于 NAT 后，只能靠"设备主动轮询"
//       拉取指令 —— 这也是全工程**唯一**的下行通道（docs/06 §4.5）。
//     ★ 端点路径由 APP_SERVER_URL 自动推导，不需要再维护第二份服务器地址：
//         APP_SERVER_URL        = .../api/v1/ingest
//         APP_CMD_POLL_URL      = .../api/v1/device/commands/poll
//         APP_CMD_ACK_URL       = .../api/v1/device/ack
//         APP_CMD_OBS_URL       = .../api/v1/observations
// ===========================================================================

// 指令轮询周期（毫秒）。docs/06 §4.1 约定 1Hz，与响应里的 poll_after_ms 一致。
#define APP_COMMAND_POLL_MS   1000

// 收到指令后必须在 min(1s, timeout_ms/3) 内发出回执，否则后端按未回执计时
#define APP_ACK_MAX_DELAY_MS  1000

// 设备侧缓存最近多少条回执（网络恢复后补发用；对应 poll 请求里的 last_ack）
#define APP_ACK_CACHE_LEN     8

// 单次采集为观测组包时，最多带几个采样点（本次请求单独采，通常就是 1 个）
#define APP_OBS_MAX_SAMPLES   8

// ===========================================================================
// 2.6 第 3 周：按键求助 + 本地物理反馈（引脚与器件参数）
//     ★ 与 Wi-Fi 无关：按键识别与本地反馈**断网也必须工作**（docs/06 §7.5）。
//       上传是"之后尽力而为"的事，反馈必须先发生。
// ===========================================================================

// 按键去抖 / 连按合并窗口（毫秒）。
//   窗口内的多次抖动只算一次按键（幂等键 = btn-<boot_count>-<触发up_ms>）。
//   ★ docs/08 TC-W3-02(e) 断言这个值恰为 1500，不要随手改。
#define APP_HELP_DEBOUNCE_MS   1500

// 触发类型（docs/06 §7.1 枚举只有 button|touch）：
//   本工程用**板载 BOOT 键**（实体按键）触发，故固定为 button。
//   若改用触摸/屏幕按钮，把这里改成 "touch" 即可 —— 后端按枚举校验，
//   不允许出现第三个取值。
#define APP_HELP_TRIGGER_TYPE  "button"

// ---- 按键引脚 ----
// ESP32-S3-EYE 板载 BOOT 按键 = GPIO0，按下为低电平，无需外接线。
//   ★ GPIO0 是 strapping 引脚：上电瞬间被拉低会进入下载模式，
//     所以"按住 BOOT 再上电"是烧录用法；正常运行时按它才是我们的求助键。
#define APP_BUTTON_GPIO        0
// 按键有效电平：0 = 按下为低（板载 BOOT 键就是这种接法，内部上拉）
#define APP_BUTTON_ACTIVE_LOW  1

// ---- 指示灯（LED）----
// 用一个普通 GPIO 直接驱动 LED（串 220Ω~1kΩ 限流电阻到 GND）。
//   ★ 选 GPIO2：它在本板上是**通用 IO**，不是 USB(JTAG) 引脚、不是 flash/PSRAM 引脚、
//     也不与板载 LCD / 摄像头冲突（LCD 用 21/43/44/47/48、摄像头用 6~18 段，
//     见本文件 §2.7），接线上最好找。
//   ★ 绝对不要用 GPIO19/GPIO20 —— 那是原生 USB D-/D+，
//     本项目**唯一的串口日志通道**就跑在上面（见 docs/01 第 2 节），
//     占用它会导致烧录/看日志全部失效。
//   ★ 也不要用 GPIO26~37（N8R8 模组的 flash/PSRAM）。
#define APP_LED_GPIO           2

// ---- 蜂鸣器（BUZZER）----
// 用**有源蜂鸣器**（自带振荡源，给高电平就响），经 NPN 三极管驱动，
// 不要把蜂鸣器直接挂在 GPIO 上（会超过引脚驱动能力）。
//   ★ 选 GPIO3：同为通用 IO，接线上与 LED(GPIO2) 相邻，便于一起插杜邦线。
#define APP_BUZZER_GPIO        3

// 蜂鸣与 LED 的有效电平：1 = 高电平点亮/发声
//   （接法：GPIO → 限流电阻 → LED → GND；GPIO → 1kΩ → 三极管基极）
#define APP_LED_ACTIVE_HIGH    1
#define APP_BUZZER_ACTIVE_HIGH 1

// ---- 本地反馈时序（毫秒）----
// 按键后必须在 ≤300ms 内产生反馈（docs/08 TC-W3-10），所以这三个值都很短。
#define APP_FEEDBACK_BEEP_MS     120   // 按下瞬间的一声短鸣
#define APP_FEEDBACK_REPEAT_MS   3000  // 等待回应期间的重复鸣叫间隔（0=不重复）
#define APP_FEEDBACK_REPEAT_BEEP_MS 60 // 重复鸣叫的单次时长

// 断网期间的本地事件队列长度（环形缓冲）。
//   满了就**如实丢弃最旧的一条并打印告警**，绝不静默丢弃、也不伪造补传。
#define APP_HELP_QUEUE_LEN     8

// ===========================================================================
// 2.7 第 3 周（补充）：板载 LCD —— 把求助状态画到屏幕上（第三路本地输出）
//     ★ 任务只要求"蜂鸣/LED/屏幕至少一种"，前两者先满足，屏幕此前如实标注为
//       "未接入"（docs/10 §6.4 L3）。这里把它补上。
//     ★★★ 最容易踩的坑：**背光是低电平点亮**。
//        Espressif 官方 BSP 里 brightness=100% 对应 LEDC duty=0（低电平），
//        所以直接把 GPIO48 拉高会让屏**一直是黑的**；而且它同时会让人误判
//        "屏坏了"。本工程的 APP_LCD_BL_ACTIVE_LOW 默认按官方接法取 1。
//     ★ 引脚/分辨率/时序全部来自官方 BSP（esp-bsp/bsp/esp32_s3_eye），不臆测：
//        include/bsp/display.h  = 240x240, 16bpp, RGB 顺序
//        include/bsp/esp32_s3_eye.h = SCLK21 MOSI47 CS44 DC43 RST未接 BL48, SPI3, 80MHz
// ===========================================================================
#define APP_LCD_ENABLE          1        // 0 = 完全不启用屏幕（整体降级为空实现）

#define APP_LCD_H_RES           240
#define APP_LCD_V_RES           240
#define APP_LCD_PIXEL_CLOCK_HZ  (80 * 1000 * 1000)

#define APP_LCD_SCLK_GPIO       21       // 官方 BSP_LCD_PCLK（SPI 时钟）
#define APP_LCD_MOSI_GPIO       47       // 官方 BSP_LCD_DATA0（单数据线）
#define APP_LCD_CS_GPIO         44       // 官方 BSP_LCD_CS
#define APP_LCD_DC_GPIO         43       // 官方 BSP_LCD_DC
#define APP_LCD_RST_GPIO        (-1)     // 官方 BSP_LCD_RST = 未连接（走软件复位）
#define APP_LCD_BL_GPIO         48       // 官方 BSP_LCD_BACKLIGHT

// 背光有效电平：1 = 低电平点亮（本板官方接法）
#define APP_LCD_BL_ACTIVE_LOW   1

// ★ LCD 自检开关（临时，排错用）：1 = 上电后做一次**满屏纯色扫描**：
//     阶段A（GPIO48 拉低，官方口径=点亮）→ 白 → 红
//     阶段B（GPIO48 拉高）              → 绿 → 蓝
//   为什么必须"换画面"而不只是"切背光电平"：待机画面底色是近黑 RGB(12,16,32)，
//   只切背光的话，"背光亮着但画面近黑"与"背光全灭"**肉眼无法区分**（上一版诊断就栽在这）。
//   判据：出现任意色块=屏与背光正常；只有A有=低电平点亮（配置正确）；
//         只有B有=极性相反（APP_LCD_BL_ACTIVE_LOW 改 0）；全程纯黑=背光/排线/供电故障。
//   ※ 确认后把它改回 0，恢复正常行为。
#define APP_LCD_BL_SELFTEST     1

// RGB565 字节序：1 = 发送前交换高低字节。
//   本板面板是大端（官方 BSP_LCD_BIGENDIAN=1），而 ESP32 是小端，故默认交换。
//   ★ 若烧录后颜色明显不对（红变蓝、发花），把它改成 0 再烧一次即可。
#define APP_LCD_SWAP_BYTES      1

// ===========================================================================
// 2.8 第 4 周：板载 OV2640 摄像头（图像链路）
//     ★ 目的：网页能看到**这块板子此刻真实拍到的画面**，并且能按需拍一张高分辨率照片。
//       不做任何占位图/录播：没有帧就是没有帧，网页如实显示"无画面"。
//
//     ★ 引脚来自 Espressif 官方 ESP32-S3-EYE 原理图与 esp-bsp（esp32_s3_eye）：
//         XCLK15 PCLK13 VSYNC6 HREF7
//         D0..D7 = 11,9,8,10,12,18,17,16
//         SIOD4 / SIOC5  ← 与板载 QMA6100P **共用**同一条 I2C 总线（官方板就是这么接的）
//       共用总线的处理办法（很关键，别改）：
//         IMU 用新版 i2c_master 驱动在 port0 建了 bus；摄像头这边**不自己建 bus**，
//         传 pin_sccb_sda = -1 + sccb_i2c_port = 0，让 esp32-camera 走
//         "复用已初始化的 I2C 端口"分支（esp_camera.c → SCCB_Use_Port），
//         于是 OV2640(0x30) 与 QMA6100P(0x12) 挂在同一条总线上，互不覆盖。
//         如果这里传 4/5 让摄像头自己建 bus，会和 IMU 抢 GPIO4/5，表现为
//         "加速度计读不到"或"摄像头探测失败"（取决于谁后初始化）。
//
//     ★ 像素格式固定 JPEG：OV2640 片内做 JPEG 压缩，S3 只需搬运，
//       320x240 一帧约 8~15KB，5fps 约 50KB/s，Wi-Fi 完全扛得住。
// ===========================================================================
#define APP_CAM_ENABLE          1        // 0 = 完全不启用摄像头（固件仍可正常编译运行）

#define APP_CAM_PIN_PWDN        (-1)     // 本板 PWDN 未接
#define APP_CAM_PIN_RESET       (-1)     // 本板 RESET 未接（相机常供电）
#define APP_CAM_PIN_XCLK        15
#define APP_CAM_PIN_SIOD        4        // 与 IMU 共用，实际由 I2C port0 的 bus 驱动
#define APP_CAM_PIN_SIOC        5
#define APP_CAM_PIN_D7          16
#define APP_CAM_PIN_D6          17
#define APP_CAM_PIN_D5          18
#define APP_CAM_PIN_D4          12
#define APP_CAM_PIN_D3          10
#define APP_CAM_PIN_D2          8
#define APP_CAM_PIN_D1          9
#define APP_CAM_PIN_D0          11
#define APP_CAM_PIN_VSYNC       6
#define APP_CAM_PIN_HREF        7
#define APP_CAM_PIN_PCLK        13

// XCLK：官方 BSP 取 20MHz（OV2640 支持 6~27MHz）。
// ★ 排错记录：曾降到 10MHz 验证"是不是时钟太高导致不应答"，结果完全一样
//   （I2C 上仍没有 0x30），所以频率不是原因，恢复官方值。
#define APP_CAM_XCLK_HZ         20000000
#define APP_CAM_I2C_PORT        0        // 复用 IMU 已建好的 I2C0（GPIO4/5）

// 预览：QVGA(320x240) / JPEG 质量 12（0~63，越小越清晰、越大越省带宽）
#define APP_CAM_PREVIEW_W       320
#define APP_CAM_PREVIEW_H       240
#define APP_CAM_PREVIEW_FPS     5        // 每秒上传几帧（真实帧率，网页按此显示）
#define APP_CAM_JPEG_QUALITY    12

// 拍照：UXGA(1600x1200)，只有点「拍照」时才临时切到这个分辨率，拍完立刻切回预览
#define APP_CAM_PHOTO_W         1600
#define APP_CAM_PHOTO_H         1200
#define APP_CAM_PHOTO_QUALITY   10

// 单帧上传超时：QVGA 正常几百毫秒；留足余量，避免网络抖动时误判失败
#define APP_CAM_FRAME_TIMEOUT_MS 5000
// 照片上传超时：UXGA 一帧可达 100~200KB，比预览帧大一个数量级
#define APP_CAM_PHOTO_TIMEOUT_MS 20000

// 连续失败多少次后打印一次告警（避免串口被同一句刷屏）
#define APP_CAM_FAIL_LOG_EVERY   10

// ===========================================================================
// 3. 采集与上传参数
// ===========================================================================
// 上传周期（毫秒）。固件按"采样一批 → 整包上传一次"的节奏工作，这里是**上传**周期。
// ★ 这是"真周期"：主循环会扣除本周期采集+上传已花掉的时间，只补足剩余等待时间。
//   注意：上传耗时本身会占用周期（本机经隧道 0.88 秒），若把周期设得比上传耗时还短，
//   实际频率就会受上传耗时限制 —— 详见 docs/05 第 8.7 节（连接复用 + 周期校准）。
#define APP_UPLOAD_PERIOD_MS 1000

// ---- 包内采样：高密度采集 + 整包上报（本次新增）----
// 采样间隔（毫秒）：相邻两个真实采样点之间的时间间隔。
#define APP_SAMPLE_PERIOD_MS 100

// 每个数据包包含多少个采样点（攒够一包才上传一次）。
//   1  = 每包 1 点（旧行为：数据 1Hz、HTTPS 1 次/秒）
//   10 = 每包 10 点（数据 10Hz、HTTPS 仍然 1 次/秒 —— 数据密 10 倍但不增加握手次数）
// 建议 APP_UPLOAD_PERIOD_MS == APP_SAMPLE_PERIOD_MS × APP_BATCH_SIZE。
#define APP_BATCH_SIZE       10

// ---- 采集与上传解耦（重要）----
// 采样任务严格按 APP_SAMPLE_PERIOD_MS 采点，上传任务异步把整包发出去。
// 这样"网络慢"只会让数据稍晚到达，而不会把采样节奏拖乱
// （实测：同步上传时包内间隔 100ms、包间却有约 1 秒空洞，曲线会一段一段地跳）。
#define APP_BATCH_QUEUE_LEN  12   // 最多缓冲多少个待上传的包（≈12 秒的数据）
// 一次上传最多把多少个采样点合并成一个包。网络慢的时候，把已经在排队的几个包
// 合并成一个大包一起发，避免"采样 10Hz、入库只有 5Hz"。
#define APP_MAX_MERGE        30

// 每次上传统计的硬件采样次数（多次读取做算术平均以抑制噪声）。
// 平均后的结果仍然完全来自真实寄存器读数，不做任何模型拟合或模拟。
#define APP_OVERSAMPLE       8

// 采样间隔（微秒），8 次 x 2ms = 16ms，远小于 100ms 的采样间隔
#define APP_OVERSAMPLE_US    2000

// 编译期检查：参数组合必须自洽，避免出现"攒不满一包"或"缓冲溢出"这类糊涂配置
_Static_assert(APP_BATCH_SIZE >= 1 && APP_BATCH_SIZE <= 50,
               "APP_BATCH_SIZE 必须在 1..50 之间");
_Static_assert(APP_MAX_MERGE >= APP_BATCH_SIZE && APP_MAX_MERGE <= 100,
               "APP_MAX_MERGE 必须 >= APP_BATCH_SIZE 且不超过 100");
_Static_assert(APP_SAMPLE_PERIOD_MS >= APP_OVERSAMPLE * APP_OVERSAMPLE_US / 1000,
               "采样间隔必须大于一次平均采样本身的耗时");
// 一包正好攒够一个上传周期：改了采样率/上传周期却忘了同步 APP_BATCH_SIZE 时，
// 这条会在**编译期**直接报错，不会等到跑起来才发现"密度没变"或"包攒不满"
_Static_assert(APP_BATCH_SIZE * APP_SAMPLE_PERIOD_MS == APP_UPLOAD_PERIOD_MS,
               "APP_BATCH_SIZE 必须 = APP_UPLOAD_PERIOD_MS / APP_SAMPLE_PERIOD_MS（一包攒够一个上传周期）");

// ---- 第 3 周：引脚与反馈参数的自洽检查 ----
// ★ 这几条是"把踩过的坑写进编译期"：ESP32-S3-EYE 上某些 GPIO 一旦被占用，
//   后果不是"某个功能不灵"，而是**整个串口日志/烧录通道失效**，排查代价极高。
_Static_assert(APP_BUTTON_GPIO != 19 && APP_BUTTON_GPIO != 20,
               "按键不能用 GPIO19/20：那是原生 USB D-/D+，占用后无法烧录与看日志");
_Static_assert(APP_LED_GPIO != 19 && APP_LED_GPIO != 20,
               "LED 不能用 GPIO19/20：那是原生 USB，会切断本项目唯一的串口日志通道");
_Static_assert(APP_BUZZER_GPIO != 19 && APP_BUZZER_GPIO != 20,
               "蜂鸣器不能用 GPIO19/20：那是原生 USB，会切断串口日志通道");
_Static_assert((APP_LED_GPIO < 26 || APP_LED_GPIO > 37) &&
               (APP_BUZZER_GPIO < 26 || APP_BUZZER_GPIO > 37),
               "LED/蜂鸣器不能用 GPIO26~37：N8R8 模组的 flash/PSRAM 占用");
_Static_assert(APP_LED_GPIO != APP_BUZZER_GPIO && APP_BUTTON_GPIO != APP_LED_GPIO &&
               APP_BUTTON_GPIO != APP_BUZZER_GPIO,
               "按键/LED/蜂鸣器必须使用互不相同的 GPIO");
_Static_assert(APP_LED_GPIO != 4 && APP_LED_GPIO != 5 &&
               APP_BUZZER_GPIO != 4 && APP_BUZZER_GPIO != 5,
               "LED/蜂鸣器不能占用 GPIO4/5：那是板载 QMA6100P 的 I2C 总线");
_Static_assert(APP_HELP_DEBOUNCE_MS >= 500 && APP_HELP_DEBOUNCE_MS <= 5000,
               "去抖窗口应在 500~5000ms 之间（docs/08 用例断言为 1500）");
_Static_assert(APP_HELP_QUEUE_LEN >= 1 && APP_HELP_QUEUE_LEN <= 64,
               "本地求助队列长度应在 1~64 之间");

// ---- 第 3 周（补充）：LCD 引脚的自洽检查 ----
// ★ 这里单独把所有"会踩"的组合写死：LCD 一旦占了 USB / flash-PSRAM / I2C 引脚，
//   后果同样是"日志全丢"或"加速度计读不到"，排查代价极高，所以放到编译期拦。
#if APP_LCD_ENABLE
_Static_assert(APP_LCD_SCLK_GPIO != 19 && APP_LCD_SCLK_GPIO != 20 &&
               APP_LCD_MOSI_GPIO != 19 && APP_LCD_MOSI_GPIO != 20 &&
               APP_LCD_CS_GPIO   != 19 && APP_LCD_CS_GPIO   != 20 &&
               APP_LCD_DC_GPIO   != 19 && APP_LCD_DC_GPIO   != 20 &&
               APP_LCD_BL_GPIO   != 19 && APP_LCD_BL_GPIO   != 20,
               "LCD 不能用 GPIO19/20：那是原生 USB D-/D+，占用后无法烧录与看日志");
_Static_assert((APP_LCD_SCLK_GPIO < 26 || APP_LCD_SCLK_GPIO > 37) &&
               (APP_LCD_MOSI_GPIO < 26 || APP_LCD_MOSI_GPIO > 37) &&
               (APP_LCD_CS_GPIO   < 26 || APP_LCD_CS_GPIO   > 37) &&
               (APP_LCD_DC_GPIO   < 26 || APP_LCD_DC_GPIO   > 37) &&
               (APP_LCD_BL_GPIO   < 26 || APP_LCD_BL_GPIO   > 37),
               "LCD 不能用 GPIO26~37：N8R8 模组的 flash/PSRAM 占用");
_Static_assert(APP_LCD_SCLK_GPIO != 4 && APP_LCD_SCLK_GPIO != 5 &&
               APP_LCD_MOSI_GPIO != 4 && APP_LCD_MOSI_GPIO != 5 &&
               APP_LCD_CS_GPIO   != 4 && APP_LCD_CS_GPIO   != 5 &&
               APP_LCD_DC_GPIO   != 4 && APP_LCD_DC_GPIO   != 5 &&
               APP_LCD_BL_GPIO   != 4 && APP_LCD_BL_GPIO   != 5,
               "LCD 不能占用 GPIO4/5：那是板载 QMA6100P 的 I2C 总线");
_Static_assert(APP_LCD_SCLK_GPIO != APP_BUTTON_GPIO && APP_LCD_SCLK_GPIO != APP_LED_GPIO &&
               APP_LCD_SCLK_GPIO != APP_BUZZER_GPIO &&
               APP_LCD_MOSI_GPIO != APP_BUTTON_GPIO && APP_LCD_MOSI_GPIO != APP_LED_GPIO &&
               APP_LCD_MOSI_GPIO != APP_BUZZER_GPIO &&
               APP_LCD_CS_GPIO   != APP_BUTTON_GPIO && APP_LCD_CS_GPIO   != APP_LED_GPIO &&
               APP_LCD_CS_GPIO   != APP_BUZZER_GPIO &&
               APP_LCD_DC_GPIO   != APP_BUTTON_GPIO && APP_LCD_DC_GPIO   != APP_LED_GPIO &&
               APP_LCD_DC_GPIO   != APP_BUZZER_GPIO &&
               APP_LCD_BL_GPIO   != APP_BUTTON_GPIO && APP_LCD_BL_GPIO   != APP_LED_GPIO &&
               APP_LCD_BL_GPIO   != APP_BUZZER_GPIO,
               "LCD 引脚不能与按键/LED/蜂鸣器复用");
_Static_assert(APP_LCD_SCLK_GPIO != APP_LCD_MOSI_GPIO && APP_LCD_SCLK_GPIO != APP_LCD_CS_GPIO &&
               APP_LCD_SCLK_GPIO != APP_LCD_DC_GPIO && APP_LCD_SCLK_GPIO != APP_LCD_BL_GPIO &&
               APP_LCD_MOSI_GPIO != APP_LCD_CS_GPIO && APP_LCD_MOSI_GPIO != APP_LCD_DC_GPIO &&
               APP_LCD_MOSI_GPIO != APP_LCD_BL_GPIO && APP_LCD_CS_GPIO != APP_LCD_DC_GPIO &&
               APP_LCD_CS_GPIO != APP_LCD_BL_GPIO && APP_LCD_DC_GPIO != APP_LCD_BL_GPIO,
               "LCD 各引脚必须互不相同");
#endif

// ---- 第 4 周：摄像头引脚的自洽检查 ----
// ★ 摄像头占用 8 根数据线 + 3 根同步线，最容易与 USB / flash-PSRAM / LCD / 按键撞车。
//   这类错误在编译期拦下来最省事：一旦占用 GPIO19/20（原生 USB），
//   烧录与日志会直接失效，现场只会看到"板子没反应"。
#if APP_CAM_ENABLE
_Static_assert(APP_CAM_PIN_XCLK  != 19 && APP_CAM_PIN_XCLK  != 20 &&
               APP_CAM_PIN_VSYNC != 19 && APP_CAM_PIN_VSYNC != 20 &&
               APP_CAM_PIN_HREF  != 19 && APP_CAM_PIN_HREF  != 20 &&
               APP_CAM_PIN_PCLK  != 19 && APP_CAM_PIN_PCLK  != 20 &&
               APP_CAM_PIN_D0    != 19 && APP_CAM_PIN_D0    != 20 &&
               APP_CAM_PIN_D1    != 19 && APP_CAM_PIN_D1    != 20 &&
               APP_CAM_PIN_D2    != 19 && APP_CAM_PIN_D2    != 20 &&
               APP_CAM_PIN_D3    != 19 && APP_CAM_PIN_D3    != 20 &&
               APP_CAM_PIN_D4    != 19 && APP_CAM_PIN_D4    != 20 &&
               APP_CAM_PIN_D5    != 19 && APP_CAM_PIN_D5    != 20 &&
               APP_CAM_PIN_D6    != 19 && APP_CAM_PIN_D6    != 20 &&
               APP_CAM_PIN_D7    != 19 && APP_CAM_PIN_D7    != 20,
               "摄像头不能用 GPIO19/20：那是原生 USB D-/D+，占用后无法烧录与看日志");
_Static_assert((APP_CAM_PIN_XCLK  < 26 || APP_CAM_PIN_XCLK  > 37) &&
               (APP_CAM_PIN_VSYNC < 26 || APP_CAM_PIN_VSYNC > 37) &&
               (APP_CAM_PIN_HREF  < 26 || APP_CAM_PIN_HREF  > 37) &&
               (APP_CAM_PIN_PCLK  < 26 || APP_CAM_PIN_PCLK  > 37) &&
               (APP_CAM_PIN_D0    < 26 || APP_CAM_PIN_D0    > 37) &&
               (APP_CAM_PIN_D1    < 26 || APP_CAM_PIN_D1    > 37) &&
               (APP_CAM_PIN_D2    < 26 || APP_CAM_PIN_D2    > 37) &&
               (APP_CAM_PIN_D3    < 26 || APP_CAM_PIN_D3    > 37) &&
               (APP_CAM_PIN_D4    < 26 || APP_CAM_PIN_D4    > 37) &&
               (APP_CAM_PIN_D5    < 26 || APP_CAM_PIN_D5    > 37) &&
               (APP_CAM_PIN_D6    < 26 || APP_CAM_PIN_D6    > 37) &&
               (APP_CAM_PIN_D7    < 26 || APP_CAM_PIN_D7    > 37),
               "摄像头不能用 GPIO26~37：N8R8 模组的 flash/PSRAM 占用");
// 摄像头数据线/同步线必须互不相同（重复会让画面撕裂或整帧错乱）
_Static_assert(APP_CAM_PIN_D0 != APP_CAM_PIN_D1 && APP_CAM_PIN_D0 != APP_CAM_PIN_D2 &&
               APP_CAM_PIN_D0 != APP_CAM_PIN_D3 && APP_CAM_PIN_D0 != APP_CAM_PIN_D4 &&
               APP_CAM_PIN_D0 != APP_CAM_PIN_D5 && APP_CAM_PIN_D0 != APP_CAM_PIN_D6 &&
               APP_CAM_PIN_D0 != APP_CAM_PIN_D7 &&
               APP_CAM_PIN_D1 != APP_CAM_PIN_D2 && APP_CAM_PIN_D1 != APP_CAM_PIN_D3 &&
               APP_CAM_PIN_D1 != APP_CAM_PIN_D4 && APP_CAM_PIN_D1 != APP_CAM_PIN_D5 &&
               APP_CAM_PIN_D1 != APP_CAM_PIN_D6 && APP_CAM_PIN_D1 != APP_CAM_PIN_D7 &&
               APP_CAM_PIN_D2 != APP_CAM_PIN_D3 && APP_CAM_PIN_D2 != APP_CAM_PIN_D4 &&
               APP_CAM_PIN_D2 != APP_CAM_PIN_D5 && APP_CAM_PIN_D2 != APP_CAM_PIN_D6 &&
               APP_CAM_PIN_D2 != APP_CAM_PIN_D7 &&
               APP_CAM_PIN_D3 != APP_CAM_PIN_D4 && APP_CAM_PIN_D3 != APP_CAM_PIN_D5 &&
               APP_CAM_PIN_D3 != APP_CAM_PIN_D6 && APP_CAM_PIN_D3 != APP_CAM_PIN_D7 &&
               APP_CAM_PIN_D4 != APP_CAM_PIN_D5 && APP_CAM_PIN_D4 != APP_CAM_PIN_D6 &&
               APP_CAM_PIN_D4 != APP_CAM_PIN_D7 &&
               APP_CAM_PIN_D5 != APP_CAM_PIN_D6 && APP_CAM_PIN_D5 != APP_CAM_PIN_D7 &&
               APP_CAM_PIN_D6 != APP_CAM_PIN_D7 &&
               APP_CAM_PIN_VSYNC != APP_CAM_PIN_HREF &&
               APP_CAM_PIN_VSYNC != APP_CAM_PIN_PCLK &&
               APP_CAM_PIN_HREF  != APP_CAM_PIN_PCLK,
               "摄像头各引脚必须互不相同");
// 摄像头不能抢走按键/LED/蜂鸣器/LCD 的引脚
_Static_assert(APP_CAM_PIN_XCLK != APP_BUTTON_GPIO && APP_CAM_PIN_XCLK != APP_LED_GPIO &&
               APP_CAM_PIN_XCLK != APP_BUZZER_GPIO &&
               APP_CAM_PIN_PCLK != APP_BUTTON_GPIO && APP_CAM_PIN_PCLK != APP_LED_GPIO &&
               APP_CAM_PIN_PCLK != APP_BUZZER_GPIO &&
               APP_CAM_PIN_VSYNC != APP_BUTTON_GPIO && APP_CAM_PIN_VSYNC != APP_LED_GPIO &&
               APP_CAM_PIN_VSYNC != APP_BUZZER_GPIO &&
               APP_CAM_PIN_HREF != APP_BUTTON_GPIO && APP_CAM_PIN_HREF != APP_LED_GPIO &&
               APP_CAM_PIN_HREF != APP_BUZZER_GPIO,
               "摄像头引脚不能与按键/LED/蜂鸣器复用");
#endif

// ===========================================================================
// 4. QMA6100P 初始化参数（寄存器含义已由真实芯片读数验证）
//    - 量程: ±2g 时灵敏度 4096 LSB/g，分辨率最高，适合静止/倾斜类测量
//    - 带宽: 100Hz
// ===========================================================================
#define APP_IMU_RANGE_2G     1
#define APP_IMU_RANGE_4G     2
#define APP_IMU_RANGE_8G     4
#define APP_IMU_RANGE_16G    8
#define APP_IMU_RANGE_32G    15

#define APP_IMU_RANGE_CODE   APP_IMU_RANGE_2G   // 默认 ±2g
#define APP_IMU_BW_100HZ     0                  // 0x10 寄存器写入值

// ===========================================================================
// 5. 时区（用于生成"本地时间戳"）—— 中国标准时间 UTC+8
// ===========================================================================
#define APP_TZ              "CST-8"