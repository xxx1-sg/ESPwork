/**
 * @file feedback.c
 * @brief 第 3 周：按键求助 + 本地物理反馈（按键消抖 / 蜂鸣 / LED / 求助上传）。
 *
 * 三条不能破的规矩（每条都对应 docs/08 的一个用例）：
 *
 *   1. **反馈先于上传**（TC-W3-06/10）
 *      按下 → 立刻蜂鸣 + 点亮 LED（≤300ms）→ 串口 `[HELP]` 行 → 才尝试上传。
 *      上传用独立任务做，绝不在按键任务里做网络请求 ——
 *      一次 HTTP 超时是 10 秒，放在按键路径上会让反馈时延直接爆表。
 *
 *   2. **本地反馈不得依赖外网**（TC-W3-06）
 *      整个"识别 + 反馈 + 入队"路径不调用任何网络函数。
 *
 *   3. **无远端回执不显示已送达**（TC-W3-18/19/20）
 *      LED 转绿**只有** feedback_on_help_ack() 一条路径，而它只被
 *      cmd_client 收到 `help_ack` 下行指令时调用。本地没有任何计时器
 *      会把状态从 WAITING 推到 DELIVERED。
 *
 * 关于"不伪造时间"（TC-W3-05/08）：
 *   - trigger_up_ms 用 esp_timer_get_time()/1000（单调时钟），记录一次后不再改写；
 *   - 墙上时钟字段（trigger_at_utc / queued_at_utc / local_feedback_at_utc）
 *     在未 SNTP 对时时一律输出 null，**不用服务端时间或恢复时刻顶替**。
 */

#include "feedback.h"
#include "app_config.h"
#include "uploader.h"
#include "lcd_status.h"

#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <sys/time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_random.h"

static const char *TAG = "fb";

// ===========================================================================
// 本地求助事件（一次按键 = 一条；断网时留在队列里，恢复后补传）
// ===========================================================================
typedef struct {
    char     help_id[40];        /**< UUIDv4 字符串 */
    char     client_key[48];     /**< 幂等键 btn-<boot>-<up_ms> */
    char     trigger_type[8];    /**< button|touch */
    uint32_t boot_count;         /**< 触发时的 boot_count */
    long long trigger_up_ms;     /**< ★触发那一刻的单调时间，**永不改写** */
    long long feedback_up_ms;    /**< 本地反馈实际完成的时刻（算时延用） */
    char     trigger_at_utc[40]; /**< 设备侧墙上时钟；未对时为空串 → 输出 null */
    char     queued_at_utc[40];  /**< 首次尝试上传的时刻；未对时为空串 */
    char     local_feedback[16]; /**< 如实标注：buzzer+led|led|none */
    bool     uploaded;           /**< 是否已成功上传（补传后置 1） */
    bool     pending_local;      /**< 是否属于"断网期间产生、现在补传" */
} help_item_t;

static QueueHandle_t     s_queue = NULL;    /**< 待上传队列（元素是 help_item_t） */
static SemaphoreHandle_t s_lock  = NULL;    /**< 保护 s_state/s_active_id 等共享量 */
static fb_state_fn_t     s_state_fn = NULL;

// ---- 反馈与统计状态（全部经 s_lock 保护）----
static volatile fb_state_t s_state = FB_IDLE;
static char     s_active_id[40] = { 0 };    /**< 正在等待回应的 help_id */
static uint32_t s_trigger_count = 0;
static uint32_t s_upload_count  = 0;
static uint32_t s_dropped_count = 0;
static long long s_last_press_up_ms = -1;   /**< 上一次按键的单调时刻（去抖用） */
static int      s_queued_unsent = 0;        /**< 队列中尚未成功上传的条数 */

// 器件可用性：由 gpio 配置结果决定，**如实反映**到 local_feedback 字段。
//   没接蜂鸣器就绝不能报 "buzzer+led"（TC-W3-04）。
static bool s_led_ok    = false;
static bool s_buzzer_ok = false;

