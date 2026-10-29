/**
 * @file camera.c
 * @brief 第 4 周：OV2640 真实画面采集 + 预览帧上传 + 按需拍照。
 *
 * ┌─────────────────────── 一条帧的生命周期 ───────────────────────┐
 * │ esp_camera_fb_get()  拿到 OV2640 片内 JPEG 压缩后的真实帧        │
 * │        ↓                                                       │
 * │ POST /api/v1/cam/frame  body=JPEG 原始字节（不转码、不重压缩）   │
 * │        ↓                                                       │
 * │ 响应 {"ok":true,"capture":false}  ← capture=true 时立刻拍一张   │
 * │        ↓                                                       │
 * │ esp_camera_fb_return()  把缓冲还给驱动（不还就会几帧后卡死）     │
 * └────────────────────────────────────────────────────────────────┘
 *
 * ★ 为什么预览走"POST 帧 + 网页轮询"而不是板子自己开 MJPEG 服务器：
 *   本工程的既有架构是"设备主动出站 → 后端 → 网页"（设备在 NAT 后面，
 *   没有公网地址）。板子开 HTTP 服务器只有同一局域网能看，VPS 部署后完全失效，
 *   而且会在板子上多开一个监听端口，破坏"唯一出站通道"的安全模型。
 *
 * ★ 为什么拍照要复用帧上传的响应：
 *   帧本来就在 5Hz 往返，响应里夹带一个 bool 的时延 < 200ms；
 *   这比新增一条 1Hz 指令轮询通道更快，也不用改第 2 周那套已经验证过的
 *   指令状态机（采集指令与拍照是两件事，混在一起会把回执语义搅乱）。
 */

#include "camera.h"

#include <stdio.h>
#include <stdarg.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "esp_camera.h"
#include "driver/i2c_master.h"   // 仅用于初始化失败时的总线诊断（扫描应答地址）

#include "app_config.h"
#include "uploader.h"          // uploader_wifi_is_connected()：没网就不空转上传

// ★ 兜底：开关宏缺失时 `#if` 会按 0 处理，快照分支被**静默**编译掉、编译还能过
//   （第 3 周 D7 的同一个坑）。让它在这里直接报错，问题留在编译期。
#ifndef APP_CAM_SNAPSHOT_ENABLE
#error "app_config.h 必须定义 APP_CAM_SNAPSHOT_ENABLE（0/1），否则按需快照会被静默编译掉"
#endif

#if APP_TLS_TRUST_MODE == 1
#include "server_ca_pem.h"     // 与 uploader 共用同一份真实 CA 链（HTTPS 时才用到）
#endif

static const char *TAG = "cam";

// ===========================================================================
// 模块状态
// ===========================================================================
static cam_state_provider_t s_provider = NULL;

static SemaphoreHandle_t s_lock = NULL;      // 保护 s_status / s_photo_pending
static cam_status_t      s_status = { 0 };
static volatile bool     s_photo_pending = false;
static volatile bool     s_photo_busy = false;   // 正在拍（避免重入）
static volatile bool     s_quit_logged = false;

// 第 7 周：待执行的按需快照（id 由后端下发，原样带回去）
static volatile bool     s_snap_pending = false;
static char              s_snap_sid[64] = { 0 };
static char              s_snap_mode[16] = { 0 };
// ★ 已经处理过的那一个 id。后端为了抗丢包会**每帧重复下发同一个 id**（at-least-once），
//   设备必须按 id 去重，否则同一个请求会被连拍好几次（第 7 周 D3 的另一半）。
static char              s_snap_last_sid[64] = { 0 };

static esp_http_client_handle_t s_client = NULL;
static char s_client_url[160] = { 0 };

static uint32_t s_seq = 0;                   // 帧序号（后端/网页据此判断新旧）
static uint32_t s_fail_streak = 0;           // 连续失败次数（用于降噪日志）

// ===========================================================================
// 小工具
// ===========================================================================
static void status_snapshot(cam_status_t *out)
{
    if (s_lock && xSemaphoreTake(s_lock, pdMS_TO_TICKS(50)) == pdTRUE) {
        *out = s_status;
        xSemaphoreGive(s_lock);
    } else {
        *out = s_status;    // 极端情况下退化为无锁拷贝（状态字都是 32 位，可容忍）
    }
}

static void status_set_error(const char *fmt, ...)
{
    char buf[sizeof(s_status.last_error)];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    if (s_lock) xSemaphoreTake(s_lock, portMAX_DELAY);
    snprintf(s_status.last_error, sizeof(s_status.last_error), "%s", buf);
    if (s_lock) xSemaphoreGive(s_lock);
}

/**
 * @brief 由 APP_SERVER_URL 推导摄像头端点（不维护第二份服务器地址）。
 *
 * APP_SERVER_URL = http://host:8000/api/v1/ingest
 *   → http://host:8000/api/v1/cam/frame?... 
 * 换域名/VPS 时只改 app_config.h 一处，摄像头跟着走。
 */
