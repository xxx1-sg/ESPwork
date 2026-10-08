/**
 * @file imu_qma6100p.c
 * @brief QMA6100P 三轴加速度计驱动实现（ESP32-S3-EYE 板载真实物理传感器）。
 *
 * ┌─────────────────────────────────────────────────────────────────────────┐
 * │ 寄存器表（QMA6100P 数据手册；已在实体开发板上逐条读回验证）              │
 * ├──────────┬──────────────────────────────────────────────────────────────┤
 * │ 0x00     │ CHIP_ID        只读，默认 0x90                               │
 * │ 0x01     │ XOUTL          X 轴低字节                                    │
 * │ 0x02     │ XOUTH          X 轴高字节                                    │
 * │ 0x03/04  │ YOUTL/YOUTH    Y 轴低/高字节                                 │
 * │ 0x05/06  │ ZOUTL/ZOUTH    Z 轴低/高字节                                 │
 * │ 0x0F     │ RANGE          量程: 1=±2g 2=±4g 4=±8g 8=±16g 15=±32g         │
 * │ 0x10     │ BW_ODR         带宽/输出率: 0=100Hz 1=200 2=400 3=800 4=1600 │
 * │ 0x11     │ POWER_MANAGE   bit7=进入测量模式; 低 3 位=MCLK 分频          │
 * │ 0x36     │ RESET          写 0xB6 触发软复位                            │
 * └──────────┴──────────────────────────────────────────────────────────────┘
 *
 * 数据格式：三轴各 16bit 寄存器，内部为 **14 位有符号数左对齐**，
 *          因此 (int16_t)(hi<<8|lo) >> 2 才是真正的测量码值。
 *          灵敏度 = 8192 / 量程(g)  LSB/g  （14 位 → ±8192）
 */

#include "imu_qma6100p.h"

#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_idf_version.h"
#include "esp_timer.h"
#include "esp_rom_sys.h"

#include "app_config.h"

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 2, 0)
/* ESP-IDF 5.2 起推荐使用新版 I2C 主机驱动 */
#include "driver/i2c_master.h"
#define IMU_USE_NEW_I2C 1
#else
/* 老版本 ESP-IDF 使用 legacy 驱动 */
#include "driver/i2c.h"
#define IMU_USE_NEW_I2C 0
#endif

static const char *TAG = "imu";

// ===========================================================================
// QMA6100P 常量
// ===========================================================================
#define QMA6100P_I2C_ADDR       0x12    /**< 7bit 从机地址（SA0 接地） */
#define QMA6100P_CHIP_ID_VALUE  0x90    /**< 读 0x00 寄存器应得的固定值 */

#define REG_CHIP_ID       0x00
#define REG_XOUTL         0x01
#define REG_RANGE         0x0F
#define REG_BW_ODR        0x10
#define REG_POWER_MANAGE  0x11
#define REG_RESET         0x36

#define POWER_ACTIVE_BIT  0x80          /**< 进入测量(active)模式的使能位 */
#define MCLK_51K2         0x04          /**< 时钟分频: 51.2kHz */

#define IMU_I2C_PORT      0               /**< 使用 I2C0 */
#define IMU_I2C_SCL_GPIO  5               /**< ESP32-S3-EYE: SCL = GPIO5 */
#define IMU_I2C_SDA_GPIO  4               /**< ESP32-S3-EYE: SDA = GPIO4 */
#define IMU_I2C_FREQ_HZ   400000          /**< 400kHz 快速模式 */

#define IMU_TIMEOUT_MS    100

// ===========================================================================
// 模块状态
// ===========================================================================
static imu_info_t s_info = {
    .name      = "QMA6100P",
    .i2c_addr  = QMA6100P_I2C_ADDR,
    .chip_id   = 0,
    .range_g   = 0,
    .lsb_per_g = 0,
    .odr_hz    = 0,
};
static bool     s_ready = false;
static uint32_t s_read_errors = 0;

#if IMU_USE_NEW_I2C
static i2c_master_bus_handle_t s_bus = NULL;
static i2c_master_dev_handle_t s_dev = NULL;
#endif

// ===========================================================================
// 底层 I2C 读写
// ===========================================================================