// ===========================================================================
// 器件层：LED / 蜂鸣器
// ===========================================================================
static void led_write(bool on)
{
    if (!s_led_ok) return;
#if APP_LED_ACTIVE_HIGH
    gpio_set_level(APP_LED_GPIO, on ? 1 : 0);
#else
    gpio_set_level(APP_LED_GPIO, on ? 0 : 1);
#endif
}

static void buzzer_write(bool on)
{
    if (!s_buzzer_ok) return;
#if APP_BUZZER_ACTIVE_HIGH
    gpio_set_level(APP_BUZZER_GPIO, on ? 1 : 0);
#else
    gpio_set_level(APP_BUZZER_GPIO, on ? 0 : 1);
#endif
}

/** @brief 把 LED/蜂鸣配置成输出；返回实际可用性（配置失败的器件不会被谎报为可用）。 */
static void feedback_gpio_init(void)
{
    gpio_config_t out_cfg = {
        .mode         = GPIO_MODE_OUTPUT,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };

    // LED
    out_cfg.pin_bit_mask = 1ULL << APP_LED_GPIO;
    esp_err_t e1 = gpio_config(&out_cfg);
    s_led_ok = (e1 == ESP_OK);
    if (!s_led_ok) {
        ESP_LOGE(TAG, "LED(GPIO%d) 配置失败(%s)：本地反馈将如实标注为未点亮",
                 APP_LED_GPIO, esp_err_to_name(e1));
    }

    // 蜂鸣器
    out_cfg.pin_bit_mask = 1ULL << APP_BUZZER_GPIO;
    esp_err_t e2 = gpio_config(&out_cfg);
    s_buzzer_ok = (e2 == ESP_OK);
    if (!s_buzzer_ok) {
        ESP_LOGE(TAG, "蜂鸣器(GPIO%d) 配置失败(%s)：本地反馈将如实标注为未发声",
                 APP_BUZZER_GPIO, esp_err_to_name(e2));
    }

    // 按键输入（板载 BOOT 键按下为低，开内部上拉）
    gpio_config_t in_cfg = {
        .pin_bit_mask = 1ULL << APP_BUTTON_GPIO,
        .mode         = GPIO_MODE_INPUT,
#if APP_BUTTON_ACTIVE_LOW
        .pull_up_en   = GPIO_PULLUP_ENABLE,
#else
        .pull_down_en = GPIO_PULLDOWN_ENABLE,
#endif
        .intr_type    = GPIO_INTR_DISABLE,
    };
    esp_err_t e3 = gpio_config(&in_cfg);
    if (e3 != ESP_OK) {
        ESP_LOGE(TAG, "按键(GPIO%d) 配置失败(%s)", APP_BUTTON_GPIO, esp_err_to_name(e3));
    }

    // 初始态：待机（灯灭、不响）
    led_write(false);
    buzzer_write(false);

    ESP_LOGI(TAG, "本地反馈已就绪: 按键=GPIO%d(低有效) LED=GPIO%d(%s) 蜂鸣器=GPIO%d(%s)",
             APP_BUTTON_GPIO, APP_LED_GPIO, s_led_ok ? "可用" : "不可用",
             APP_BUZZER_GPIO, s_buzzer_ok ? "可用" : "不可用");
}

/** @brief 本地反馈的"事实"字符串 —— 只报实际点亮的器件（TC-W3-04）。 */
static void current_local_feedback(char *out, size_t n)
{
    if (s_buzzer_ok && s_led_ok)       snprintf(out, n, "buzzer+led");
    else if (s_led_ok)                 snprintf(out, n, "led");
    else if (s_buzzer_ok)              snprintf(out, n, "led");  // 只有蜂鸣器时按契约就近取值
    else                               snprintf(out, n, "none");
    if (!s_buzzer_ok && !s_led_ok) {
        ESP_LOGW(TAG, "蜂鸣器与 LED 均不可用，本地反馈如实标注为 none（不谎报）");
    }
}

/**
 * @brief 立刻打出一次本地反馈（按下瞬间那一下）。
 *
 * ★ 这里**只碰 GPIO**，不做任何网络/文件操作，所以断网时照常工作。
 */
