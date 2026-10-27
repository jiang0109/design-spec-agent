"""诊断：为什么「通用规则 + 特定子对象」会被闸门拒掉。

背景
----
生成层评测（eval/generation_report.md）剩两处错误，都是同一类：

  G08  主按钮文字色使用 color-text-primary   期望 pass → 实际 uncertain
  G14  辅助注释文字使用 300 字重              期望 fail → 实际 uncertain

模型给出的理由是"检索到的内容仅涉及背景色/标题字重"。

三个可能的原因，必须区分清楚 —— 修错方向会白费功夫：

  A. 检索层根本没取到通用规则
     （BM25 top-10 里没有 COLOR-003 / TYPO-003）
  B. 检索取到了，但被 funnel 截断
     （只对 top-2 做相关性判定，通用规则排在第 3 名之后）
  C. 检索取到了、也被判定了，但判定器把它判为不适用
     （真正的"通用 vs 子对象"判定问题）

这个顺序很重要：A/B 是检索与截断问题（改参数/改排序），
C 才是判定问题（改提示词/改结构）。历史教训：确认根因之前不要动判定器。

用法
----
    python -m eval.probe_general_rule
"""

from __future__ import annotations

import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.chunking import build_chunks, load_sections  # noqa: E402
from src.relevance import score_relevance  # noqa: E402
from src.retrieve import BM25  # noqa: E402

# 两条出问题的查询，以及"本应适用"的通用规则
CASES = [
    (
        "G08",
        "主按钮文字颜色的取值是否必须使用语义令牌",
        "COLOR-003",
        "COLOR-003「色彩必须使用语义 token」应对所有颜色生效，包括文字色",
    ),
    (
        "G14",
        "辅助注释文字允许使用哪些字重",
        "TYPO-003",
        "TYPO-003「字重只用 400/500/600」应对所有文字生效，包括注释文字",
    ),
]


def main() -> int:
    sections = load_sections()
    chunks = build_chunks("heading_split", sections, max_chars=300)
    index = BM25(chunks)

    for label, query, gold, note in CASES:
        print("=" * 88)
        print(f"【{label}】{query}")
        print(f"  本应适用的通用规则：{gold} —— {note}")
        print("=" * 88)

        cands = index.search(query, top_k=10)
        print(f"  {'排名':<4}{'section':<14}{'BM25':>8}   相关性判定")
        print("  " + "-" * 76)

        gold_rank = None
        gold_rel = None
        for c in cands:
            rel, reason = score_relevance(query, c.chunk)
            mark = " ←gold（本应适用）" if c.chunk.section_id == gold else ""
            if c.chunk.section_id == gold:
                gold_rank = c.rank
                gold_rel = rel
            print(f"  {c.rank:<4}{c.chunk.section_id:<14}{c.score:>8.1f}   [{rel}] {reason}{mark}")

        print()
        # 定位原因
        if gold_rank is None:
            verdict = "A：检索层根本没取到该通用规则（不在 top-10）"
        elif gold_rank > 2:
            verdict = (
                f"B：取到了但被 funnel 截断 —— gold 排第 {gold_rank}，"
                f"而只对 top-2 做判定"
            )
        elif gold_rel is not None and gold_rel < 2:
            verdict = f"C：判定了但判为不适用 —— gold 排第 {gold_rank}，relevance={gold_rel}"
        else:
            verdict = f"? gold 排第 {gold_rank}、relevance={gold_rel}，本应通过"

        print(f"  → 根因 {verdict}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
