/**
 * @file lcd_status.c
 * @brief 用 esp_lcd 驱动板载 ST7789（240x240）把求助状态画出来。
 *
 * 设计取舍（写下来，免得下次又纠结）：
 *   1. **不引入 LVGL**：只需要"整屏底色 + 一个几何符号"就能表达四个状态，
 *      用 esp_lcd 的 draw_bitmap 分带刷屏即可，省掉一整套 GUI 依赖与内存开销。
 *   2. **不做文字**：写字需要字库（ASCII 要 5x7/8x8 点阵，中文更大）。
 *      本模块的目标是"让屏亮起来、状态一眼可辨"，用**颜色 + 符号**达到同样效果：
 *        待机=暗底空心环   等待=琥珀底感叹号   已送达=绿底对勾   取消=灰底叉
 *      需要文字时再挂 LVGL + 字体，不影响本模块的状态机。
 *   3. **失败不致命**：SPI/面板任一步失败都只降级，不影响按键/蜂鸣/LED/上传。
 *
 * ★ 背光是**低电平点亮**（官方 BSP 的 brightness=100% 对应 LEDC duty=0）。
 *   初始化期间先把背光置为"灭"，避免上电瞬间花屏；面板点起来后再点亮。
 */
#include "lcd_status.h"

/* ★★ 顺序极重要：app_config.h 必须**先于**下面的 #if 包含。
 *    APP_LCD_ENABLE 就定义在 app_config.h 里。曾经把它写在 #if 内部 ——
 *    判断那一刻宏尚未定义，C 预处理器把未定义标识符当作 0，
 *    于是整份 LCD 代码被**静默编译掉**，走空实现，屏永远不亮。
 *    现象极具迷惑性：编译通过、.o 也生成了，但屏是黑的（docs/10 §6.2 D7）。★勿再犯★
 */
#include "app_config.h"
#include "esp_log.h"   /* 两个分支都要用：关闭时也要打印一行"本次不用屏幕" */

#ifndef APP_LCD_ENABLE
#error "lcd_status.c 必须先包含 app_config.h 才能拿到 APP_LCD_ENABLE（包含顺序错了会让 LCD 被静默编译掉）"
#endif

/* 把编译期走向**显式打出来**：万一又被写反，编译日志里立刻看得见，
 * 而不是等到烧录后盯着黑屏发呆。 */
#if APP_LCD_ENABLE
#  pragma message("lcd_status: APP_LCD_ENABLE=1 -> LCD 代码已编入")
#else
#  warning "lcd_status: APP_LCD_ENABLE=0 -> LCD 代码被排除，屏幕不会亮"
#endif

#if APP_LCD_ENABLE

#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_panel_ops.h"

static const char *TAG = "lcd";

// 分带刷屏：一次刷 16 行。240*16*2 = 7680 字节，放内部 DMA 内存毫无压力，
// 既不需要 115KB 整屏缓冲，也不需要 PSRAM。
#define LCD_BAND_H      16
#define LCD_BAND_BYTES  (APP_LCD_H_RES * LCD_BAND_H * sizeof(uint16_t))
#define LCD_CMD_BITS    8
#define LCD_PARAM_BITS  8

// ---- 颜色（RGB565）----
#define RGB565(r, g, b) ((uint16_t)((((r) & 0xF8) << 8) | (((g) & 0xFC) << 3) | ((b) >> 3)))
#define C_IDLE_BG    RGB565(12, 16, 32)
#define C_IDLE_FG    RGB565(92, 112, 152)
#define C_WAIT_BG    RGB565(230, 152, 20)     // 琥珀：与"等待中"的 LED 慢闪同一语义
#define C_WAIT_FG    RGB565(34, 22, 0)
#define C_DELIV_BG   RGB565(24, 160, 80)      // 绿：与"已送达"的 LED 常亮同一语义
#define C_DELIV_FG   RGB565(255, 255, 255)
#define C_CANCEL_BG  RGB565(72, 82, 96)
#define C_CANCEL_FG  RGB565(235, 240, 245)
#define C_BOOT_BG    RGB565(16, 18, 28)
#define C_BOOT_FG    RGB565(70, 200, 220)

