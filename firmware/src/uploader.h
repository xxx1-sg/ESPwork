/**
 * @file uploader.h
 * @brief 联网与上传：开发板独立通过 Wi-Fi 把真实传感数据 POST 到后端。
 *
 * 重要: 本模块只负责「把数据送到后端」，不参与任何数据生成。
 *      USB 串口仅用于烧录与打印排错，绝不是上传通道。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * @brief 以 STA 模式连接 Wi-Fi（阻塞直到拿到 IP 或超过最大重试次数）。
 * @return ESP_OK 已联网；其它为失败原因。
 */
esp_err_t uploader_wifi_connect(void);

/** @brief 当前是否已联网并持有 IP。 */
bool uploader_wifi_is_connected(void);

/**
 * @brief 联网状态机的心跳：由主循环周期性调用（建议每秒一次）。
 *
 * 断线重连的延时等待放在这里完成，而不是在 Wi-Fi 事件回调里 vTaskDelay ——
 * 事件回调阻塞会拖慢整个事件任务，这里交给主循环更安全。
 */
void uploader_wifi_tick(void);

/** @brief 取本机 IP 字符串（未联网返回 "0.0.0.0"）。 */
const char *uploader_get_ip(void);

/**
 * @brief 通过 SNTP 同步网络时间（给每条数据打真实 UTC 时间戳）。
 * @param timeout_ms 等待同步的最长时间
 * @return ESP_OK 已同步；ESP_ERR_TIMEOUT 超时（此时上传仍会继续，但 UTC 字段为 null）。
 */
esp_err_t uploader_sync_time(int timeout_ms);

/** @brief 系统时间是否已通过 SNTP 同步。 */
bool uploader_time_synced(void);

/**
 * @brief 扫描周边 2.4GHz Wi-Fi 热点并逐条打印到串口（排错用）。
 *
 * 为什么需要它：连不上热点时驱动只会给出 reason=201(找不到 AP)，
 * 无法区分「热点没开」「热点只在 5GHz」「SSID 不一致」「信号太弱」。
 * 扫描结果把这些可能性变成可核对的证据：扫不到目标 SSID 就说明板子这边
 * 确实收不到该热点（ESP32-S3 只支持 2.4GHz）。
 *
 * 注意：扫描期间会短暂中断当前连接尝试，属于正常现象。
 */
void uploader_scan_and_log(void);

/**
 * @brief 把一段 JSON 文本 POST 到 APP_SERVER_URL。
 *
 * @param[in]  json      要上传的报文（UTF-8，以 '\0' 结尾）
 * @param[out] resp      服务器响应正文缓冲（可为 NULL）
 * @param[in]  resp_len  resp 缓冲区长度
 * @param[out] http_status 服务器返回的 HTTP 状态码（可为 NULL）
 * @return ESP_OK 表示 HTTP 2xx；其它为网络错误或非 2xx。
 */
esp_err_t uploader_post_json(const char *json, char *resp, size_t resp_len, int *http_status);

#ifdef __cplusplus
}
#endif