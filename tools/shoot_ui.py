# -*- coding: utf-8 -*-
"""
用 Edge/Chrome 的 CDP 协议给网页截图（文档用，不参与业务链路）。

为什么不用 --screenshot：语言助手面板默认是收起的，
需要先点开、选中设备、发一句话，才拍得到"有内容的界面"。

用法（后端在 8000 端口已在线）：
    D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\shoot_ui.py
        [--out docs/img/page_week4.png] [--query "查看上次数据"] [--device <id>]

注意：截图里显示的都是后端真实返回的内容，不做任何前端伪造。
"""
import argparse
import asyncio
import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.request

import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333


def _targets():
    with urllib.request.urlopen("http://127.0.0.1:%d/json" % PORT, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


class CDP(object):
    def __init__(self, ws):
        self.ws = ws
        self.n = 0

    async def call(self, method, **params):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})

    async def js(self, expr):
        r = await self.call("Runtime.evaluate", expression=expr,
                            awaitPromise=True, returnByValue=True)
        return r.get("result", {}).get("value")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/")
    ap.add_argument("--out", default="docs/img/page_week4.png")
    ap.add_argument("--query", default="查看上次数据")
    ap.add_argument("--device", default="")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=1050)
    ap.add_argument("--no-crop", action="store_true",
                    help="不裁到语言助手面板，直接拍整页顶部（做封面图用）")
    ap.add_argument("--target", choices=("nl", "snap"), default="nl",
                    help="拍哪一块：nl=语言助手面板（第 4~6 周）；snap=按需采集面板（第 7 周）")
    ap.add_argument("--replay", action="store_true",
                    help="配合 --target snap：改成点「回放一张历史图像」，拍回放路径")
    args = ap.parse_args()

    profile = tempfile.mkdtemp(prefix="edge_shot_")
    proc = subprocess.Popen([
        EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--remote-debugging-port=%d" % PORT,
        "--user-data-dir=" + profile,
        "--window-size=%d,%d" % (args.width, args.height),
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        # 等调试端口起来
        for _ in range(40):
            try:
                tgt = [t for t in _targets() if t.get("type") == "page"]
                if tgt:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        else:
            raise SystemExit("调试端口没起来")

        async with websockets.connect(tgt[0]["webSocketDebuggerUrl"],
                                      max_size=32 * 1024 * 1024) as ws:
            cdp = CDP(ws)
            await cdp.call("Page.enable")
            await cdp.call("Runtime.enable")
            await cdp.call("Emulation.setDeviceMetricsOverride",
                           width=args.width, height=args.height,
                           deviceScaleFactor=1.5, mobile=False)
            await cdp.call("Page.navigate", url=args.url)
            await asyncio.sleep(4.0)

            # 1) 选中设备（避免"全部设备"导致的歧义反问）
            if args.device:
                ok = await cdp.js(
                    "(function(){var s=document.getElementById('deviceSelect');"
                    "if(!s) return 'no-select';"
                    "var hit=[...s.options].filter(o=>o.value==='%s');"
                    "if(!hit.length) return 'no-option';"
                    "s.value='%s'; s.dispatchEvent(new Event('change'));"
                    "return 'ok';})()" % (args.device, args.device))
                print("选择设备:", ok, args.device)
                await asyncio.sleep(1.2)

            # 2) 打开目标面板
            if args.target == "snap":
                # 第 7 周：先展开相机抽屉，再真的点一次「看一次现场」，等它走完整个链路
                await cdp.js("(function(){var b=document.getElementById('btnCamera');"
                             "if(b) b.click(); return !!b;})()")
                await asyncio.sleep(1.0)
                if args.replay:
                    # 回放路径：拿最近一张历史照片当"过去的画面"，故意标成 replayed
                    await cdp.js("(function(){var b=document.getElementById('snapReplay');"
                                 "if(b) b.click(); return !!b;})()")
                    await asyncio.sleep(2.5)
                else:
                    await cdp.js("(function(){var b=document.getElementById('snapAsk');"
                                 "if(b) b.click(); return !!b;})()")
                    for _ in range(60):          # 最多等 30s（真机 UXGA 模式约 9s）
                        await asyncio.sleep(0.5)
                        st = await cdp.js("(function(){var e=document.getElementById('snapState');"
                                          "return e ? e.textContent.trim() : '';})()")
                        if st and ("成功" in st or "失败" in st or "超时" in st or "遮挡" in st
                                   or "不可用" in st):
                            break
                    await asyncio.sleep(1.0)
            else:
                await cdp.js("(function(){var b=document.getElementById('btnNl');"
                             "if(b) b.click(); return !!b;})()")
                await asyncio.sleep(0.8)

                # 3) 发一句话
                await cdp.js(
                    "(function(q){var i=document.getElementById('nlInput');"
                    "var b=document.getElementById('nlSend');"
                    "if(!i||!b) return 'no-input';"
                    "i.value=q; i.dispatchEvent(new Event('input')); b.click();"
                    "return 'sent';})(%s)" % json.dumps(args.query, ensure_ascii=False))
                # 等后端返回（含等设备回执的时间）
                for _ in range(30):
                    await asyncio.sleep(0.5)
                    txt = await cdp.js("(function(){var e=document.querySelector('#nlAnswer');"
                                       "return e ? e.textContent.trim().slice(0,40) : '';})()")
                    if txt:
                        break
                await asyncio.sleep(1.2)

            # 4) 把目标面板滚到视口顶部，再把视口缩到面板高度，裁出一张"面板特写"
            box_id = "snapBox" if args.target == "snap" else "nlPanel"
            rect = await cdp.js(
                "(function(){var p=document.getElementById('%s');"
                "if(!p) return null;"
                "p.scrollIntoView({block:'start'});"
                "var r=p.getBoundingClientRect();"
                "return {h:r.height};})()" % box_id)
            await asyncio.sleep(0.5)
            vh = args.height
            if args.no_crop:
                await cdp.js("window.scrollTo(0,0)")
                await asyncio.sleep(0.5)
                rect = None
            if rect and rect.get("h", 0) > 60:
                vh = int(min(args.height, rect["h"] + 24))
                await cdp.call("Emulation.setDeviceMetricsOverride",
                               width=args.width, height=vh,
                               deviceScaleFactor=1.5, mobile=False)
                await asyncio.sleep(0.6)
                await cdp.js("document.getElementById('%s')"
                             ".scrollIntoView({block:'start'});window.scrollBy(0,-96);" % box_id)
                await asyncio.sleep(0.4)
                # 视口变了会重新排版，面板高度可能跟着变 → 再量一次，取大者
                h2 = await cdp.js("document.getElementById('%s')"
                                  ".getBoundingClientRect().height" % box_id)
                need = int(min(args.height, max(vh, (h2 or 0) + 120)))
                if need != vh:
                    await cdp.call("Emulation.setDeviceMetricsOverride",
                                   width=args.width, height=need,
                                   deviceScaleFactor=1.5, mobile=False)
                    vh = need
                    await asyncio.sleep(0.6)
            shot = await cdp.call("Page.captureScreenshot", format="png",
                                  captureBeyondViewport=False)
            out = os.path.abspath(args.out)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(base64.b64decode(shot["data"]))
            print("已保存:", out)
            if args.target == "snap":
                print("状态徽标:", await cdp.js(
                    "(function(){var e=document.getElementById('snapState');"
                    "return e?e.textContent.trim():'(空)';})()"))
                print("说明:", (await cdp.js(
                    "(function(){var e=document.getElementById('snapExplain');"
                    "return e?e.textContent.trim():'(空)';})()"))[:300])
            else:
                print("回答片段:", (await cdp.js(
                    "(function(){var e=document.querySelector('#nlAnswer');"
                    "return e?e.textContent.trim():'(空)';})()"))[:200])
    finally:
        proc.terminate()


if __name__ == "__main__":
    asyncio.run(main())