// ---- 模块状态 ----
static esp_lcd_panel_handle_t    s_panel   = NULL;
static esp_lcd_panel_io_handle_t s_io      = NULL;
static uint16_t                 *s_band    = NULL;
static SemaphoreHandle_t         s_spi_lock = NULL;   // 保护 SPI/面板（多个任务会请求刷屏）
static SemaphoreHandle_t         s_state_lock = NULL; // 保护 s_target
static volatile bool             s_ready   = false;
static volatile lcd_scene_t      s_target  = LCD_SCENE_OFF;
static volatile uint32_t         s_painted = 0;
static volatile TickType_t        s_hold_until = 0;  // 限时画面到期 tick（0 = 无）
static int                       s_bl_gpio = -1;

// ===========================================================================
// 颜色与几何小工具
// ===========================================================================

/** 按配置决定是否交换高低字节（本板面板是大端，ESP32 是小端，默认交换）。 */
static inline uint16_t out_color(uint16_t c)
{
#if APP_LCD_SWAP_BYTES
    return (uint16_t)((c >> 8) | (c << 8));
#else
    return c;
#endif
}

/** 点 (px,py) 到线段 (x1,y1)-(x2,y2) 的距离是否 <= half（用整数运算，避免浮点）。 */
static bool near_seg(int px, int py, int x1, int y1, int x2, int y2, int half)
{
    long long dx = (long long)x2 - x1, dy = (long long)y2 - y1;
    long long len2 = dx * dx + dy * dy;
    long long ex, ey, t;

    if (len2 == 0) {
        ex = (long long)px - x1; ey = (long long)py - y1;
        return (ex * ex + ey * ey) <= (long long)half * half;
    }
    t = (long long)(px - x1) * dx + (long long)(py - y1) * dy;
    if (t < 0) t = 0;
    if (t > len2) t = len2;
    // 把"到投影点的距离"乘以 len2 以免除零与浮点
    ex = (long long)(px - x1) * len2 - t * dx;
    ey = (long long)(py - y1) * len2 - t * dy;
    return (ex * ex + ey * ey) <= (long long)half * half * len2 * len2;
}

/** 每个像素该是什么颜色 —— 整个画面就是这一个纯函数。 */
static uint16_t scene_pixel(lcd_scene_t sc, int x, int y)
{
    const int cx = APP_LCD_H_RES / 2;
    const int cy = APP_LCD_V_RES / 2;

    switch (sc) {
    case LCD_SCENE_WAITING: {
        // 感叹号：短竖杠 + 圆点
        if (near_seg(x, y, cx, cy - 46, cx, cy + 2, 9))   return out_color(C_WAIT_FG);
        long long dx = x - cx, dy = y - (cy + 26);
        if (dx * dx + dy * dy <= 9 * 9)                   return out_color(C_WAIT_FG);
        return out_color(C_WAIT_BG);
    }
    case LCD_SCENE_DELIVERED: {
        // 对勾：两段粗线
        if (near_seg(x, y, cx - 44, cy + 2, cx - 14, cy + 34, 8)) return out_color(C_DELIV_FG);
        if (near_seg(x, y, cx - 14, cy + 34, cx + 46, cy - 38, 8)) return out_color(C_DELIV_FG);
        return out_color(C_DELIV_BG);
    }
    case LCD_SCENE_CANCELLED: {
        // 叉：两条对角线
        if (near_seg(x, y, cx - 34, cy - 34, cx + 34, cy + 34, 7)) return out_color(C_CANCEL_FG);
        if (near_seg(x, y, cx + 34, cy - 34, cx - 34, cy + 34, 7)) return out_color(C_CANCEL_FG);
        return out_color(C_CANCEL_BG);
    }
    case LCD_SCENE_BOOT: {
        long long dx = x - cx, dy = y - cy;
        long long d2 = dx * dx + dy * dy;
        if (d2 <= 44 * 44 && d2 >= 36 * 36) return out_color(C_BOOT_FG);
        return out_color(C_BOOT_BG);
    }
    case LCD_SCENE_IDLE:
    default: {
        long long dx = x - cx, dy = y - cy;
        long long d2 = dx * dx + dy * dy;
        if (d2 <= 52 * 52 && d2 >= 46 * 46) return out_color(C_IDLE_FG);
        return out_color(C_IDLE_BG);
    }
    }
}

