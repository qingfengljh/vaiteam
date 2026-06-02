#!/usr/bin/env python3
"""无依赖冒烟：与 ai_leader._stage2_doc_incomplete_reason 保持同步，改规则时请同步改本脚本。

用法：python3 scripts/smoke_stage2_doc_guard.py
"""
from __future__ import annotations

import re
import sys


def incomplete_reason(text: str | None) -> str | None:
    if not text or not text.strip():
        return "正文为空或仅空白"
    t = text.strip()
    if not re.search(r"^##\s*4[.\s、]", t, re.MULTILINE):
        return "缺少「## 4. 核心交互流程」独立章节（二级标题）"
    low = t.lower()
    if "```json" not in low:
        return "缺少文末 UI Spec 的 ```json 代码块起始标记"
    idx = low.find("```json")
    after = t[idx + 7 :]
    if not re.search(r"\n```\s*(\n|$)", after):
        return "UI Spec ```json 代码块未正常闭合（缺尾部 ```），或 JSON 被截断"
    return None


def main() -> int:
    cases: list[tuple[str, str | None, str]] = [
        ("缺第4章", "# T\n```json\n{}\n```\n", "缺少"),
        ("缺 json", "# T\n## 4. 流程\n", "缺少"),
        ("json 未闭合", "# T\n## 4. 流程\n```json\n{\"a\":1", "未正常闭合"),
        ("完整", "# T\n## 4. 核心交互流程\n\n```json\n{}\n```\n", "OK"),
    ]
    failed = 0
    for name, body, expect in cases:
        r = incomplete_reason(body)
        if expect == "OK":
            ok = r is None
        else:
            ok = r is not None and expect in r
        if not ok:
            print(f"FAIL {name}: got {r!r}", file=sys.stderr)
            failed += 1
        else:
            print(f"ok  {name}")
    if failed:
        print(f"\n{failed} case(s) failed", file=sys.stderr)
        return 1
    print("\n全部通过。端到端请在部署 dispatcher 后于工作台再生成一次 Stage2 文档，并看日志 finish=length / Stage2 doc incomplete fixup。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
