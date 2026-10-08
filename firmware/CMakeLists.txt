# ============================================================================
#  顶层 CMakeLists.txt —— PlatformIO (framework=espidf) 的标准入口
#  它把工程交给 ESP-IDF 的构建系统，行为与 `idf.py build` 完全一致。
# ============================================================================
cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

project(esp32s3eye_imu_uploader)