/**
 * @file cmd_client.c
 * @brief 第 2 周：远程采集指令客户端实现。
 *
 * 一次远程采集的完整时序（与 docs/06 §1 链路总览一致）：
 *
 *   ① 本任务每 APP_COMMAND_POLL_MS 轮询 POST /device/commands/poll
 *      —— 请求体带上设备当前进度（boot_count / seq_last / up_ms / health）
 *   ② 解析出 collect_now 指令（含 request_id / nonce / baseline）
 *   ③ **立刻**回执 POST /device/ack (ack_type=received)   ← 页面「设备接收回执」的来源
 *   ④ 读一次真实寄存器（imu_read_raw_averaged）
 *   ⑤ 组观测报文（带 request_id + nonce + 新的 seq）POST /observations
 *      → 后端做 L1∧L2∧L3 校验，通过才 completed
 *      → 读失败则回执 rejected/SENSOR_ERROR，**不上传任何值**
 *
 * 红线复述：本文件不产生任何测量值，也不缓存任何"上次的值"来顶替。
 */

#include "cmd_client.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <sys/time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_heap_caps.h"

#include "app_config.h"
#include "imu_qma6100p.h"
#include "uploader.h"

static const char *TAG = "cmd";

// ---- 由 main.c 注入的状态提供者 ----
static cmd_state_fn_t s_state_fn = NULL;
void cmd_client_set_state_provider(cmd_state_fn_t fn) { s_state_fn = fn; }

static cmd_periodic_fn_t s_periodic_fn = NULL;
void cmd_client_set_periodic_handler(cmd_periodic_fn_t fn) { s_periodic_fn = fn; }

static uint32_t s_ack_count     = 0;
static uint32_t s_collect_count = 0;
uint32_t cmd_client_ack_count(void)     { return s_ack_count; }
uint32_t cmd_client_collect_count(void) { return s_collect_count; }

/** 采样互斥：远程采集要"自己采一个点"，不能与采样任务的读并发打 I2C。 */
static SemaphoreHandle_t s_imu_lock = NULL;

// 端点 URL 由 APP_SERVER_URL 推导（最后一次 '/' 之前的基址 + 各自的路径）
static char s_base[160]  = { 0 };
static char s_obs_url[200]   = { 0 };
static char s_ack_url[200]   = { 0 };

/**
 * @brief 从 APP_SERVER_URL 推导出服务器基址。
 *
 * APP_SERVER_URL 形如 "http://10.1.41.182:8000/api/v1/ingest"，
 * 把最后一段路径去掉就是基址 —— 这样换服务器地址时仍然只改 config 里那一行。
 */
static void build_urls(void)
{
    strncpy(s_base, APP_SERVER_URL, sizeof(s_base) - 1);
    char *p = strstr(s_base, "/api/v1/");
    if (p) {
        *p = '\0';
    } else {
        // 配置里没写标准路径时如实告警，并按"去掉最后一段"兜底
        ESP_LOGW(TAG, "APP_SERVER_URL 不含 /api/v1/，按最后一段路径截断推导基址");
        char *slash = strrchr(s_base, '/');
        if (slash && slash != s_base) *slash = '\0';
    }
    snprintf(s_obs_url, sizeof(s_obs_url), "%s/api/v1/observations", s_base);
    snprintf(s_ack_url, sizeof(s_ack_url), "%s/api/v1/device/ack", s_base);
    ESP_LOGI(TAG, "指令通道基址 = %s", s_base);
}

/**
 * @brief 从 JSON 里取一个字符串字段的值（简单解析，够用且不引入 cJSON）。
 *
 * ★ 必须容忍冒号后的空格：后端用 Python 的 json.dumps 输出，分隔符是
 *   `"key": "value"`（冒号后**带一个空格**），而板子如果按 `"key":"value"`
 *   去匹配就会**一条指令都解析不出来**。
 *   实测踩过这个坑：板子每秒钟都成功收到含 collect_now 的响应（HTTP 200、
 *   402 字节），却始终不打 "[CMD] recv"，直到请求超时 —— 看起来像"设备没收到
 *   指令"，其实是解析器的分隔符写死了。所以这里允许 `:` 后面有任意个空格。
 *
 * @return true=找到并取值成功；false=没有该字段。
 */