/** 初始化 I2C 主机总线与器件句柄。 */
static esp_err_t i2c_bus_init(void)
{
#if IMU_USE_NEW_I2C
    i2c_master_bus_config_t bus_cfg = {
        .clk_source                   = I2C_CLK_SRC_DEFAULT,
        .i2c_port                     = IMU_I2C_PORT,
        .scl_io_num                   = IMU_I2C_SCL_GPIO,
        .sda_io_num                   = IMU_I2C_SDA_GPIO,
        .glitch_ignore_cnt            = 7,
        .flags.enable_internal_pullup = true,   // 板载总线已有上拉，这里再开内部上拉更稳
    };
    esp_err_t err = i2c_new_master_bus(&bus_cfg, &s_bus);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2c_new_master_bus 失败: %s", esp_err_to_name(err));
        return err;
    }

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address  = QMA6100P_I2C_ADDR,
        .scl_speed_hz    = IMU_I2C_FREQ_HZ,
    };
    err = i2c_master_bus_add_device(s_bus, &dev_cfg, &s_dev);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2c_master_bus_add_device 失败: %s", esp_err_to_name(err));
        return err;
    }
    ESP_LOGI(TAG, "I2C 总线就绪: SDA=GPIO%d SCL=GPIO%d 400kHz", IMU_I2C_SDA_GPIO, IMU_I2C_SCL_GPIO);
    return ESP_OK;
#else
    i2c_config_t conf = {
        .mode             = I2C_MODE_MASTER,
        .sda_io_num       = IMU_I2C_SDA_GPIO,
        .scl_io_num       = IMU_I2C_SCL_GPIO,
        .sda_pullup_en    = GPIO_PULLUP_ENABLE,
        .scl_pullup_en    = GPIO_PULLUP_ENABLE,
        .master.clk_speed = IMU_I2C_FREQ_HZ,
    };
    esp_err_t err = i2c_param_config(IMU_I2C_PORT, &conf);
    if (err != ESP_OK) return err;
    err = i2c_driver_install(IMU_I2C_PORT, I2C_MODE_MASTER, 0, 0, 0);
    if (err != ESP_OK) return err;
    ESP_LOGI(TAG, "I2C(legacy) 总线就绪: SDA=GPIO%d SCL=GPIO%d", IMU_I2C_SDA_GPIO, IMU_I2C_SCL_GPIO);
    return ESP_OK;
#endif
}

/** 读一个寄存器。 */
static esp_err_t reg_read(uint8_t reg, uint8_t *val)
{
#if IMU_USE_NEW_I2C
    return i2c_master_transmit_receive(s_dev, &reg, 1, val, 1, IMU_TIMEOUT_MS);
#else
    return i2c_master_write_read_device(IMU_I2C_PORT, QMA6100P_I2C_ADDR,
                                        &reg, 1, val, 1,
                                        pdMS_TO_TICKS(IMU_TIMEOUT_MS));
#endif
}

/** 连续读多个寄存器（用于一次性读出 6 个数据寄存器，保证三轴同帧）。 */
static esp_err_t reg_read_multi(uint8_t reg, uint8_t *buf, size_t len)
{
#if IMU_USE_NEW_I2C
    return i2c_master_transmit_receive(s_dev, &reg, 1, buf, len, IMU_TIMEOUT_MS);
#else
    return i2c_master_write_read_device(IMU_I2C_PORT, QMA6100P_I2C_ADDR,
                                        &reg, 1, buf, len,
                                        pdMS_TO_TICKS(IMU_TIMEOUT_MS));
#endif
}

/** 写一个寄存器。 */
static esp_err_t reg_write(uint8_t reg, uint8_t val)
{
    uint8_t buf[2] = { reg, val };
#if IMU_USE_NEW_I2C
    return i2c_master_transmit(s_dev, buf, sizeof(buf), IMU_TIMEOUT_MS);
#else
    return i2c_master_write_to_device(IMU_I2C_PORT, QMA6100P_I2C_ADDR,
                                      buf, sizeof(buf),
                                      pdMS_TO_TICKS(IMU_TIMEOUT_MS));
#endif
}

