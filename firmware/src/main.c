/**
 * @file main.c
 * @brief ESP32-S3-EYE 真实物理传感器采集 + Wi-Fi 定时上传 主程序。
 *
 * 数据流（每一步都发生在真实硬件上）：
 *   ① QMA6100P 芯片寄存器 → ② 原始码值(raw) → ③ 物理量换算(g / m/s^2)
 *   → ④ 组 JSON（含单位、设备唯一 ID、本地时间戳）→ ⑤ Wi-Fi POST 到后端
 *
 * 设计红线（对应任务硬性约束）：
 *   - 代码中不存在任何 rand()/随机数/固定常量充当测量值；
 *   - 传感器读失败就如实报错并跳过本次上传，绝不用假数据顶替；
 *   - USB 串口只打印与烧录，正式数据一律走 Wi-Fi。
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_log.h"
#include "esp_system.h"
#include "esp_chip_info.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "esp_idf_version.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "math.h"

#include "app_config.h"
#include "imu_qma6100p.h"
#include "uploader.h"
#include "cmd_client.h"

static const char *TAG = "main";

// ===========================================================================
// 设备身份 / 启动计数
// ===========================================================================
static char     s_device_id[32] = { 0 };   /**< 设备唯一 ID，由芯片 eFuse MAC 派生 */
static char     s_mac_str[24]   = { 0 };   /**< 完整 MAC（形如 94:A9:90:1C:70:1C），供页面显示 */
static uint32_t s_boot_count     = 0;      /**< 第几次开机（保存在 NVS，掉电不丢） */
// 采样点的全局序号 seq 由采样任务内部维护（每个采样点自带 seq，见 batch_sample_t）
static uint32_t s_post_fail      = 0;      /**< 上传失败计数（如实统计，便于排错） */

/**
 * @brief 采样序号的"对外可见"副本（第 2 周新增）。
 *
 * ★ 为什么不能只让采样任务自己持有 seq：远程采集在**另一个任务**里为本次请求
 *   单独采一个点，它必须知道"当前进度到哪了"，才能给自己的点分配一个
 *   严格大于基线的 seq（后端的 L2 新旧性判定就靠这个）。
 *   所以把 seq 放进一个 volatile 全局量，两边共享。
 */
static volatile uint32_t s_seq = 0;

/** @brief 周期上报当前是否使能（收到 pause_reports/resume_reports 后切换）。 */
static volatile bool s_periodic_enabled = true;

/**
 * @brief 把设备当前状态提供给 cmd_client（组 poll/ack/观测报文时用）。
 *
 * 每次组包都现取，避免把 seq 定格成过期值 —— 采样任务以 10Hz 推进它。
 */
static void cmd_state_provider(cmd_device_state_t *out)
{
    out->device_id  = s_device_id;
    out->mac_str    = s_mac_str;
    out->boot_count = s_boot_count;
    out->seq_last   = s_seq;
    out->periodic   = s_periodic_enabled;
}

/**
 * @brief 应用后端下发的 pause_reports / resume_reports。
 *
 * ★ 语义：暂停的是**周期上报**，不是采集能力 —— 暂停后远程 collect_now
 *   仍必须能读到当下寄存器（当堂验证要证明的正是这一点）。
 */
static void cmd_periodic_handler(bool enable)
{
    if (s_periodic_enabled == enable) return;
    s_periodic_enabled = enable;
    ESP_LOGI(TAG, "周期上报已%s（采集能力不受影响，远程采集仍可用）",
             enable ? "恢复" : "暂停");
}

/**
 * @brief 用芯片出厂 eFuse MAC 生成设备唯一 ID。
 *        这是硬件唯一标识，不是随机数，重启/重烧都不变。
 *        同时把完整的 6 字节 MAC 格式化成字符串一并上报 —— 页面上要显示真实 MAC，
 *        与其让前端"猜"，不如让芯片把它报上来。
 */
static void init_device_id(void)
{
    uint8_t mac[6] = { 0 };
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(s_device_id, sizeof(s_device_id), "esp32s3eye-%02X%02X%02X",
             mac[3], mac[4], mac[5]);
    snprintf(s_mac_str, sizeof(s_mac_str), "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

/** @brief 从 NVS 读取并自增开机次数（真实计数，用于区分不同上电周期）。 */
static void init_boot_count(void)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open("app", NVS_READWRITE, &h);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "NVS 打开失败(%s), 本次 boot 计数按 0 处理", esp_err_to_name(err));
        return;
    }
    nvs_get_u32(h, "boot_count", &s_boot_count);
    s_boot_count++;
    nvs_set_u32(h, "boot_count", s_boot_count);
    nvs_commit(h);
    nvs_close(h);
    ESP_LOGI(TAG, "设备 ID = %s, 本次为第 %u 次开机", s_device_id, (unsigned)s_boot_count);
}

