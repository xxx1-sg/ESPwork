/**
 * @file uploader.c
 * @brief Wi-Fi 联网 + SNTP 对时 + HTTP(S) POST 上传实现。
 */

#include "uploader.h"

#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"

#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "esp_timer.h"
#include "nvs_flash.h"

#include "app_config.h"

#if APP_TLS_TRUST_MODE == 1
#include "server_ca_pem.h"     // 由 tools/gen_ca_header.py 生成的真实 CA 链
#endif

static const char *TAG = "uploader";

/** @brief 释放复用的 HTTP 客户端句柄（断网/失败时调用）—— 定义在文件后半部分。 */
static void client_drop(void);

/**
 * @brief 真正执行一次 POST（调用者必须已持有 s_http_lock）—— 定义在文件后半部分。
 */
static esp_err_t uploader_post_json_locked(const char *url, const char *json,
                                           char *resp, size_t resp_len, int *http_status);

// ---- 事件组：用于等待「连上 AP」与「拿到 IP」 ----
#define WIFI_CONNECTED_BIT  BIT0
#define WIFI_GOT_IP_BIT     BIT1

static EventGroupHandle_t s_wifi_events = NULL;
static esp_netif_t       *s_netif       = NULL;
static volatile bool      s_connected   = false;
static char               s_ip_str[16]  = "0.0.0.0";
static volatile bool      s_time_synced = false;

// 重连由主循环驱动（见 uploader_wifi_tick）：
// 事件回调里绝不做 vTaskDelay —— 否则会阻塞 Wi-Fi 事件任务，拖慢其它事件处理。
static volatile bool    s_need_reconnect  = false;
static volatile int64_t s_reconnect_at_us = 0;
static volatile bool    s_scanning        = false;

// ---- 候选网络列表：主用连不上会自动切到备用（现场多网络环境更稳） ----
typedef struct {
    const char *ssid;
    const char *password;
} wifi_cred_t;

static const wifi_cred_t s_creds[] = {
    { APP_WIFI_SSID,          APP_WIFI_PASSWORD          },
    { APP_WIFI_FALLBACK_SSID, APP_WIFI_FALLBACK_PASSWORD },
};
#define WIFI_CRED_COUNT (sizeof(s_creds) / sizeof(s_creds[0]))

static volatile int     s_cred_idx            = 0;   /**< 当前使用的候选网络下标 */
static volatile int64_t s_disconnected_since  = 0;   /**< 本次连续离线起始时刻(0=在线) */

/** @brief 把第 idx 个候选网络的账号写进 Wi-Fi 驱动配置。 */
static esp_err_t apply_cred(int idx)
{
    if (idx < 0 || idx >= (int)WIFI_CRED_COUNT) return ESP_ERR_INVALID_ARG;

    wifi_config_t wifi_cfg = { 0 };
    strncpy((char *)wifi_cfg.sta.ssid, s_creds[idx].ssid,
            sizeof(wifi_cfg.sta.ssid) - 1);
    strncpy((char *)wifi_cfg.sta.password, s_creds[idx].password,
            sizeof(wifi_cfg.sta.password) - 1);
    wifi_cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;  // 只做最低强度过滤
    wifi_cfg.sta.pmf_cfg.capable    = true;
    wifi_cfg.sta.pmf_cfg.required   = false;

    esp_err_t err = esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg);
    if (err == ESP_OK) {
        s_cred_idx = idx;
        ESP_LOGI(TAG, "使用网络[%d/%d]: SSID=\"%s\"", idx + 1, (int)WIFI_CRED_COUNT,
                 s_creds[idx].ssid);
    } else {
        ESP_LOGE(TAG, "写入 Wi-Fi 配置失败: %s", esp_err_to_name(err));
    }
    return err;
}