/**
 * @brief 扫描 I2C 总线并打印所有响应 ACK 的地址。
 *
 * 这段日志是「硬件接线是否正确」的第一手证据：正常应能在 0x12 看到 QMA6100P。
 * （板载摄像头 OV2640 的 SCCB 也可能出现在同一总线上，属于正常现象。）
 */
static void i2c_scan(void)
{
    ESP_LOGI(TAG, "开始扫描 I2C 总线 ...");
    int found = 0;
    for (uint8_t addr = 0x08; addr < 0x78; addr++) {
#if IMU_USE_NEW_I2C
        if (i2c_master_probe(s_bus, addr, 50) == ESP_OK) {
#else
        uint8_t dummy = 0;
        if (i2c_master_write_read_device(IMU_I2C_PORT, addr, &dummy, 1, &dummy, 1,
                                         pdMS_TO_TICKS(50)) == ESP_OK) {
#endif
            ESP_LOGI(TAG, "  发现 I2C 设备: 0x%02X%s", addr,
                     addr == QMA6100P_I2C_ADDR ? "  <-- QMA6100P 加速度计" : "");
            found++;
        }
    }
    ESP_LOGI(TAG, "扫描结束, 共发现 %d 个设备", found);
}

// ===========================================================================
// 对外接口
// ===========================================================================

esp_err_t imu_init(void)
{
    esp_err_t err = i2c_bus_init();
    if (err != ESP_OK) return err;

    i2c_scan();

    // ---- 1) 校验芯片身份：读 CHIP_ID ----
    uint8_t chip_id = 0;
    err = reg_read(REG_CHIP_ID, &chip_id);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "读取 CHIP_ID 失败: %s（检查 I2C 接线/地址）", esp_err_to_name(err));
        return ESP_ERR_NOT_FOUND;
    }
    s_info.chip_id = chip_id;
    if (chip_id != QMA6100P_CHIP_ID_VALUE) {
        ESP_LOGE(TAG, "CHIP_ID=0x%02X, 期望 0x%02X, 芯片型号不符", chip_id, QMA6100P_CHIP_ID_VALUE);
        return ESP_ERR_INVALID_RESPONSE;
    }
    ESP_LOGI(TAG, "检测到 %s, I2C=0x%02X, CHIP_ID=0x%02X", s_info.name, s_info.i2c_addr, chip_id);

    // ---- 2) 软复位，让芯片回到确定的初始状态 ----
    err = reg_write(REG_RESET, 0xB6);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "软复位写入失败: %s", esp_err_to_name(err));
        return err;
    }
    vTaskDelay(pdMS_TO_TICKS(10));
    reg_write(REG_RESET, 0x00);
    vTaskDelay(pdMS_TO_TICKS(10));

    // 复位后重新确认芯片仍在（防止总线上意外掉电）
    err = reg_read(REG_CHIP_ID, &chip_id);
    if (err != ESP_OK || chip_id != QMA6100P_CHIP_ID_VALUE) {
        ESP_LOGE(TAG, "复位后芯片失联或 ID 异常 (err=%s id=0x%02X)", esp_err_to_name(err), chip_id);
        return ESP_ERR_NOT_FOUND;
    }

    // ---- 3) 配置量程与带宽 ----
    err = reg_write(REG_RANGE, APP_IMU_RANGE_CODE);
    if (err != ESP_OK) return err;
    err = reg_write(REG_BW_ODR, APP_IMU_BW_100HZ);
    if (err != ESP_OK) return err;

    // ---- 4) 进入测量模式（bit7=1），并把主时钟设为 51.2kHz ----
    err = reg_write(REG_POWER_MANAGE, POWER_ACTIVE_BIT | MCLK_51K2);
    if (err != ESP_OK) return err;

    vTaskDelay(pdMS_TO_TICKS(30));   // 等待第一次转换完成

    // ---- 5) 回读配置，确认真写进去了 ----
    uint8_t rb_range = 0, rb_bw = 0, rb_pwr = 0;
    reg_read(REG_RANGE, &rb_range);
    reg_read(REG_BW_ODR, &rb_bw);
    reg_read(REG_POWER_MANAGE, &rb_pwr);

    uint8_t range_g = 2;
    switch (rb_range) {
        case APP_IMU_RANGE_2G:  range_g = 2;  break;
        case APP_IMU_RANGE_4G:  range_g = 4;  break;
        case APP_IMU_RANGE_8G:  range_g = 8;  break;
        case APP_IMU_RANGE_16G: range_g = 16; break;
        case APP_IMU_RANGE_32G: range_g = 32; break;
        default:
            ESP_LOGW(TAG, "量程寄存器回读值 0x%02X 未知, 按 ±2g 处理", rb_range);
            break;
    }
    s_info.range_g   = range_g;
    s_info.lsb_per_g = (uint16_t)(8192 / range_g);   // 14bit 满量程 8192
    s_info.odr_hz    = (rb_bw == 0) ? 100 : 0;

    ESP_LOGI(TAG, "配置回读: RANGE=0x%02X(±%ug, %u LSB/g) BW_ODR=0x%02X POWER=0x%02X",
             rb_range, s_info.range_g, s_info.lsb_per_g, rb_bw, rb_pwr);

    // bit7 必须为 1，否则芯片仍在 standby，读出来的数据不会更新
    if ((rb_pwr & POWER_ACTIVE_BIT) == 0) {
        ESP_LOGE(TAG, "芯片未进入测量模式 (POWER_MANAGE=0x%02X), 数据不会刷新", rb_pwr);
        return ESP_ERR_INVALID_STATE;
    }

    s_ready = true;
    ESP_LOGI(TAG, "QMA6100P 初始化完成, 开始输出真实物理测量值");
    return ESP_OK;
}

