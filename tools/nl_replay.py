#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nl_replay.py —— 用结构化样例回放「自然语言 → 意图」的解析结果

用途（对应第 4 周备用路径）：
  当授权语言服务不可用、或线上回答明显跑偏时，先跑这个脚本。
  它**不连数据库、不碰设备**，只把 tools/nl_cases.json 里的每句话喂给
  解析器，逐条对比 intent / time_range_s / limit，从而把问题定位到：
    · 解析层（这句话被理解错了）——改规则或提示词；
    · 工具层（意图对、但查询/采集结果不对）——跑 selftest_nl.py。

用法：
  D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\nl_replay.py
  D:\\ESP\\.venv\\Scripts\\python.exe D:\\ESP\\tools\\nl_replay.py --verbose
"""

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "backend"))

import nl_assistant  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=os.path.join(BASE, "tools", "nl_cases.json"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    with open(args.cases, encoding="utf-8") as f:
        data = json.load(f)
    cases = data.get("cases") or []

    fails = []
    print("=" * 78)
    print("第 4 周语言解析回放：%d 条结构化样例（不连数据库、不碰设备）" % len(cases))
    print("=" * 78)
    for c in cases:
        got = nl_assistant._rule_parse(c["q"])
        ok = got["intent"] == c["intent"]
        det = []
        if "time_range_s" in c:
            ok = ok and got.get("time_range_s") == c["time_range_s"]
            det.append("time_range_s=%s" % got.get("time_range_s"))
        if "limit" in c:
            ok = ok and got.get("limit") == c["limit"]
            det.append("limit=%s" % got.get("limit"))
        line = "  %s %s  「%s」→ %s" % (
            "OK  " if ok else "FAIL", c["id"], c["q"], got["intent"])
        if not ok:
            line += "（期望 %s）" % c["intent"]
        if det:
            line += "  [" + ", ".join(det) + "]"
        if c.get("why"):
            line += "  # " + c["why"]
        print(line)
        if args.verbose and got.get("notes"):
            print("        notes: " + "; ".join(got["notes"]))
        if not ok:
            fails.append(c["id"])

    print("-" * 78)
    if fails:
        print("解析不符 %d 条：%s" % (len(fails), ", ".join(fails)))
        print("提示：先确认该句是否属于产品能力范围；属于则修 RE_* 规则或 _LLM_SYSTEM。")
        return 1
    print("全部通过：%d 条意图解析符合预期" % len(cases))
    return 0


if __name__ == "__main__":
    sys.exit(main())