// ===========================================================================
// Wi-Fi 事件处理
// ===========================================================================
static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT) {
        switch (id) {
        case WIFI_EVENT_STA_START:
            ESP_LOGI(TAG, "Wi-Fi 启动, 开始连接 SSID=\"%s\"", s_creds[s_cred_idx].ssid);
            esp_wifi_connect();
            break;

        case WIFI_EVENT_STA_CONNECTED:
            ESP_LOGI(TAG, "已关联到 AP, 等待 DHCP 分配 IP ...");
            xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
            break;

        case WIFI_EVENT_STA_DISCONNECTED: {
            wifi_event_sta_disconnected_t *d = (wifi_event_sta_disconnected_t *)data;
            s_connected = false;
            client_drop();          // 底层 socket 已失效，把复用的连接句柄一起丢掉
            xEventGroupClearBits(s_wifi_events, WIFI_CONNECTED_BIT | WIFI_GOT_IP_BIT);
            if (s_disconnected_since == 0) s_disconnected_since = esp_timer_get_time();
            // reason: 15=密码错误 201=AP 未找到 等，直接打印出来便于定位
            ESP_LOGW(TAG, "Wi-Fi 断开 (reason=%d, SSID=\"%s\"), %d ms 后重连",
                     d ? d->reason : -1, s_creds[s_cred_idx].ssid, APP_WIFI_RETRY_MS);
            // 只登记「待重连」，真正的重连动作交给主循环（不阻塞事件任务）
            if (!s_scanning) {
                s_need_reconnect  = true;
                s_reconnect_at_us = esp_timer_get_time() + (int64_t)APP_WIFI_RETRY_MS * 1000;
            }
            break;
        }
        default:
            break;
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        snprintf(s_ip_str, sizeof(s_ip_str), IPSTR, IP2STR(&e->ip_info.ip));
        s_connected          = true;
        s_need_reconnect     = false;
        s_disconnected_since = 0;
        xEventGroupSetBits(s_wifi_events, WIFI_GOT_IP_BIT);
        ESP_LOGI(TAG, "获取 IP 成功: %s (网关 " IPSTR ", SSID=\"%s\")", s_ip_str,
                 IP2STR(&e->ip_info.gw), s_creds[s_cred_idx].ssid);
    }
}

// ===========================================================================
// 公共接口
// ===========================================================================
esp_err_t uploader_wifi_connect(void)
{
    s_wifi_events = xEventGroupCreate();
    if (!s_wifi_events) return ESP_ERR_NO_MEM;

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    s_netif = esp_netif_create_default_wifi_sta();

    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        &wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        &wifi_event_handler, NULL, NULL));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(apply_cred(0));                 // 先用主用网络
    ESP_ERROR_CHECK(esp_wifi_start());

    // 降低射频峰值电流：供电偏弱的 USB 口在 Wi-Fi 启动瞬间容易 brownout 复位，
    // 调小最大发射功率是最有效的软件侧缓解手段（详见 app_config.h 的说明）。
    int8_t    tx_qdbm = APP_WIFI_MAX_TX_POWER_QDBM;
    esp_err_t perr    = esp_wifi_set_max_tx_power(tx_qdbm);
    if (perr == ESP_OK) {
        ESP_LOGI(TAG, "Wi-Fi 最大发射功率 = %.2f dBm（降低峰值电流，缓解供电不足导致的复位）",
                 tx_qdbm * 0.25);
    } else {
        ESP_LOGW(TAG, "设置最大发射功率失败: %s", esp_err_to_name(perr));
    }

    // 等待拿到 IP：最多等 APP_WIFI_CONNECT_TIMEOUT_MS，
    // 超时不阻塞主流程（采集照常进行），事件回调会继续在后台自动重连。
    ESP_LOGI(TAG, "等待联网 (最长 %d ms) ...", APP_WIFI_CONNECT_TIMEOUT_MS);
    EventBits_t bits = xEventGroupWaitBits(s_wifi_events,
                                           WIFI_GOT_IP_BIT,
                                           pdFALSE, pdTRUE,
                                           pdMS_TO_TICKS(APP_WIFI_CONNECT_TIMEOUT_MS));
    if (bits & WIFI_GOT_IP_BIT) {
        return ESP_OK;
    }
    ESP_LOGW(TAG, "联网超时：请检查 SSID/密码是否正确、热点是否为 2.4GHz、信号是否覆盖。"
                  "采集会继续，联网后会自动开始上传。");
    return ESP_ERR_TIMEOUT;
}

bool uploader_wifi_is_connected(void) { return s_connected; }
const char *uploader_get_ip(void)     { return s_ip_str; }

void uploader_wifi_tick(void)
{
    if (s_connected || s_scanning) return;

    int64_t now = esp_timer_get_time();

    // 连续离线超过阈值 -> 轮换到下一个候选网络（多网络现场自动切换）
    if (s_disconnected_since != 0 &&
        now - s_disconnected_since > (int64_t)APP_WIFI_SWITCH_AFTER_MS * 1000) {
        int next = (s_cred_idx + 1) % (int)WIFI_CRED_COUNT;
        ESP_LOGW(TAG, "已连续离线 %d 秒, 切换到候选网络[%d/%d]",
                 APP_WIFI_SWITCH_AFTER_MS / 1000, next + 1, (int)WIFI_CRED_COUNT);
        if (apply_cred(next) == ESP_OK) {
            s_disconnected_since = now;      // 重新计时
            s_need_reconnect     = true;
            s_reconnect_at_us    = now;      // 立即尝试
        }
    }

    if (!s_need_reconnect) return;
    if (now < s_reconnect_at_us) return;
    s_need_reconnect = false;
    esp_wifi_connect();
}