// ===========================================================================
// 工具函数
// ===========================================================================

/** @brief 把给定时刻格式化成带毫秒与时区的本地时间串，如 2026-06-05T13:04:05.123+0800。 */
static void format_local_time_at(const struct timeval *tv, char *buf, size_t len)
{
    struct tm tmv;
    time_t sec = tv->tv_sec;
    localtime_r(&sec, &tmv);
    char base[48];
    strftime(base, sizeof(base), "%Y-%m-%dT%H:%M:%S", &tmv);
    char zone[8];
    strftime(zone, sizeof(zone), "%z", &tmv);
    snprintf(buf, len, "%s.%03d%s", base, (int)(tv->tv_usec / 1000), zone);
}

/**
 * @brief 取「本次采样的时间戳」，全流程只调用一次 gettimeofday。
 *
 * ★ 为什么必须只取一次：串口打印的 ts 与上传 JSON 里的 ts_local 是同一个数据包
 *   的同一个时间戳，必须是**完全相同的字符串**。早先两处各自调用 gettimeofday，
 *   实测相差 2ms，三方核对时会显示成"时间戳不一致"，把真实链路误判成有问题。
 *
 * @return true=已对时并填写成功；false=未对时（此时不写时间，绝不编造）
 */
static bool sample_timestamp(char *local_buf, size_t local_len, long long *utc_ms)
{
    if (!uploader_time_synced()) {
        return false;
    }
    struct timeval tv;
    gettimeofday(&tv, NULL);
    format_local_time_at(&tv, local_buf, local_len);
    if (utc_ms) {
        *utc_ms = (long long)tv.tv_sec * 1000LL + (long long)(tv.tv_usec / 1000);
    }
    return true;
}

/** @brief 复位原因文字（排错时非常重要：区分掉电/看门狗/崩溃/人为复位）。 */
static const char *reset_reason_text(esp_reset_reason_t r)
{
    switch (r) {
    case ESP_RST_POWERON:   return "上电复位";
    case ESP_RST_EXT:       return "外部复位脚";
    case ESP_RST_SW:        return "软件复位";
    case ESP_RST_PANIC:     return "程序崩溃(panic)";
    case ESP_RST_INT_WDT:   return "中断看门狗";
    case ESP_RST_TASK_WDT:  return "任务看门狗";
    case ESP_RST_WDT:       return "其它看门狗";
    case ESP_RST_DEEPSLEEP: return "深度睡眠唤醒";
    case ESP_RST_BROWNOUT:  return "电压不足(brownout)";
    case ESP_RST_SDIO:      return "SDIO 复位";
    case ESP_RST_USB:       return "USB 外设复位";
    case ESP_RST_JTAG:      return "JTAG 复位";
    default:                return "未知";
    }
}

