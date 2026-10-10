"""解析器自测（不依赖 kb/ 内容，验证结构化通道能被正确提取）。

为什么单独写这个：Week 1 的教训是"先验证指标，再相信指标"。
解析器是评测集的 ground truth 来源，它错了后面全错。
这里用手写的最小样本做断言，确保解析行为符合预期。
"""

from __future__ import annotations

import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.validate_kb import _items, _parse_front_matter  # noqa: E402

SAMPLE = """---
doc_id: demo
title: 演示规范
version: 1.0
scope: 用于自测

rules:
  - id: DEMO-001
    title: 第一条
    statement: >
      这是一个跨行的规则陈述。
    params:
      allowed: [1, 2, 3]
      mapping:
        a: 1
        b: 2
    applies_when:
      - 场景一
    exceptions:
      - 无
    positive_case:
      desc: 正例描述
      value: 1
      expected_verdict: pass
    negative_case:
      desc: 反例描述
      value: 9
      expected_verdict: fail
    rationale: >
      意图说明。

  - id: DEMO-002
    title: 第二条
    statement: 单行陈述。
    params:
      range: 1-2
    applies_when:
      - 场景二
    exceptions:
      - 特殊情况例外
    positive_case:
      desc: 正例二
      value: 2
      expected_verdict: pass
    negative_case:
      desc: 反例二
      value: 5
      expected_verdict: fail
    rationale: >
      第二条的意图。
---

## 第一条

正文。

## 第二条

正文。
"""


def main() -> int:
    fm, body = _parse_front_matter(SAMPLE)
    items = _items(fm, "rules")

    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    check(len(items) == 2, f"应解析出 2 个条目，实际 {len(items)}")

    if len(items) == 2:
        a, b = items
        check(a.get("id") == "DEMO-001", f"条目1 id 错误：{a.get('id')!r}")
        check(a.get("title") == "第一条", f"条目1 title 错误：{a.get('title')!r}")
        check("跨行" in (a.get("statement") or ""), "条目1 statement 未正确拼接跨行内容")
        check("allowed" in (a.get("params") or ""), "条目1 params 未包含子键 allowed")
        check("场景一" in (a.get("applies_when") or ""), "条目1 applies_when 缺失")
        check("无" in (a.get("exceptions") or ""), "条目1 exceptions 缺失")
        check("正例描述" in (a.get("positive_case") or ""), "条目1 positive_case 缺失")
        check("反例描述" in (a.get("negative_case") or ""), "条目1 negative_case 缺失")
        check("意图说明" in (a.get("rationale") or ""), "条目1 rationale 缺失")

        check(b.get("id") == "DEMO-002", f"条目2 id 错误：{b.get('id')!r}")
        # 关键：条目2 的字段不能被条目1 的残留污染
        check("特殊情况例外" in (b.get("exceptions") or ""), "条目2 exceptions 内容错误")
        check("allowed" not in (b.get("params") or ""), "条目2 params 被条目1 的子键污染")
        check("场景二" in (b.get("applies_when") or ""), "条目2 applies_when 缺失")

    print(f"解析出 {len(items)} 个条目")
    for it in items:
        print(f"  {it.get('id')}  {it.get('title')}  (字段数 {len(it)})")

    if failures:
        print(f"\n❌ 解析器自测失败 {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\n✅ 解析器自测通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