// ===========================================================================
// 背光 / 刷屏
// ===========================================================================
static void bl_write(bool on)
{
    if (s_bl_gpio < 0) return;
#if APP_LCD_BL_ACTIVE_LOW
    gpio_set_level((gpio_num_t)s_bl_gpio, on ? 0 : 1);   // ★ 低电平点亮
#else
    gpio_set_level((gpio_num_t)s_bl_gpio, on ? 1 : 0);
#endif
}

static void paint_scene(lcd_scene_t sc)
{
    if (!s_ready || !s_panel || !s_band) return;
    if (xSemaphoreTake(s_spi_lock, portMAX_DELAY) != pdTRUE) return;

    s_painted++;   // 计数只表示"确实发起过一次刷屏"

    for (int y0 = 0; y0 < APP_LCD_V_RES; y0 += LCD_BAND_H) {
        int y1 = y0 + LCD_BAND_H;
        if (y1 > APP_LCD_V_RES) y1 = APP_LCD_V_RES;

        uint16_t *p = s_band;
        for (int y = y0; y < y1; y++) {
            for (int x = 0; x < APP_LCD_H_RES; x++) {
                *p++ = scene_pixel(sc, x, y);
            }
        }
        esp_lcd_panel_draw_bitmap(s_panel, 0, y0, APP_LCD_H_RES, y1, s_band);
    }
    xSemaphoreGive(s_spi_lock);
}

/**
 * @brief 用**纯色铺满整屏**（自检专用）。
 *
 * 为什么需要它：状态画面（待机）的底色是**近黑** RGB(12,16,32)。
 * 如果只切换背光电平而不换画面，"背光亮着但显示近黑"与"背光全灭"
 * **在肉眼上几乎无法区分** —— 这正是上一轮背光诊断失败的原因。
 * 自检必须让屏上出现**饱和度拉满的大色块**，做到"亮没亮"一眼可判。
 */
static void fill_screen(uint16_t color)
{
    if (!s_ready || !s_panel || !s_band) return;
    if (xSemaphoreTake(s_spi_lock, portMAX_DELAY) != pdTRUE) return;

    const int n = APP_LCD_H_RES * LCD_BAND_H;
    const uint16_t c = out_color(color);
    for (int i = 0; i < n; i++) s_band[i] = c;

    for (int y0 = 0; y0 < APP_LCD_V_RES; y0 += LCD_BAND_H) {
        int y1 = y0 + LCD_BAND_H;
        if (y1 > APP_LCD_V_RES) y1 = APP_LCD_V_RES;
        esp_lcd_panel_draw_bitmap(s_panel, 0, y0, APP_LCD_H_RES, y1, s_band);
    }
    s_painted++;
    xSemaphoreGive(s_spi_lock);
}

