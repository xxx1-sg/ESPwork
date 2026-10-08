#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
switch_endpoint.py —— 一条命令完成「更换后端地址」

什么时候用：
  - 内网穿透(cloudflared/ngrok)重启后域名变了；
  - 从本机联调地址切到正式 VPS 域名；
  - 换成了别的 HTTPS 服务（证书链不同）。
手工做要改 app_config.h、重生成 CA 头文件、再重新编译烧录，容易漏步骤；
本脚本把这几步串起来，避免「改了地址忘了换证书」这类低级问题。

用法:
    python tools/switch_endpoint.py --url https://xxx.trycloudflare.com/api/v1/ingest --flash
    python tools/switch_endpoint.py --url https://sensor.example.com/api/v1/ingest --check-only
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONFIG = os.path.join(ROOT, "firmware", "src", "app_config.h")
PENV_PY = os.path.join(ROOT, ".platformio", "penv", ".espidf-6.1.0", "Scripts", "python.exe")
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")


def run(cmd, **kw):
    print(">>", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=False, **kw)


def read_current_url() -> str | None:
    text = open(CONFIG, encoding="utf-8").read()
    m = re.search(r'#define\s+APP_SERVER_URL\s+"([^"]+)"', text)
    return m.group(1) if m else None


def write_url(url: str) -> None:
    text = open(CONFIG, encoding="utf-8").read()
    new, n = re.subn(r'(#define\s+APP_SERVER_URL\s+)"[^"]+"', r'\1"%s"' % url, text)
    if n != 1:
        raise SystemExit("在 app_config.h 里没有唯一定位到 APP_SERVER_URL，请手工检查")
    open(CONFIG, "w", encoding="utf-8", newline="\n").write(new)
    print("已写入 APP_SERVER_URL =", url)


def check_url(url: str) -> bool:
    """用一个空 POST 探一下地址是否活着：预期返回 400(缺字段) 或 401(token) 而不是连不上。"""
    import json
    import urllib.error
    import urllib.request
    body = json.dumps({"probe": "switch_endpoint"}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print("探测 %s -> HTTP %d（地址可用）" % (url, r.status))
            return True
    except urllib.error.HTTPError as e:
        # 4xx 说明服务器在正常回应（缺 token/字段），地址本身是通的
        print("探测 %s -> HTTP %d（服务器有回应，地址可用）" % (url, e.code))
        return True
    except Exception as e:                                  # noqa: BLE001
        print("探测 %s 失败: %s" % (url, e))
        return False


def gen_ca() -> bool:
    """重新生成 server_ca_pem.h（需要 cryptography，故优先用 PlatformIO 的 IDF venv）。"""
    py = PENV_PY if os.path.isfile(PENV_PY) else sys.executable
    rc = run([py, os.path.join(HERE, "gen_ca_header.py")]).returncode
    if rc != 0:
        print("!! 生成 CA 链失败（rc=%d）。若是 http:// 地址可忽略证书步骤。" % rc)
        return False
    return True


def build_and_flash(skip_build: bool) -> int:
    py = VENV_PY if os.path.isfile(VENV_PY) else sys.executable
    env = dict(os.environ)
    tmp = os.path.join(ROOT, ".tmp")
    os.makedirs(tmp, exist_ok=True)
    env.update(TMP=tmp, TEMP=tmp,
               PYTHONPATH=os.path.join(ROOT, ".pylibs"),
               PLATFORMIO_CORE_DIR=os.path.join(ROOT, ".platformio"))
    target = ["run"] if skip_build else ["run", "-t", "upload"]
    return run([py, "-m", "platformio"] + target, cwd=os.path.join(ROOT, "firmware"),
               env=env).returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="新的后端 ingest 地址")
    ap.add_argument("--no-ca", action="store_true", help="跳过 CA 头文件重新生成（http:// 地址用）")
    ap.add_argument("--check-only", action="store_true", help="只探测地址，不改文件")
    ap.add_argument("--build-only", action="store_true", help="改完只编译不烧录")
    ap.add_argument("--no-build", action="store_true", help="只改配置，不编译")
    a = ap.parse_args()

    old = read_current_url()
    print("当前地址:", old)
    print("新地址  :", a.url)

    if not check_url(a.url):
        print("!! 新地址探测失败：如果服务器还没起来可以先忽略；请确认隧道/VPS 已运行")
        if a.check_only:
            return 2
    if a.check_only:
        return 0

    write_url(a.url)

    if not a.no_ca and a.url.startswith("https://"):
        if not gen_ca():
            print("提示: 可以临时把 app_config.h 的 APP_TLS_TRUST_MODE 设为 0（内置根证书包）再试。")
    elif a.url.startswith("http://"):
        print("注意: 明文 HTTP 地址不需要证书；生产环境请务必使用 HTTPS。")

    if a.no_build:
        return 0

    rc = build_and_flash(skip_build=a.build_only)
    if rc != 0:
        print("编译/烧录失败，rc=%d" % rc)
        return rc

    print()
    print("完成。若更换的是隧道地址，记得把新地址也写进交付文档/网页配置：")
    print("  新地址:", a.url)
    if shutil.which("pio") is None:
        print("  (本机通过 python -m platformio 调用 PlatformIO)")
    return 0


if __name__ == "__main__":
    sys.exit(main())