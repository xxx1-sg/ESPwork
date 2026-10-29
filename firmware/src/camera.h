/**
 * @file camera.h
 * @brief 第 4 周：板载 OV2640 摄像头 —— 真实画面采集、预览帧上传、按需拍照。
 *
 * 设计原则（与本工程其它模块一致）：
 *   - 只搬运**传感器真实输出**的 JPEG，不做任何合成/占位/重复播放；
 *   - 摄像头起不来时如实报错并停用该功能，绝不影响采样/上传/按键求助；
 *   - 预览帧与照片都走同一条已有 HTTP(S) 通道（设备主动 POST），
 *     不新开端口、不引入 MQTT/WebSocket，符合"设备在 NAT 后只能主动出站"的约束。
 *
 * 数据流：
 *   OV2640 --DVP--> ESP32-S3 --JPEG--> POST /api/v1/cam/frame  (320x240, 5fps)
 *                                    POST /api/v1/cam/photo  (1600x1200, 按需)
 *
 * 拍照的触发方式：
 *   网页点「拍照」→ 后端置一个待拍照标记 → **下一次帧上传的响应**里回
 *   {"capture":true} → 设备立刻切到 UXGA 拍一张并上传。
 *   为什么复用帧上传的响应、而不是新加一条下行指令通道：
 *   帧本来就在 5Hz 往返，响应里夹带一个 bit 的时延 < 200ms，且不需要
 *   在 NAT 后面新开任何监听端口；拍照失败的因果也能在同一条链路上看清楚。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/** @brief 由 main.c 提供的静态身份信息（与 cmd_client / feedback 同一套 provider 模式）。 */
typedef struct {
    const char *device_id;   /**< 设备唯一 ID（eFuse MAC 派生） */
} cam_state_t;

typedef void (*cam_state_provider_t)(cam_state_t *out);

/** @brief 摄像头运行状态（全部来自真实计数，不估算、不美化）。 */
typedef struct {
    bool     ready;           /**< 传感器探测成功且已初始化 */
    bool     streaming;       /**< 任务在跑且 Wi-Fi 在线 */
    uint16_t sensor_pid;      /**< OV2640 = 0x26；0 表示未探测到 */
    uint32_t frames_sent;     /**< 成功上传的预览帧总数 */
    uint32_t photos_taken;    /**< 成功上传的照片总数 */
    uint32_t snapshots_taken; /**< 第 7 周：成功上传的按需快照总数 */
    uint32_t frame_errors;    /**< 预览帧上传失败次数（累计） */
    uint32_t last_seq;        /**< 最近一帧的序号（后端据此判断新旧） */
    int      last_w;          /**< 最近一帧实际宽度 */
    int      last_h;          /**< 最近一帧实际高度 */
    int      last_bytes;      /**< 最近一帧实际字节数 */
    int64_t  last_frame_us;   /**< 最近一帧成功上传的时刻（esp_timer） */
    char     last_error[96];  /**< 最近一次失败原因（空串=无） */
} cam_status_t;

/** @brief 注入身份 provider（必须在 camera_start 之前调用）。 */
void camera_set_state_provider(cam_state_provider_t fn);

/**
 * @brief 初始化摄像头并启动采集/上传任务（可重复调用，幂等）。
 *
 * ★ 必须在 imu_init() 之后调用：摄像头的 SCCB 复用 IMU 已在 I2C0 建好的总线
 *   （GPIO4/5 是两者共用的物理引脚）。反过来会互相抢引脚。
 *
 * @return ESP_OK 已成功初始化并创建任务；其它值为失败原因（功能停用，系统继续跑）。
 */
esp_err_t camera_start(void);

/** @brief 摄像头是否可用（初始化成功）。 */
bool camera_is_ready(void);

/** @brief 取一份状态快照（内部加锁，可在任意任务调用）。 */
void camera_get_status(cam_status_t *out);

/**
 * @brief 请求拍一张高分辨率照片（线程安全，可重复调用；正在拍时返回 false）。
 * @return true 已排入请求；false 摄像头不可用或已有拍照在进行中。
 */
bool camera_request_photo(void);

#ifdef __cplusplus
}
#endif