static void build_url(char *out, size_t out_len, const char *suffix_with_query)
{
    const char *p = strstr(APP_SERVER_URL, "/api/v1/");
    if (p) {
        int prefix = (int)(p - APP_SERVER_URL);
        snprintf(out, out_len, "%.*s%s", prefix, APP_SERVER_URL, suffix_with_query);
    } else {
        // 兜底：URL 被改成了非 /api/v1/ 形式时，仍然给出可用的绝对地址
        snprintf(out, out_len, "%s%s", APP_SERVER_URL, suffix_with_query);
    }
}

// ===========================================================================
// HTTP：上传一帧 JPEG（独立句柄，keep-alive 复用）
// ===========================================================================
static void client_drop(void)
{
    if (s_client) {
        esp_http_client_close(s_client);
        esp_http_client_cleanup(s_client);
        s_client = NULL;
    }
    s_client_url[0] = '\0';
}

static esp_http_client_handle_t client_get(const char *url, int timeout_ms)
{
    if (s_client && strcmp(s_client_url, url) == 0) {
        esp_http_client_set_timeout_ms(s_client, timeout_ms);
        return s_client;
    }
    // 端点或超时策略变化：重建（换端点时 keep-alive 的旧连接本来也不能用）
    client_drop();

    bool is_https = (strncmp(url, "https://", 8) == 0);
    const char *ca_pem = NULL;
    void       *bundle = NULL;
#if APP_TLS_TRUST_MODE == 0
    if (is_https) bundle = (void *)esp_crt_bundle_attach;
#elif APP_TLS_TRUST_MODE == 1
    if (is_https) ca_pem = SERVER_CA_PEM;
#endif

    esp_http_client_config_t cfg = {
        .url               = url,
        .method            = HTTP_METHOD_POST,
        .timeout_ms        = timeout_ms,
        .keep_alive_enable = true,     // 5fps 下绝不能每帧重做一次 TLS 握手
        .cert_pem          = ca_pem,
        .crt_bundle_attach = bundle,
        .buffer_size       = 2048,
        .buffer_size_tx    = 2048,
    };
    s_client = esp_http_client_init(&cfg);
    if (!s_client) return NULL;

    esp_http_client_set_header(s_client, "Content-Type", "image/jpeg");
    esp_http_client_set_header(s_client, "X-Device-Token", APP_DEVICE_TOKEN);
    esp_http_client_set_header(s_client, "User-Agent", "esp32s3eye-cam/1.0");
    snprintf(s_client_url, sizeof(s_client_url), "%s", url);
    return s_client;
}

/**
 * @brief POST 一帧 JPEG，并把响应正文读回来（用于解析拍照指令）。
 * @return ESP_OK 表示 HTTP 2xx 且响应已读完。
 */
static esp_err_t post_bytes(const char *url, const uint8_t *data, size_t len,
                            const char *ctype, int timeout_ms,
                            char *resp, size_t resp_len)
{
    esp_http_client_handle_t client = client_get(url, timeout_ms);
    if (!client) return ESP_FAIL;

    // 每次显式设置 Content-Type：同一个 keep-alive 句柄会在
    // image/jpeg（预览帧/照片）与 application/json（状态上报）之间切换。
    esp_http_client_set_header(client, "Content-Type", ctype);
    esp_err_t err = esp_http_client_open(client, len);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "连接失败: %s（下一帧重建连接）", esp_err_to_name(err));
        client_drop();
        return err;
    }
    int written = esp_http_client_write(client, (const char *)data, len);
    if (written < 0 || (size_t)written != len) {
        ESP_LOGW(TAG, "写入不完整 %d/%u", written, (unsigned)len);
        client_drop();
        return ESP_FAIL;
    }

    esp_http_client_fetch_headers(client);
    int status = esp_http_client_get_status_code(client);

    // 读响应（可能 very small；chunked 也要读完，否则 keep-alive 连接会错位）
    size_t got = 0;
    char scratch[128];
    while (true) {
        int r = esp_http_client_read(client, scratch, sizeof(scratch));
        if (r <= 0) break;
        if (resp && got + 1 < resp_len) {
            size_t n = (size_t)r;
            if (got + n > resp_len - 1) n = resp_len - 1 - got;
            memcpy(resp + got, scratch, n);
            got += n;
            resp[got] = '\0';
        }
    }
    if (resp && resp_len) resp[got < resp_len ? got : resp_len - 1] = '\0';

    if (status < 200 || status >= 300) {
        // 非 2xx 是明确失败：不静默当作成功（例如令牌错会一直 401）
        if (s_fail_streak == 0 || (s_fail_streak % APP_CAM_FAIL_LOG_EVERY) == 0) {
            ESP_LOGW(TAG, "上传被拒: HTTP %d, 响应=%s", status,
                     (resp && resp[0]) ? resp : "(空)");
        }
        return ESP_FAIL;
    }
    return ESP_OK;
}