/** 渲染任务：只在"目标画面变了"时才真正刷屏，不空刷 SPI。 */
static void lcd_task(void *arg)
{
    (void)arg;
    lcd_scene_t last = LCD_SCENE_OFF;

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(120));

        lcd_scene_t want;
        if (xSemaphoreTake(s_state_lock, portMAX_DELAY) == pdTRUE) {
            // 限时画面到期 → 自动回待机（在同一把锁里改，避免与 set_scene 打架）
            if (s_hold_until != 0 && xTaskGetTickCount() >= s_hold_until) {
                s_hold_until = 0;
                s_target     = LCD_SCENE_IDLE;
            }
            want = s_target;
            xSemaphoreGive(s_state_lock);
        } else {
            continue;
        }

        if (want != last) {
            paint_scene(want);
            last = want;
            ESP_LOGI(TAG, "屏幕已刷新：scene=%d（累计刷屏 %u 次）", (int)want,
                     (unsigned)s_painted);
        }
    }
}

// ===========================================================================
// 初始化
// ===========================================================================
esp_err_t lcd_status_init(void)
{
    if (s_ready) return ESP_OK;

    // ---- 0) 背光先关（低电平点亮的板子，这里写"高"才是灭）----
    if (APP_LCD_BL_GPIO >= 0) {
        gpio_config_t bl = {
            .pin_bit_mask = 1ULL << APP_LCD_BL_GPIO,
            .mode         = GPIO_MODE_OUTPUT,
            .intr_type    = GPIO_INTR_DISABLE,
        };
        if (gpio_config(&bl) == ESP_OK) {
            s_bl_gpio = APP_LCD_BL_GPIO;
            bl_write(false);
        } else {
            ESP_LOGW(TAG, "背光 GPIO%d 配置失败：屏幕内容可能可见但亮度不可控",
                     APP_LCD_BL_GPIO);
        }
    }

    s_spi_lock   = xSemaphoreCreateMutex();
    s_state_lock = xSemaphoreCreateMutex();
    if (!s_spi_lock || !s_state_lock) {
        ESP_LOGW(TAG, "互斥量创建失败，LCD 降级为不可用");
        return ESP_OK;   // 不致命
    }

    s_band = heap_caps_malloc(LCD_BAND_BYTES, MALLOC_CAP_DMA);
    if (!s_band) {
        ESP_LOGW(TAG, "分带缓冲分配失败(%u 字节)，LCD 降级为不可用", (unsigned)LCD_BAND_BYTES);
        return ESP_OK;
    }

    // ---- 1) SPI3 总线 ----
    const spi_bus_config_t buscfg = {
        .sclk_io_num     = APP_LCD_SCLK_GPIO,
        .mosi_io_num     = APP_LCD_MOSI_GPIO,
        .miso_io_num     = -1,
        .quadwp_io_num   = -1,
        .quadhd_io_num   = -1,
        .max_transfer_sz = LCD_BAND_BYTES,
    };
    esp_err_t err = spi_bus_initialize(SPI3_HOST, &buscfg, SPI_DMA_CH_AUTO);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "SPI3 初始化失败(%s)：LCD 不可用，但按键/蜂鸣/LED/上传不受影响",
                 esp_err_to_name(err));
        return ESP_OK;
    }

    // ---- 2) 面板 IO ----
    const esp_lcd_panel_io_spi_config_t io_cfg = {
        .cs_gpio_num       = APP_LCD_CS_GPIO,
        .dc_gpio_num       = APP_LCD_DC_GPIO,
        .spi_mode          = 0,
        .pclk_hz           = APP_LCD_PIXEL_CLOCK_HZ,
        .trans_queue_depth = 10,
        .lcd_cmd_bits      = LCD_CMD_BITS,
        .lcd_param_bits    = LCD_PARAM_BITS,
    };
    err = esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)SPI3_HOST, &io_cfg, &s_io);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "面板 IO 创建失败(%s)：LCD 不可用", esp_err_to_name(err));
        spi_bus_free(SPI3_HOST);
        return ESP_OK;
    }

    // ---- 3) ST7789 面板驱动 ----
    const esp_lcd_panel_dev_config_t panel_cfg = {
        .reset_gpio_num = APP_LCD_RST_GPIO,      // -1 = 不接复位脚，走软件复位
        .rgb_ele_order  = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
    };
    err = esp_lcd_new_panel_st7789(s_io, &panel_cfg, &s_panel);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "ST7789 驱动创建失败(%s)：LCD 不可用", esp_err_to_name(err));
        esp_lcd_panel_io_del(s_io); s_io = NULL;
        spi_bus_free(SPI3_HOST);
        return ESP_OK;
    }

    // ---- 4) 复位 / 初始化 / 方向（与官方 BSP 一致）----
    esp_lcd_panel_reset(s_panel);
    esp_lcd_panel_init(s_panel);
    esp_lcd_panel_invert_color(s_panel, true);
    esp_lcd_panel_swap_xy(s_panel, false);
    esp_lcd_panel_mirror(s_panel, false, false);
    esp_lcd_panel_disp_on_off(s_panel, true);

    s_ready = true;
    s_target = LCD_SCENE_BOOT;
    xTaskCreatePinnedToCore(lcd_task, "lcd", 4096, NULL, 1, NULL, 0);

    bl_write(true);   // ★ 面板已初始化完，这才点亮背光

    ESP_LOGI(TAG, "板载 LCD 就绪：ST7789 %dx%d @ SPI3 (SCLK=%d MOSI=%d CS=%d DC=%d BL=%d)",
             APP_LCD_H_RES, APP_LCD_V_RES, APP_LCD_SCLK_GPIO, APP_LCD_MOSI_GPIO,
             APP_LCD_CS_GPIO, APP_LCD_DC_GPIO, APP_LCD_BL_GPIO);
    ESP_LOGI(TAG, "若屏幕颜色明显不对（红蓝互换/发花），把 APP_LCD_SWAP_BYTES 翻转后重烧");