/** @brief 开机时的一次性硬件自检：静止状态下重力应约等于 1g。 */
static void gravity_selfcheck(void)
{
    ESP_LOGI(TAG, "硬件自检: 连续读取真实测量值, 检查合矢量是否接近 1g ...");
    float sum_mag = 0.0f;
    int   ok      = 0;
    for (int i = 0; i < 20; i++) {
        imu_raw_sample_t s;
        if (imu_read_raw(&s) == ESP_OK) {
            float gx = imu_raw_to_g(s.x);
            float gy = imu_raw_to_g(s.y);
            float gz = imu_raw_to_g(s.z);
            float mag = sqrtf(gx * gx + gy * gy + gz * gz);
            sum_mag += mag;
            ok++;
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    if (ok == 0) {
        ESP_LOGE(TAG, "自检失败: 20 次读取全部失败, 传感器无响应");
        return;
    }
    float avg = sum_mag / ok;
    ESP_LOGI(TAG, "自检结果: 平均合矢量 = %.3f g (%d/%d 次有效)", avg, ok, 20);
    if (avg > 0.80f && avg < 1.20f) {
        ESP_LOGI(TAG, "自检通过: 读数符合静止状态下的真实重力加速度");
    } else {
        ESP_LOGW(TAG, "自检异常: 合矢量偏离 1g 较多, 请确认板子是否被固定/是否有外力加速度");
    }
}

// ===========================================================================
// 组包
// ===========================================================================
/**
 * @brief 一个待上传的采样点（真实寄存器读数 + 它自己的时间戳）。
 *
 * 高密度采集的关键：每个点都带自己的时间戳，攒够一包后一次性上报。
 * 这样数据密度可以做到 10Hz，而 HTTPS 请求仍然是 1 次/秒
 * （每次握手/请求的固定开销被摊薄到 10 个点上）。
 */
typedef struct {
    imu_raw_sample_t raw;
    float            gx, gy, gz;
    float            ms2x, ms2y, ms2z;
    float            mag_g;
    bool             ts_synced;      // 该点是否已对时（未对时则时间戳如实写 null）
    long long        utc_ms;
    long long        up_ms;          // 该点的开机运行时间（毫秒）
    char             ts_local[64];   // 该点的本地时间字符串
    // ★ 每个采样点自带 (seq, idx)：网络慢时几个包会被合并成一个大包上传，
    //   有了这两个字段，串口 [DATA] 行、数据库行、网页显示仍然能一一对应。
    uint32_t         seq;
    uint16_t         idx;            // 包内序号，1..APP_BATCH_SIZE
} batch_sample_t;

/** @brief 一个"待上传的包"：APP_BATCH_SIZE 个采样点 + 包级信息。 */
typedef struct {
    batch_sample_t s[APP_BATCH_SIZE];
    int            n;
    uint32_t       seq_first, seq_last;
} batch_packet_t;

/**
 * @brief 把一批真实测量值组装成一个 JSON 数据包。
 *
 * 数据包包含（对应任务要求：原始测量值 / 物理单位 / 本地时间戳 / 设备唯一 ID）：
 *   - samples[]：本批每个采样点的 raw 码值、accel_g、accel_ms2、magnitude_g 与各自时间戳
 *   - 顶层 raw/accel_g/... 保留为"本批最后一个点"，老工具/老页面不改也能用
 *   - 传感器声明 sensor{}、单位声明 unit{}
 *   - 设备唯一 ID、开机计数、包序号、采样周期、本包点数
 *
 * 实现说明：这里用 snprintf 手工组包，不依赖任何第三方 JSON 库
 * （ESP-IDF 6.x 已移除内置 cJSON 组件，且手写更省 Flash、数值格式完全可控）。
 * 包内所有字符串均为本程序生成的固定格式（设备 ID、芯片型号、时间串），
 * 不含外部输入，因此不存在需要转义的字符。
 *
 * @return 成功返回 malloc 出来的 JSON 字符串（调用者用 free() 释放）；失败返回 NULL。
 */
static char *build_batch_json(const batch_sample_t *b, int n)
{
    if (n <= 0) return NULL;
    const imu_info_t     *info = imu_get_info();
    const batch_sample_t *last = &b[n - 1];

    // 顶层运行时长取"本批最后一个采样点"的值，保证与串口打印的是同一份数据
    const long long uptime_ms = last->up_ms;

    // ---- 时间戳：只有真的对上时(SNTP)才写时间，否则显式 null，绝不编时间 ----
    char ts_utc[32];
    char ts_local[80];
    if (last->ts_synced) {
        snprintf(ts_utc, sizeof(ts_utc), "%lld", last->utc_ms);
        snprintf(ts_local, sizeof(ts_local), "\"%s\"", last->ts_local);
    } else {
        snprintf(ts_utc, sizeof(ts_utc), "null");
        snprintf(ts_local, sizeof(ts_local), "null");
    }

    // ---- 先把本批每个采样点拼成 JSON 片段（每个点的码值与时间戳都是它自己的）----
    const size_t cap = (size_t)n * 320 + 64;        // 每点最多约 280 字节，留足余量
    char *arr = (char *)malloc(cap);
    if (!arr) {
        ESP_LOGE(TAG, "采样数组内存分配失败 (%u 字节)", (unsigned)cap);
        return NULL;
    }
    size_t off = 0;
    off += (size_t)snprintf(arr + off, cap - off, "\"samples\":[");
    for (int i = 0; i < n; i++) {
        const batch_sample_t *s = &b[i];
        char s_utc[32], s_local[80];
        if (s->ts_synced) {
            snprintf(s_utc, sizeof(s_utc), "%lld", s->utc_ms);
            snprintf(s_local, sizeof(s_local), "\"%s\"", s->ts_local);
        } else {
            snprintf(s_utc, sizeof(s_utc), "null");
            snprintf(s_local, sizeof(s_local), "null");
        }
        int w = snprintf(arr + off, cap - off,
                         "%s{\"seq\":%u,\"idx\":%d,\"up_ms\":%lld,\"ts_utc_ms\":%s,\"ts_local\":%s,"
                         "\"raw\":{\"x\":%d,\"y\":%d,\"z\":%d},"
                         "\"accel_g\":{\"x\":%.6f,\"y\":%.6f,\"z\":%.6f},"
                         "\"accel_ms2\":{\"x\":%.6f,\"y\":%.6f,\"z\":%.6f},"
                         "\"magnitude_g\":%.6f}",
                         i ? "," : "", (unsigned)s->seq, (int)s->idx,
                         s->up_ms, s_utc, s_local,
                         s->raw.x, s->raw.y, s->raw.z,
                         (double)s->gx, (double)s->gy, (double)s->gz,
                         (double)s->ms2x, (double)s->ms2y, (double)s->ms2z,
                         (double)s->mag_g);
        if (w <= 0 || (size_t)w >= cap - off) {
            ESP_LOGE(TAG, "采样数组拼装溢出 (第 %d 点)", i);
            free(arr);
            return NULL;
        }
        off += (size_t)w;
    }
    off += (size_t)snprintf(arr + off, cap - off, "]");

    const char *fmt =
        "{"
        "\"device_id\":\"%s\","
        "\"mac\":\"%s\","
        "\"boot_count\":%u,"
        "\"seq\":%u,"
        "\"seq_first\":%u,"
        "\"uptime_ms\":%lld,"
        "\"ts_utc_ms\":%s,"
        "\"ts_local\":%s,"
        "\"sample_period_ms\":%d,"
        "\"sample_count\":%d,"
        "\"sensor\":{"
            "\"model\":\"%s\","
            "\"type\":\"accelerometer\","
            "\"i2c_addr\":%u,"
            "\"chip_id\":%u,"
            "\"range_g\":%u,"
            "\"lsb_per_g\":%u,"
            "\"odr_hz\":%u,"
            "\"oversample\":%d"
        "},"
        "\"unit\":{"
            "\"raw\":\"LSB(14bit signed)\","
            "\"accel_g\":\"g\","
            "\"accel_ms2\":\"m/s^2\","
            "\"magnitude_g\":\"g\""
        "},"
        "%s,"
        "\"raw\":{\"x\":%d,\"y\":%d,\"z\":%d},"
        "\"accel_g\":{\"x\":%.6f,\"y\":%.6f,\"z\":%.6f},"
        "\"accel_ms2\":{\"x\":%.6f,\"y\":%.6f,\"z\":%.6f},"
        "\"magnitude_g\":%.6f"
        "}";

    // 先算长度再分配，保证不截断
    int len = snprintf(NULL, 0, fmt,
                       s_device_id, s_mac_str, (unsigned)s_boot_count,
                       (unsigned)last->seq, (unsigned)b[0].seq,
                       uptime_ms, ts_utc, ts_local,
                       APP_SAMPLE_PERIOD_MS, n,
                       info->name, (unsigned)info->i2c_addr, (unsigned)info->chip_id,
                       (unsigned)info->range_g, (unsigned)info->lsb_per_g,
                       (unsigned)info->odr_hz, APP_OVERSAMPLE,
                       arr,
                       last->raw.x, last->raw.y, last->raw.z,
                       (double)last->gx, (double)last->gy, (double)last->gz,
                       (double)last->ms2x, (double)last->ms2y, (double)last->ms2z,
                       (double)last->mag_g);
    if (len <= 0) {
        ESP_LOGE(TAG, "JSON 长度计算失败");
        free(arr);
        return NULL;
    }

    char *text = (char *)malloc((size_t)len + 1);
    if (!text) {
        ESP_LOGE(TAG, "JSON 内存分配失败 (%d 字节)", len);
        free(arr);
        return NULL;
    }

    int written = snprintf(text, (size_t)len + 1, fmt,
                           s_device_id, s_mac_str, (unsigned)s_boot_count,
                           (unsigned)last->seq, (unsigned)b[0].seq,
                           uptime_ms, ts_utc, ts_local,
                           APP_SAMPLE_PERIOD_MS, n,
                           info->name, (unsigned)info->i2c_addr, (unsigned)info->chip_id,
                           (unsigned)info->range_g, (unsigned)info->lsb_per_g,
                           (unsigned)info->odr_hz, APP_OVERSAMPLE,
                           arr,
                           last->raw.x, last->raw.y, last->raw.z,
                           (double)last->gx, (double)last->gy, (double)last->gz,
                           (double)last->ms2x, (double)last->ms2y, (double)last->ms2z,
                           (double)last->mag_g);
    free(arr);
    if (written != len) {
        ESP_LOGE(TAG, "JSON 组包长度异常 (%d != %d)", written, len);
        free(text);
        return NULL;
    }
    return text;
}

// ===========================================================================
// 主流程：采样任务 + 上传任务（解耦）
// ===========================================================================
/**
 * @brief 联网维护（只做"便宜"的事：状态机心跳 + 到点补对时）。
 *
 * 由采样任务每 100ms 调用一次，所以必须足够快。
 * 重扫热点这种"贵"操作放在上传任务的空闲分支里（见 uploader_task）。
 *
 *  - 到点自动重连（事件回调里不做阻塞等待）
 *  - 开机时没连上、后来连上了 → 补做 SNTP 对时
 */
static void service_network_fast(void)
{
    static int64_t last_sntp_try_us = 0;

    uploader_wifi_tick();

    if (!uploader_time_synced() && uploader_wifi_is_connected()) {
        int64_t now_us = esp_timer_get_time();
        if (now_us - last_sntp_try_us > 30LL * 1000000LL) {
            last_sntp_try_us = now_us;
            // 8 秒上限：采样任务会被它挡住，不能在这里耗太久
            uploader_sync_time(8000);
        }
    }
}

/** @brief 长时间连不上时定期重扫周边热点（放在上传任务里，别挡采样）。 */
static void service_network_slow(void)
{
    static int64_t last_scan_us = 0;
    if (uploader_wifi_is_connected()) return;

    int64_t now_us = esp_timer_get_time();
    if (last_scan_us == 0) last_scan_us = now_us;
    if (now_us - last_scan_us > 120LL * 1000000LL) {
        last_scan_us = now_us;
        uploader_scan_and_log();
    }
}

// ---- 采样池 / 队列：采集任务生产、上传任务消费 ----
static batch_packet_t   s_pool[APP_BATCH_QUEUE_LEN];
static QueueHandle_t    s_q_free  = NULL;   // 空闲槽位
static QueueHandle_t    s_q_ready = NULL;   // 已装满、等待上传的包
static uint32_t         s_dropped_pkts = 0; // 因缓冲满而丢弃的包数（如实计数，不补造）

/**
 * @brief 采样任务：严格按 APP_SAMPLE_PERIOD_MS 采点，攒够一包就交给上传任务。
 *
 * ★ 采集与上传解耦的意义：上传慢只影响"数据晚到"，不会把采样节奏拖成
 *   "包内 100ms、包间 1 秒空洞"。实测同步版本就是这个毛病（见 docs/05 第 9 节）。
 */
static void sampler_task(void *arg)
{
    (void)arg;
    batch_packet_t *pkt = NULL;
    int64_t         next_us = esp_timer_get_time() + (int64_t)APP_SAMPLE_PERIOD_MS * 1000;

    while (1) {
        // ---- 绝对时间表：按 100ms 的整数倍推进，杜绝累计漂移 ----
        int64_t wait_us = next_us - esp_timer_get_time();
        if (wait_us > 0) vTaskDelay(pdMS_TO_TICKS((wait_us + 500) / 1000));
        next_us += (int64_t)APP_SAMPLE_PERIOD_MS * 1000;
        // 若被长时间阻塞（例如刚做过 SNTP 对时），就重新对齐，不去"补采"一堆点
        if (esp_timer_get_time() > next_us + 3LL * APP_SAMPLE_PERIOD_MS * 1000) {
            next_us = esp_timer_get_time() + (int64_t)APP_SAMPLE_PERIOD_MS * 1000;
        }

        service_network_fast();

        // ---- ① 读取真实硬件测量值（多次采样平均，抑制噪声）----
        //   与远程采集的读互斥：两个任务并发读同一条 I2C 会偶发失败，
        //   而失败会被如实报成 SENSOR_ERROR，造成"假故障"。
        imu_raw_sample_t raw = { 0 };
        SemaphoreHandle_t lock = (SemaphoreHandle_t)cmd_client_imu_lock();
        if (lock) xSemaphoreTake(lock, portMAX_DELAY);
        esp_err_t err = imu_read_raw_averaged(&raw, APP_OVERSAMPLE, APP_OVERSAMPLE_US);
        if (lock) xSemaphoreGive(lock);
        if (err != ESP_OK) {
            // 读失败就如实报错并丢弃这个点，绝不用任何构造值顶替
            ESP_LOGE(TAG, "传感器读取失败(%s), 丢弃该采样点 (累计失败 %u 次)",
                     esp_err_to_name(err), (unsigned)imu_read_error_count());
            continue;
        }

        // ---- ①.5 暂停周期上报时：照常采点、照常推进 seq，但**不发包** ----
        //   ★ 语义要点：暂停的是"周期上报"，不是采集能力。远程 collect_now
        //     依然能读到当下寄存器（这正是当堂验证要证明的事）。
        //   这里仍要推进 seq：否则恢复上报后 seq 会与远程采集分配过的号码撞车。
        if (!s_periodic_enabled) {
            s_seq++;
            if (s_seq % 50 == 0) {          // 每 5 秒提示一次，避免刷屏
                ESP_LOGI(TAG, "周期上报已暂停（采点继续，seq 推进到 %u，不发包）",
                         (unsigned)s_seq);
            }
            continue;
        }

        // ---- ② 需要新包时取一个空闲槽位 ----
        if (pkt == NULL) {
            if (xQueueReceive(s_q_free, &pkt, 0) != pdTRUE) {
                // 空闲槽位用光 = 上传积压了。这里宁可丢弃"最早的待传包"，
                // 保证界面和数据库始终跟得上当前时刻（丢弃会如实打印，不补造）。
                batch_packet_t *victim = NULL;
                if (xQueueReceive(s_q_ready, &victim, 0) == pdTRUE) {
                    s_dropped_pkts++;
                    ESP_LOGW(TAG, "上传积压(%d 个包待传), 丢弃最早的包 seq=%u(%d 点), "
                                  "累计丢 %u 包 —— 丢弃原因是网络太慢, 不做假数据补发",
                             APP_BATCH_QUEUE_LEN, (unsigned)victim->seq_first, victim->n,
                             (unsigned)s_dropped_pkts);
                    pkt = victim;
                } else {
                    ESP_LOGE(TAG, "无空闲缓冲且无待传包(不应发生), 丢弃本采样点");
                    continue;
                }
            }
            // ★ 关键：拿到的槽位可能刚被上传任务归还（里面还是上一包的 10 个点），
            //   也可能就是刚丢弃的那个满包。必须清零 n，否则会写到数组外面去
            //   （上一版就踩了这个坑：Core 1 LoadProhibited 崩溃重启）。
            if (pkt->n != 0) {
                ESP_LOGD(TAG, "复用槽位, 清空上一包的 %d 个点", pkt->n);
                pkt->n = 0;
            }
            pkt->seq_first = 0;
            pkt->seq_last  = 0;
        }

        // ---- ③ 填充采样点（每个点带自己的 seq/idx/时间戳）----
        const uint32_t seq = ++s_seq;          // 共享计数器：远程采集也按它分配 seq
        batch_sample_t *b = &pkt->s[pkt->n];
        b->raw   = raw;
        b->gx    = imu_raw_to_g(raw.x);
        b->gy    = imu_raw_to_g(raw.y);
        b->gz    = imu_raw_to_g(raw.z);
        b->ms2x  = imu_raw_to_ms2(raw.x);
        b->ms2y  = imu_raw_to_ms2(raw.y);
        b->ms2z  = imu_raw_to_ms2(raw.z);
        b->mag_g = sqrtf(b->gx * b->gx + b->gy * b->gy + b->gz * b->gz);
        b->up_ms = (long long)(esp_timer_get_time() / 1000);
        b->seq   = seq;
        b->idx   = (uint16_t)(pkt->n + 1);
        // 时间戳：每个点只取一次，串口打印与上传报文共用同一个值
        b->ts_synced = sample_timestamp(b->ts_local, sizeof(b->ts_local), &b->utc_ms);
        if (pkt->n == 0) pkt->seq_first = seq;
        pkt->n++;
        pkt->seq_last = seq;

        // ---- ④ 串口打印真实实测值（三方核对的第一方证据）----
        ESP_LOGI(TAG,
                 "[DATA] dev=%s boot=%u seq=%u idx=%d/%d up=%lldms ts=%s "
                 "raw=(%d,%d,%d) g=(%.3f,%.3f,%.3f) ms2=(%.2f,%.2f,%.2f) |a|=%.3fg",
                 s_device_id, (unsigned)s_boot_count, (unsigned)seq,
                 (int)b->idx, APP_BATCH_SIZE, b->up_ms,
                 b->ts_synced ? b->ts_local : "未对时",
                 raw.x, raw.y, raw.z,
                 b->gx, b->gy, b->gz, b->ms2x, b->ms2y, b->ms2z, b->mag_g);

        // ---- ⑤ 攒满一包 → 交给上传任务（上传任务可能把几包合并成一个大包）----
        if (pkt->n >= APP_BATCH_SIZE) {
            if (xQueueSend(s_q_ready, &pkt, 0) != pdTRUE) {
                ESP_LOGE(TAG, "待传队列已满, 丢弃包 seq=%u..%u(%d 点)",
                         (unsigned)pkt->seq_first, (unsigned)pkt->seq_last, pkt->n);
                xQueueSend(s_q_free, &pkt, 0);
                s_dropped_pkts++;
            }
            pkt = NULL;
        }
    }
}

/**
 * @brief 上传任务：把排队的包（必要时几个包合并）POST 到服务器。
 *
 * 上传过程中采样任务仍在按 100ms 采点，所以网络抖动只会让数据"晚到"，
 * 不会造成采样空洞。
 */
static void uploader_task(void *arg)
{
    (void)arg;
    batch_sample_t merged[APP_MAX_MERGE];      // 合并后的采样点（栈上，见任务栈大小）
    batch_packet_t *carry = NULL;              // 装不下时暂存一包，留到下一轮
    char *json = NULL;

    while (1) {
        batch_packet_t *pkt = carry;
        carry = NULL;
        if (!pkt) {
            // 空闲等待时顺便处理"贵"的联网维护（重扫热点）
            if (xQueueReceive(s_q_ready, &pkt, pdMS_TO_TICKS(500)) != pdTRUE) {
                service_network_slow();
                continue;
            }
        }

        // ---- 把已经在排队的包也一起合并（网络慢时一次多传点，避免丢点）----
        int    n_total = 0, n_free = 0;
        uint32_t seq_first = pkt->seq_first, seq_last = pkt->seq_last;
        batch_packet_t *to_free[APP_BATCH_QUEUE_LEN];
        bool first = true;

        while (pkt) {
            for (int i = 0; i < pkt->n; i++) merged[n_total++] = pkt->s[i];
            if (first) { seq_first = pkt->seq_first; first = false; }
            seq_last = pkt->seq_last;
            to_free[n_free++] = pkt;
            if (n_total >= APP_MAX_MERGE) break;

            batch_packet_t *next = NULL;
            if (xQueueReceive(s_q_ready, &next, 0) != pdTRUE) break;
            if (n_total + next->n > APP_MAX_MERGE) { carry = next; break; }
            pkt = next;
        }

        // ---- 组包 + 上传（失败则如实计数并重试）----
        if (json) { free(json); json = NULL; }
        json = build_batch_json(merged, n_total);
        if (!json) {
            ESP_LOGE(TAG, "JSON 组包失败, 丢弃包 seq=%u..%u(%d 点)",
                     (unsigned)seq_first, (unsigned)seq_last, n_total);
        } else if (!uploader_wifi_is_connected()) {
            s_post_fail++;
            ESP_LOGW(TAG, "[POST] seq=%u..%u 未联网, 本包 %d 个点跳过上传",
                     (unsigned)seq_first, (unsigned)seq_last, n_total);
        } else {
            char resp[512] = { 0 };
            int  status = 0;
            esp_err_t perr = ESP_FAIL;
            for (int attempt = 1; attempt <= APP_HTTP_RETRY; attempt++) {
                perr = uploader_post_json(json, resp, sizeof(resp), &status);
                if (perr == ESP_OK) break;
                ESP_LOGW(TAG, "上传失败(第 %d/%d 次, http=%d), 1s 后重试",
                         attempt, APP_HTTP_RETRY, status);
                vTaskDelay(pdMS_TO_TICKS(1000));
            }
            if (perr == ESP_OK) {
                // 回显服务器保存后的内容，便于"串口/数据库/网页"三方核对
                ESP_LOGI(TAG, "[POST] seq=%u..%u n=%d http=%d resp=%s",
                         (unsigned)seq_first, (unsigned)seq_last, n_total, status, resp);
            } else {
                s_post_fail++;
                ESP_LOGE(TAG, "[POST] seq=%u..%u 上传失败(累计 %u 次), 本包 %d 个点已丢弃(不做假补发)",
                         (unsigned)seq_first, (unsigned)seq_last,
                         (unsigned)s_post_fail, n_total);
            }
        }

        // ---- 归还槽位（约定：回到空闲队列的槽位 n=0）----
        for (int i = 0; i < n_free; i++) {
            to_free[i]->n = 0;
            xQueueSend(s_q_free, &to_free[i], portMAX_DELAY);
        }
    }
}

void app_main(void)
{
    // ---- 时区：让"本地时间戳"真的是本地时间 ----
    setenv("TZ", APP_TZ, 1);
    tzset();

    // ---- 打印硬件与固件信息（第一个可核对的证据点） ----
    esp_chip_info_t chip;
    esp_chip_info(&chip);
    esp_reset_reason_t rr = esp_reset_reason();
    ESP_LOGI(TAG, "==================================================");
    ESP_LOGI(TAG, "ESP32-S3-EYE 真实物理传感器采集固件 v1.0.0");
    ESP_LOGI(TAG, "芯片: ESP32-S3, %d 核, 版本 v%d.%d, ESP-IDF %s",
             chip.cores, chip.revision / 100, chip.revision % 100, esp_get_idf_version());
    // 复位原因直接打印出来：区分"我按了复位/重新烧录"和"板子自己崩了/掉电了"
    ESP_LOGI(TAG, "本次复位原因: %s (代码 %d)", reset_reason_text(rr), (int)rr);
    ESP_LOGI(TAG, "==================================================");

    // ---- NVS（保存 boot 计数） ----
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    init_device_id();
    init_boot_count();

    // ---- 初始化真实物理传感器 ----
    err = imu_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "传感器初始化失败(%s)！本程序不会用任何假数据替代，"
                      "请检查硬件接线后重启。", esp_err_to_name(err));
        // 如实报错，并持续重试（不产生数据）
        while (1) {
            vTaskDelay(pdMS_TO_TICKS(10000));
            ESP_LOGE(TAG, "重试传感器初始化 ...");
            if (imu_init() == ESP_OK) {
                ESP_LOGI(TAG, "传感器恢复, 继续运行");
                break;
            }
        }
    }

    gravity_selfcheck();

    // 再报一次复位原因：串口(USB-Serial-JTAG)的发送缓冲很小，开机最初的几行
    // 常常在宿主机打开串口前就被冲掉了；放在自检之后打印能保证它出现在可见窗口里，
    // 排查"板子是不是在自己反复重启 / 是不是供电不足"时非常关键。
    ESP_LOGI(TAG, "【复位原因】%s (代码 %d)，本次为第 %u 次开机",
             reset_reason_text(esp_reset_reason()), (int)esp_reset_reason(),
             (unsigned)s_boot_count);

    // ---- 联网 ----
    err = uploader_wifi_connect();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "联网失败(%s)，无法上传；采集仍会继续并打印到串口。", esp_err_to_name(err));
        // 连不上时立刻扫描周边热点：把「找不到 AP(reason=201)」变成可核对的证据
        uploader_scan_and_log();
    } else {
        ESP_LOGI(TAG, "Wi-Fi 已连接, 本机 IP = %s", uploader_get_ip());
        uploader_sync_time(20000);
    }

    ESP_LOGI(TAG, "开始高密度采集: 每 %d ms 采一个点, 攒满 %d 点整包上传; "
                  "每个采样点由 %d 次硬件采样取平均后得到",
             APP_SAMPLE_PERIOD_MS, APP_BATCH_SIZE, APP_OVERSAMPLE);
    ESP_LOGI(TAG, "采集与上传已解耦: 采样任务严格 %dms 采点, 上传任务异步发送"
                  "(最多缓冲 %d 包≈%d 秒, 网络慢时自动合并上传, 单包最多 %d 点)",
             APP_SAMPLE_PERIOD_MS, APP_BATCH_QUEUE_LEN,
             APP_BATCH_QUEUE_LEN * APP_BATCH_SIZE * APP_SAMPLE_PERIOD_MS / 1000,
             APP_MAX_MERGE);

    // ---- 起两个任务：采集（高优先级、独立核）+ 上传（网络慢也不影响采样）----
    s_q_free  = xQueueCreate(APP_BATCH_QUEUE_LEN, sizeof(batch_packet_t *));
    s_q_ready = xQueueCreate(APP_BATCH_QUEUE_LEN, sizeof(batch_packet_t *));
    if (!s_q_free || !s_q_ready) {
        ESP_LOGE(TAG, "队列创建失败，无法运行");
        return;
    }
    for (int i = 0; i < APP_BATCH_QUEUE_LEN; i++) {
        batch_packet_t *p = &s_pool[i];
        p->n = 0;
        xQueueSend(s_q_free, &p, 0);
    }

    // 采样任务放 core 1（Wi-Fi 协议栈在 core 0），避免互相抢 CPU 造成采样抖动
    xTaskCreatePinnedToCore(sampler_task,  "sampler",  8192, NULL, 6, NULL, 1);
    xTaskCreatePinnedToCore(uploader_task, "uploader", 12288, NULL, 5, NULL, 0);
    ESP_LOGI(TAG, "采样任务与上传任务已启动");

    // ---- 第 2 周：启动远程采集指令客户端（1Hz 轮询，复用房第 1 周 HTTP 栈）----
    //   它与上面两个任务完全解耦：轮询慢/失败都不会影响 10Hz 采样。
    cmd_client_set_state_provider(cmd_state_provider);
    cmd_client_set_periodic_handler(cmd_periodic_handler);
    if (cmd_client_start() == ESP_OK) {
        ESP_LOGI(TAG, "远程采集指令客户端已启动（下行通道＝设备主动轮询，无 MQTT/WebSocket）");
    } else {
        // 起不来只影响远程采集，周期上报仍照常工作 —— 如实说明而不是假装成功
        ESP_LOGE(TAG, "远程采集指令客户端启动失败！周期上报继续，但网页点「采集最新数据」会超时");
    }

    // app_main 自身的使命到此为止（三个任务会一直跑下去）
    vTaskDelete(NULL);
}