esp_err_t uploader_sync_time(int timeout_ms)
{
    // SNTP 客户端只需要初始化一次；之后可以反复调用 sync_wait 等待同步完成
    // （开机时没联网、后来又连上时，主循环会再次调用本函数补对时）
    static bool s_sntp_inited = false;
    esp_err_t err;

    if (!s_sntp_inited) {
        esp_sntp_config_t cfg  = ESP_NETIF_SNTP_DEFAULT_CONFIG("ntp.aliyun.com");
        esp_sntp_config_t cfg2 = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
        cfg.start          = true;                 // 初始化后就自动开始同步
        cfg.num_of_servers = 2;
        cfg.servers[1]     = cfg2.servers[0];      // 备用 NTP 服务器

        err = esp_netif_sntp_init(&cfg);
        if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
            ESP_LOGW(TAG, "SNTP 初始化失败: %s", esp_err_to_name(err));
            return err;
        }
        s_sntp_inited = true;
    }

    err = esp_netif_sntp_sync_wait(pdMS_TO_TICKS(timeout_ms));
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "SNTP 对时超时(%d ms)，本次上传的 UTC 时间字段将为空", timeout_ms);
        return err;
    }

    time_t now = 0;
    time(&now);
    struct tm tmv;
    localtime_r(&now, &tmv);
    char buf[64];
    strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S %z", &tmv);
    ESP_LOGI(TAG, "SNTP 对时成功, 设备本地时间: %s", buf);
    s_time_synced = true;
    return ESP_OK;
}

bool uploader_time_synced(void) { return s_time_synced; }

void uploader_scan_and_log(void)
{
    ESP_LOGI(TAG, "开始扫描周边 2.4GHz 热点（用于定位连不上的原因）...");

    // 扫描必须在「不在连接中」的状态下发起，否则驱动直接返回 ESP_ERR_WIFI_STATE。
    // 所以先主动断开并暂停自动重连，扫完再立刻恢复重连。
    s_scanning       = true;
    s_need_reconnect = false;
    esp_wifi_disconnect();               // 未连接时返回 ESP_ERR_WIFI_NOT_CONNECT，忽略
    vTaskDelay(pdMS_TO_TICKS(200));

    wifi_scan_config_t scfg = {
        .ssid        = NULL,
        .bssid       = NULL,
        .channel     = 0,          // 0 = 全信道扫描
        .show_hidden = true,
    };

    esp_err_t err = esp_wifi_scan_start(&scfg, true /* 阻塞直到扫描完成 */);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "扫描启动失败: %s", esp_err_to_name(err));
        goto done;
    }

    uint16_t ap_num = 0;
    esp_wifi_scan_get_ap_num(&ap_num);
    if (ap_num == 0) {
        ESP_LOGW(TAG, "扫描结果: 一个 2.4GHz 热点都没有扫到。"
                      "请确认手机热点已开启且频段为 2.4GHz（ESP32-S3 不支持 5GHz）。");
        goto done;
    }

    if (ap_num > 20) ap_num = 20;                 // 最多打印 20 条，避免日志过长
    wifi_ap_record_t *recs = calloc(ap_num, sizeof(wifi_ap_record_t));
    if (!recs) {
        ESP_LOGW(TAG, "扫描结果内存不足");
        goto done;
    }

    uint16_t got = ap_num;
    if (esp_wifi_scan_get_ap_records(&got, recs) != ESP_OK) {
        ESP_LOGW(TAG, "读取扫描结果失败");
        free(recs);
        goto done;
    }

    ESP_LOGI(TAG, "扫描到 %u 个热点:", (unsigned)got);
    bool found[WIFI_CRED_COUNT];
    for (int c = 0; c < (int)WIFI_CRED_COUNT; c++) found[c] = false;

    for (int i = 0; i < got; i++) {
        char ssid[33];                            // 驱动里 ssid 不保证以 '\0' 结尾
        memcpy(ssid, recs[i].ssid, 32);
        ssid[32] = '\0';
        for (int c = 0; c < (int)WIFI_CRED_COUNT; c++) {
            if (strcmp(ssid, s_creds[c].ssid) == 0) found[c] = true;
        }
        ESP_LOGI(TAG, "  [%2d] SSID=\"%s\" RSSI=%d dBm 信道=%u 加密方式=%d",
                 i + 1, ssid, recs[i].rssi, (unsigned)recs[i].primary,
                 (int)recs[i].authmode);
    }
    free(recs);

    for (int c = 0; c < (int)WIFI_CRED_COUNT; c++) {
        if (found[c]) {
            ESP_LOGI(TAG, "候选网络[%d] \"%s\" 在扫描结果中 -> 板子能收到，"
                          "连不上的话大概率是密码/加密方式问题。", c + 1, s_creds[c].ssid);
        } else {
            ESP_LOGW(TAG, "候选网络[%d] \"%s\" 不在扫描结果中 -> 板子收不到该热点。"
                          "请检查：① 热点是否开着；② 是否被设为 5GHz（ESP32-S3 只支持 2.4GHz）；"
                          "③ 距离是否过远。", c + 1, s_creds[c].ssid);
        }
    }