#if APP_LCD_BL_SELFTEST
    /* ------------------------------------------------------------------
     * ★ 背光诊断（临时，`APP_LCD_BL_SELFTEST`）：
     *   分两段强制背光电平，让"肉眼"来决定到底哪种电平点亮 —— 比争论文档可靠。
     *     阶段A：GPIO 拉**低** 8 秒（官方 BSP 口径下应为"亮"）
     *     阶段B：GPIO 拉**高** 8 秒
     *   结束后恢复常规（按 APP_LCD_BL_ACTIVE_LOW 点亮）。
     *   确认后请把 app_config.h 里的 APP_LCD_BL_SELFTEST 改回 0。
     * ------------------------------------------------------------------ */
    if (s_bl_gpio >= 0) {
        /* ------------------------------------------------------------------
         * ★ LCD 自检（临时，`APP_LCD_BL_SELFTEST`）：**满屏纯色扫描**。
         *
         * 上一版只切背光电平、画面仍是"近黑待机"，导致"背光亮着"和"背光全灭"
         * 在肉眼上无法区分 → 诊断无效。现在改成让屏显示**饱和度拉满的大色块**：
         *     阶段A（GPIO 低，官方口径=点亮）：白 → 红
         *     阶段B（GPIO 高）              ：绿 → 蓝
         * 判据：
         *   ① 出现 白/红/绿/蓝 任意色块 ⇒ 屏 + 背光 + 排线都正常（问题只在"待机画面太暗"）；
         *   ② 只有阶段A 有颜色          ⇒ 低电平点亮（与官方一致，配置正确）；
         *   ③ 只有阶段B 有颜色          ⇒ 极性相反，把 APP_LCD_BL_ACTIVE_LOW 改成 0；
         *   ④ 全程纯黑                  ⇒ 背光/排线/供电故障，固件侧已排除。
         * 确认后请把 app_config.h 里的 APP_LCD_BL_SELFTEST 改回 0。
         * ------------------------------------------------------------------ */
        ESP_LOGW(TAG, "===== LCD 自检开始：满屏纯色扫描（每色 3 秒）=====");
        ESP_LOGW(TAG, "  期望：白→红（阶段A,GPIO低） / 绿→蓝（阶段B,GPIO高）；全程纯黑=硬件/排线问题");

        // 先让渲染任务把待机画面画完，免得它在我们扫描期间插一脚
        lcd_status_set_scene(LCD_SCENE_IDLE);
        vTaskDelay(pdMS_TO_TICKS(400));

        // ---- 阶段A：GPIO48 拉低（官方口径 = 点亮）----
        ESP_LOGW(TAG, "===== 阶段A：GPIO%d 拉低（官方口径=点亮）→ 白 3 秒 → 红 3 秒 =====",
                 s_bl_gpio);
        gpio_set_level((gpio_num_t)s_bl_gpio, 0);
        fill_screen(RGB565(255, 255, 255));
        vTaskDelay(pdMS_TO_TICKS(3000));
        fill_screen(RGB565(255, 0, 0));
        vTaskDelay(pdMS_TO_TICKS(3000));

        // ---- 阶段B：GPIO48 拉高 ----
        ESP_LOGW(TAG, "===== 阶段B：GPIO%d 拉高 → 绿 3 秒 → 蓝 3 秒 =====", s_bl_gpio);
        gpio_set_level((gpio_num_t)s_bl_gpio, 1);
        fill_screen(RGB565(0, 255, 0));
        vTaskDelay(pdMS_TO_TICKS(3000));
        fill_screen(RGB565(0, 128, 255));
        vTaskDelay(pdMS_TO_TICKS(3000));

        ESP_LOGW(TAG, "===== LCD 自检结束，恢复常规（APP_LCD_BL_ACTIVE_LOW=%d）=====",
                 APP_LCD_BL_ACTIVE_LOW);
        bl_write(true);
        lcd_status_set_scene(LCD_SCENE_IDLE);
    } else {
        ESP_LOGW(TAG, "LCD 自检跳过：背光 GPIO 未配置成功");
    }
