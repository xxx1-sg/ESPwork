/**
 * @file gpio_probe.c
 * @brief 一次性硬件探针：确认这块 ESP32-S3-EYE 上 BOOT 键到底接在哪个 GPIO、
 *        以及 LED/蜂鸣器所选引脚是否可用。
 *
 * ★ 为什么需要它：`feedback.c` 假设"板载 BOOT 键 = GPIO0、按下为低"。
 *   如果这个假设在这块板子（或这个板子版本）上不成立，按键就永远检测不到，
 *   而且**从代码上看不出任何问题** —— 表现为"任务都启动了，就是没反应"。
 *   与其猜，不如扫一遍：把每个候选引脚的电平打印出来，按一下键看哪个在变。
 *
 * 用法：把下面的 PROBE_MODE 打开（或直接编译本文件为 main），
 *       按住 BOOT 键观察哪个引脚从 1 变 0（或从 0 变 1）。
 *
 * 本文件不参与正式固件编译（未登记进 CMakeLists 的 SRCS）。
 */
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "probe";

/* 候选按键引脚：ESP32-S3-EYE 上常见的是 0；同时把几个"用户可用"引脚一起扫，
 * 便于发现这块板子其实接在别处。避开 USB(19/20)、flash/PSRAM(26~37)、I2C(4/5)。 */
static const int CANDIDATES[] = { 0, 1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 13, 14,
                                  15, 16, 17, 18, 21, 38, 39, 40, 41, 42, 45, 46, 47, 48 };
#define NCAND ((int)(sizeof(CANDIDATES) / sizeof(CANDIDATES[0])))

void app_main(void)
{
    ESP_LOGI(TAG, "=== GPIO 探针启动：扫描 %d 个候选引脚 ===", NCAND);
    ESP_LOGI(TAG, "请**按住 BOOT 键不放**，观察哪一行电平发生变化");
    ESP_LOGI(TAG, "（每 1 秒打印一次；电平颠倒在按下/松开之间）");

    gpio_config_t cfg = {
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,     // 统一开上拉：按下接地则读到 0
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    for (int i = 0; i < NCAND; i++) {
        cfg.pin_bit_mask = 1ULL << CANDIDATES[i];
        gpio_config(&cfg);
    }

    int last[NCAND];
    for (int i = 0; i < NCAND; i++) last[i] = -1;

    while (1) {
        char line[512];
        int off = 0;
        bool changed = false;
        for (int i = 0; i < NCAND && off < (int)sizeof(line) - 16; i++) {
            int v = gpio_get_level(CANDIDATES[i]);
            if (last[i] >= 0 && v != last[i]) changed = true;
            off += snprintf(line + off, sizeof(line) - off, "G%d=%d ", CANDIDATES[i], v);
            last[i] = v;
        }
        /* 只在有变化时用 WARN 级别打（便于在日志里一眼看到），否则按 INFO 打全量 */
        if (changed) {
            ESP_LOGW(TAG, "★ 电平变化! %s", line);
        } else {
            ESP_LOGI(TAG, "%s", line);
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}