done:
    s_scanning        = false;
    s_need_reconnect  = true;
    s_reconnect_at_us = esp_timer_get_time();     // 立刻恢复重连
}

/**
 * @brief 复用的 HTTP(S) 客户端句柄（连接保活）。
 *
 * ★ 为什么要复用：每次上传都新建连接的话，HTTPS 要重新做一次 TLS 握手
 *   （实测约 3.5 秒：TCP + TLS + 证书校验），于是"每 1 秒上传一次"变成
 *   每 4~5 秒才上传一次，采样密度远低于设计值，而且握手消耗的射频/CPU 电流
 *   还会加重供电负担（本机 USB 口就出现过 brownout 复位）。
 *   开启 keep-alive 并复用同一个 client 后，后续上传只发数据、不再握手。
 *
 * 连接失效（超时/对端关闭/换网）时句柄会被重建，逻辑见 uploader_post_json()。
 */
static esp_http_client_handle_t s_client = NULL;

/**
 * @brief 保护 s_client 的互斥锁（★ 第 2 周新增，必须要有）。
 *
 * 为什么必须有：第 2 周多了一个任务（cmd_poll）也会发 HTTP 请求，它与
 * uploader_task 共用同一个 keep-alive 句柄。两个任务同时在一个句柄上
 * 做 open/write/read 会互相穿插 —— 实测现象是**响应张冠李戴**：
 *   cmd 任务拿到的其实是 /ingest 的响应体，于是"指令解析失败"疯狂刷屏，
 *   而真正的 poll 响应被 uploader 当成自己的回执打印出来。
 * 加锁后每个请求串行完成，连接照旧复用（TLS 握手只做一次）。
 *
 * 注意：这里只锁"一次请求"，不锁整个网络状态，所以不会拖慢 10Hz 采样
 * （采样任务不经过这里）。
 */
static SemaphoreHandle_t s_http_lock = NULL;

static void http_lock_init(void)
{
    if (!s_http_lock) {
        s_http_lock = xSemaphoreCreateMutex();
    }
}

/** @brief 释放复用的客户端句柄（换网、长时间失败后调用）。 */
static void client_drop(void)
{
    if (s_client) {
        esp_http_client_close(s_client);
        esp_http_client_cleanup(s_client);
        s_client = NULL;
    }
}

esp_err_t uploader_post_json(const char *json, char *resp, size_t resp_len, int *http_status)
{
    // 保持第 1 周的签名与语义不变（老调用方一行不用改），内部转调通用实现
    return uploader_post_json_url(APP_SERVER_URL, json, resp, resp_len, http_status);
}

esp_err_t uploader_post_json_url(const char *url, const char *json,
                                 char *resp, size_t resp_len, int *http_status)
{
    // ★ 串行化：多个任务共用一个 keep-alive 句柄时必须排队，否则响应会错位
    http_lock_init();
    if (s_http_lock) xSemaphoreTake(s_http_lock, portMAX_DELAY);
    esp_err_t ret = uploader_post_json_locked(url, json, resp, resp_len, http_status);
    if (s_http_lock) xSemaphoreGive(s_http_lock);
    return ret;
}

