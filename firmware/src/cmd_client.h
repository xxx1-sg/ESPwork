/**
 * @file cmd_client.h
 * @brief 第 2 周：远程采集指令客户端（轮询取指令 → 回执 → 采一次 → 回传观测）。
 *
 * 设计要点（对应 docs/06 §4 与 §4.6）：
 *   - **复用第 1 周的 HTTP 栈**：所有请求都走 uploader_post_json_url()，
 *     共用同一个 keep-alive 连接、同一套 TLS 校验、同一个 X-Device-Token。
 *     本模块不新建 socket、不做 TLS、不引入 MQTT/WebSocket。
 *   - **下行只有轮询一条通道**：设备主动 POST /api/v1/device/commands/poll
 *     取走指令；后端没有任何"直接通知设备"的端点。
 *   - **红线**：本次采集的数值只能来自 imu_read_raw_averaged() 的真实寄存器读；
 *     读失败就回执 rejected/SENSOR_ERROR，**绝不上传任何构造值或上次的值**。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 本模块需要的"设备当前状态"，由 main.c 提供（避免反向依赖 main 里的静态变量）。 */
typedef struct {
    const char *device_id;   /**< 设备唯一 ID（eFuse MAC 派生） */
    const char *mac_str;     /**< 完整 MAC 字符串，供页面显示 */
    uint32_t    boot_count;  /**< 第几次开机（重启后 seq 从 1 重新开始，必须带） */
    uint32_t    seq_last;    /**< 采样任务当前的全局序号（用户在上报时取快照） */
    bool        periodic;    /**< 周期上报当前是否使能（供后端核对暂停是否生效） */
} cmd_device_state_t;

/**
 * @brief 注册"设备状态读取回调"，供 cmd_client 在组包时取用实时值。
 *
 * ★ 必须回调而不是拷贝快照：seq 由采样任务以 10Hz 推进，
 *   若在启动时拷一份，之后每条报文都会带着过期的 seq，
 *   后端的 L2 新旧性判定会把新观测误判成旧值。
 */
typedef void (*cmd_state_fn_t)(cmd_device_state_t *out);
void cmd_client_set_state_provider(cmd_state_fn_t fn);

/**
 * @brief 注册"周期上报开关"回调：收到 pause_reports / resume_reports 时由 main 应用。
 *
 * ★ 为什么不直接在 cmd_client 里改一个变量：周期上报是由 sampler_task 执行的，
 *   开关必须落在采样任务真正读取的那份状态上，否则会出现
 *   "回执说暂停了、但周期包还在发"（页面显示暂停却仍在涨数据）。
 */
typedef void (*cmd_periodic_fn_t)(bool enable);
void cmd_client_set_periodic_handler(cmd_periodic_fn_t fn);

/**
 * @brief 启动指令客户端任务（1Hz 轮询，独立任务，不阻塞采样与上传）。
 */
esp_err_t cmd_client_start(void);

/** @brief 已发出的回执条数（如实统计，供串口核对）。 */
uint32_t cmd_client_ack_count(void);

/** @brief 已完成的远程采集次数。 */
uint32_t cmd_client_collect_count(void);

/**
 * @brief 取 I2C 读互斥锁句柄（采样任务在读传感器前后加锁）。
 *
 * ★ 为什么需要：远程采集要"当场读一次寄存器"，而采样任务正以 10Hz 读同一个
 *   I2C 设备。两个任务并发访问同一条 I2C 总线会互相打断读时序，
 *   表现为偶发的读取失败（而失败会被如实报成 SENSOR_ERROR，造成假故障）。
 *   cmd_client_start() 之后该锁才非空。
 */
void *cmd_client_imu_lock(void);

#ifdef __cplusplus
}
#endif