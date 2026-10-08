#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_idf_venv.py —— 修复 PlatformIO 的 ESP-IDF Python 虚拟环境（本机专用，可复现）

【为什么需要这个脚本】
PlatformIO 为 ESP-IDF 创建 Python 虚拟环境时执行的是：
    <python> -m venv --clear <CORE_DIR>/penv/.espidf-<IDF版本>
venv 内部会用 `python -Im ensurepip` 安装 pip。而本机有一个环境问题：
Python 3.13+ 在 Windows 上会把 os.mkdir(path, mode) 真正翻译成 ACL，
tempfile.mkdtemp() 用的是 mode=0o700，本机安全策略下这种目录随后完全不可写，
于是 ensurepip 解包 pip 时报 `PermissionError: [Errno 13]`，
最终 PlatformIO 报：
    Error: Failed to create a proper virtual environment. Missing the `pip` binary!

【解决思路】
`-I`(isolated) 会忽略 PYTHONPATH，所以外部的 sitecustomize 补丁进不去；
但 venv 自己的 site-packages 仍在 sys.path 上。因此：
    1) 先用 `--without-pip` 建好 venv（这一步不会触发 ensurepip）
    2) 把修好的 sitecustomize.py 放进该 venv 的 site-packages
    3) 再在 venv 内跑 ensurepip —— 此时补丁已生效，pip 安装成功
    4) 按 PlatformIO 的清单安装 IDF 的 Python 依赖
    5) 写 PlatformIO 认的标记文件 pio-idf-venv.json
之后 PlatformIO 看到标记文件就会直接复用这个环境，不再重建。

用法:
    python tools/fix_idf_venv.py                 # 自动探测 IDF 版本
    python tools/fix_idf_venv.py --idf-version 6.1.0
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SITECUSTOMIZE_SRC = os.path.join(ROOT, ".pylibs", "sitecustomize.py")


def run(cmd, **kw):
    """执行外部命令，实时打印，失败即退出。"""
    print(">>", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=True, **kw).returncode


def detect_idf_version(core_dir):
    # ① 已有的 penv 目录名里带版本
    for d in glob.glob(os.path.join(core_dir, "penv", ".espidf-*")):
        m = re.search(r"\.espidf-(.+)$", os.path.basename(d))
        if m:
            return m.group(1)
    # ② 从 frameworks 头文件里解析
    pat = os.path.join(core_dir, "packages", "framework-espidf",
                       "components", "esp_common", "include", "esp_idf_version.h")
    if os.path.isfile(pat):
        text = open(pat, encoding="utf-8", errors="replace").read()
        def g(k):
            m = re.search(r"#define\s+ESP_IDF_VERSION_%s\s+(\d+)" % k, text)
            return m.group(1) if m else None
        parts = [g("MAJOR"), g("MINOR"), g("PATCH")]
        if all(parts):
            return ".".join(parts)
    raise SystemExit("无法探测 ESP-IDF 版本，请用 --idf-version 指定，例如 --idf-version 6.1.0")


def read_idf_env_version(core_dir):
    """从 platform 的 espidf.py 里读出 IDF_ENV_VERSION，保证与 PlatformIO 判断一致。"""
    pat = os.path.join(core_dir, "platforms", "espressif32", "builder",
                       "frameworks", "espidf.py")
    if os.path.isfile(pat):
        m = re.search(r'^IDF_ENV_VERSION\s*=\s*"([^"]+)"', open(pat, encoding="utf-8").read(),
                      re.M)
        if m:
            return m.group(1)
    return "1.0.0"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--core-dir", default=os.environ.get("PLATFORMIO_CORE_DIR",
                                                         os.path.join(ROOT, ".platformio")))
    ap.add_argument("--idf-version", default=None)
    ap.add_argument("--skip-pypackages", action="store_true",
                    help="额外写 .pio_skip_pypackages，让 PlatformIO 完全跳过依赖安装")
    a = ap.parse_args()

    core_dir = os.path.abspath(a.core_dir)
    idf_ver = a.idf_version or detect_idf_version(core_dir)
    venv_dir = os.path.join(core_dir, "penv", ".espidf-" + idf_ver)
    env_version = read_idf_env_version(core_dir)

    print("=" * 78)
    print("PlatformIO core dir :", core_dir)
    print("ESP-IDF 版本        :", idf_ver)
    print("venv 目录           :", venv_dir)
    print("IDF_ENV_VERSION     :", env_version)
    print("=" * 78)

    # 临时目录也放到工程内（系统临时目录在沙箱下不可写）
    tmp = os.path.join(ROOT, ".tmp")
    os.makedirs(tmp, exist_ok=True)
    env = dict(os.environ, TMP=tmp, TEMP=tmp)

    # ---- 1) 建一个不带 pip 的 venv ----
    if os.path.isdir(venv_dir):
        print("删除旧的 venv ...")
        shutil.rmtree(venv_dir, ignore_errors=True)
    run([sys.executable, "-m", "venv", "--without-pip", venv_dir], env=env)

    # ---- 2) 注入 sitecustomize 补丁 ----
    site_packages = os.path.join(venv_dir, "Lib", "site-packages") if os.name == "nt" \
        else os.path.join(venv_dir, "lib", "python%d.%d" % sys.version_info[:2], "site-packages")
    os.makedirs(site_packages, exist_ok=True)
    if os.path.isfile(SITECUSTOMIZE_SRC):
        shutil.copy2(SITECUSTOMIZE_SRC, os.path.join(site_packages, "sitecustomize.py"))
        print("已注入 sitecustomize.py ->", site_packages)
    else:
        print("警告: 未找到", SITECUSTOMIZE_SRC, "（若后续步骤失败，请检查该文件）")

    venv_python = os.path.join(venv_dir, "Scripts" if os.name == "nt" else "bin",
                               "python.exe" if os.name == "nt" else "python")

    # ---- 3) 在 venv 内安装 pip（此时补丁已生效） ----
    run([venv_python, "-Im", "ensurepip", "--upgrade", "--default-pip"], env=env)
    run([venv_python, "-m", "pip", "--version"], env=env)

    # ---- 4) 安装 PlatformIO 要求 IDF 使用的 Python 依赖 ----
    deps = [
        "urllib3<2",
        "cryptography~=46.0.0",
        "pyparsing>=3.1.0,<4",
        "idf-component-manager~=3.1",
        "esp-idf-kconfig~=3.9.0",
        "pydantic~=2.12.0",
    ]
    if os.name == "nt":
        deps.append("windows-curses")
    run([venv_python, "-m", "pip", "install", "--no-cache-dir", "-U"] + deps, env=env)

    # ---- 5) 写 PlatformIO 认的标记文件 ----
    ver_out = subprocess.check_output(
        [venv_python, "-c",
         "import sys;print('{0}.{1}.{2}-{3}.{4}'.format(*list(sys.version_info)))"],
        text=True, env=env).strip()
    marker = os.path.join(venv_dir, "pio-idf-venv.json")
    with open(marker, "w", encoding="utf-8") as f:
        json.dump({"version": env_version, "python_version": ver_out}, f, indent=2)
    print("已写标记文件:", marker, "->", {"version": env_version, "python_version": ver_out})

    if a.skip_pypackages:
        skip = os.path.join(core_dir, "packages", "framework-espidf", ".pio_skip_pypackages")
        open(skip, "w").close()
        print("已写:", skip)

    print("\n完成。现在可以正常执行: python -m platformio run")
    return 0


if __name__ == "__main__":
    sys.exit(main())