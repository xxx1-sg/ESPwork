# -*- coding: utf-8 -*-
"""更细粒度的目录/权限规则探测。

目标：确定「哪种方式创建的目录可以被写入」，从而决定能否运行
PlatformIO / ESP-IDF 这类会使用临时目录的 Python 工具链。
"""
import os
import subprocess
import sys
import tempfile

BASE = r"D:\ESP\.tmp"


def w(path, data=b"x"):
    try:
        with open(path, "wb") as f:
            f.write(data)
        return "OK"
    except Exception as e:  # noqa: BLE001
        return "FAIL %s: %s" % (type(e).__name__, e)


def main():
    os.makedirs(BASE, exist_ok=True)

    # A) os.mkdir 新建目录后写入
    a = os.path.join(BASE, "caseA_mkdir")
    os.makedirs(a, exist_ok=True)
    print("A os.mkdir                  :", w(os.path.join(a, "f.txt")))

    # B) mkdtemp 默认前缀
    b = tempfile.mkdtemp(dir=BASE)
    print("B mkdtemp(default prefix)   :", w(os.path.join(b, "f.txt")), "|", b)

    # C) mkdtemp 自定义前缀
    c = tempfile.mkdtemp(prefix="foo-", dir=BASE)
    print("C mkdtemp(prefix=foo-)      :", w(os.path.join(c, "f.txt")), "|", c)

    # D) os.mkdir 后再 chmod 0o700 再写
    d = os.path.join(BASE, "caseD_chmod")
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, 0o700)
        ch = "chmod ok"
    except Exception as e:  # noqa: BLE001
        ch = "chmod FAIL %s" % e
    print("D mkdir+chmod0o700          :", w(os.path.join(d, "f.txt")), "|", ch)

    # E) mkdtemp 目录改用 makedirs 风格的子目录再写
    e = os.path.join(b, "sub")
    os.makedirs(e, exist_ok=True)
    print("E write into mkdtemp/sub    :", w(os.path.join(e, "f.txt")))

    # F) 用 PowerShell 往 mkdtemp 目录写（判断是路径问题还是进程问题）
    pf = os.path.join(c, "ps.txt")
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Set-Content -Path '%s' -Value hi -ErrorAction Stop" % pf],
                       capture_output=True, text=True)
    print("F powershell write->mkdtemp :", "OK" if r.returncode == 0 else "FAIL " + r.stderr.strip()[:200])

    # G) 打印 mkdtemp 目录的 ACL（icacls）
    r = subprocess.run(["icacls", c], capture_output=True, text=True)
    print("G icacls(mkdtemp dir):")
    print(r.stdout.strip() or r.stderr.strip())

    # H) 打印 mkdir 目录的 ACL 做对比
    r = subprocess.run(["icacls", a], capture_output=True, text=True)
    print("H icacls(mkdir dir):")
    print(r.stdout.strip() or r.stderr.strip())

    return 0


if __name__ == "__main__":
    sys.exit(main())