// ===========================================================================
// 诊断：扫描 I2C 总线上所有应答的地址
//
// ★ 为什么要有它：摄像头探测失败时，"排线没插"、"子板没供电"、"XCLK 没起来"、
//   "SCCB 复用错了端口" 在日志上长得一模一样（全是 NOT_SUPPORTED）。
//   扫描同一时刻的总线能一刀切开：
//     · 只看到 0x12（QMA6100P）→ 总线本身是好的，是**摄像头侧**没应答；
//     · 一个都看不到          → 连 IMU 都不应答，问题在总线/上拉/供电；
//     · 看到 0x12 和 0x30     → 传感器在线，问题在探测器（地址/型号）而非硬件。
//   注意：这是故障时的诊断输出，正常路径不会跑到。
// ===========================================================================
static void cam_i2c_scan_bus(void)
{
    i2c_master_bus_handle_t bus = NULL;
    esp_err_t err = i2c_master_get_bus_handle(APP_CAM_I2C_PORT, &bus);
    if (err != ESP_OK || bus == NULL) {
        ESP_LOGW(TAG, "诊断: 取不到 I2C%d 总线句柄 (%s)：IMU 是否初始化成功？",
                 APP_CAM_I2C_PORT, esp_err_to_name(err));
        return;
    }

    int n = 0;
    char found[160] = { 0 };
    size_t used = 0;
    for (uint8_t addr = 0x08; addr < 0x78; addr++) {
        if (i2c_master_probe(bus, addr, 50) == ESP_OK) {
            n++;
            int w = snprintf(found + used, sizeof(found) - used, " 0x%02X", addr);
            if (w > 0) used += (size_t)w;
            if (used >= sizeof(found) - 8) break;
        }
    }
    if (n == 0) {
        ESP_LOGW(TAG, "诊断: I2C%d 上没有任何地址应答 (GPIO4/5) —— 检查上拉与供电",
                 APP_CAM_I2C_PORT);
    } else {
        ESP_LOGI(TAG, "诊断: I2C%d 上应答的地址:%s（共 %d 个）",
                 APP_CAM_I2C_PORT, found, n);
        if (strstr(found, "0x30") == NULL) {
            ESP_LOGW(TAG, "诊断: 总线上没有 0x30（OV2640 的 SCCB 地址）→ "
                          "摄像头模组未接好/未供电，或 XCLK 未输出");
        }
    }
}

// ===========================================================================
// 状态上报：让网页能区分"设备没联网"与"设备在线但摄像头没就绪"
//
// ★ 为什么需要单独上报：画面本身能证明"摄像头正常"，但没有任何画面时，
//   网页无法区分"板子没开机/没联网"还是"摄像头排线没插"。
//   把摄像头探测结论（ready/pid/error）在启动时上报一次，这两种情况就能分辨，
//   排查不用再靠猜。上报失败不影响任何本地功能。
// ===========================================================================
static void report_camera_state(bool ready, const char *err)
{
    cam_state_t st = { 0 };
    if (s_provider) s_provider(&st);

    char json[288];
    snprintf(json, sizeof(json),
             "{\"device_id\":\"%s\",\"ready\":%s,\"pid\":%u,\"error\":\"%s\","
             "\"w\":%d,\"h\":%d}",
             st.device_id ? st.device_id : "unknown",
             ready ? "true" : "false",
             (unsigned)(ready ? s_status.sensor_pid : 0),
             err ? err : "",
             APP_CAM_PREVIEW_W, APP_CAM_PREVIEW_H);

    char url[200];
    build_url(url, sizeof(url), "/api/v1/cam/state");
    char resp[128] = { 0 };
    esp_err_t e = post_bytes(url, (const uint8_t *)json, strlen(json),
                             "application/json", 5000, resp, sizeof(resp));
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "摄像头状态上报失败（不影响本地功能）");
    }
}

// ===========================================================================
// 相机配置构造（预览与拍照共用，只是分辨率/帧数不同）
// ===========================================================================
static void build_camera_config(camera_config_t *cfg, framesize_t fs, int fb_count)
{
    camera_config_t c = {
        .pin_pwdn      = APP_CAM_PIN_PWDN,
        .pin_reset     = APP_CAM_PIN_RESET,
        .pin_xclk      = APP_CAM_PIN_XCLK,
        // ★ sda = -1：让 esp32-camera 复用 IMU 已在 I2C0 建好的 bus（GPIO4/5 共用）
        .pin_sccb_sda  = -1,
        .pin_sccb_scl  = -1,
        .sccb_i2c_port = APP_CAM_I2C_PORT,
        .pin_d7        = APP_CAM_PIN_D7,
        .pin_d6        = APP_CAM_PIN_D6,
        .pin_d5        = APP_CAM_PIN_D5,
        .pin_d4        = APP_CAM_PIN_D4,
        .pin_d3        = APP_CAM_PIN_D3,
        .pin_d2        = APP_CAM_PIN_D2,
        .pin_d1        = APP_CAM_PIN_D1,
        .pin_d0        = APP_CAM_PIN_D0,
        .pin_vsync     = APP_CAM_PIN_VSYNC,
        .pin_href      = APP_CAM_PIN_HREF,
        .pin_pclk      = APP_CAM_PIN_PCLK,

        .xclk_freq_hz  = APP_CAM_XCLK_HZ,
        .ledc_timer    = LEDC_TIMER_0,       // LCD 背光用普通 GPIO，不占 LEDC
        .ledc_channel  = LEDC_CHANNEL_0,

        .pixel_format  = PIXFORMAT_JPEG,     // OV2640 片内压缩，S3 只搬运
        .frame_size    = fs,
        .jpeg_quality  = APP_CAM_JPEG_QUALITY,
        .fb_count      = fb_count,
        .fb_location   = CAMERA_FB_IN_PSRAM, // ★ 必须：内部 SRAM 装不下 UXGA 帧
        .grab_mode     = CAMERA_GRAB_LATEST, // 总是拿最新帧，避免画面延迟越积越大
    };
    *cfg = c;
}

