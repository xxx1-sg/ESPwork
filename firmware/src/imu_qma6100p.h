/**
 * @file imu_qma6100p.h
 * @brief 板载 QMA6100P 三轴加速度计驱动接口（真实物理传感器）。
 *
 * 硬件事实（已在本机实体开发板上实测确认）：
 *   - 芯片     : QMA6100P（上海矽睿 QST 三轴加速度计）
 *   - I2C 地址 : 0x12 (7bit)
 *   - 接线     : SDA = GPIO4, SCL = GPIO5（ESP32-S3-EYE 板载 I2C 总线）
 *   - CHIP_ID  : 寄存器 0x00 读回 0x90
 *
 * 本驱动只做一件事：把芯片寄存器里的原始物理测量值读出来。
 * 不产生、不猜测、不模拟任何数值。
 */
#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** 一帧三轴原始数据（芯片内部 14 位有符号 ADC 码值，已右移对齐） */
typedef struct {
    int16_t x;      /**< X 轴原始码值（有符号，LSB） */
    int16_t y;      /**< Y 轴原始码值（有符号，LSB） */
    int16_t z;      /**< Z 轴原始码值（有符号，LSB） */
} imu_raw_sample_t;

/** 传感器静态信息（用于写进每一条数据包，便于后端/网页解释数值单位） */
typedef struct {
    const char *name;       /**< 芯片型号字符串 */
    uint8_t     i2c_addr;   /**< 7bit I2C 地址 */
    uint8_t     chip_id;    /**< 读回的 CHIP_ID */
    uint8_t     range_g;    /**< 当前量程（g） */
    uint16_t    lsb_per_g;  /**< 灵敏度：每 g 对应的原始码值 */
    uint8_t     odr_hz;     /**< 输出数据率（Hz） */
} imu_info_t;

/**
 * @brief 初始化 I2C 总线并配置 QMA6100P。
 *
 * 流程：扫描 I2C 总线 -> 校验 CHIP_ID -> 软复位 -> 设置量程/带宽 -> 进入测量模式
 *      -> 回读寄存器确认配置真的写进去了（不是"写完就算成功"）。
 *
 * @return ESP_OK 成功；ESP_ERR_NOT_FOUND 总线上没找到 QMA6100P；
 *         ESP_ERR_INVALID_RESPONSE CHIP_ID 不符；其它为 I2C 错误。
 */
esp_err_t imu_init(void);

/**
 * @brief 读取一帧真实的原始三轴码值。
 * @param[out] out 读到的原始数据
 */
esp_err_t imu_read_raw(imu_raw_sample_t *out);

/**
 * @brief 连读 count 次并做算术平均（抑制噪声，仍完全来自真实寄存器）。
 * @param[out] out          平均后的原始码值
 * @param[in]  count        采样次数
 * @param[in]  interval_us  相邻两次采样间隔（微秒）
 */
esp_err_t imu_read_raw_averaged(imu_raw_sample_t *out, int count, int interval_us);

/** @brief 取传感器静态信息（初始化成功后有效）。 */
const imu_info_t *imu_get_info(void);

/** @brief 把原始码值换算成 g（重力加速度倍数）。 */
float imu_raw_to_g(int16_t raw);

/** @brief 把原始码值换算成 m/s^2（国际单位制）。 */
float imu_raw_to_ms2(int16_t raw);

/** @brief 记录开机以来的读取失败次数（供排查用）。 */
uint32_t imu_read_error_count(void);

#ifdef __cplusplus
}
#endif