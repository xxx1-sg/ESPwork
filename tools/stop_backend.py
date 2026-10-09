# -*- coding: utf-8 -*-
"""
stop_backend.py —— 停掉常驻后端（守护进程 + 真正的 server.py 一起停）

如果不把守护进程也停掉，它会立刻把后端重新拉起来，看起来就像"杀不死"。
所以这里按 PID 文件先停守护、再停监听 8000 端口的那个进程。

用法:
    D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\stop_backend.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIDFILE = os.path.join(ROOT, "evidence", "backend.pid")


def kill(pid, why):
    """结束进程。分别尝试 taskkill 和 PowerShell Stop-Process，任一成功即可。

    ★ 有些受限环境（沙箱/组策略）会拒绝 taskkill，所以这里做双重尝试，
      而不是一次失败就放弃。
    """
    import subprocess

    # 方案 1：taskkill（能带走整个子进程树，最干净）
    try:
        out = subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                             capture_output=True, text=True)
        if out.returncode == 0:
            print("  [已停] PID=%s (%s)" % (pid, why))
            return True
        err1 = (out.stderr or out.stdout or "").strip().splitlines()
        err1 = err1[0] if err1 else "taskkill 失败"
    except Exception as e:
        err1 = repr(e)

    # 方案 2：PowerShell Stop-Process
    try:
        out2 = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Stop-Process -Id %d -Force -ErrorAction Stop" % pid],
            capture_output=True, text=True)
        if out2.returncode == 0:
            print("  [已停] PID=%s (%s)" % (pid, why))
            return True
    except Exception:
        pass

    print("  [失败] PID=%s (%s): %s" % (pid, why, err1))
    print("         （若提示 Access denied，请用【管理员】PowerShell 再跑一次本脚本）")
    return False


def main():
    print("=== 停止 ESP 传感器后端 ===")
    stopped = False

    # 1) 先停守护进程，否则它会马上把后端拉回来
    if os.path.isfile(PIDFILE):
        try:
            with open(PIDFILE) as f:
                pid = int(f.read().strip())
            if kill(pid, "守护进程"):
                stopped = True
            os.remove(PIDFILE)
        except (ValueError, OSError):
            print("  PID 文件无效或已不存在，跳过")
    else:
        print("  没有 PID 文件（守护进程可能没在跑）")

    # 2) 再停真正监听 8000 的进程（兜底：手动起的、或守护残留的）
    import subprocess
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    pids = set()
    for line in out.splitlines():
        if ":8000" in line and "LISTENING" in line:
            parts = line.split()
            if parts and parts[-1].isdigit():
                pids.add(parts[-1])
    for pid in pids:
        if kill(pid, "监听 8000 的进程"):
            stopped = True

    if not stopped:
        print("  没有发现正在运行的后端（可能本来就没起）")
    print("\n完成。要重新常驻启动，双击启动文件夹里的 ESP_SensorBackend_Autostart.cmd，")
    print("或直接运行: python tools\\run_backend_forever.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())