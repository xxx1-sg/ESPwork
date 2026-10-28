# -*- coding: utf-8 -*-
"""
用 Edge/Chrome 的 CDP 协议给「语音助手」面板截图（文档用，不参与业务链路）。

为什么单独一个脚本：`shoot_ui.py` 拍的是第 4 周的语言助手，操作的 DOM 完全不同。
语音面板默认收起，要先点开、选中设备、再点一条**历史记录**才会渲染六段状态；
直接 `--screenshot` 只能拍到空面板。

用法（后端 8000 在线 + 语音引擎 8013 在线 + 历史里至少有一条记录）：
    D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\shoot_voice.py
        [--out-prefix docs/img/voice_week5] [--device esp32s3eye-1C704C]

产出两张图：
    <prefix>_stages.png    六段状态 + 结果区（识别文本 / 意图 / 回答 / 来源 / 播放）
    <prefix>_compare.png   同一句话「语音入口 vs 文字入口」对照表

★ 纪律：截图里显示的每个数字、每句回答都来自后端真实返回，脚本不做任何前端伪造。
  唯一"人为"的动作是**点了哪一条历史记录**（等价于点鼠标）。
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
PORT = 9334


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
        res = r.get("result", {})
        if res.get("subtype") == "error":
            raise RuntimeError("JS 报错: %s" % (res.get("description") or res.get("value")))
        return res.get("value")


async def shoot(cdp, out, width, height, anchor_id="voicePanel", extra_scroll=96):
    """把某个元素滚到视口顶部、把视口裁到它的高度、截一张图。

    ★ 顺序很重要：改视口高度会重置滚动位置，所以**最后一次滚动必须放在
      改完视口之后**，否则页面顶部那条 sticky 头会把标题盖掉（第一版就踩了这个坑，
      截出来的图上标题只剩半行）。
    """
    rect = await cdp.js(
        "(function(){var p=document.getElementById('%s');"
        "if(!p) return null;"
        "p.scrollIntoView({block:'start'});"
        "var r=p.getBoundingClientRect();"
        "return {h:r.height};})()" % anchor_id)
    await asyncio.sleep(0.6)
    vh = height
    if rect and rect.get("h", 0) > 60:
        vh = int(min(height, rect["h"] + 24))
        await cdp.call("Emulation.setDeviceMetricsOverride",
                       width=width, height=vh, deviceScaleFactor=1.5, mobile=False)
        await asyncio.sleep(0.7)
        # 视口变了会重新排版 → 再量一次，取大者
        h2 = await cdp.js("document.getElementById('%s').getBoundingClientRect().height" % anchor_id)
        need = int(min(height, max(vh, (h2 or 0) + 120)))
        if need != vh:
            await cdp.call("Emulation.setDeviceMetricsOverride",
                           width=width, height=need, deviceScaleFactor=1.5, mobile=False)
            await asyncio.sleep(0.7)

    # ★ 最后的定位：先对齐，再往上留出 sticky 头的高度
    await cdp.js("var e=document.getElementById('%s');"
                 "e.scrollIntoView({block:'start'});window.scrollBy(0,-%d);"
                 % (anchor_id, extra_scroll))
    await asyncio.sleep(0.6)

    shot = await cdp.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
        f.write(base64.b64decode(shot["data"]))
    size = os.path.getsize(out)
    print("已保存: %s (%.0f KB)" % (out, size / 1024.0))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/")
    ap.add_argument("--out-prefix", default="docs/img/voice_week5")
    ap.add_argument("--device", default="")
    ap.add_argument("--turn", default="",
                    help="要拍的那一轮 turn_id（不给就点历史第一条；建议显式指定，否则"
                         "别的进程刚好产生新记录时首行会被挤掉，拍到一条不相干的轮次）")
    ap.add_argument("--width", type=int, default=1360)
    ap.add_argument("--height", type=int, default=1100)
    args = ap.parse_args()

    profile = tempfile.mkdtemp(prefix="edge_voice_")
    proc = subprocess.Popen([
        EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--remote-debugging-port=%d" % PORT,
        "--user-data-dir=" + profile,
        "--window-size=%d,%d" % (args.width, args.height),
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
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
                                      max_size=64 * 1024 * 1024) as ws:
            cdp = CDP(ws)
            await cdp.call("Page.enable")
            await cdp.call("Runtime.enable")
            await cdp.call("Emulation.setDeviceMetricsOverride",
                           width=args.width, height=args.height,
                           deviceScaleFactor=1.5, mobile=False)
            await cdp.call("Page.navigate", url=args.url)
            await asyncio.sleep(4.5)

            # 1) 选中设备（避免"全部设备"下的歧义反问）
            if args.device:
                ok = await cdp.js(
                    "(function(){var s=document.getElementById('deviceSelect');"
                    "if(!s) return 'no-select';"
                    "var hit=[...s.options].filter(o=>o.value==='%s');"
                    "if(!hit.length) return 'no-option';"
                    "s.value='%s'; s.dispatchEvent(new Event('change'));"
                    "return 'ok';})()" % (args.device, args.device))
                print("选择设备:", ok, args.device)
                await asyncio.sleep(1.5)

            # 2) 打开语音面板（等价于点工具栏那个按钮）
            opened = await cdp.js("(function(){var b=document.getElementById('btnVoice');"
                                  "if(!b) return 'no-btn'; b.click(); return 'clicked';})()")
            print("打开语音面板:", opened)
            await asyncio.sleep(2.0)

            # 3) 渲染某一轮：显式给了 turn_id 就直接载入（走的是页面自己的
            #    __voiceDebug.render，跟 voiceShowTurn 内部调用的是同一个函数）；
            #    没给就点历史第一条，走完整鼠标路径。
            if args.turn:
                got = await cdp.js(
                    "(async function(){var r=await fetch('/api/v1/voice/turn/%s',{cache:'no-store'});"
                    "var j=await r.json();"
                    "if(!j.ok) return 'fail';"
                    "window.__voiceDebug.render(j.data, true);"
                    "return j.data.turn_id;})()" % args.turn)
                print("载入 turn:", got)
                if got == "fail":
                    raise SystemExit("找不到这一轮：%s" % args.turn)
                await asyncio.sleep(2.5)
            else:
                rowtxt = ""
                for _ in range(24):
                    await asyncio.sleep(0.5)
                    rowtxt = await cdp.js(
                        "(function(){var r=document.querySelector('#voiceHistory .voice-hist-row');"
                        "return r ? r.textContent.trim() : '';})()")
                    if rowtxt:
                        break
                print("历史首行:", (rowtxt or "(空)")[:80])
                if not rowtxt:
                    raise SystemExit("历史列表为空：先跑一轮 tools/voice_probe.py --say \"查看上次数据\"")

                clicked = await cdp.js(
                    "(function(){var r=document.querySelector('#voiceHistory .voice-hist-row');"
                    "if(!r) return 'no-row'; r.click(); return 'clicked';})()")
                print("点击历史行:", clicked)
                await asyncio.sleep(3.0)

            shown = await cdp.js("(function(){var e=document.getElementById('voiceTranscript');"
                                 "return e ? e.textContent.trim() : '';})()")
            print("面板识别文本:", (shown or "(空)")[:40])

            stages = await cdp.js(
                "(function(){var o=[];document.querySelectorAll('#voiceStages li').forEach(function(li){"
                "o.push((li.getAttribute('data-k')||'')+':'+li.className);});return o.join(' | ');})()")
            print("六段状态:", stages)

            # 4) 第一张：六段 + 结果区
            await shoot(cdp, args.out_prefix + "_stages.png", args.width, args.height)

            # 5) 第二张：触发两入口对照（真的再发一次文字请求），拍对照表
            cmp_ok = await cdp.js("(function(){"
                                  "if(!window.__voiceDebug||!window.__voiceDebug.compare) return 'no-api';"
                                  "window.__voiceDebug.compare(); return 'sent';})()")
            print("触发两入口对照:", cmp_ok)
            for _ in range(30):
                await asyncio.sleep(0.5)
                txt = await cdp.js("(function(){var e=document.getElementById('voiceCompareOut');"
                                   "return e ? e.textContent.trim() : '';})()")
                if txt and "正在把" not in txt:
                    break
            await asyncio.sleep(1.0)
            await shoot(cdp, args.out_prefix + "_compare.png", args.width, args.height,
                        anchor_id="voiceCompare", extra_scroll=132)

            print("回答片段:",
                  (await cdp.js("(function(){var e=document.getElementById('voiceAnswer');"
                                "return e?e.textContent.trim():'(空)';})()"))[:160])
    finally:
        proc.terminate()


if __name__ == "__main__":
    asyncio.run(main())