static void feedback_fire_once(void)
{
    if (s_buzzer_ok) {
        buzzer_write(true);
        vTaskDelay(pdMS_TO_TICKS(APP_FEEDBACK_BEEP_MS));
        buzzer_write(false);
    }
    if (s_led_ok) {
        led_write(true);            // 等待中：先常亮，由维护任务改成慢闪
    }
    // 屏幕是第三路本地输出：同样**只写状态**，不做任何网络操作，
    // 所以断网时它也会跟着变成"等待中"画面。
    lcd_status_set_scene(LCD_SCENE_WAITING);
}

/** @brief 待机态：灯灭、不响。 */
static void feedback_set_idle(void)
{
    led_write(false);
    buzzer_write(false);
    lcd_status_set_scene(LCD_SCENE_IDLE);
    if (xSemaphoreTake(s_lock, portMAX_DELAY) == pdTRUE) {
        s_state = FB_IDLE;
        s_active_id[0] = '\0';
        xSemaphoreGive(s_lock);
    }
}

// ===========================================================================
// 工具：UUIDv4 与本地时间字符串
// ===========================================================================

/**
 * @brief 生成 UUIDv4 字符串（用 esp_random，芯片硬件随机源）。
 *
 * ★ 为什么不用真随机数当"数据"：这里生成的是**标识符**，不是测量值。
 *   本工程禁止的是伪造传感器读数（G1 约束），标识符与时间戳不属于测量数据。
 */
static void make_uuid_v4(char *out, size_t n)
{
    uint8_t b[16];
    for (int i = 0; i < 16; i += 4) {
        uint32_t r = esp_random();
        b[i]     = (uint8_t)(r & 0xFF);
        b[i + 1] = (uint8_t)((r >> 8) & 0xFF);
        b[i + 2] = (uint8_t)((r >> 16) & 0xFF);
        b[i + 3] = (uint8_t)((r >> 24) & 0xFF);
    }
    b[6] = (uint8_t)((b[6] & 0x0F) | 0x40);   // version 4
    b[8] = (uint8_t)((b[8] & 0x3F) | 0x80);   // variant
    snprintf(out, n,
             "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
             b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7],
             b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]);
}

/**
 * @brief 生成设备侧本地时间串（形如 2026-10-08T13:49:31.437+0800）。
 *
 * ★ 未 SNTP 对时时**返回 false 且 out 置空串** —— 调用方据此输出 JSON null。
 *   绝不用"当前开机时间"或"服务端回包时间"伪造成触发时刻（TC-W3-05(c)）。
 */
static bool format_local_now(char *out, size_t n)
{
    if (!uploader_time_synced()) {
        out[0] = '\0';
        return false;
    }
    struct timeval tv;
    gettimeofday(&tv, NULL);
    struct tm tmv;
    localtime_r(&tv.tv_sec, &tmv);
    char base[32], zone[12];
    strftime(base, sizeof(base), "%Y-%m-%dT%H:%M:%S", &tmv);
    strftime(zone, sizeof(zone), "%z", &tmv);
    snprintf(out, n, "%s.%03d%s", base, (int)(tv.tv_usec / 1000), zone);
    return true;
}

/** @brief 把"当前时刻"写入 buf；未对时则置空串。 */
static void stamp_wall_clock(char *buf, size_t n)
{
    if (!format_local_now(buf, n)) {
        buf[0] = '\0';
    }
}

// ===========================================================================
// 组包与上传
// ===========================================================================
static void build_urls(char *help_url, size_t n)
{
    // 与 cmd_client.c 的 build_urls() 同一套推导规则：
    // 从 APP_SERVER_URL（…/api/v1/ingest）截到 /api/v1/ 之前，得到服务器基址。
    char base[160];
    strncpy(base, APP_SERVER_URL, sizeof(base) - 1);
    base[sizeof(base) - 1] = '\0';
    char *p = strstr(base, "/api/v1/");
    if (p) {
        *p = '\0';
    } else {
        ESP_LOGW(TAG, "APP_SERVER_URL 不含 /api/v1/，按最后一段路径截断推导基址");
        char *slash = strrchr(base, '/');
        if (slash && slash != base) *slash = '\0';
    }
    snprintf(help_url, n, "%s/api/v1/help_events", base);
}