// ===========================================================================
// 第 7 周：按需环境快照 —— 只有后端下发了 snapshot_id 才取图
//
// ★ 为什么不能直接复用预览帧：预览流是"一直开着"的，用户点「查看环境」时
//   内存里那一帧是在**请求之前**就拍好的。把它当成本次结果，就是典型的
//   "上次成功图像被标成本次成功"。所以快照必须是请求之后**重新取的一帧**，
//   并且带着后端给的 id 回来 —— id 对不上、序号不比基线新，一律不算。
//
// ★ 为什么默认 QVGA：用户要的是"现在环境长什么样"，不是一张存档原图。
//   QVGA 取帧 + 上传约 0.2~0.5 秒，能立刻证明"这是请求之后拍的"；
//   需要清晰画面时后端可以指定 mode=uxga，代价是重建相机约 8.7 秒（第 4 周实测）。
// ===========================================================================

/** 从 JSON 响应里抠出一个字符串字段（设备侧不引入 JSON 库，只认这一种形状）。
 *
 *  ★ 必须同时容忍 `"k":"v"` 和 `"k": "v"` 两种写法。
 *    后端是 Python 的 `json.dumps`，冒号后**带空格**；设备侧如果只认紧凑写法，
 *    就会**静默**匹配不到任何东西 —— 症状是"后端日志显示已下发 id，板子却像没收到"，
 *    而固件不报任何错（第 7 周 D1，和第 4 周 `"capture": true` 是同一个坑）。
 *    第 4 周之所以没暴露，是因为那里手写了两个 strstr 变体；这里统一在解析器里解决。
 */
static bool json_find_str(const char *json, const char *key, char *out, size_t out_len)
{
    if (out && out_len) out[0] = '\0';
    if (!json || !key || !out || out_len < 2) return false;

    char needle[40];
    snprintf(needle, sizeof(needle), "\"%s\"", key);
    const char *p = strstr(json, needle);
    if (!p) return false;
    p += strlen(needle);
    while (*p == ' ' || *p == '\t') p++;
    if (*p != ':') return false;          // 命中的是别的字段（或是数组里的同名串）
    p++;
    while (*p == ' ' || *p == '\t') p++;
    if (*p != '"') return false;          // 值不是字符串（如 null / true / 数字）→ 当作没有
    p++;
    const char *e = strchr(p, '"');
    if (!e) return false;
    size_t n = (size_t)(e - p);
    if (n == 0 || n + 1 > out_len) return false;
    memcpy(out, p, n);
    out[n] = '\0';
    return true;
}

/** 上报"开始取图"（best-effort：失败了照常取图，只是后端少一个中间态）。 */
static void snapshot_report_begin(const cam_state_t *st, const char *sid)
{
#if APP_CAM_SNAPSHOT_REPORT_BEGIN
    char json[192];
    snprintf(json, sizeof(json),
             "{\"snapshot_id\":\"%s\",\"device_id\":\"%s\",\"uptime_ms\":%lld}",
             sid, st->device_id ? st->device_id : "unknown",
             (long long)(esp_timer_get_time() / 1000));
    char url[200];
    build_url(url, sizeof(url), "/api/v1/cam/snapshot/begin");
    char resp[96] = { 0 };
    if (post_bytes(url, (const uint8_t *)json, strlen(json), "application/json",
                   3000, resp, sizeof(resp)) != ESP_OK) {
        ESP_LOGW(TAG, "快照 begin 上报失败（不影响取图）");
    }
#else
    (void)st; (void)sid;
#endif
}

/** 明确回报"这次取不到图"，而不是让后端一直等到超时。 */
static void snapshot_report_fail(const cam_state_t *st, const char *sid,
                                 const char *code, const char *msg)
{
    char json[288];
    snprintf(json, sizeof(json),
             "{\"snapshot_id\":\"%s\",\"device_id\":\"%s\",\"error_code\":\"%s\","
             "\"error_message\":\"%s\"}",
             sid, st->device_id ? st->device_id : "unknown",
             code ? code : "CAPTURE_FAILED", msg ? msg : "");
    char url[200];
    build_url(url, sizeof(url), "/api/v1/cam/snapshot/fail");
    char resp[96] = { 0 };
    post_bytes(url, (const uint8_t *)json, strlen(json), "application/json",
               4000, resp, sizeof(resp));
    ESP_LOGW(TAG, "已回报快照失败: %s (%s)", code, msg);
}