static esp_err_t uploader_post_json_locked(const char *url, const char *json,
                                           char *resp, size_t resp_len, int *http_status)
{
    if (http_status) *http_status = 0;

    // 根据 URL 协议决定是否启用 TLS 证书校验（三种信任方式见 app_config.h）
    bool is_https = (url && strncmp(url, "https://", 8) == 0);

    const char *ca_pem = NULL;    // 自定义 CA 链（方式 1）
    void       *bundle = NULL;    // 内置根证书包（方式 0）

    if (!s_client) {
#if APP_TLS_TRUST_MODE == 0
        if (is_https) bundle = (void *)esp_crt_bundle_attach;
        ESP_LOGI(TAG, "HTTPS 校验方式: ESP-IDF 内置根证书包");
#elif APP_TLS_TRUST_MODE == 1
        if (is_https) ca_pem = SERVER_CA_PEM;
        ESP_LOGI(TAG, "HTTPS 校验方式: 工程内嵌 CA 链（%u 字节）", (unsigned)strlen(SERVER_CA_PEM));
#else
        ESP_LOGW(TAG, "APP_TLS_TRUST_MODE=2：HTTPS 不校验证书，仅限本机排错，生产严禁！");
#endif

        esp_http_client_config_t cfg = {
            .url               = url,
            .method            = HTTP_METHOD_POST,
            .timeout_ms        = APP_HTTP_TIMEOUT_MS,
            .keep_alive_enable = true,          // 复用连接，避免每次重做 TLS 握手
            .cert_pem          = ca_pem,
            // crt_bundle_attach 非空 -> 用内置根证书包校验；
            // 为空且 cert_pem 也为空 -> 不校验对端证书（仅当 APP_TLS_TRUST_MODE=2）
            .crt_bundle_attach = bundle,
        };

        s_client = esp_http_client_init(&cfg);
        if (!s_client) return ESP_FAIL;

        esp_http_client_set_header(s_client, "Content-Type", "application/json");
        esp_http_client_set_header(s_client, "X-Device-Token", APP_DEVICE_TOKEN);
        esp_http_client_set_header(s_client, "User-Agent", "esp32s3eye-imu/1.0");
        ESP_LOGI(TAG, "已建立可复用连接 (keep-alive)");
    }

    esp_http_client_handle_t client = s_client;

    // ★ 同一句柄复用时，URL 必须每次重新设置：采集链路上会在
    //   /ingest、/device/commands/poll、/device/ack、/observations 之间来回切换，
    //   不重设就会把回执发到上一个端点去（表现为"指令收到了但后端说没回执"）。
    if (url) {
        esp_http_client_set_url(client, url);
    }

    esp_err_t err = esp_http_client_open(client, strlen(json));
    if (err != ESP_OK) {
        // 连接可能已被对端关闭（隧道/网关会定期断开空闲连接）：丢掉句柄，
        // 本次如实返回失败，下一次上传会自动重建连接。
        ESP_LOGW(TAG, "HTTP 连接失败: %s（将重建连接）", esp_err_to_name(err));
        client_drop();
        return err;
    }

    int written = esp_http_client_write(client, json, strlen(json));
    if (written != (int)strlen(json)) {
        ESP_LOGW(TAG, "HTTP 发送不完整: %d/%d（将重建连接）", written, (int)strlen(json));
        client_drop();
        return ESP_FAIL;
    }

    int content_len = esp_http_client_fetch_headers(client);
    int status      = esp_http_client_get_status_code(client);
    if (http_status) *http_status = status;

    int total_read = 0;
    if (resp && resp_len > 0) {
        int n = 0;
        if (content_len > 0) {
            n = esp_http_client_read_response(client, resp, resp_len - 1);
        }
        resp[n > 0 ? n : 0] = '\0';
        total_read = (n > 0) ? n : 0;
    }

    // ★ 必须把响应体读干净。resp 缓冲区只有 512 字节，而服务器回显的 JSON 更长；
    //   如果留着没读完的字节，复用同一条连接时下一次请求就会错位，
    //   表现为 "服务器返回非 2xx: -1"（实测就是这样：一次成功一次失败交替出现）。
    if (content_len > 0 && total_read < content_len) {
        char scratch[128];
        while (total_read < content_len) {
            int r = esp_http_client_read(client, scratch, sizeof(scratch));
            if (r <= 0) break;
            total_read += r;
        }
        ESP_LOGD(TAG, "响应体共 %d 字节，已全部读走（缓冲区保留前 %d 字节）",
                 content_len, (int)(resp_len - 1));
    }

    // 注意：这里不 close/cleanup —— 句柄保留下来给下一次上传复用；
    // esp_http_client 在读完响应后会按 keep-alive 语义把连接放回可用状态。

    if (status < 200 || status >= 300) {
        ESP_LOGW(TAG, "服务器返回非 2xx: %d", status);
        return ESP_ERR_INVALID_RESPONSE;
    }
    return ESP_OK;
}