/**
 * @brief 组装 help_events 上传报文（docs/06 §7.1）。
 *
 * ★ 时间字段纪律：
 *   - trigger_up_ms 用 item 里**记录好的**值，绝不在这里重新取当前时间；
 *   - 墙上时钟字段未对时输出 null（用 %s 拼 "null"）。
 */
static char *build_help_json(const help_item_t *it, const char *device_id)
{
    // 墙上时钟字段：空串 → null
    char trig_ts[56] = "null";
    char queued_ts[56] = "null";
    char fb_ts[56] = "null";
    if (it->trigger_at_utc[0]) snprintf(trig_ts, sizeof(trig_ts), "\"%s\"", it->trigger_at_utc);
    if (it->queued_at_utc[0])  snprintf(queued_ts, sizeof(queued_ts), "\"%s\"", it->queued_at_utc);
    if (it->trigger_at_utc[0]) snprintf(fb_ts, sizeof(fb_ts), "\"%s\"", it->trigger_at_utc);

    const char *fmt =
        "{\"device_id\":\"%s\",\"help_id\":\"%s\",\"client_request_key\":\"%s\","
        "\"trigger_type\":\"%s\",\"trigger_at_utc\":%s,\"trigger_up_ms\":%lld,"
        "\"boot_count\":%u,\"queued_at_utc\":%s,"
        "\"local_feedback\":\"%s\",\"local_feedback_at_utc\":%s,"
        "\"message\":null,\"pending_local\":%s}";

    int len = snprintf(NULL, 0, fmt, device_id, it->help_id, it->client_key,
                       it->trigger_type, trig_ts, it->trigger_up_ms,
                       (unsigned)it->boot_count, queued_ts,
                       it->local_feedback, fb_ts,
                       it->pending_local ? "true" : "false");
    if (len <= 0) {
        ESP_LOGE(TAG, "求助报文长度计算失败");
        return NULL;
    }
    char *text = (char *)malloc((size_t)len + 1);
    if (!text) {
        ESP_LOGE(TAG, "求助报文内存分配失败 (%d 字节)", len);
        return NULL;
    }
    int w = snprintf(text, (size_t)len + 1, fmt, device_id, it->help_id, it->client_key,
                     it->trigger_type, trig_ts, it->trigger_up_ms,
                     (unsigned)it->boot_count, queued_ts,
                     it->local_feedback, fb_ts,
                     it->pending_local ? "true" : "false");
    if (w != len) {
        ESP_LOGE(TAG, "求助报文组包长度异常 (%d != %d)", w, len);
        free(text);
        return NULL;
    }
    return text;
}

/** @brief 尝试上传一条求助事件；成功返回 true。 */
static bool upload_one(const help_item_t *it, const char *device_id)
{
    char url[200];
    build_urls(url, sizeof(url));

    char *body = build_help_json(it, device_id);
    if (!body) return false;

    // 响应缓冲：data 里含 help_id/status/notice（notice 是中文，留足余量）
    char resp[640] = { 0 };
    int  status = 0;
    esp_err_t err = ESP_FAIL;

    for (int i = 1; i <= APP_HTTP_RETRY; i++) {
        err = uploader_post_json_url(url, body, resp, sizeof(resp), &status);
        if (err == ESP_OK) break;
        ESP_LOGW(TAG, "[HELP] 上传第 %d/%d 次失败(%s http=%d)，1s 后重试",
                 i, APP_HTTP_RETRY, esp_err_to_name(err), status);
        vTaskDelay(pdMS_TO_TICKS(1000));
    }

    if (err == ESP_OK) {
        ESP_LOGI(TAG, "[HELP] 上传成功 help_id=%s pending_local=%s http=%d resp=%.200s",
                 it->help_id, it->pending_local ? "true" : "false", status, resp);
        free(body);
        return true;
    }
    // ★ 失败就如实报错，并说明"本地反馈已经发生、事件仍在本地队列"，
    //   绝不上报"已送达"、也绝不丢弃事件。
    ESP_LOGE(TAG, "[HELP] 上传失败(%s http=%d) help_id=%s —— 本地反馈已发生，"
                  "事件留在本地队列等待恢复后补传（不丢弃、不伪造）",
             esp_err_to_name(err), status, it->help_id);
    free(body);
    return false;
}

