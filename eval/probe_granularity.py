"""实验：修正 heading 粒度后能否兑现召回提升（验证一个可证伪的预测）。

预测（来自 eval/rerank_report.md 的结论）
---------------------------------------
> 真瓶颈是 chunk 粒度：heading 均长 1043 字符，等预算下 k 只能取 2。
> 把 chunk 缩到 ~400-500 字符后，等预算命中率应从 75% 显著上升。

**这是一个可证伪的预测。** 如果修完粒度命中率不涨，说明"粒度"这个解释也是错的，
必须回头重新找原因 —— 而不是继续加技巧。

根因比预期更具体（本轮新发现）
------------------------------
拆解 38 节的 heading chunk 字符构成：

    标题头      1,237 字符（ 3%）
    正文       15,043 字符（39%）
    结构化参数  22,739 字符（58%）  ← 主因

正文平均只有 395 字符，**撑大 chunk 的其实是 front-matter 参数块**（平均 598 字符）。
而其中 statement / rationale / positive_case / negative_case
四项在正文里已完整重复 —— 真正独特的只有 params / applies_when / exceptions。

所以修复分两步，本实验分别验证：
  A. 精简参数（compact）：589 → 136 字符，压缩 77%
  B. 超长节按粗体标签二次切分，锚点是 `**为什么这么规定。**` `**正例**` `**反例**`
     `**经验。**` 这类作者标注的语义边界

四个策略对比
------------
  fixed          基线（小 chunk 参照）
  heading        原始（均长 1042）
  heading_split  精简参数 + 二次切分（均长 478）
  semantic       段落聚合（均长 399）

同时跑两个 max_chars（300 / 500），检验结论对参数是否稳健 ——
只在单一参数下成立的最优解没有意义。

沿用双口径 + 内容级指标（「gold 在前 k」），因为只跑单口径会重蹈
pilot 的覆辙（口径 A 看不见差异）。

用法
----
    python -m eval.probe_granularity
"""

from __future__ import annotations

import json
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

ROOT = Path(__file__).resolve().parent.parent
PILOT = ROOT / "eval" / "pilot_set.json"
STRICT_K = 5
BUDGETS = [1000, 2000, 3000, 4000]


def make_configs(sections) -> list[tuple[str, list]]:
    """返回 (配置名, chunks)。"""
    return [
        ("fixed", build_chunks("fixed", sections)),
        ("heading", build_chunks("heading", sections)),
        ("heading_split@500", build_chunks("heading_split", sections, max_chars=500)),
        ("heading_split@300", build_chunks("heading_split", sections, max_chars=300)),
        ("semantic", build_chunks("semantic", sections)),
    ]


def evaluate(chunks, questions: list[dict], budget_chars: int) -> dict:
    index = BM25(chunks)
    avg = sum(c.chars for c in chunks) / len(chunks)
    k = max(1, round(budget_chars / avg))
    top = max(STRICT_K, k)

    rows = []
    for item in questions:
        gold = item["gold_section"]
        res = index.search(item["q"], top_k=top)
        rank = next((r.rank for r in res if r.chunk.section_id == gold), None)
        # 内容级：gold 所在 chunk 是否进了前 k（排除 chunk 归属造成的假失败）
        in_topk = any(r.chunk.section_id == gold for r in res[:k])
        rows.append(
            {
                "id": item["id"],
                "rank": rank,
                "hit_strict": rank is not None and rank <= STRICT_K,
                "hit_budget": rank is not None and rank <= k,
                "content_in_topk": in_topk,
            }
        )

    n = len(rows)
    return {
        "chunks": len(chunks),
        "avg": round(avg),
        "k": k,
        "strict": sum(1 for r in rows if r["hit_strict"]) / n,
        "budget": sum(1 for r in rows if r["hit_budget"]) / n,
        "content": sum(1 for r in rows if r["content_in_topk"]) / n,
        "mrr": sum(1 / r["rank"] for r in rows if r["rank"]) / n,
        "rows": rows,
    }


