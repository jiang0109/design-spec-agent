"""正式检索评测：30 题分层评测集 × 多分块配置 × 双口径。

与 pilot 的区别
---------------
`run_pilot.py` 用的 10 题集区分力已耗尽（12 题里 11 题命中 = 92% 是天花板）。
本脚本改用 `eval/retrieval_set.json`（30 题，分层设计），并新增两项：
  1. **按难度层拆分统计** —— 看出配置差异具体集中在哪一类题上
  2. **拒答检验** —— 对 3 道知识库无覆盖的题，检查检索得分是否与可答题可分

拒答检验为什么重要
------------------
Week 1 已验证过生成层的反幻觉能力（无依据时判 uncertain）。
检索层面对应的问题是：**能不能召回到"看起来相关"但实际答不了问题的内容？**
如果"表格行内边距"这种无覆盖问题也能召回到高分 chunk，那 RAG 一定会编答案。
这里统计可答题/不可答题的 top1 分数分布，看是否存在可分阈值。

用法
----
    python -m eval.run_eval
    python -m eval.run_eval --budget 2000
"""

from __future__ import annotations

import argparse
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
DATASET = ROOT / "eval" / "retrieval_set.json"
STRICT_K = 5

# 待评测配置：(显示名, 策略, 构建参数)
CONFIGS: list[tuple[str, str, dict]] = [
    ("fixed", "fixed", {}),
    ("heading", "heading", {}),
    ("heading_split@500", "heading_split", {"max_chars": 500}),
    ("heading_split@300", "heading_split", {"max_chars": 300}),
    ("semantic", "semantic", {}),
]


def load_questions() -> list[dict]:
    return json.loads(DATASET.read_text(encoding="utf-8"))["questions"]