/**
 * 上传一张图像：sid != NULL 走快照口（带 id/seq/耗时），否则走第 4 周的拍照口。
 * @param t_get_us 取帧那一刻的 esp_timer（用于算"取帧→开始上传"的耗时）
 */
static esp_err_t upload_image(const cam_state_t *st, const camera_fb_t *fb,
                              const char *sid, int64_t t_get_us, int timeout_ms)
{
    char suffix[288];
    if (sid) {
        // 取帧→准备上传的耗时：后端拿它估计"快门时刻"（received - upload_ms）。
        // ★ 这里刻意**不**把整趟 POST 的耗时算进去 —— 那是上行传输，算进去会让
        //   快门时刻被推得太早。真实的误差上限就是这趟上行，文档里如实写明。
        int prep_ms = (int)((esp_timer_get_time() - t_get_us) / 1000);
        if (prep_ms < 0) prep_ms = 0;
        snprintf(suffix, sizeof(suffix),
                 "/api/v1/cam/snapshot/upload?sid=%s&device_id=%s&seq=%lu&w=%u&h=%u"
                 "&upload_ms=%d&uptime_ms=%lld",
                 sid, st->device_id ? st->device_id : "unknown",
                 (unsigned long)s_seq, (unsigned)fb->width, (unsigned)fb->height,
                 prep_ms, (long long)(t_get_us / 1000));
    } else {
        snprintf(suffix, sizeof(suffix),
                 "/api/v1/cam/photo?device_id=%s&w=%u&h=%u&seq=%lu",
                 st->device_id ? st->device_id : "unknown",
                 (unsigned)fb->width, (unsigned)fb->height, (unsigned long)s_seq);
    }

    char full[352];
    build_url(full, sizeof(full), suffix);
    char resp[256] = { 0 };
    return post_bytes(full, fb->buf, fb->len, "image/jpeg", timeout_ms,
                      resp, sizeof(resp));
}

// ===========================================================================
// 取一张图并上传（预览分辨率 / UXGA 重建）
//
// ★ 为什么 UXGA 不能只 set_framesize：帧缓冲/DMA 节点是 esp_camera_init 时按
//   recv_size = width*height/5 一次性分配好的。QVGA 建的是 16KB 级缓冲，
//   直接切到 UXGA（JPEG 常 100~200KB）会 FB-OVF、NO-EOI，最后取帧超时——
//   真机实测就是这个现象。要真拍到 1600x1200，必须让驱动按 UXGA 重新分配。
// ★ 代价：UXGA 期间预览中断约 9 秒，拍完立刻以 QVGA 重建并恢复推流。
// ===========================================================================
static bool capture_at(const cam_state_t *st, bool uxga, const char *sid)
{
    bool ok = false;
    int64_t t_get_us = 0;
    camera_config_t cfg;

    if (uxga) {
        ESP_LOGI(TAG, "收到%s → 以 %dx%d 重建相机（DMA 缓冲要按目标分辨率重分配）",
                 sid ? "快照(UXGA)指令" : "拍照指令", APP_CAM_PHOTO_W, APP_CAM_PHOTO_H);

        // ① 停掉预览实例（本函数在 cam_task 内调用，不存在并发取帧）
        esp_camera_deinit();
        build_camera_config(&cfg, FRAMESIZE_UXGA, 1);
        esp_err_t err = esp_camera_init(&cfg);
        if (err != ESP_OK) {
            status_set_error("UXGA 初始化失败: %s", esp_err_to_name(err));
            ESP_LOGW(TAG, "UXGA 初始化失败（%s）：本次无图像，绝不拿预览帧冒充",
                     esp_err_to_name(err));
            if (sid) snapshot_report_fail(st, sid, "UXGA_INIT_FAILED",
                                          esp_err_to_name(err));
            goto restore;
        }
        sensor_t *s = esp_camera_sensor_get();
        if (s) s->set_quality(s, APP_CAM_PHOTO_QUALITY);

        // ② 新分辨率下 AE/AGC 要几帧才稳，先丢 3 帧
        for (int i = 0; i < 3; i++) {
            camera_fb_t *warm = esp_camera_fb_get();
            if (warm) esp_camera_fb_return(warm);
            vTaskDelay(pdMS_TO_TICKS(80));
        }
    }

    s_seq++;                      // ★ 快照的序号必须严格大于请求时的基线
    t_get_us = esp_timer_get_time();
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
        status_set_error("%s取帧失败", uxga ? "UXGA" : "快照");
        ESP_LOGW(TAG, "%s取帧失败（本次无图像，不补拍假的）", uxga ? "UXGA" : "快照");
        if (sid) snapshot_report_fail(st, sid, "GRAB_FAILED",
                                      "esp_camera_fb_get() 返回空（取帧失败）");
        goto restore;
    }

    {
        int timeout = uxga ? APP_CAM_PHOTO_TIMEOUT_MS : APP_CAM_SNAPSHOT_TIMEOUT_MS;
        esp_err_t perr = upload_image(st, fb, sid, t_get_us, timeout);
        if (perr == ESP_OK) {
            ok = true;
            if (s_lock) xSemaphoreTake(s_lock, portMAX_DELAY);
            if (sid) s_status.snapshots_taken++; else s_status.photos_taken++;
            if (s_lock) xSemaphoreGive(s_lock);
            ESP_LOGI(TAG, "%s已上传: %ux%u, %u 字节, seq=%lu",
                     sid ? "快照" : "照片", (unsigned)fb->width, (unsigned)fb->height,
                     (unsigned)fb->len, (unsigned long)s_seq);
        } else {
            status_set_error("%s上传失败 (%s)", sid ? "快照" : "照片",
                             esp_err_to_name(perr));
            ESP_LOGW(TAG, "%s上传失败: %s", sid ? "快照" : "照片", esp_err_to_name(perr));
            if (sid) snapshot_report_fail(st, sid, "UPLOAD_FAILED",
                                          esp_err_to_name(perr));
        }
        esp_camera_fb_return(fb);
    }