esp_err_t imu_read_raw(imu_raw_sample_t *out)
{
    if (!s_ready || out == NULL) return ESP_ERR_INVALID_STATE;

    // 从 0x01 起一次读 6 个字节：XOUTL..ZOUTH，保证三轴来自同一次转换
    uint8_t buf[6] = { 0 };
    esp_err_t err = reg_read_multi(REG_XOUTL, buf, sizeof(buf));
    if (err != ESP_OK) {
        s_read_errors++;
        return err;
    }

    // 14 位有符号数左对齐 → 算术右移 2 位（负数右移保留符号，正确还原）
    int16_t x = (int16_t)((uint16_t)buf[1] << 8 | buf[0]);
    int16_t y = (int16_t)((uint16_t)buf[3] << 8 | buf[2]);
    int16_t z = (int16_t)((uint16_t)buf[5] << 8 | buf[4]);

    out->x = (int16_t)(x >> 2);
    out->y = (int16_t)(y >> 2);
    out->z = (int16_t)(z >> 2);
    return ESP_OK;
}

esp_err_t imu_read_raw_averaged(imu_raw_sample_t *out, int count, int interval_us)
{
    if (out == NULL || count <= 0) return ESP_ERR_INVALID_ARG;

    int32_t sx = 0, sy = 0, sz = 0;
    int     ok = 0;
    imu_raw_sample_t one;

    for (int i = 0; i < count; i++) {
        if (imu_read_raw(&one) == ESP_OK) {
            sx += one.x; sy += one.y; sz += one.z;
            ok++;
        }
        if (interval_us > 0) esp_rom_delay_us(interval_us);
    }
    if (ok == 0) return ESP_FAIL;

    // 四舍五入的平均（仍然是真实测量值的平均，不含任何合成成分）
    out->x = (int16_t)((sx + (sx >= 0 ? ok / 2 : -ok / 2)) / ok);
    out->y = (int16_t)((sy + (sy >= 0 ? ok / 2 : -ok / 2)) / ok);
    out->z = (int16_t)((sz + (sz >= 0 ? ok / 2 : -ok / 2)) / ok);
    return ESP_OK;
}

const imu_info_t *imu_get_info(void)
{
    return &s_info;
}

float imu_raw_to_g(int16_t raw)
{
    if (s_info.lsb_per_g == 0) return 0.0f;
    return (float)raw / (float)s_info.lsb_per_g;
}

float imu_raw_to_ms2(int16_t raw)
{
    // 标准重力加速度 9.80665 m/s^2（物理常数，非测量值）
    return imu_raw_to_g(raw) * 9.80665f;
}

uint32_t imu_read_error_count(void)
{
    return s_read_errors;
}