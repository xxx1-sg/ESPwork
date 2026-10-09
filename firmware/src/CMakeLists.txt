# ============================================================================
#  src/CMakeLists.txt —— 注册本工程的源文件与依赖组件
#
#  I2C 组件的名字在不同 ESP-IDF 版本里不一样：
#    - ESP-IDF >= 5.2 : 新版主机驱动在 esp_driver_i2c 组件里 (driver/i2c_master.h)
#    - ESP-IDF <= 5.1 : legacy 驱动在 driver 组件里 (driver/i2c.h)
#  这里按实际 IDF 版本自动选择，保证工程在新老 IDF 上都能编译。
# ============================================================================

if(IDF_VERSION_MAJOR GREATER 5 OR
   (IDF_VERSION_MAJOR EQUAL 5 AND IDF_VERSION_MINOR GREATER_EQUAL 2))
    set(I2C_COMPONENT esp_driver_i2c)
else()
    set(I2C_COMPONENT driver)
endif()

message(STATUS "I2C component in use: ${I2C_COMPONENT}")

idf_component_register(
    SRCS
        "main.c"              # 主流程: 初始化 -> 采集 -> 上传
        "imu_qma6100p.c"      # QMA6100P 真实物理传感器驱动 (I2C)
        "uploader.c"          # Wi-Fi 联网 + HTTP(S) POST 上传
        "cmd_client.c"        # 第 2 周: 远程采集指令轮询/回执/观测回传（复用上述 HTTP 栈）
    INCLUDE_DIRS
        "."
    REQUIRES
        ${I2C_COMPONENT}    # I2C 驱动（按 IDF 版本自动选择）
        esp_wifi            # Wi-Fi
        esp_netif           # 网络接口 + SNTP
        esp_event           # 事件循环
        nvs_flash           # 掉电保存 boot 计数
        esp_http_client     # HTTP 客户端
        esp-tls             # TLS
        mbedtls             # 证书包 esp_crt_bundle
        esp_timer           # 微秒级单调时钟
        lwip                # SNTP
        # 注意: 组包用 snprintf 手写, 不依赖 cJSON
        # (ESP-IDF 6.x 已移除内置 json/cJSON 组件)
)