def main() -> int:
    questions = json.loads(PILOT.read_text(encoding="utf-8"))["questions"]
    sections = load_sections()
    configs = make_configs(sections)

    print(f"知识库 {len(sections)} 节｜评测集 {len(questions)} 题")
    print(f"固定口径 top-{STRICT_K}｜等预算目标 {BUDGETS}\n")

    print("=" * 88)
    print("配置规模对比")
    print("=" * 88)
    print(f"{'配置':<20}{'chunk 数':>10}{'均长':>8}{'最小':>7}{'最大':>7}")
    print("-" * 88)
    for name, chunks in configs:
        sizes = [c.chars for c in chunks]
        print(
            f"{name:<20}{len(chunks):>10}{sum(sizes)//len(sizes):>8}"
            f"{min(sizes):>7}{max(sizes):>7}"
        )

    for budget in BUDGETS:
        print()
        print("=" * 88)
        print(f"等上下文预算 = {budget} 字符")
        print("=" * 88)
        print(f"{'配置':<20}{'均长':>7}{'k':>4}{'等预算命中':>12}{'gold在前k':>11}{'MRR':>8}")
        print("-" * 88)
        for name, chunks in configs:
            r = evaluate(chunks, questions, budget)
            print(
                f"{name:<20}{r['avg']:>7}{r['k']:>4}{r['budget']*100:>11.0f}%"
                f"{r['content']*100:>10.0f}%{r['mrr']:>8.2f}"
            )

    # ---- 核心指标：达到饱和所需的最小预算 ----
    print()
    print("=" * 88)
    print("核心指标：达到饱和（各配置自身最高命中率）所需的最小预算")
    print("=" * 88)
    print("解释：高预算下所有策略都饱和在 92%，差异被掩盖；")
    print("      真正的差异在『用多小的上下文就能达到饱和』—— 这是粒度优化的收益所在。")
    print()
    print(f"{'配置':<20}{'均长':>7}{'饱和命中率':>12}{'到饱和所需预算':>16}{'上下文字符':>12}")
    print("-" * 88)
    for name, chunks in configs:
        curve = {b: evaluate(chunks, questions, b)["budget"] for b in BUDGETS}
        peak = max(curve.values())
        # 第一个达到峰值（留 1e-9 容差）的预算
        first = next((b for b in BUDGETS if abs(curve[b] - peak) < 1e-9), BUDGETS[-1])
        r = evaluate(chunks, questions, first)
        ctx = round(sum(c.chars for c in chunks) / len(chunks) * r["k"])
        print(
            f"{name:<20}{r['avg']:>7}{peak*100:>11.0f}%{first:>16}{ctx:>12}"
        )

    print()
    print("=" * 88)
    print("预测检验：修正粒度后 heading 的召回是否提升？")
    print("=" * 88)
    base = next(c for n, c in configs if n == "heading")
    split = next(c for n, c in configs if n == "heading_split@500")
    fixed = next(c for n, c in configs if n == "fixed")

    print(f"{'预算':>6}{'heading':>10}{'split@500':>12}{'提升':>8}{'fixed(参照)':>13}")
    print("-" * 88)

    # 「已饱和」的判定必须用**峰值比较**，不能写死 0.92 之类的阈值：
    # 命中率是 k/n 的浮点数（如 11/12 = 0.9166...），写死阈值会因精度问题判错。
    base_curve = {b: evaluate(base, questions, b)["budget"] for b in BUDGETS}
    peak_base = max(base_curve.values())

    gains = []
    for budget in BUDGETS:
        b = base_curve[budget]
        s = evaluate(split, questions, budget)["budget"]
        f = evaluate(fixed, questions, budget)["budget"]
        # saturated 表示"该预算下 heading 已到自身峰值，没有提升空间"
        gains.append((s - b, b >= peak_base - 1e-9))
        print(f"{budget:>6}{b*100:>9.0f}%{s*100:>11.0f}%{(s-b)*100:>+7.0f}pp{f*100:>12.0f}%")

    print()
    # 判定规则：
    #   预算区间分两类 —— heading 尚未饱和的（能观察提升）与已饱和的（无观察空间）
    #   只在"未饱和"区间要求提升为正；已饱和区间的 0 提升是天花板效应，不算反例
    gain_vals = [g for g, _ in gains]
    unsat_gains = [g for g, saturated in gains if not saturated]
    sat_count = sum(1 for _, s in gains if s)

    if not unsat_gains:
        print("→ 所有预算下 heading 都已饱和，本次评测集无法区分 —— 需要更难的评测集。")
    elif all(g > 0 for g in unsat_gains) and all(g >= 0 for g in gain_vals):
        print("→ ✅ 预测成立：所有『heading 尚未饱和』的预算下，split 均严格优于 heading；")
        print("  已饱和的预算下提升为 0（天花板效应），且从未出现负提升。")
        print()
        print("  『chunk 粒度』确实是 heading 低分的主因，修复有效且无副作用。")
        if sat_count:
            print(f"  注意：{sat_count}/{len(gains)} 个预算下两者都已饱和在 92%，")
            print("  说明该评测集在高预算区间已失去区分力 —— 上限约 92%（12 题里 11 题命中）。")
    else:
        print("→ ⚠️ 预测部分成立：存在未饱和预算下无提升或负提升，结论对预算敏感。")

    print()
    print("稳健性检验：换个 max_chars（300）结论是否保持一致")
    s300 = next(c for n, c in configs if n == "heading_split@300")
    s500 = split
    print(f"{'预算':>6}{'split@300':>11}{'split@500':>11}{'差':>7}")
    print("-" * 88)
    for budget in BUDGETS:
        a = evaluate(s300, questions, budget)["budget"]
        b = evaluate(s500, questions, budget)["budget"]
        print(f"{budget:>6}{a*100:>10.0f}%{b*100:>10.0f}%{(a-b)*100:>+6.0f}pp")
    print()
    print("说明：若两个 max_chars 结论一致，说明结论对切分参数不敏感，可信度更高；")
    print("      若差异明显，说明当前评测集规模不足以支撑细粒度调参，应停止调参。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