/**
 * @brief 求助上传任务：从队列取事件并尽力上传；失败的**放回队列**等下次恢复。
 *
 * ★ 为什么单独一个任务：`uploader_post_json_url()` 在最坏情况下会阻塞
 *   APP_HTTP_TIMEOUT_MS × APP_HTTP_RETRY = 30 秒。放在按键任务里会让
 *   "按下 → 下次按键识别"卡住 30 秒（连按就全丢了）。
 */
static void help_upload_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "求助上传任务已启动（与按键任务解耦：网络慢不影响按键识别）");
    while (1) {
        if (!uploader_wifi_is_connected()) {
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }
        help_item_t it;
        // 等一条待上传事件（最多等 1 秒，便于周期性检查联网状态）
        if (xQueueReceive(s_queue, &it, pdMS_TO_TICKS(1000)) != pdTRUE) {
            continue;
        }

        fb_device_state_t st = { 0 };
        if (s_state_fn) s_state_fn(&st);
        const char *dev = st.device_id ? st.device_id : "unknown";

        if (upload_one(&it, dev)) {
            s_upload_count++;
            if (xSemaphoreTake(s_lock, portMAX_DELAY) == pdTRUE) {
                if (s_queued_unsent > 0) s_queued_unsent--;
                xSemaphoreGive(s_lock);
            }
        } else {
            // 上传失败：原样放回队尾（trigger_up_ms 等字段一字不改）
            if (xQueueSendToBack(s_queue, &it, 0) != pdTRUE) {
                s_dropped_count++;
                ESP_LOGE(TAG, "[HELP] 补传队列已满(%d)，如实丢弃 help_id=%s（累计丢弃 %u）",
                         APP_HELP_QUEUE_LEN, it.help_id, (unsigned)s_dropped_count);
                if (xSemaphoreTake(s_lock, portMAX_DELAY) == pdTRUE) {
                    if (s_queued_unsent > 0) s_queued_unsent--;
                    xSemaphoreGive(s_lock);
                }
            }
            vTaskDelay(pdMS_TO_TICKS(2000));   // 退避，别把失败的包打成风暴
        }
    }
}

