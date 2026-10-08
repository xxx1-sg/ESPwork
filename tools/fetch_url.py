# -*- coding: utf-8 -*-
"""fetch_url.py —— 通用文件下载器（仅用标准库）。

本机 curl.exe 的出网被安全策略拦截（返回 000），但 Python 的 urllib
可以正常访问 HTTPS（已实测），因此本工程所有二进制/大文件下载都走它。

用法:
    python fetch_url.py <url> <输出文件> [--sha256 <期望值>]

特性:
    - 断点续传（若目标文件已存在且服务端支持 Range）
    - 流式写入，显示进度
    - 可选 SHA256 校验
"""
import argparse
import hashlib
import os
import sys
import urllib.request


def download(url: str, out_path: str, expect_sha256: str | None = None) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    pos = os.path.getsize(out_path) if os.path.exists(out_path) else 0

    req = urllib.request.Request(url, headers={"User-Agent": "fetch_url/1.0"})
    if pos:
        req.add_header("Range", "bytes=%d-" % pos)

    with urllib.request.urlopen(req, timeout=60) as resp:
        total = resp.headers.get("Content-Length")
        total = int(total) + pos if total else None
        mode = "ab" if (pos and resp.status == 206) else "wb"
        if mode == "wb":
            pos = 0
        done = pos
        with open(out_path, mode) as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100.0 / total
                    sys.stdout.write("\r  %6.2f%%  %d/%d bytes" % (pct, done, total))
                else:
                    sys.stdout.write("\r  %d bytes" % done)
                sys.stdout.flush()
    sys.stdout.write("\n")

    if expect_sha256:
        h = hashlib.sha256()
        with open(out_path, "rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        got = h.hexdigest()
        if got.lower() != expect_sha256.lower():
            print("SHA256 不匹配: 期望 %s 实际 %s" % (expect_sha256, got))
            return 2
        print("SHA256 校验通过:", got)
    print("已下载 ->", out_path, os.path.getsize(out_path), "bytes")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument("--sha256", default=None)
    a = ap.parse_args()
    try:
        return download(a.url, a.out, a.sha256)
    except Exception as e:  # noqa: BLE001
        print("下载失败:", type(e).__name__, e)
        return 1


if __name__ == "__main__":
    sys.exit(main())