restore:
    if (!uxga) return ok;

    // ③ 无论成败都必须恢复预览：一直停在 UXGA 会让 5fps 推流变成 0.5fps
    esp_camera_deinit();
    build_camera_config(&cfg, FRAMESIZE_QVGA, 2);
    esp_err_t err = esp_camera_init(&cfg);
    if (err == ESP_OK) {
        if (s_lock) xSemaphoreTake(s_lock, portMAX_DELAY);
        s_status.ready  = true;
        s_status.last_w = APP_CAM_PREVIEW_W;
        s_status.last_h = APP_CAM_PREVIEW_H;
        if (s_lock) xSemaphoreGive(s_lock);
        report_camera_state(true, "");
        ESP_LOGI(TAG, "已恢复 %dx%d 预览", APP_CAM_PREVIEW_W, APP_CAM_PREVIEW_H);
    } else {
        if (s_lock) xSemaphoreTake(s_lock, portMAX_DELAY);
        s_status.ready = false;
        if (s_lock) xSemaphoreGive(s_lock);
        status_set_error("拍照后恢复预览失败: %s", esp_err_to_name(err));
        report_camera_state(false, esp_err_to_name(err));
        ESP_LOGE(TAG, "拍照后恢复预览失败: %s（需要重启板子或等自动重试）",
                 esp_err_to_name(err));
    }
    return ok;
}

static bool capture_photo(const cam_state_t *st)
{
    return capture_at(st, true, NULL);
}

static void capture_snapshot(const cam_state_t *st, const char *sid, const char *mode)
{
    bool uxga = (mode && strcmp(mode, "uxga") == 0);
    snapshot_report_begin(st, sid);
    ESP_LOGI(TAG, "按需快照 %s（mode=%s）→ 请求之后重新取一帧", sid, uxga ? "uxga" : "qvga");
    capture_at(st, uxga, sid);
}


