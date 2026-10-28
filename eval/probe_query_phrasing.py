"""诊断：查询措辞与检索方式，哪个能让正确规则排进前列。

背景
----
`query_format_report.md` 显示：把查询改成「对象+属性+取值」后，
预设框架措辞归零、漏报归零，但准确率 96%→95%，多出 4 处过度拒答。

初步归因是"查询变短 → BM25 召回变差"。但**这个归因没有验证**：
可能是措辞问题（缺少检索特征），也可能是 BM25 本身排不上来（需要换检索方式）。

本脚本**完全离线**（只做 BM25 排序，不调 API），对照两类因素：

  因素 A · 查询措辞：同一意图的 4 种写法，看 gold 排到第几
  因素 B · 检索方式：普通 BM25 vs 标题字段加权（w=1/2）

这样能在改提示词之前先确定：
  - 如果某个措辞能让 gold 稳定进 top-3 → 改措辞即可
  - 如果所有措辞都不行、但标题加权能救 → 该改检索方式
  - 如果都不行 → 是索引/分块问题，别再折腾措辞

用法
----
    python -m eval.probe_query_phrasing
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
from src.retrieve import BM25  # noqa: E402

# (标签, gold, [V0 当前实际查询, V1 去掉冗余修饰, V2 更短, V3 最短/属性优先])
CASES = [
    (
        "E030",
        "TYPO-001",
        [
            "文章正文文字字号 14px",      # V0 当前（含"文字"，与对比度规则冲突）
            "文章正文字号 14px",          # V1 去掉"文字"
            "正文字号 14px",              # V2 只留对象+属性+取值
            "字号 14px",                  # V3 属性优先
        ],
    ),
    (
        "E037",
        "TYPO-002",
        [
            "区块标题行高 34px",
            "区块标题 行高 34px",
            "标题行高 34px",
            "行高 34px",
        ],
    ),
    (
        "E013",
        "SPACING-004",
        [
            "内容区最大宽度 1440px",
            "页面内容区最大宽度 1440px",
            "内容区最大宽度",
            "最大宽度 1440px",
        ],
    ),
    (
        "E024",
        "COLOR-003",
        [
            "分隔线颜色引用 gray-300 色阶变量",
            "分隔线 颜色引用 gray-300",
            "颜色引用 gray-300",
            "颜色引用 禁止色阶变量 gray-300",
        ],
    ),
    # 对照组：v5 里本来通过的，确认改措辞不会把它们弄坏
    (
        "N1(通过)",
        "SPACING-001",
        [
            "卡片内边距 20px",
            "卡片 内边距 20px",
            "内边距 20px",
            "卡片内边距允许的档位 20px",
        ],
    ),
    (
        "N2(通过)",
        "COLOR-001",
        [
            "正文文字对比度 4.1:1",
            "正文 文字对比度 4.1:1",
            "文字对比度 4.1:1",
            "对比度 4.1:1",
        ],
    ),
]

VARIANTS = ["V0 当前", "V1 去修饰", "V2 更短", "V3 属性优先"]


def rank_of(index: BM25, query: str, gold: str, top: int = 20) -> int | None:
    for h in index.search(query, top_k=top):
        if h.chunk.section_id == gold:
            return h.rank
    return None


def rank_field(index: BM25, query: str, gold: str, w: float, top: int = 20) -> int | None:
    for h in index.search_field_weighted(query, title_weight=w, top_k=top):
        if h.chunk.section_id == gold:
            return h.rank
    return None


def main() -> int:
    sections = load_sections()
    chunks = build_chunks("heading_split", sections, max_chars=300)
    index = BM25(chunks)

    print(f"chunk 数 {len(chunks)}｜完全离线（仅 BM25 排序，无 API 调用）\n")
    print("=" * 96)
    print("因素 A · 查询措辞：gold 规则排到第几（- 表示 20 名内没有）")
    print("=" * 96)
    print(f"{'案例':<10}{'gold':<12}" + "".join(f"{v:>14}" for v in VARIANTS))
    print("-" * 96)
    for label, gold, queries in CASES:
        marks = []
        for q in queries:
            r = rank_of(index, q, gold)
            marks.append(str(r) if r else "-")
        print(f"{label:<10}{gold:<12}" + "".join(f"{m:>14}" for m in marks))

    print()
    print("=" * 96)
    print("因素 B · 检索方式：同一批 V0 查询，换成标题字段加权后的排名")
    print("=" * 96)
    print(f"{'案例':<10}{'gold':<12}{'普通BM25':>12}{'title w=1':>12}{'title w=2':>12}{'title w=3':>12}")
    print("-" * 96)
    for label, gold, queries in CASES:
        q = queries[0]
        row = [rank_of(index, q, gold)]
        row += [rank_field(index, q, gold, w) for w in (1.0, 2.0, 3.0)]
        print(f"{label:<10}{gold:<12}" + "".join(f"{(str(r) if r else '-'):>12}" for r in row))

    print()
    print("=" * 96)
    print("因素 C · 最优组合：V1/V2 措辞 + 标题加权")
    print("=" * 96)
    print(f"{'案例':<10}{'gold':<12}{'V1':>8}{'V1+w2':>9}{'V2':>8}{'V2+w2':>9}")
    print("-" * 96)
    for label, gold, queries in CASES:
        v1, v2 = queries[1], queries[2]
        r1, r2 = rank_of(index, v1, gold), rank_of(index, v2, gold)
        r1w = rank_field(index, v1, gold, 2.0)
        r2w = rank_field(index, v2, gold, 2.0)
        print(f"{label:<10}{gold:<12}{(str(r1) if r1 else '-'):>8}{(str(r1w) if r1w else '-'):>9}"
              f"{(str(r2) if r2 else '-'):>8}{(str(r2w) if r2w else '-'):>9}")

    print()
    print("=" * 96)
    print("解读指引")
    print("=" * 96)
    print("· 若某列在全部案例上都 <=3 → 该措辞/检索方式足够，可以据此改提示词")
    print("· 若所有措辞都排不上、但标题加权能救 → 瓶颈在检索方式而非措辞")
    print("· 若所有组合都不行 → 问题在索引或分块，不要再调措辞")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