#endif

    return ESP_OK;
}

// ===========================================================================
// 对外 API
// ===========================================================================
bool lcd_status_ready(void) { return s_ready; }

void lcd_status_set_scene(lcd_scene_t scene)
{
    if (!s_ready) return;
    if (s_state_lock && xSemaphoreTake(s_state_lock, portMAX_DELAY) == pdTRUE) {
        s_target     = scene;
        s_hold_until = 0;      // 常驻画面：清掉可能残留的限时
        xSemaphoreGive(s_state_lock);
    }
}

void lcd_status_set_scene_timed(lcd_scene_t scene, uint32_t hold_ms)
{
    if (!s_ready) return;
    if (s_state_lock && xSemaphoreTake(s_state_lock, portMAX_DELAY) == pdTRUE) {
        s_target     = scene;
        s_hold_until = xTaskGetTickCount() + pdMS_TO_TICKS(hold_ms);
        xSemaphoreGive(s_state_lock);
    }
}

lcd_scene_t lcd_status_scene(void) { return s_target; }

void lcd_status_backlight(bool on) { bl_write(on); }

uint32_t lcd_status_paint_count(void) { return s_painted; }

#else  /* APP_LCD_ENABLE == 0：整体降级为惰性空实现 */

static const char *TAG = "lcd";

esp_err_t lcd_status_init(void)
{
    ESP_LOGI(TAG, "LCD 已在配置里关闭（APP_LCD_ENABLE=0）：本次运行不使用屏幕，"
                  "按键/蜂鸣/LED 照常工作");
    return ESP_OK;
}
bool        lcd_status_ready(void)         { return false; }
void        lcd_status_set_scene(lcd_scene_t scene) { (void)scene; }
void        lcd_status_set_scene_timed(lcd_scene_t scene, uint32_t hold_ms)
                                            { (void)scene; (void)hold_ms; }
lcd_scene_t lcd_status_scene(void)         { return LCD_SCENE_OFF; }
void        lcd_status_backlight(bool on)  { (void)on; }
uint32_t    lcd_status_paint_count(void)   { return 0; }

#endif /* APP_LCD_ENABLE */