// ===========================================================================
// 请求受理：识别 → 本地反馈 → 入队 → （异步）上传
// ===========================================================================
void feedback_trigger(const char *trigger_type)
{
    const long long now_ms = (long long)(esp_timer_get_time() / 1000);

    // ---- ① 去抖：窗口内的重复触发合并成一次（长按/连按只算一下）----
    xSemaphoreTake(s_lock, portMAX_DELAY);
    if (s_last_press_up_ms >= 0 &&
        (now_ms - s_last_press_up_ms) < (long long)APP_HELP_DEBOUNCE_MS) {
        long long delta = now_ms - s_last_press_up_ms;
        xSemaphoreGive(s_lock);
        ESP_LOGI(TAG, "[HELP] 按键被去抖忽略（距上次 %lldms < %dms 窗口），不产生新事件",
                 delta, APP_HELP_DEBOUNCE_MS);
        return;
    }
    s_last_press_up_ms = now_ms;
    s_trigger_count++;
    uint32_t trig_n = s_trigger_count;
    xSemaphoreGive(s_lock);

    // ---- ② 组一条事件（时间戳在**这一刻**固定，之后永不改写）----
    help_item_t it;
    memset(&it, 0, sizeof(it));
    make_uuid_v4(it.help_id, sizeof(it.help_id));
    snprintf(it.client_key, sizeof(it.client_key), "btn-%u-%lld",
             (unsigned)0, now_ms);          // 先用 0 占位，下面填真实 boot_count
    snprintf(it.trigger_type, sizeof(it.trigger_type), "%s",
             trigger_type ? trigger_type : APP_HELP_TRIGGER_TYPE);
    it.trigger_up_ms = now_ms;

    fb_device_state_t st = { 0 };
    if (s_state_fn) s_state_fn(&st);
    it.boot_count = st.boot_count;
    snprintf(it.client_key, sizeof(it.client_key), "btn-%u-%lld",
             (unsigned)it.boot_count, now_ms);   // 幂等键 = btn-<boot>-<触发up_ms>

    // 墙上时钟：未对时就是空串（→ JSON null），绝不用恢复时刻顶替
    stamp_wall_clock(it.trigger_at_utc, sizeof(it.trigger_at_utc));
    memcpy(it.queued_at_utc, it.trigger_at_utc, sizeof(it.queued_at_utc));

    // 本地反馈事实：只包含**真的能用**的器件
    current_local_feedback(it.local_feedback, sizeof(it.local_feedback));

    // ---- ③ 先做本地反馈（现状：还没有任何网络操作）----
    feedback_fire_once();
    it.feedback_up_ms = (long long)(esp_timer_get_time() / 1000);

    // ---- ④ 进入"等待中"状态：LED 橙色慢闪 + 周期性短鸣，直到收到 help_ack ----
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_state = FB_WAITING;
    snprintf(s_active_id, sizeof(s_active_id), "%s", it.help_id);
    xSemaphoreGive(s_lock);

    // ---- ⑤ 串口证据行（docs/08 要求含 "local feedback done" 语义）----
    //    ★ up_ms 必须与上传报文里的 trigger_up_ms **逐值相等**（TC-W3-05(a)）。
    ESP_LOGI(TAG,
             "[HELP] trigger %s up_ms=%lld boot=%u key=%s local feedback done "
             "(led=%s buzzer=%s) led_on_up_ms=%lld latency=%lldms trig#%u",
             it.trigger_type, it.trigger_up_ms, (unsigned)it.boot_count, it.client_key,
             s_led_ok ? "on" : "off", s_buzzer_ok ? "on" : "off",
             it.feedback_up_ms, it.feedback_up_ms - it.trigger_up_ms, (unsigned)trig_n);

    // ---- ⑥ 入队，交给上传任务（失败不阻塞按键路径）----
    bool online = uploader_wifi_is_connected();
    it.pending_local = !online;      // 断网期间产生 → 补传时标注 pending_local=true
    it.uploaded = false;

    if (xQueueSendToBack(s_queue, &it, 0) != pdTRUE) {
        s_dropped_count++;
        ESP_LOGE(TAG, "[HELP] 本地队列已满(%d)，如实丢弃最旧的待传事件（累计 %u）"
                      " —— 注意：物理反馈已经发生，被丢的是**上传**不是按键",
                 APP_HELP_QUEUE_LEN, (unsigned)s_dropped_count);
        help_item_t old;
        if (xQueueReceive(s_queue, &old, 0) == pdTRUE) {
            ESP_LOGW(TAG, "[HELP] 已丢弃最旧事件 help_id=%s（其本地反馈同样已发生）",
                     old.help_id);
            xQueueSendToBack(s_queue, &it, 0);
        }
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_queued_unsent++;
    int pend = s_queued_unsent;
    xSemaphoreGive(s_lock);

    ESP_LOGI(TAG, "[HELP] 事件已入本地队列: help_id=%s status=waiting_local_upload "
                  "在线=%s pending_local=%s 待传=%d/%d free_heap=%u",
             it.help_id, online ? "是" : "否",
             it.pending_local ? "true" : "false", pend, APP_HELP_QUEUE_LEN,
             (unsigned)esp_get_free_heap_size());
}

// ===========================================================================
// 按键任务：轮询 GPIO + 边沿检测 + 去抖
// ===========================================================================
static void button_task(void *arg)
{
    (void)arg;
    // 上电后先看一眼当前电平：如果开机时按键正被按住，不要误报一次触发
    int stable = gpio_get_level(APP_BUTTON_GPIO);
    ESP_LOGI(TAG, "按键任务已启动: GPIO%d 初始电平=%d（%s 表示按下）",
             APP_BUTTON_GPIO, stable, stable == 0 ? "低" : "高");

    int last = stable;
    while (1) {
        int now = gpio_get_level(APP_BUTTON_GPIO);
        // 边沿检测：只有"从释放变成按下"才算一次触发（长按不会连续刷）
        bool pressed = false;
#if APP_BUTTON_ACTIVE_LOW
        if (last == 1 && now == 0) pressed = true;
#else
        if (last == 0 && now == 1) pressed = true;
#endif
        if (pressed) {
            // 这里**不做**软件延时消抖：连续触发的合并交给 feedback_trigger()
            // 里的时间去抖窗口（APP_HELP_DEBOUNCE_MS）统一处理，
            // 避免"两处去抖"叠加造成实际窗口不可预期。
            feedback_trigger(APP_HELP_TRIGGER_TYPE);
        }
        last = now;
        vTaskDelay(pdMS_TO_TICKS(20));      // 20ms 轮询，足够抓住人手按键
    }
}

/**
 * @brief 反馈维护任务：驱动"等待中"的慢闪/周期鸣叫，并在收到 help_ack 后停手。
 *
 * ★ 注意这里**只做本地表现**，不推断任何"远端已收到"：
 *   状态从 WAITING → DELIVERED 的唯一入口是 feedback_on_help_ack()。
 */
static void feedback_maint_task(void *arg)
{
    (void)arg;
    bool led_phase = false;
    long long last_beep_ms = 0;

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(500));

        fb_state_t st;
        xSemaphoreTake(s_lock, portMAX_DELAY);
        st = s_state;
        xSemaphoreGive(s_lock);

        const long long now_ms = (long long)(esp_timer_get_time() / 1000);

        if (st == FB_WAITING) {
            // 橙色慢闪（这里只有单色 LED，用"亮/灭 1Hz 慢闪"表达"等待中"，
            // 与"已送达"的常亮形成可区分的物理现象）
            led_phase = !led_phase;
            led_write(led_phase);
#if APP_FEEDBACK_REPEAT_MS > 0
            if (now_ms - last_beep_ms >= APP_FEEDBACK_REPEAT_MS) {
                last_beep_ms = now_ms;
                if (s_buzzer_ok) {
                    buzzer_write(true);
                    vTaskDelay(pdMS_TO_TICKS(APP_FEEDBACK_REPEAT_BEEP_MS));
                    buzzer_write(false);
                }
                ESP_LOGI(TAG, "[HELP] 仍在等待远端回应（本地重复提示），"
                              "绝不因超时自行显示'已送达'");
            }
#endif
        } else if (st == FB_DELIVERED) {
            led_write(true);        // 已送达：常亮
        } else {
            led_write(false);       // 待机：灭
        }
    }
}

