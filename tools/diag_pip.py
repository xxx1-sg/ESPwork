# -*- coding: utf-8 -*-
"""诊断脚本：复现 pip 在 D:\\ESP\\.tmp 下的写入失败，并验证 urllib HTTPS 是否可用。

用途：在沙箱/杀软环境下定位「pip 能下载但解包报 EPERM」的真实原因，
并确认后续能否改用「自建 wheel 下载器」绕过 pip 安装阶段。
"""
import os
import sys
import tempfile
import urllib.request

BASE = r"D:\ESP\.tmp"


def attempt(label, path, data=b"x"):
    """尝试写入文件，打印结果而不是抛出异常。"""
    try:
        with open(path, "wb") as f:
            f.write(data)
        print("OK   %-40s -> %s" % (label, path))
        return True
    except Exception as e:  # noqa: BLE001
        print("FAIL %-40s -> %s | %s: %s" % (label, path, type(e).__name__, e))
        return False


def main():
    os.makedirs(BASE, exist_ok=True)

    # 1) 直接写普通文件
    attempt("plain", os.path.join(BASE, "diag_plain.txt"))

    # 2) 完全复刻 pip 的临时目录用法：tempfile.mkdtemp(prefix='pip-unpack-')
    d = tempfile.mkdtemp(prefix="pip-unpack-", dir=BASE)
    print("mkdtemp ->", d)
    attempt("mkdtemp + .whl.metadata",
            os.path.join(d, "pyserial-3.5-py2.py3-none-any.whl.metadata"))

    # 3) 复刻 pip 的 TempDirectory：先 chmod 0o700 再写
    d2 = tempfile.mkdtemp(prefix="pip-unpack-", dir=BASE)
    try:
        os.chmod(d2, 0o700)
        print("chmod 0o700 ok ->", d2)
    except Exception as e:  # noqa: BLE001
        print("chmod failed:", type(e).__name__, e)
    attempt("chmod后写 .whl.metadata",
            os.path.join(d2, "x-1.0-py3-none-any.whl.metadata"))

    # 4) urllib HTTPS 是否可用（这决定能否自建下载器）
    url = "https://pypi.org/pypi/pyserial/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            body = r.read(200)
        print("urllib HTTPS OK, %d bytes, head=%r" % (len(body), body[:40]))
    except Exception as e:  # noqa: BLE001
        print("urllib HTTPS FAIL:", type(e).__name__, e)

    return 0


if __name__ == "__main__":
    sys.exit(main())