def evaluate(chunks, questions: list[dict], budget_chars: int) -> dict:
    index = BM25(chunks)
    avg = sum(c.chars for c in chunks) / len(chunks)
    k = max(1, round(budget_chars / avg))
    top = max(STRICT_K, k)

    answerable = [q for q in questions if q.get("answerable", True)]
    unanswerable = [q for q in questions if not q.get("answerable", True)]

    rows = []
    for item in answerable:
        res = index.search(item["q"], top_k=top)
        gold = item["gold_section"]
        rank = next((r.rank for r in res if r.chunk.section_id == gold), None)
        rows.append(
            {
                "id": item["id"],
                "layer": item.get("layer", ""),
                "gold": gold,
                "rank": rank,
                "hit_strict": rank is not None and rank <= STRICT_K,
                "hit_budget": rank is not None and rank <= k,
            }
        )

    # 拒答检验：不可答题的 top1 分数
    noans_scores = []
    for item in unanswerable:
        res = index.search(item["q"], top_k=1)
        noans_scores.append(res[0].score if res else 0.0)

    # 可答题的 top1 分数，用于对比分布
    ans_scores = []
    for item in answerable:
        res = index.search(item["q"], top_k=1)
        ans_scores.append(res[0].score if res else 0.0)

    n = len(rows) or 1
    by_layer: dict[str, list[bool]] = {}
    for r in rows:
        by_layer.setdefault(r["layer"], []).append(r["hit_budget"])

    return {
        "chunks": len(chunks),
        "avg": round(avg),
        "k": k,
        "strict": sum(1 for r in rows if r["hit_strict"]) / n,
        "budget": sum(1 for r in rows if r["hit_budget"]) / n,
        "mrr": sum(1 / r["rank"] for r in rows if r["rank"]) / n,
        "by_layer": {lay: sum(v) / len(v) for lay, v in by_layer.items()},
        "rows": rows,
        "ans_top1": ans_scores,
        "noans_top1": noans_scores,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="正式检索评测")
    ap.add_argument("--budget", type=int, default=2000, help="等上下文预算（字符）")
    args = ap.parse_args()

    questions = load_questions()
    sections = load_sections()
    ans = [q for q in questions if q.get("answerable", True)]
    noans = [q for q in questions if not q.get("answerable", True)]

    print(f"知识库 {len(sections)} 节｜评测集 {len(questions)} 题"
          f"（可答 {len(ans)}，无覆盖 {len(noans)}）")
    print(f"等上下文预算 {args.budget} 字符｜固定口径 top-{STRICT_K}\n")

    # ---- 评测集自检：gold 必须存在、不可答题不得有 gold ----
    known = {s.section_id for s in sections}
    errs = []
    for q in questions:
        if q.get("answerable", True):
            if q.get("gold_section") not in known:
                errs.append(f"{q['id']}: gold_section 不存在 ({q.get('gold_section')})")
        elif q.get("gold_section") is not None:
            errs.append(f"{q['id']}: 标记为不可答却给了 gold_section")
    if errs:
        print("❌ 评测集自检失败：")
        for e in errs:
            print(f"  - {e}")
        return 1
    covered = {q["gold_section"] for q in ans}
    print(f"✅ 自检通过｜覆盖 {len(covered)}/23 条规则\n")

    results = []
    for name, strategy, kw in CONFIGS:
        chunks = build_chunks(strategy, sections, **kw)
        r = evaluate(chunks, questions, args.budget)
        r["name"] = name
        results.append(r)

    print("=" * 92)
    print(f"总览（等预算 {args.budget} 字符）")
    print("=" * 92)
    print(f"{'配置':<20}{'chunk数':>9}{'均长':>7}{'k':>4}{'固定top5':>10}{'等预算':>9}{'MRR':>8}")
    print("-" * 92)
    for r in results:
        print(
            f"{r['name']:<20}{r['chunks']:>9}{r['avg']:>7}{r['k']:>4}"
            f"{r['strict']*100:>9.0f}%{r['budget']*100:>8.0f}%{r['mrr']:>8.2f}"
        )

    # ---- 按难度层拆分 ----
    layers = [lay for lay in ["literal", "value", "colloquial", "paraphrase", "context", "confusable"]
              if any(lay in r["by_layer"] for r in results)]
    print()
    print("=" * 92)
    print("按难度层拆分（等预算命中率）—— 差异集中在哪一类题上")
    print("=" * 92)
    print(f"{'配置':<20}" + "".join(f"{lay[:9]:>11}" for lay in layers))
    print("-" * 92)
    for r in results:
        row = f"{r['name']:<20}"
        for lay in layers:
            v = r["by_layer"].get(lay)
            row += f"{(f'{v*100:.0f}%' if v is not None else '-'):>11}"
        print(row)

    # ---- 拒答检验 ----
    print()
    print("=" * 92)
    print("拒答检验：不可答题的检索得分是否与可答题可分？")
    print("=" * 92)
    print(f"{'配置':<20}{'可答top1均值':>14}{'可答最小':>10}{'无覆盖最大':>12}{'可分?':>8}")
    print("-" * 92)
    for r in results:
        amin, amax = min(r["ans_top1"]), max(r["ans_top1"])
        nmax = max(r["noans_top1"]) if r["noans_top1"] else 0.0
        separable = nmax < amin
        print(
            f"{r['name']:<20}{sum(r['ans_top1'])/len(r['ans_top1']):>14.3f}"
            f"{amin:>10.3f}{nmax:>12.3f}{('是' if separable else '否'):>8}"
        )
    print()
    print("『可分=是』表示存在一个阈值，能把无覆盖问题全部识别为「查不到」——")
    print("这是实现拒答（而不是让模型硬答）的前提。否 = 当前纯词法检索无法区分。")
    print()
    print(f"{len(noans)} 道无覆盖题的问句：")
    for q in noans:
        print(f"  {q['id']}  {q['q']}")

    # ---- 逐题明细 ----
    print()
    print("=" * 92)
    print("逐题命中排名（- = 未命中 top-5）")
    print("=" * 92)
    print(f"{'题号':<6}{'层':<12}" + "".join(f"{r['name'][:11]:>12}" for r in results))
    print("-" * 92)
    for i, q in enumerate(ans):
        row = f"{q['id']:<6}{q.get('layer','')[:11]:<12}"
        for r in results:
            rk = r["rows"][i]["rank"]
            row += f"{(str(rk) if rk else '-'):>12}"
        print(row)

    print()
    print(f"评测集：{DATASET.name}｜共 {len(questions)} 题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