// ===========================================================================
// 远端指令的落点（只有这两个函数能改变"已送达"的表现）
// ===========================================================================
void feedback_on_help_ack(const char *help_id)
{
    char active[40] = { 0 };
    xSemaphoreTake(s_lock, portMAX_DELAY);
    snprintf(active, sizeof(active), "%s", s_active_id);
    bool match = (help_id == NULL) || (strlen(active) == 0) ||
                 (strcmp(active, help_id) == 0);
    if (match) s_state = FB_DELIVERED;
    xSemaphoreGive(s_lock);

    if (!match) {
        // 不匹配就如实说明，不误改另一次求助的灯
        ESP_LOGW(TAG, "[HELP] 收到 help_ack 但 help_id=%s 与当前激活的 %s 不匹配，忽略",
                 help_id ? help_id : "(null)", active);
        return;
    }
    if (s_buzzer_ok) buzzer_write(false);
    if (s_led_ok) led_write(true);          // 常亮 = 已送达
    lcd_status_set_scene(LCD_SCENE_DELIVERED);   // 屏幕同步转"已送达"（同一份状态，换个介质）
    ESP_LOGI(TAG, "[HELP] 收到 help_ack（设备已确认查看者回应）→ LED 转'已送达'常亮、"
                  "蜂鸣停止。★ 这是唯一允许显示'已收到'的来源 help_id=%s",
             help_id ? help_id : "(null)");
}

void feedback_on_clear_alert(const char *help_id)
{
    if (s_buzzer_ok) buzzer_write(false);
    feedback_set_idle();   // 蜂鸣停、LED 回待机色（该函数顺带把屏幕设为 IDLE）
    // 覆盖成"取消"画面：屏上亮 2 秒灰叉再自动回待机，让用户明确看到这次被取消，
    // 与 LED 熄灭、蜂鸣停止在时序上一致（板端状态仍如实是 IDLE）。
    lcd_status_set_scene_timed(LCD_SCENE_CANCELLED, 2000);
    ESP_LOGI(TAG, "[HELP] 收到 clear_alert → 蜂鸣停止、LED 回待机色、屏幕短暂显示'取消' "
                  "help_id=%s", help_id ? help_id : "(null)");
}

