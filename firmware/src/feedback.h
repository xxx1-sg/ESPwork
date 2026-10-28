/**
 * @file feedback.h
 * @brief 第 3 周：按键求助 + 本地物理反馈（按键消抖 / 蜂鸣 / LED / 求助上传）。
 *
 * 设计要点（对应 docs/06 §7）：
 *
 *   ★★ 最重要的一条：**本地反馈先发生，上传后尝试**。
 *      按下的瞬间就把蜂鸣与 LED 打出来（不依赖任何网络操作），然后再去排队上传。
 *      这样"拔掉外网"时按键依然有实体反馈 —— 这是当堂验证第 1 项。
 *
 *   ★★ 第二条：**无远端回执不得声称已收到**。
 *      本模块的 LED 只有收到后端下发的 `help_ack` 指令才会转"已送达"色，
 *      绝不用本地计时器推断"对方已经知道了"。本地只知道自己按下了。
 *
 *   ★ 时间口径（docs/06 §7.1）：trigger_up_ms 用 esp_timer 单调时钟，
 *     且**一旦记录就不再改写**；墙上时钟字段在未对时时一律为 null，
 *     绝不用服务端时间或"恢复联网的时刻"顶替。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 本地反馈的状态机（LED/蜂鸣的表现就由它决定）。 */
typedef enum {
    FB_IDLE = 0,      /**< 待机：灯灭、不响 */
    FB_WAITING,       /**< 已按下、等待远端回应：橙色慢闪 + 周期性短鸣 */
    FB_DELIVERED,     /**< 设备已确认收到「查看者回应」：绿色常亮、蜂鸣停 */
} fb_state_t;

/** 本模块需要的设备身份（由 main.c 注入，避免反向依赖 main 里的静态变量）。 */
typedef struct {
    const char *device_id;   /**< 设备唯一 ID（eFuse MAC 派生） */
    uint32_t    boot_count;  /**< 第几次开机（幂等键与补传都要带） */
} fb_device_state_t;

/** 设备身份回调（与 cmd_client 的 state provider 同一套路：拉取而不是推送）。 */
typedef void (*fb_state_fn_t)(fb_device_state_t *out);
void feedback_set_state_provider(fb_state_fn_t fn);

/**
 * @brief 启动按键任务与求助上传任务（两个独立任务，互不阻塞）。
 *
 * ★ 必须在 app_main 里、**不依赖 Wi-Fi 成功**地调用 ——
 *   断网时本地识别与反馈仍要工作。
 */
esp_err_t feedback_start(void);

/** @brief 模拟/程序化触发一次求助（与实体按键走完全相同的路径）。 */
void feedback_trigger(const char *trigger_type);

/* ---- 供 cmd_client 在收到下行指令后调用（这两个是"远端事实"的落点）---- */

/** @brief 收到 help_ack 指令：设备确认已收到「查看者回应」→ LED 转绿。 */
void feedback_on_help_ack(const char *help_id);

/** @brief 收到 clear_alert 指令：停蜂鸣、LED 回待机色。 */
void feedback_on_clear_alert(const char *help_id);

/* ---- 如实统计（供串口核对，不做任何美化）---- */
uint32_t feedback_trigger_count(void);   /**< 累计识别到的按键次数 */
uint32_t feedback_upload_count(void);    /**< 累计成功上传的求助次数 */
uint32_t feedback_pending_count(void);   /**< 当前本地待补传条数 */
uint32_t feedback_dropped_count(void);   /**< 因队列满而丢弃的条数（如实统计） */
bool     feedback_is_offline_pending(void);

/** @brief 当前本地反馈状态（供串口打印与核对）。 */
fb_state_t feedback_state(void);

/** @brief 当前激活的 help_id（无则 NULL）——用于核对"哪一次求助正在等待"。 */
const char *feedback_active_help_id(void);

#ifdef __cplusplus
}
#endif