// ===========================================================================
// 采集/上传任务
// ===========================================================================
static void cam_task(void *arg)
{
    const int period_ms = 1000 / (APP_CAM_PREVIEW_FPS > 0 ? APP_CAM_PREVIEW_FPS : 5);
    TickType_t last_wake = xTaskGetTickCount();

    // 等 Wi-Fi：没网时取帧上传毫无意义，还会白耗电与 PSRAM 带宽
    while (!uploader_wifi_is_connected()) {
        if (!s_quit_logged) {
            ESP_LOGI(TAG, "等待 Wi-Fi 连接后开始上传画面 ...");
            s_quit_logged = true;
        }
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    s_quit_logged = false;

    cam_state_t st = { 0 };
    if (s_provider) s_provider(&st);

    char log_url[300];
    build_url(log_url, sizeof(log_url), "/api/v1/cam/frame");
    ESP_LOGI(TAG, "画面开始上传：%dx%d JPEG @ %dfps → %s",
             APP_CAM_PREVIEW_W, APP_CAM_PREVIEW_H, APP_CAM_PREVIEW_FPS, log_url);

    if (s_lock) xSemaphoreTake(s_lock, portMAX_DELAY);
    s_status.streaming = true;
    if (s_lock) xSemaphoreGive(s_lock);

    while (true) {
        // ---- 1) 取一帧真实画面 ----
        camera_fb_t *fb = esp_camera_fb_get();
        if (!fb) {
            status_set_error("预览取帧失败");
            ESP_LOGW(TAG, "预览取帧失败（不重复发送上一帧冒充新帧）");
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }

        // ---- 2) 上传（URL 带 seq/宽高，后端据此判断新旧）----
        s_seq++;
        char suffix[200];
        snprintf(suffix, sizeof(suffix),
                 "/api/v1/cam/frame?device_id=%s&seq=%lu&w=%u&h=%u",
                 st.device_id ? st.device_id : "unknown",
                 (unsigned long)s_seq, (unsigned)fb->width, (unsigned)fb->height);
        char url[300];
        build_url(url, sizeof(url), suffix);

        // ★ 384 而不是 192：这条响应里塞了 server_time_utc + snapshot_id + snapshot_mode，
        //   实测 184 字节左右，留 192 只剩几个字节余量 —— 一旦后端多加一个字段，
        //   snapshot_id 就会被截断成半截字符串，设备拿它去上传只会收到 404。
        char resp[384] = { 0 };
        esp_err_t err = post_bytes(url, fb->buf, fb->len, "image/jpeg",
                                   APP_CAM_FRAME_TIMEOUT_MS, resp, sizeof(resp));

        if (err == ESP_OK) {
            s_fail_streak = 0;
            if (s_lock) xSemaphoreTake(s_lock, portMAX_DELAY);
            s_status.frames_sent++;
            s_status.last_seq     = s_seq;
            s_status.last_w       = (int)fb->width;
            s_status.last_h       = (int)fb->height;
            s_status.last_bytes   = (int)fb->len;
            s_status.last_frame_us = esp_timer_get_time();
            s_status.last_error[0] = '\0';
            uint32_t sent = s_status.frames_sent;
            if (s_lock) xSemaphoreGive(s_lock);

            // 每 50 帧打一次真实统计（不是目标值，是实测值）
            if ((sent % 50) == 0) {
                ESP_LOGI(TAG, "预览已上传 %lu 帧，最近一帧 %ux%u / %u 字节",
                         (unsigned long)sent, (unsigned)fb->width,
                         (unsigned)fb->height, (unsigned)fb->len);
            }

            // ---- 3) 响应里夹带的拍照指令 ----
            if (strstr(resp, "\"capture\":true") != NULL ||
                strstr(resp, "\"capture\": true") != NULL) {
                s_photo_pending = true;
            }

            // ---- 3b) 第 7 周：响应里夹带的按需快照请求 ----
            //   ★ 只有当后端真的下发了一个 snapshot_id 才取图。
            //     snapshot_id 为空 = 没有请求，"顺手拍一张"是绝对不允许的。
#if APP_CAM_SNAPSHOT_ENABLE
            {
                char sid[64];
                char smode[16];
                if (json_find_str(resp, "snapshot_id", sid, sizeof(sid))) {
                    if (!json_find_str(resp, "snapshot_mode", smode, sizeof(smode))) {
                        smode[0] = '\0';
                    }
                    if (!s_snap_pending && strcmp(sid, s_snap_last_sid) != 0) {
                        snprintf(s_snap_sid, sizeof(s_snap_sid), "%s", sid);
                        snprintf(s_snap_mode, sizeof(s_snap_mode), "%s", smode);
                        snprintf(s_snap_last_sid, sizeof(s_snap_last_sid), "%s", sid);
                        s_snap_pending = true;
                        ESP_LOGI(TAG, "后端请求按需快照: %s（mode=%s）", sid,
                                 smode[0] ? smode : "qvga");
                    }
                }
            }
#endif
        } else {
            s_fail_streak++;
            if (s_lock) xSemaphoreTake(s_lock, portMAX_DELAY);
            s_status.frame_errors++;
            if (s_lock) xSemaphoreGive(s_lock);
            status_set_error("帧上传失败 (%s)", esp_err_to_name(err));
        }

        esp_camera_fb_return(fb);

        // ---- 4) 拍照（放在 return 之后：拍照会重新取帧，不能占着预览缓冲）----
        if (s_photo_pending && !s_photo_busy) {
            s_photo_pending = false;
            s_photo_busy = true;
            capture_photo(&st);
            s_photo_busy = false;
        }

        // ---- 4b) 第 7 周：按需快照（同样必须在 fb_return 之后取新帧）----
#if APP_CAM_SNAPSHOT_ENABLE
        if (s_snap_pending && !s_photo_busy) {
            s_snap_pending = false;
            s_photo_busy = true;          // 借用同一个"相机正忙"标志，避免与拍照抢相机
            capture_snapshot(&st, s_snap_sid, s_snap_mode);
            s_photo_busy = false;
        }
#endif

        // ---- 5) 严格按预览帧率走（上传耗时计入周期）----
        vTaskDelayUntil(&last_wake, pdMS_TO_TICKS(period_ms));
    }
}

// ===========================================================================
// 对外接口
// ===========================================================================
void camera_set_state_provider(cam_state_provider_t fn)
{
    s_provider = fn;
}

bool camera_is_ready(void)
{
    return s_status.ready;
}

void camera_get_status(cam_status_t *out)
{
    if (!out) return;
    status_snapshot(out);
}

bool camera_request_photo(void)
{
    if (!s_status.ready) return false;
    if (s_photo_busy) return false;
    s_photo_pending = true;
    return true;
}

// ===========================================================================
// 探测失败后的自动重试
//
// ★ 为什么要有：摄像头"没插好"是排障现场最常见的状态。没有重试时，用户插紧排线
//   之后必须断电重启才能生效；有重试时最多等 30 秒画面就自己出现。
//   每轮都是完整的 init/deinit（库的 fail 路径自己会 deinit，XCLK 与 SCCB 都会被释放），
//   不会累积 LEDC 通道或 I2C 设备表。
// ★ 重试成功之前，状态一路保持 ready=false 并持续上报后端 —— 后端重启后
//   也能重新拿到"摄像头探测失败"的结论，网页不会退化成含糊的"还没有画面"。
// ===========================================================================
// 真正的一次探测尝试：成功就起上传任务，失败就把原因写进状态并上报。
// verbose=true 时打完整排查提示与总线扫描（只在开机第一次做，重试时只打一行，避免刷屏）。
static esp_err_t cam_try_init(bool verbose);

static void cam_retry_task(void *arg)
{
    (void)arg;
    static const int backoff_s[] = { 5, 10, 20, 30 };   // 30s 封顶，别刷日志
    int idx = 0;
    int attempt = 0;

    while (!s_status.ready) {
        vTaskDelay(pdMS_TO_TICKS(backoff_s[idx] * 1000));
        if (s_status.ready) break;

        attempt++;
        esp_err_t err = cam_try_init(false);
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "摄像头第 %d 次自动重试成功：排线已修好，画面即将开始上传", attempt);
            break;
        }
        report_camera_state(false, esp_err_to_name(err));
        if (idx < (int)(sizeof(backoff_s) / sizeof(backoff_s[0])) - 1) idx++;
    }
    vTaskDelete(NULL);
}

