"""Pilot 评测：判断当前知识库规模能否区分分块策略。

目的（先明确，否则数字会被误读）
--------------------------------
这不是在选最优策略，而是在回答一个问题：
**当前 38 条 / 4 万字符的知识库，是否已经能让分块策略产生可观测的差异？**

如果所有策略召回都接近 100%，说明语料规模不足、评测集区分度不够 ——
那时正确的动作是扩充知识库，而不是继续调分块参数。

两个必须同时看的口径
--------------------
各策略的 chunk 大小差异很大（实测 heading 平均 1042 字符，fixed 只有 301），
所以只看"固定 top-5"是不公平的：heading 一次塞进 5000+ 字符，fixed 才 1500。
因此同时报告：

  口径 A  strict@5   固定取 top-5，比"同样的条数谁更准"
  口径 B  budget     按各策略平均 chunk 大小反推 k，使**总字符数对齐**，
                     比"同样的上下文预算谁更准" —— 这个才是真实上线时的口径

只报 A 会高估大 chunk 策略，只报 B 会掩盖排序质量，两个都要看。

指标定义
--------
  hit@k      前 k 个结果里，是否存在 gold_section 对应的 chunk（文档级命中）
  strict@k   与 hit@k 相同，此处单列以强调它是固定 k
  budget@k   预算对齐后各策略各自的 k 值下的命中率
  MRR        第一个命中结果的排名倒数均值，反映排序质量
  ctx_chars  平均检索回来的上下文字符数（成本代理指标）

用法
----
    python -m eval.run_pilot
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
BUDGET_CHARS = 2000  # 等预算口径的目标上下文长度


def evaluate(strategy: str, questions: list[dict], sections) -> dict:
    chunks = build_chunks(strategy, sections)
    index = BM25(chunks)
    sizes = [c.chars for c in chunks]
    avg_size = sum(sizes) / len(sizes)

    # 预算对齐：让 k 满足 k * avg_size ≈ BUDGET_CHARS
    k_budget = max(1, round(BUDGET_CHARS / avg_size))

    rows = []
    for item in questions:
        gold = item["gold_section"]
        results = index.search(item["q"], top_k=max(STRICT_K, k_budget))

        rank = None
        for r in results:
            if r.chunk.section_id == gold:
                rank = r.rank
                break

        ctx_strict = sum(r.chunk.chars for r in results[:STRICT_K])
        ctx_budget = sum(r.chunk.chars for r in results[:k_budget])

        rows.append(
            {
                "id": item["id"],
                "q": item["q"],
                "gold": gold,
                "rank": rank,  # None = 未命中
                "top1": (results[0].chunk.section_id if results else ""),
                "hit_strict": rank is not None and rank <= STRICT_K,
                "hit_budget": rank is not None and rank <= k_budget,
                "ctx_strict": ctx_strict,
                "ctx_budget": ctx_budget,
            }
        )

    n = len(rows)
    hits_s = sum(1 for r in rows if r["hit_strict"])
    hits_b = sum(1 for r in rows if r["hit_budget"])
    mrr = sum((1 / r["rank"]) for r in rows if r["rank"]) / n

    return {
        "strategy": strategy,
        "chunks": len(chunks),
        "avg_size": round(avg_size),
        "k_budget": k_budget,
        "strict_rate": hits_s / n,
        "budget_rate": hits_b / n,
        "mrr": mrr,
        "ctx_strict": round(sum(r["ctx_strict"] for r in rows) / n),
        "ctx_budget": round(sum(r["ctx_budget"] for r in rows) / n),
        "misses": [r for r in rows if not r["hit_strict"]],
        "rows": rows,
    }


def main() -> int:
    data = json.loads(PILOT.read_text(encoding="utf-8"))
    questions = data["questions"]
    sections = load_sections()

    print(f"知识库：{len(sections)} 个语义单元")
    print(f"评测集：{PILOT.name}，{len(questions)} 道题\n")

    # 先验证 gold 是否真的存在（否则评测无意义）
    known = {s.section_id for s in sections}
    bad = [q["id"] for q in questions if q["gold_section"] not in known]
    if bad:
        print(f"❌ 以下题目的 gold_section 在知识库中不存在：{bad}")
        print("   评测集本身有错，先修它再跑。")
        return 1
    print(f"✅ gold 校验通过：{len(questions)} 道题的答案都在知识库内\n")

    results = [evaluate(name, questions, sections) for name in STRATEGIES]

    print("=" * 78)
    print(f"口径 A：固定 top-{STRICT_K}（比条数）")
    print("=" * 78)
    print(f"{'策略':<10}{'chunk数':>8}{'均长':>7}{'命中率':>8}{'MRR':>8}{'上下文':>9}")
    print("-" * 78)
    for r in results:
        print(
            f"{r['strategy']:<10}{r['chunks']:>8}{r['avg_size']:>7}"
            f"{r['strict_rate']*100:>7.0f}%{r['mrr']:>8.2f}{r['ctx_strict']:>9}"
        )

    print()
    print("=" * 78)
    print(f"口径 B：等上下文预算（约 {BUDGET_CHARS} 字符，比真实成本下的准确率）")
    print("=" * 78)
    print(f"{'策略':<10}{'均长':>7}{'k':>4}{'命中率':>8}{'上下文':>9}")
    print("-" * 78)
    for r in results:
        print(
            f"{r['strategy']:<10}{r['avg_size']:>7}{r['k_budget']:>4}"
            f"{r['budget_rate']*100:>7.0f}%{r['ctx_budget']:>9}"
        )

    # 逐题明细：能看出是"题目太难"还是"策略不行"
    print()
    print("=" * 78)
    print("逐题命中排名（数字=gold 出现在第几位；- = top-5 未命中）")
    print("=" * 78)
    header = f"{'题号':<6}{'类型':<12}" + "".join(f"{r['strategy']:>10}" for r in results)
    print(header)
    print("-" * 78)
    for i, q in enumerate(questions):
        row = f"{q['id']:<6}{q.get('phrasing',''):<12}"
        for r in results:
            rk = r["rows"][i]["rank"]
            row += f"{(str(rk) if rk else '-'):>10}"
        print(row)

    # 结论判定：必须同时看两个口径
    print()
    print("=" * 78)
    rates_a = {r["strategy"]: r["strict_rate"] for r in results}
    rates_b = {r["strategy"]: r["budget_rate"] for r in results}
    spread_a = max(rates_a.values()) - min(rates_a.values())
    spread_b = max(rates_b.values()) - min(rates_b.values())

    print(f"口径 A 命中率区间：{min(rates_a.values())*100:.0f}% ~ {max(rates_a.values())*100:.0f}%"
          f"（极差 {spread_a*100:.0f} 个百分点）")
    print(f"口径 B 命中率区间：{min(rates_b.values())*100:.0f}% ~ {max(rates_b.values())*100:.0f}%"
          f"（极差 {spread_b*100:.0f} 个百分点）")
    print()

    if spread_a < 0.10 and spread_b < 0.10:
        print("→ 结论：两个口径都无差异。当前语料不足，应先扩充知识库再谈分块。")
    elif spread_a < 0.10 <= spread_b:
        print("→ 结论：**口径 A 无差异，但口径 B 差异显著。**")
        print("  说明：当前语料已有区分度，但只有把上下文预算对齐才看得出来。")
        print("  单看『固定 top-k』会得出错误的『无差异』结论 —— ")
        print("  这正是本次 pilot 最重要的发现：**指标口径决定了你能否看见差异。**")
    else:
        print("→ 结论：两个口径都出现可观测差异，评测集有效，可继续做策略对比。")

    print()
    print("备注：口径 B 下 heading 掉分，是因为它单个 chunk 就吃掉大半预算")
    print("（均长 1043 vs fixed 301），k 只能取 2。它的优势是『规则自包含完整』，")
    print("但这一优势需要 rerank 把正确的那 1-2 个 chunk 排进来才能兑现。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
