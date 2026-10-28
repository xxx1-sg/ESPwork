/**
 * @file lcd_status.h
 * @brief 第 3 周（补充）：把求助状态画到板载 LCD 上（ST7789 / 240x240 / SPI3）。
 *
 * 为什么单独一个模块：
 *   - 按键/LED/蜂鸣那条"必须断网可用"的路径（feedback.c）不能被屏幕拖累 ——
 *     屏幕初始化失败时本模块**整体降级为惰性空实现**，LED/蜂鸣/上传照常工作；
 *   - 屏幕是"第三路本地输出"，只表达状态，不参与任何"远端事实"的判定。
 *
 * ★ 红线不变：屏幕显示"已送达"的唯一来源仍是 feedback_on_help_ack()
 *   （也就是 cmd_client 收到 help_ack 下行指令那一条路径）。屏幕不做任何
 *   本地计时器推断 —— 它只是把同一份状态换个介质显示出来。
 *
 * 硬件事实（全部来自 Espressif 官方 BSP，不臆测）：
 *   SCLK=GPIO21  MOSI=GPIO47  CS=GPIO44  DC=GPIO43  RST=未连接  背光=GPIO48
 *   ★ 背光**低电平点亮**（官方 BSP 里 brightness=100% 对应 LEDC duty=0），
 *     这是"屏不亮"最常见的坑：直接把 GPIO48 拉高反而一直是黑的。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 屏幕要表达的画面（与 fb_state_t 一一对应，多两个补充场景）。 */
typedef enum {
    LCD_SCENE_OFF = 0,     /**< 屏幕未启用/不可用 */
    LCD_SCENE_BOOT,        /**< 刚上电：自检画面 */
    LCD_SCENE_IDLE,        /**< 待机：灯灭、不响，屏也是暗底 */
    LCD_SCENE_WAITING,     /**< 已按键、等待远端回应（琥珀色 + 感叹号） */
    LCD_SCENE_DELIVERED,   /**< 设备已确认收到「查看者回应」（绿色 + 对勾） */
    LCD_SCENE_CANCELLED,   /**< 求助被取消（灰色 + 叉） */
} lcd_scene_t;

/**
 * @brief 初始化 LCD（SPI3 + esp_lcd ST7789 + 背光）。
 *
 * ★ **绝不致命**：任何一步失败都只打日志并把模块置为不可用，返回 ESP_OK。
 *   调用方（feedback_start）因此不需要处理错误，也不会因为屏坏了影响求助链路。
 */
esp_err_t lcd_status_init(void);

/** @brief 屏幕是否真的可用（初始化成功且面板已点亮）。 */
bool lcd_status_ready(void);

/** @brief 请求切换画面（非阻塞：只记录目标，由渲染任务真正刷屏）。 */
void lcd_status_set_scene(lcd_scene_t scene);

/**
 * @brief 显示一个画面 hold_ms 毫秒后**自动回待机**（用于"取消"这类瞬时提示）。
 *
 * 为什么要这个：板端状态机只有 IDLE/WAITING/DELIVERED 三态，"取消"是查看者侧动作
 * （收到 clear_alert → 板端回 IDLE）。但直接回 IDLE 会让用户看不到"这次被取消了"，
 * 于是这里让屏幕先亮灰叉 2 秒再回待机 —— 与 LED 熄灭、蜂鸣停止在时序上一致。
 */
void lcd_status_set_scene_timed(lcd_scene_t scene, uint32_t hold_ms);

/** @brief 当前目标画面（供串口核对）。 */
lcd_scene_t lcd_status_scene(void);

/** @brief 背光开/关（关掉后屏幕内容仍在，只是看不见）。 */
void lcd_status_backlight(bool on);

/** @brief 渲染统计：真正刷过多少次屏（用于核对"屏确实被驱动过"）。 */
uint32_t lcd_status_paint_count(void);

#ifdef __cplusplus
}
#endif
