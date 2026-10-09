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