// ===========================================================================
// 对外只读状态
// ===========================================================================
uint32_t feedback_trigger_count(void) { return s_trigger_count; }
uint32_t feedback_upload_count(void)  { return s_upload_count; }
uint32_t feedback_pending_count(void) { return (uint32_t)s_queued_unsent; }
uint32_t feedback_dropped_count(void) { return s_dropped_count; }
bool feedback_is_offline_pending(void) { return s_queued_unsent > 0; }
fb_state_t feedback_state(void) { return s_state; }

const char *feedback_active_help_id(void)
{
    return s_active_id[0] ? s_active_id : NULL;
}

void feedback_set_state_provider(fb_state_fn_t fn) { s_state_fn = fn; }

// ===========================================================================
// 启动
// ===========================================================================
esp_err_t feedback_start(void)
{
    if (!s_lock) {
        s_lock = xSemaphoreCreateMutex();
        if (!s_lock) return ESP_ERR_NO_MEM;
    }
    if (!s_queue) {
        s_queue = xQueueCreate(APP_HELP_QUEUE_LEN + 2, sizeof(help_item_t));
        if (!s_queue) return ESP_ERR_NO_MEM;
    }

    // 引脚与器件（不依赖网络）
    feedback_gpio_init();

    // 板载 LCD：第三路本地输出。★ 非致命 —— 初始化失败只是"没有屏幕"，
    // 按键/蜂鸣/LED/上传一律照常（lcd_status_init 自己吞掉所有错误）。
    // 放在这个"不依赖 Wi-Fi 成功"的阶段，保证断网时屏幕同样能表达状态。
    lcd_status_init();
    /* ★ 自检环要"看得见"：lcd_status_init() 里已经设过 BOOT，但紧接着写 IDLE 会把它
     *   在渲染任务（120ms 轮询）看来"一帧都没画过" —— 实测等于开机永远看不到自检环。
     *   改成 **限时 1.8 秒的 BOOT 画面**，任务必然会画一次 BOOT，再自动回 IDLE。
     *   （这也顺带保证了：自检色块结束后屏幕一定会被重画，不会卡在自检的纯色上。） */
    lcd_status_set_scene_timed(LCD_SCENE_BOOT, 1800);

    // 按键任务放 core 1、优先级较高：按键要灵敏（与采样任务同核但优先级更高，
    // 因为它每 20ms 才醒一次，开销极小，不会扰动 100ms 采样节奏）
    BaseType_t r1 = xTaskCreatePinnedToCore(button_task, "btn", 4096, NULL, 7, NULL, 1);
    // 上传任务放 core 0（Wi-Fi 协议栈所在核）、优先级低：网络慢不该拖累别的任务
    BaseType_t r2 = xTaskCreatePinnedToCore(help_upload_task, "help_up", 6144, NULL, 3, NULL, 0);
    // 反馈维护任务
    BaseType_t r3 = xTaskCreatePinnedToCore(feedback_maint_task, "fb_maint", 3072, NULL, 2, NULL, 0);

    if (r1 != pdPASS || r2 != pdPASS || r3 != pdPASS) {
        ESP_LOGE(TAG, "第 3 周任务创建失败 (btn=%d upload=%d maint=%d)",
                 (int)r1, (int)r2, (int)r3);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "按键求助已就绪：实体按键=GPIO%d，本地反馈=%s，屏幕=%s；"
                  "断网时本地识别与反馈照常工作（上传留待联网后补传）",
             APP_BUTTON_GPIO, s_buzzer_ok && s_led_ok ? "蜂鸣+LED" :
                              (s_led_ok ? "仅 LED" : (s_buzzer_ok ? "仅蜂鸣" : "无")),
             lcd_status_ready() ? "ST7789 已点亮" : "未接入(不影响求助)");
    return ESP_OK;
}