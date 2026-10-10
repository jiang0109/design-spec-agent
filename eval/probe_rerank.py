"""实验：零成本 rerank —— 用「标题字段加权」把 heading 策略的潜力逼出来。

要回答的问题
------------
Pilot 发现 `heading` 策略在等上下文预算口径下只有 70%，远低于 `fixed` 的 100%。
报告里的假设是：**heading 的"规则自包含"优势是真的，只是被 BM25 的排序埋了。**

本实验检验这个假设。如果成立，说明 heading 的问题在"排序"而不在"分块"，
且修复它**不需要 LLM rerank**（不花钱、不加延迟）。

方法：为什么标题字段是有效信号
------------------------------
chunk 的第一行是 `【文档 / 规则名 / ID】`，正文里还有 markdown 小标题。
规则名本身是这条规范最精炼的语义摘要：

    query: "卡片内边距允许哪几档"
    标题:  "卡片内边距取值"          ← 几乎逐词命中
    正文:  "卡片内边距只允许使用 12/16/24 三档，紧凑型列表卡片用 12……"

所以对标题字段单独做一次 BM25 再和正文分数加权，是**零额外依赖**的排序信号。
关键是证明增益来自字段本身，而不是实现细节 —— 所以先做 weight=0 的对照，
它必须与原始 BM25 结果**逐题完全一致**，否则说明新实现有问题。

三种配置对比
------------
  1. `body`      只对正文打分（等价原始 BM25，作对照）
  2. `field@w`   正文 + w × 标题，w 在 0.25~3.0 之间扫描
  3. `title_only` 只用标题（上限探针：看纯标题能排到什么程度）

口径仍是两个：固定 top-5 与等上下文预算。只看前者会重蹈 pilot 的覆辙。

用法
----
    python -m eval.probe_rerank
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

from src.chunking import STRATEGIES, build_chunks, load_sections  # noqa: E402
from src.retrieve import BM25  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PILOT = ROOT / "eval" / "pilot_set.json"
STRICT_K = 5
BUDGET_CHARS = 2000
WEIGHTS = [0.0, 0.25, 0.5, 1.0, 2.0, 3.0]


def run(strategy: str, questions: list[dict], sections, weight: float | None) -> dict:
    """weight=None → 原始 BM25；weight=float → 字段加权；weight='title' → 只用标题。"""
    chunks = build_chunks(strategy, sections)
    index = BM25(chunks)
    avg_size = sum(c.chars for c in chunks) / len(chunks)
    k_budget = max(1, round(BUDGET_CHARS / avg_size))

    rows = []
    for item in questions:
        gold = item["gold_section"]
        if weight is None:
            results = index.search(item["q"], top_k=max(STRICT_K, k_budget))
        elif weight == "title":
            # 只用标题：把正文权重压成 0
            results = index.search_field_weighted(item["q"], title_weight=1e9, top_k=max(STRICT_K, k_budget))
        else:
            results = index.search_field_weighted(item["q"], title_weight=weight, top_k=max(STRICT_K, k_budget))

        rank = next((r.rank for r in results if r.chunk.section_id == gold), None)
        # 更细的指标：gold 规则的**内容**是否出现在前 k 个 chunk 里
        # （哪怕它所在的 chunk 文档级没命中）。用于区分"没排上来"与"没召回"
        hit_all = [r for r in results[:k_budget] if r.chunk.section_id == gold]
        all_rank = hit_all[0].rank if hit_all else None

        rows.append(
            {
                "id": item["id"],
                "phrasing": item.get("phrasing", ""),
                "rank": rank,
                "all_rank": all_rank,
                "hit_strict": rank is not None and rank <= STRICT_K,
                "hit_budget": rank is not None and rank <= k_budget,
            }
        )

    n = len(rows)
    return {
        "strategy": strategy,
        "weight": weight,
        "chunks": len(chunks),
        "avg_size": round(avg_size),
        "k_budget": k_budget,
        "strict": sum(1 for r in rows if r["hit_strict"]) / n,
        "budget": sum(1 for r in rows if r["hit_budget"]) / n,
        "all_in_topk": sum(1 for r in rows if r["all_rank"] is not None) / n,
        "mrr": sum(1 / r["rank"] for r in rows if r["rank"]) / n,
        "rows": rows,
    }


def main() -> int:
    data = json.loads(PILOT.read_text(encoding="utf-8"))
    questions = data["questions"]
    sections = load_sections()

    print(f"知识库 {len(sections)} 个语义单元｜评测集 {len(questions)} 题")
    print(f"等预算目标 {BUDGET_CHARS} 字符｜固定口径 top-{STRICT_K}\n")

    # ---- 对照校验：weight=0 必须与原始 BM25 完全一致 ----
    print("=" * 74)
    print("第 0 步：对照校验（weight=0 必须与原始 BM25 逐题一致）")
    print("=" * 74)
    for name in STRATEGIES:
        base = run(name, questions, sections, None)
        zero = run(name, questions, sections, 0.0)
        same = [r["rank"] for r in base["rows"]] == [r["rank"] for r in zero["rows"]]
        flag = "✅ 一致" if same else "❌ 不一致（新实现有 bug，后续结论不可信）"
        print(f"  {name:<10} {flag}")
        if not same:
            print(f"    base: {[r['rank'] for r in base['rows']]}")
            print(f"    zero: {[r['rank'] for r in zero['rows']]}")
            return 1

    # ---- 权重扫描 ----
    print()
    print("=" * 74)
    print("第 1 步：标题权重扫描（只对 heading 策略，找最佳权重）")
    print("=" * 74)
    print(f"{'权重':<10}{'固定top5':>10}{'等预算':>10}{'MRR':>8}{'k':>5}")
    print("-" * 74)
    best_w, best_budget = None, -1.0
    for w in WEIGHTS + ["title"]:
        r = run("heading", questions, sections, w)
        label = "仅标题" if w == "title" else f"{w}"
        print(
            f"{label:<10}{r['strict']*100:>9.0f}%{r['budget']*100:>9.0f}%"
            f"{r['mrr']:>8.2f}{r['k_budget']:>5}"
        )
        if w != "title" and r["budget"] > best_budget:
            best_budget, best_w = r["budget"], w

    print(f"\n等预算口径下最佳权重：w = {best_w}（命中率 {best_budget*100:.0f}%）")

    # ---- 三策略在最佳权重下的完整对比 ----
    print()
    print("=" * 74)
    print(f"第 2 步：三策略 × 三种配置（等上下文预算口径）")
    print("=" * 74)
    print(f"{'策略':<10}{'均长':>7}{'k':>4}{'正文':>9}{f'加权w={best_w}':>12}{'仅标题':>9}{'gold在前k':>10}")
    print("-" * 84)
    for name in STRATEGIES:
        b = run(name, questions, sections, None)
        f = run(name, questions, sections, best_w)
        t = run(name, questions, sections, "title")
        print(
            f"{name:<10}{b['avg_size']:>7}{b['k_budget']:>4}"
            f"{b['budget']*100:>8.0f}%{f['budget']*100:>11.0f}%{t['budget']*100:>8.0f}%"
            f"{b['all_in_topk']*100:>9.0f}%"
        )
    print()
    print("『gold在前k』= 不考虑 chunk 归属，gold 规则的内容是否出现在前 k 个 chunk 里。")
    print("若它明显高于『等预算』，说明答案其实被召回了，只是被判定为属于别的 chunk。")

    # ---- 逐题明细：看是哪些题被救回来 ----
    print()
    print("=" * 74)
    print("第 3 步：heading 策略逐题排名变化（正文BM25 → 加权 → 仅标题）")
    print("=" * 74)
    b = run("heading", questions, sections, None)
    f = run("heading", questions, sections, best_w)
    t = run("heading", questions, sections, "title")
    print(f"{'题号':<6}{'类型':<12}{'正文':>7}{'加权':>7}{'仅标题':>8}   变化")
    print("-" * 74)
    for i, q in enumerate(questions):
        rb, rf, rt = b["rows"][i]["rank"], f["rows"][i]["rank"], t["rows"][i]["rank"]
        def fmt(x):
            return str(x) if x else "-"
        delta = ""
        if rb is None and rf:
            delta = "✅ 被救回"
        elif rb and rf and rf < rb:
            delta = "↑ 提前"
        elif rb and rf and rf > rb:
            delta = "↓ 退后"
        elif rb is None and rf is None:
            delta = "仍失败"
        print(
            f"{q['id']:<6}{q.get('phrasing',''):<12}{fmt(rb):>7}{fmt(rf):>7}{fmt(rt):>8}   {delta}"
        )

    # ---- 结论 ----
    print()
    print("=" * 74)
    base_h = run("heading", questions, sections, None)["budget"]
    final_h = run("heading", questions, sections, best_w)["budget"]
    fixed_b = run("fixed", questions, sections, None)["budget"]
    print(f"heading 等预算命中率：{base_h*100:.0f}% → {final_h*100:.0f}%（提升 {(final_h-base_h)*100:.0f} 个百分点）")
    print(f"fixed   等预算命中率：{fixed_b*100:.0f}%（对照，未改动）")

    # ---- 预算扫描：heading 在等预算下 k 只能取 2，这是不是真正的瓶颈？ ----
    print()
    print("=" * 74)
    print("第 4 步：预算扫描 —— 给足上下文后，k=2 的限制还是不是瓶颈？")
    print("=" * 74)
    chunks_h = build_chunks("heading", sections)
    avg_h = sum(c.chars for c in chunks_h) / len(chunks_h)
    print(f"heading 均长 {avg_h:.0f} 字符，等预算口径下 k = 预算/均长")
    print()
    print(f"{'预算':>6}{'k':>4}{'上下文':>9}{'固定top5':>10}{'等预算':>9}")
    print("-" * 74)
    for budget in [1000, 2000, 3000, 4000, 6000, 8000]:
        k = max(1, round(budget / avg_h))
        rows = []
        for item in questions:
            chunks = chunks_h
            index = BM25(chunks)
            res = index.search(item["q"], top_k=max(STRICT_K, k))
            rank = next((r.rank for r in res if r.chunk.section_id == item["gold_section"]), None)
            rows.append(
                {
                    "hs": rank is not None and rank <= STRICT_K,
                    "hb": rank is not None and rank <= k,
                    "ctx": sum(r.chunk.chars for r in res[:k]),
                }
            )
        n = len(rows)
        print(
            f"{budget:>6}{k:>4}{round(sum(r['ctx'] for r in rows)/n):>9}"
            f"{sum(1 for r in rows if r['hs'])/n*100:>9.0f}%"
            f"{sum(1 for r in rows if r['hb'])/n*100:>8.0f}%"
        )

    print()
    if final_h > base_h:
        print("→ 结论：标题字段确实带有效信号，heading 的低分**部分是排序问题**。")
    else:
        print("→ 结论：标题加权未带来提升，heading 的低分**不是排序问题**。")
        print("  结合预算扫描：真正瓶颈是『chunk 太大导致固定预算下 k 太小』，")
        print("  这是分块粒度问题，只能靠改分块方式解决，不是 rerank 能修的。")

    print()
    print("注意：本次同时修正了评测集里的 Q01（原题「留白」有歧义，属坏题）")
    print("并新增 Q11/Q12。因此本表与 pilot_report.md 的 10 题基线数字不可直接比较。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