static bool json_get_str(const char *json, const char *key, char *out, size_t out_len)
{
    char pat[48];
    snprintf(pat, sizeof(pat), "\"%s\":", key);
    const char *p = strstr(json, pat);
    if (!p) return false;
    p += strlen(pat);
    while (*p == ' ' || *p == '\t') p++;      // ← 容忍 json.dumps 的空格
    if (*p != '"') return false;              // 不是字符串（例如 null / 数字）
    p++;
    const char *e = strchr(p, '"');
    if (!e) return false;
    size_t n = (size_t)(e - p);
    if (n >= out_len) n = out_len - 1;
    memcpy(out, p, n);
    out[n] = '\0';
    return true;
}

// ===========================================================================
// 回执
// ===========================================================================
/**
 * @brief 发送设备回执（POST /api/v1/device/ack）。
 *
 * ★ 回执 ≠ 完成：acked 只证明"设备收到了指令"。
 *   页面上「设备接收回执」这一状态**只**由本函数产生的报文驱动。
 */
static esp_err_t send_ack(const char *request_id, const char *nonce,
                          const char *ack_type, const char *reason, int attempt)
{
    cmd_device_state_t st = { 0 };
    if (s_state_fn) s_state_fn(&st);

    const long long up_ms = (long long)(esp_timer_get_time() / 1000);
    char *body = (char *)malloc(512);
    if (!body) return ESP_ERR_NO_MEM;

    snprintf(body, 512,
             "{\"request_id\":\"%s\",\"device_id\":\"%s\",\"ack_type\":\"%s\","
             "\"nonce\":%s%s%s,\"attempt\":%d,"
             "\"boot_count\":%u,\"up_ms\":%lld,\"reason\":%s%s%s}",
             request_id, st.device_id ? st.device_id : "unknown", ack_type,
             nonce ? "\"" : "null", nonce ? nonce : "", nonce ? "\"" : "",
             attempt,
             (unsigned)st.boot_count, up_ms,
             reason ? "\"" : "null", reason ? reason : "", reason ? "\"" : "");

    char resp[256] = { 0 };
    int status = 0;
    esp_err_t err = ESP_FAIL;
    // ★ 回执必须重试：后端按 `deadline_utc` 计时，一次网络抖动就会让整次采集
    //   白白超时。采集链路的每一步（回执 + 观测）都要有重试，与第 1 周上传一致。
    for (int i = 1; i <= APP_HTTP_RETRY; i++) {
        err = uploader_post_json_url(s_ack_url, body, resp, sizeof(resp), &status);
        if (err == ESP_OK) break;
        ESP_LOGW(TAG, "[ACK] 第 %d/%d 次失败(%s http=%d)，500ms 后重试",
                 i, APP_HTTP_RETRY, esp_err_to_name(err), status);
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    if (err == ESP_OK) {
        s_ack_count++;
        ESP_LOGI(TAG, "[ACK] %s request_id=%s attempt=%d http=%d",
                 ack_type, request_id, attempt, status);
    } else {
        ESP_LOGE(TAG, "[ACK] 发送失败(%s) http=%d request_id=%s —— 后端会按未回执计时",
                 esp_err_to_name(err), status, request_id);
    }
    free(body);
    return err;
}

// ===========================================================================
// 观测组包与回传
// ===========================================================================
/**
 * @brief 为本次请求单独采一个点并组观测报文。
 *
 * ★ 数值来源：imu_read_raw_averaged() 的真实寄存器读。
 *   读失败 → 返回 ESP_FAIL，由调用方回执 rejected/SENSOR_ERROR，
 *   **绝不用任何构造值顶替**（沿用第 1 周红线）。
 */
static char *build_observation_json(const char *request_id, const char *nonce,
                                    uint32_t seq, long long up_ms,
                                    const char *ts_local, long long utc_ms, bool ts_ok,
                                    const imu_raw_sample_t *raw)
{
    const imu_info_t *info = imu_get_info();
    float gx = imu_raw_to_g(raw->x);
    float gy = imu_raw_to_g(raw->y);
    float gz = imu_raw_to_g(raw->z);
    float ms2x = imu_raw_to_ms2(raw->x);
    float ms2y = imu_raw_to_ms2(raw->y);
    float ms2z = imu_raw_to_ms2(raw->z);
    float mag = sqrtf(gx * gx + gy * gy + gz * gz);

    cmd_device_state_t st = { 0 };
    if (s_state_fn) s_state_fn(&st);

    char ts_utc[32], ts_loc[80];
    if (ts_ok) {
        snprintf(ts_utc, sizeof(ts_utc), "%lld", utc_ms);
        snprintf(ts_loc, sizeof(ts_loc), "\"%s\"", ts_local);
    } else {
        // 未对时 → 如实写 null，绝不编造时间
        snprintf(ts_utc, sizeof(ts_utc), "null");
        snprintf(ts_loc, sizeof(ts_loc), "null");
    }

    const char *fmt =
        "{"
        "\"request_id\":\"%s\","
        "\"nonce\":\"%s\","
        "\"collect_mode\":\"single\","
        "\"device_id\":\"%s\","
        "\"mac\":\"%s\","
        "\"boot_count\":%u,"
        "\"seq\":%u,"
        "\"seq_first\":%u,"
        "\"seq_last\":%u,"
        "\"sample_count\":1,"
        "\"sample_period_ms\":%d,"
        "\"up_ms\":%lld,"
        "\"ts_utc_ms\":%s,"
        "\"ts_local\":%s,"
        "\"sensor\":{\"model\":\"%s\",\"type\":\"accelerometer\",\"i2c_addr\":%u,"
            "\"chip_id\":%u,\"range_g\":%u,\"lsb_per_g\":%u,\"odr_hz\":%u,"
            "\"oversample\":%d},"
        "\"unit\":{\"raw\":\"LSB(14bit signed)\",\"accel_g\":\"g\","
            "\"accel_ms2\":\"m/s^2\",\"magnitude_g\":\"g\"},"
        "\"samples\":[{"
            "\"seq\":%u,\"idx\":1,\"up_ms\":%lld,\"ts_utc_ms\":%s,\"ts_local\":%s,"
            "\"bound_request_id\":\"%s\","
            "\"raw\":{\"x\":%d,\"y\":%d,\"z\":%d},"
            "\"accel_g\":{\"x\":%.6f,\"y\":%.6f,\"z\":%.6f},"
            "\"accel_ms2\":{\"x\":%.6f,\"y\":%.6f,\"z\":%.6f},"
            "\"magnitude_g\":%.6f}],"
        "\"raw\":{\"x\":%d,\"y\":%d,\"z\":%d},"
        "\"accel_g\":{\"x\":%.6f,\"y\":%.6f,\"z\":%.6f},"
        "\"accel_ms2\":{\"x\":%.6f,\"y\":%.6f,\"z\":%.6f},"
        "\"magnitude_g\":%.6f"
        "}";

    int len = snprintf(NULL, 0, fmt,
                       request_id, nonce, st.device_id ? st.device_id : "unknown",
                       st.mac_str ? st.mac_str : "", (unsigned)st.boot_count,
                       (unsigned)seq, (unsigned)seq, (unsigned)seq,
                       APP_SAMPLE_PERIOD_MS, up_ms, ts_utc, ts_loc,
                       info->name, (unsigned)info->i2c_addr, (unsigned)info->chip_id,
                       (unsigned)info->range_g, (unsigned)info->lsb_per_g,
                       (unsigned)info->odr_hz, APP_OVERSAMPLE,
                       (unsigned)seq, up_ms, ts_utc, ts_loc, request_id,
                       raw->x, raw->y, raw->z,
                       (double)gx, (double)gy, (double)gz,
                       (double)ms2x, (double)ms2y, (double)ms2z, (double)mag,
                       raw->x, raw->y, raw->z,
                       (double)gx, (double)gy, (double)gz,
                       (double)ms2x, (double)ms2y, (double)ms2z, (double)mag);
    if (len <= 0) return NULL;

    char *text = (char *)malloc((size_t)len + 1);
    if (!text) return NULL;
    snprintf(text, (size_t)len + 1, fmt,
             request_id, nonce, st.device_id ? st.device_id : "unknown",
             st.mac_str ? st.mac_str : "", (unsigned)st.boot_count,
             (unsigned)seq, (unsigned)seq, (unsigned)seq,
             APP_SAMPLE_PERIOD_MS, up_ms, ts_utc, ts_loc,
             info->name, (unsigned)info->i2c_addr, (unsigned)info->chip_id,
             (unsigned)info->range_g, (unsigned)info->lsb_per_g,
             (unsigned)info->odr_hz, APP_OVERSAMPLE,
             (unsigned)seq, up_ms, ts_utc, ts_loc, request_id,
             raw->x, raw->y, raw->z,
             (double)gx, (double)gy, (double)gz,
             (double)ms2x, (double)ms2y, (double)ms2z, (double)mag,
             raw->x, raw->y, raw->z,
             (double)gx, (double)gy, (double)gz,
             (double)ms2x, (double)ms2y, (double)ms2z, (double)mag);
    return text;
}

/**
 * @brief 执行一次 collect_now：真实读一次 → 组包 → 回传观测。
 *
 * @return true 表示观测已成功送达后端（后端是否判 completed 由 L1/L2/L3 决定）
 */
static bool do_collect(const char *request_id, const char *nonce)
{
    ESP_LOGI(TAG, "[CMD] 执行 collect_now: request_id=%s nonce=%.8s...", request_id, nonce);

    // ---- 读一次真实寄存器（与采样任务互斥，避免并发打 I2C）----
    imu_raw_sample_t raw = { 0 };
    esp_err_t err;
    if (s_imu_lock) xSemaphoreTake(s_imu_lock, portMAX_DELAY);
    err = imu_read_raw_averaged(&raw, APP_OVERSAMPLE, APP_OVERSAMPLE_US);
    if (s_imu_lock) xSemaphoreGive(s_imu_lock);

    if (err != ESP_OK) {
        // ★ 读失败 → 如实回执 rejected/SENSOR_ERROR，**不上传任何值**。
        //   这才是"硬件问题"的证据路径（与 timeout 彻底分开）。
        ESP_LOGE(TAG, "[CMD] 传感器读取失败(%s) request_id=%s → 回执 rejected/SENSOR_ERROR，"
                      "不上传任何构造值", esp_err_to_name(err), request_id);
        send_ack(request_id, nonce, "rejected", "SENSOR_ERROR", 1);
        return false;
    }

    cmd_device_state_t st = { 0 };
    if (s_state_fn) s_state_fn(&st);
    const uint32_t seq = st.seq_last + 1;          // 严格大于基线（L2）

    const long long up_ms = (long long)(esp_timer_get_time() / 1000);
    char ts_local[64] = { 0 };
    long long utc_ms = 0;
    bool ts_ok = false;
    if (uploader_time_synced()) {
        struct timeval tv;
        gettimeofday(&tv, NULL);
        struct tm tmv;
        time_t sec = tv.tv_sec;
        localtime_r(&sec, &tmv);
        char base[48], zone[8];
        strftime(base, sizeof(base), "%Y-%m-%dT%H:%M:%S", &tmv);
        strftime(zone, sizeof(zone), "%z", &tmv);
        snprintf(ts_local, sizeof(ts_local), "%s.%03d%s", base,
                 (int)(tv.tv_usec / 1000), zone);
        utc_ms = (long long)tv.tv_sec * 1000LL + (long long)(tv.tv_usec / 1000);
        ts_ok = true;
    }

    char *json = build_observation_json(request_id, nonce, seq, up_ms,
                                        ts_local, utc_ms, ts_ok, &raw);
    if (!json) {
        ESP_LOGE(TAG, "[CMD] 观测组包失败 request_id=%s", request_id);
        send_ack(request_id, nonce, "rejected", "INTERNAL", 1);
        return false;
    }

    // 串口打印本次远程采集的真实读数（与上报报文同源，可三方核对）
    ESP_LOGI(TAG, "[CMD-DATA] request_id=%s seq=%u up=%lldms raw=(%d,%d,%d) "
                  "|a|=%.3fg ts=%s",
             request_id, (unsigned)seq, up_ms, raw.x, raw.y, raw.z,
             (double)sqrtf(powf(imu_raw_to_g(raw.x), 2) + powf(imu_raw_to_g(raw.y), 2)
                           + powf(imu_raw_to_g(raw.z), 2)),
             ts_ok ? ts_local : "未对时");

    char resp[512] = { 0 };
    int status = 0;
    esp_err_t perr = ESP_FAIL;
    for (int attempt = 1; attempt <= APP_HTTP_RETRY; attempt++) {
        perr = uploader_post_json_url(s_obs_url, json, resp, sizeof(resp), &status);
        if (perr == ESP_OK) break;
        ESP_LOGW(TAG, "[OBS] 上传失败(第 %d/%d 次, http=%d)，1s 后重试",
                 attempt, APP_HTTP_RETRY, status);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    free(json);

    if (perr == ESP_OK) {
        s_collect_count++;
        // 回显后端判定结果，便于现场直接读出 checks / freshness
        ESP_LOGI(TAG, "[OBS] request_id=%s http=%d resp=%s", request_id, status, resp);
        return true;
    }
    ESP_LOGE(TAG, "[OBS] 观测上传失败 http=%d request_id=%s（后端将等到 deadline 后置 timeout）",
             status, request_id);
    return false;
}

// ===========================================================================
// 轮询任务
// ===========================================================================
/** @brief 把 poll 请求体组好：带上设备当前进度，供后端生成下一条指令的基线。 */
static char *build_poll_json(void)
{
    cmd_device_state_t st = { 0 };
    if (s_state_fn) s_state_fn(&st);
    const long long up_ms = (long long)(esp_timer_get_time() / 1000);
    uint32_t free_heap = (uint32_t)esp_get_free_heap_size();

    char *body = (char *)malloc(384);
    if (!body) return NULL;
    snprintf(body, 384,
             "{\"device_id\":\"%s\",\"boot_count\":%u,\"up_ms\":%lld,"
             "\"seq_last\":%u,\"periodic_enabled\":%s,"
             "\"health\":{\"sensor_ok\":true,\"free_heap\":%u},"
             "\"last_ack\":null}",
             st.device_id ? st.device_id : "unknown", (unsigned)st.boot_count,
             up_ms, (unsigned)st.seq_last, st.periodic ? "true" : "false",
             (unsigned)free_heap);
    return body;
}

static void cmd_poll_task(void *arg)
{
    (void)arg;
    char resp[1024];
    char request_id[64], nonce[64], type[32];
    bool logged_wait = false;

    ESP_LOGI(TAG, "指令轮询任务已启动: 每 %d ms 轮询 %s", APP_COMMAND_POLL_MS, s_base);

    while (1) {
        if (!uploader_wifi_is_connected()) {
            if (!logged_wait) {
                ESP_LOGW(TAG, "未联网，暂停指令轮询（周期上报不受影响）");
                logged_wait = true;
            }
            vTaskDelay(pdMS_TO_TICKS(APP_COMMAND_POLL_MS));
            continue;
        }
        logged_wait = false;

        char *body = build_poll_json();
        if (!body) {
            vTaskDelay(pdMS_TO_TICKS(APP_COMMAND_POLL_MS));
            continue;
        }

        char url[200];
        snprintf(url, sizeof(url), "%s/api/v1/device/commands/poll", s_base);
        int status = 0;
        resp[0] = '\0';
        esp_err_t err = uploader_post_json_url(url, body, resp, sizeof(resp), &status);
        free(body);

        if (err != ESP_OK) {
            // 轮询失败是正常情况（网络抖动），只按 debug 记录，不刷屏
            ESP_LOGD(TAG, "轮询失败(%s) http=%d", esp_err_to_name(err), status);
            vTaskDelay(pdMS_TO_TICKS(APP_COMMAND_POLL_MS));
            continue;
        }

        // ---- 解析指令 ----
        //   ★ 判断"有没有指令"必须靠**是否有 type 字段**，不能去匹配
        //     `"commands":[]` 这样的字面串 —— 后端 json.dumps 在空数组前后
        //     可能带空格（实测就是 `"commands": []`），字面匹配会漏判，
        //     把"正常无指令"误报成"解析失败"疯狂刷屏。
        if (!json_get_str(resp, "type", type, sizeof(type))) {
            ESP_LOGD(TAG, "无待执行指令");
            vTaskDelay(pdMS_TO_TICKS(APP_COMMAND_POLL_MS));
            continue;
        }
        if (!json_get_str(resp, "request_id", request_id, sizeof(request_id))) {
            ESP_LOGW(TAG, "指令缺少 request_id，忽略: %.120s", resp);
            vTaskDelay(pdMS_TO_TICKS(APP_COMMAND_POLL_MS));
            continue;
        }
        if (!json_get_str(resp, "nonce", nonce, sizeof(nonce))) {
            nonce[0] = '\0';
        }

        ESP_LOGI(TAG, "[CMD] recv %s request_id=%s", type, request_id);

        if (strcmp(type, "collect_now") == 0) {
            // ③ 立刻回执 received（后端据此把 submitted → acked）
            send_ack(request_id, nonce, "received", NULL, 1);
            // ④⑤ 读一次真实寄存器并回传观测
            do_collect(request_id, nonce);
        } else if (strcmp(type, "pause_reports") == 0 ||
                   strcmp(type, "resume_reports") == 0) {
            const bool enable = (strcmp(type, "resume_reports") == 0);
            // 先把本地行为切过去，再回执 —— 顺序不能反：
            // 否则会出现"后端显示已暂停，板子却还在发周期包"。
            if (s_periodic_fn) s_periodic_fn(enable);
            ESP_LOGI(TAG, "[CMD] %s 已应用（周期上报 = %s）", type,
                     enable ? "开启" : "暂停");
            send_ack(request_id, NULL, "received", NULL, 1);
        } else {
            ESP_LOGW(TAG, "[CMD] 未知指令类型 %s，如实回执 rejected", type);
            send_ack(request_id, nonce, "rejected", "UNSUPPORTED_COMMAND", 1);
        }

        vTaskDelay(pdMS_TO_TICKS(APP_COMMAND_POLL_MS));
    }
}

esp_err_t cmd_client_start(void)
{
    build_urls();
    if (!s_imu_lock) {
        s_imu_lock = xSemaphoreCreateMutex();
        if (!s_imu_lock) return ESP_ERR_NO_MEM;
    }
    // 放 core 0（Wi-Fi 协议栈所在核），不干扰 core 1 上严格 100ms 的采样任务
    BaseType_t ok = xTaskCreatePinnedToCore(cmd_poll_task, "cmd_poll", 8192, NULL, 4, NULL, 0);
    return (ok == pdPASS) ? ESP_OK : ESP_FAIL;
}

/** @brief 供 main.c 的采样任务在每次读 I2C 前后加锁（与远程采集互斥）。 */
void *cmd_client_imu_lock(void) { return (void *)s_imu_lock; }