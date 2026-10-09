# -*- coding: utf-8 -*-
"""
run_backend_forever.py —— 后端常驻守护进程（自带崩溃自愈 + 日志轮转）

为什么需要它：
  tools/start_system.ps1 只是"启动一次"，进程崩了就没了；而开发板是 7x24 在
  上报的，后端一挂数据就断。这个守护脚本负责：
    1) 拉起 backend/server.py；
    2) 进程意外退出（崩溃/被杀）→ 等几秒自动重拉，指数退避，避免疯狂重启；
    3) 把 stdout/stderr 追加写进 evidence/backend_run.log（带时间戳分隔）；
    4) 日志超过上限自动切一份 .1 备份，防止把磁盘写满；
    5) 端口已被占用时不再重复起一个（避免"两个后端抢同一个 sqlite"）。

用法（一般由"启动文件夹"里的 cmd 自动调用，无需手动执行）：
    D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\run_backend_forever.py

停止：
    python D:\\ESP\\tools\\stop_backend.py
"""

import os
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
SERVER = os.path.join(ROOT, "backend", "server.py")
LOG_DIR = os.path.join(ROOT, "evidence")
LOG = os.path.join(LOG_DIR, "backend_run.log")
GUARD_LOG = os.path.join(LOG_DIR, "backend_guard.log")
PIDFILE = os.path.join(LOG_DIR, "backend.pid")
LOCKFILE = os.path.join(LOG_DIR, "backend_guard.lock")

# 令牌与后端默认值保持一致（见 firmware/src/app_config.h）
ENV = dict(os.environ)
ENV.update({
    "SENSOR_HOST": "0.0.0.0",
    "SENSOR_PORT": "8000",
    "SENSOR_DB": os.path.join(ROOT, "backend", "data", "sensor.db"),
    "SENSOR_WEB_DIR": os.path.join(ROOT, "web"),
    "SENSOR_TOKEN": "esp32s3eye-demo-token-2026",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUNBUFFERED": "1",     # ★ 关键：不缓冲，日志才能实时看到
})

MAX_LOG_BYTES = 20 * 1024 * 1024   # 单个日志上限 20MB
RESTART_DELAY = 3                   # 首次重拉等待秒数
MAX_RESTART_DELAY = 60              # 退避上限


def log(msg):
    """守护进程自己的日志单独写一份文件。

    ★ 不能只写 stdout：守护是被"启动文件夹的 cmd"或后台方式拉起的，
      那些父进程一退出，stdout 就没了，出问题时完全看不到守护的状态
      （实测踩过这个坑：后端挂了、守护有没有重拉，日志里查不到）。
    """
    line = "[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(GUARD_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except Exception:
        pass


def rotate_log():
    """日志超过上限就切成 .1（只留一份备份）。"""
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > MAX_LOG_BYTES:
            bak = LOG + ".1"
            if os.path.exists(bak):
                os.remove(bak)
            os.rename(LOG, bak)
    except OSError:
        pass


def port_in_use(port=8000):
    """检测端口是否已被监听（用标准库，不依赖 netstat/权限）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def write_pidfile(pid):
    try:
        with open(PIDFILE, "w") as f:
            f.write(str(pid))
    except OSError:
        pass


def pid_alive(pid):
    """该 PID 是否还活着（用 tasklist，避免误信陈旧的锁文件）。"""
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                             capture_output=True, text=True)
        return str(pid) in out.stdout
    except Exception:
        return False


def acquire_single_instance_lock():
    """确保同时只有一个守护进程在跑。

    为什么需要：开机自启 + 用户手动双击，很容易起两个守护；两个守护会争抢
    同一个 sqlite 和 8000 端口，行为诡异（一个起来了、另一个疯狂重拉）。
    返回 True 表示拿到锁，可以继续。
    """
    if os.path.isfile(LOCKFILE):
        try:
            with open(LOCKFILE) as f:
                old = int(f.read().strip())
            if old != os.getpid() and pid_alive(old):
                log("已有守护进程在运行 (PID %d)，本进程退出，避免重复拉起" % old)
                return False
        except (ValueError, OSError):
            pass  # 锁文件损坏 → 当它不存在，覆盖掉
    try:
        with open(LOCKFILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    return True


def main():
    os.makedirs(LOG_DIR, exist_ok=True)

    if not os.path.isfile(PYTHON):
        log("找不到 Python: %s" % PYTHON)
        return 1
    if not os.path.isfile(SERVER):
        log("找不到后端: %s" % SERVER)
        return 1

    log("=" * 70)
    log("后端守护启动 (PID %d)" % os.getpid())
    trim_guard_log()

    if not acquire_single_instance_lock():
        return 0

    delay = RESTART_DELAY
    while True:
        if port_in_use(8000):
            # 已经有后端在跑（可能是上次残留下来的），不重复起
            log("端口 8000 已在监听，等待其释放（30s 后复查）...")
            time.sleep(30)
            continue

        rotate_log()
        log("-" * 70)
        log("拉起后端: %s" % SERVER)
        try:
            with open(LOG, "ab") as lf:
                lf.write(b"\n")
                p = subprocess.Popen(
                    [PYTHON, SERVER],
                    cwd=ROOT,
                    env=ENV,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                write_pidfile(p.pid)
                log("后端已启动, PID=%d" % p.pid)
                rc = p.wait()
        except Exception as e:
            log("启动后端失败: %r" % (e,))
            rc = -1

        # 退避：刚起来就跑挂的多半是配置错，别疯狂重启刷屏
        if rc == 0:
            log("后端正常退出 (rc=0)，5 秒后重启")
            delay = RESTART_DELAY
            time.sleep(delay)
        else:
            log("后端异常退出 (rc=%s)，%d 秒后自动重拉" % (rc, delay))
            time.sleep(delay)
            delay = min(delay * 2, MAX_RESTART_DELAY)


def trim_guard_log():
    """守护自己的日志也不会无限增长。"""
    try:
        if os.path.exists(GUARD_LOG) and os.path.getsize(GUARD_LOG) > 4 * 1024 * 1024:
            bak = GUARD_LOG + ".1"
            if os.path.exists(bak):
                os.remove(bak)
            os.replace(GUARD_LOG, bak)
    except OSError:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("守护被 Ctrl+C 终止")
        sys.exit(0)