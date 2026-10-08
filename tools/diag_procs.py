# -*- coding: utf-8 -*-
"""diag_procs.py —— 打印当前进程树（含命令行），用于定位构建卡在哪个子进程。

只读操作：不 kill、不修改任何东西。
"""
import sys

try:
    import psutil
except ImportError:
    print("需要 psutil：用 D:\\ESP\\.platformio\\penv\\.espidf-6.1.0\\Scripts\\python.exe 运行本脚本")
    sys.exit(1)

KEYWORDS = ("cmake", "ninja", "python", "xtensa", "esptool", "conhost", "cmd", "pwsh")


def short(cmd, n=200):
    if not cmd:
        return ""
    s = " ".join(cmd)
    return s if len(s) <= n else s[:n] + " ..."


def main():
    rows = []
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline", "create_time", "cpu_times"]):
        try:
            i = p.info
        except Exception:
            continue
        name = (i["name"] or "").lower()
        if not any(k in name for k in KEYWORDS):
            continue
        cpu = i["cpu_times"]
        rows.append({
            "pid": i["pid"], "ppid": i["ppid"], "name": i["name"],
            "cpu": (cpu.user + cpu.system) if cpu else 0,
            "ctime": i["create_time"] or 0,
            "cmd": short(i["cmdline"], 240),
        })
    rows.sort(key=lambda r: r["ctime"])
    print("%-8s %-8s %-18s %8s  %s" % ("PID", "PPID", "NAME", "CPU(s)", "CMDLINE"))
    print("-" * 130)
    for r in rows:
        print("%-8d %-8d %-18s %8.2f  %s" % (r["pid"], r["ppid"], r["name"], r["cpu"], r["cmd"]))
    print()
    print("提示: CPU 时间几乎为 0 且存在很久的进程就是卡住的那一个（顺着 PPID 往上看它的父进程）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())