static esp_err_t cam_try_init(bool verbose)
{
    // 引脚/时钟/PSRAM 都在 build_camera_config() 里统一维护（预览与拍照共用）
    camera_config_t cfg;
    build_camera_config(&cfg, FRAMESIZE_QVGA, 2);

    esp_err_t err = esp_camera_init(&cfg);
    if (err != ESP_OK) {
        // 如实停用：不降级成"假画面"。其它功能完全不受影响。
        // 失败时库里已自行 deinit（XCLK/SCCB 都释放），所以可以安全地反复重试。
        if (verbose) {
            ESP_LOGE(TAG, "摄像头初始化失败: %s (0x%x)。", esp_err_to_name(err), err);
            ESP_LOGE(TAG, "  请检查：① 摄像头排线是否插好；② PSRAM 是否已启用"
                          "（menuconfig → Component config → ESP PSRAM）；"
                          "③ OV2640 是否被正确供电。");
            cam_i2c_scan_bus();     // 把"为什么失败"变成可核对的证据
        } else {
            ESP_LOGW(TAG, "摄像头重试仍失败: %s", esp_err_to_name(err));
        }
        status_set_error("esp_camera_init 失败: %s", esp_err_to_name(err));
        report_camera_state(false, esp_err_to_name(err));
        return err;
    }

    sensor_t *s = esp_camera_sensor_get();
    uint16_t pid = s ? (uint16_t)s->id.PID : 0;

    if (s_lock) xSemaphoreTake(s_lock, portMAX_DELAY);
    s_status.ready      = true;
    s_status.sensor_pid = pid;
    s_status.last_w     = APP_CAM_PREVIEW_W;
    s_status.last_h     = APP_CAM_PREVIEW_H;
    s_status.last_error[0] = '\0';
    if (s_lock) xSemaphoreGive(s_lock);

    // ★ 真机判据：这行日志必须出现在串口里，才说明传感器被真的探测到了
    ESP_LOGI(TAG, "板载摄像头就绪：%s (PID=0x%02X) %dx%d JPEG q%d，目标 %d fps",
             pid == 0x26 ? "OV2640" : "未知型号", pid,
             APP_CAM_PREVIEW_W, APP_CAM_PREVIEW_H, APP_CAM_JPEG_QUALITY,
             APP_CAM_PREVIEW_FPS);

    report_camera_state(true, "");   // 让网页知道"摄像头是好的，只是还没出帧"

    BaseType_t ret = xTaskCreatePinnedToCore(cam_task, "cam", 8192, NULL, 3, NULL, 0);
    if (ret != pdPASS) {
        ESP_LOGE(TAG, "摄像头任务创建失败（内存不足）");
        s_status.ready = false;
        esp_camera_deinit();
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

esp_err_t camera_start(void)
{
    if (s_status.ready) return ESP_OK;          // 幂等

    if (!s_lock) {
        s_lock = xSemaphoreCreateMutex();
        if (!s_lock) return ESP_ERR_NO_MEM;
    }

    esp_err_t err = cam_try_init(true);
    if (err != ESP_OK) {
        // 坚持"不伪造画面"，但挂一个低频重试任务：
        // 现场把排线插紧后最多等 30 秒画面自己出现，不必断电重启。
        BaseType_t ret = xTaskCreatePinnedToCore(cam_retry_task, "cam_retry",
                                                 4096, NULL, 2, NULL, 0);
        if (ret != pdPASS) {
            ESP_LOGW(TAG, "重试任务创建失败：插好排线后需要重启板子才会重新探测");
        } else {
            ESP_LOGW(TAG, "摄像头将按 5/10/20/30 秒自动重试探测（插好排线后无需重启）");
        }
    }
    return err;
}
