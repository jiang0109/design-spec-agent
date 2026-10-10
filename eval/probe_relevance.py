"""实验：LLM 相关性判定能否实现拒答，以及是否同时改善 rerank。

要解决的问题（来自 eval/retrieval_report.md 三、）
-------------------------------------------------
检索层拒答检验**五个配置全部不可分**：无覆盖题召回的 top1 分数
（34~38）比可答题的均值（30~35）还高。

根因：BM25 只看词面重合。"表格行内边距"含"内边距"，命中"卡片内边距"规范 ——
**词面对上了，语义上答不了。** 所以拒答不可能靠调 BM25 阈值实现。

本实验检验两个假设：
  H1（拒答）：LLM 相关性判定的分数能分开可答题与无覆盖题
      → 判定标准：可答题的最大相关性分数一致地高于无覆盖题
  H2（rerank）：用相关性分数重排后，召回率提升
      → 特别是 retrieval_report.md 里 heading 在 confusable 层只有 55%

成本控制
--------
1. 结果**缓存到磁盘**（eval/.cache/relevance.json）。首次跑真实调用，
   之后重跑零成本 —— 这是保证可复现的前提，也让调参不需要反复付费。
2. 分阶段：先跑 H1（只对 top-5 打分），若不可分则不再投入 H2。

用法
----
    python -m eval.probe_relevance                 # 全部
    python -m eval.probe_relevance --limit 8       # 只跑前 8 题（快速验证）
    python -m eval.probe_relevance --no-cache      # 忽略缓存，强制重算
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
from src.relevance import score_relevance  # noqa: E402
from src.retrieve import BM25  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "eval" / "retrieval_set.json"
CACHE = ROOT / "eval" / ".cache" / "relevance.json"
TOPK = 5
STRICT_K = 5


def load_questions() -> list[dict]:
    return json.loads(DATASET.read_text(encoding="utf-8"))["questions"]


def load_cache() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


def cached_score(cache: dict, qid: str, question: str, chunk, no_cache: bool) -> tuple[int, str]:
    key = f"{qid}|{chunk.section_id}"
    if not no_cache and key in cache:
        e = cache[key]
        return e["score"], e["reason"]
    score, reason = score_relevance(question, chunk)
    cache[key] = {"score": score, "reason": reason}
    return score, reason


def main() -> int:
    ap = argparse.ArgumentParser(description="相关性判定 / 拒答实验")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全部）")
    ap.add_argument("--no-cache", action="store_true", help="忽略缓存强制重算")
    ap.add_argument("--config", default="heading_split@300", help="参与评测的分块配置")
    args = ap.parse_args()

    questions = load_questions()
    if args.limit:
        questions = questions[: args.limit]
    sections = load_sections()

    cfg_map = {
        "fixed": ("fixed", {}),
        "heading": ("heading", {}),
        "heading_split@500": ("heading_split", {"max_chars": 500}),
        "heading_split@300": ("heading_split", {"max_chars": 300}),
        "semantic": ("semantic", {}),
    }
    if args.config not in cfg_map:
        print(f"未知配置 {args.config}，可选：{list(cfg_map)}")
        return 1
    strategy, kw = cfg_map[args.config]
    chunks = build_chunks(strategy, sections, **kw)
    index = BM25(chunks)

    ans = [q for q in questions if q.get("answerable", True)]
    noans = [q for q in questions if not q.get("answerable", True)]

    cache = load_cache()
    print(f"配置 {args.config}｜{len(chunks)} chunks｜题目 {len(questions)}（可答 {len(ans)}，无覆盖 {len(noans)}）")
    print(f"缓存条目 {len(cache)}（已缓存的题不再调用 API）\n")

    print("=" * 84)
    print(f"阶段 1：H1 拒答可分性 —— 对每题的 BM25 top-{TOPK} 做语义相关性打分")
    print("=" * 84)

    records: list[dict] = []
    for i, q in enumerate(questions, 1):
        cands = index.search(q["q"], top_k=TOPK)
        if not cands:
            records.append(
                {
                    "id": q["id"],
                    "layer": q.get("layer"),
                    "answerable": q.get("answerable", True),
                    "gold": q.get("gold_section"),
                    "bm25_top1": 0.0,
                    "llm_max": 0,
                    "llm_scores": [],
                    "gold_score": None,
                    "gold_rank_bm25": None,
                    "gold_rank_llm": None,
                }
            )
            continue

        scores = []
        for c in cands:
            sc, _ = cached_score(cache, q["id"], q["q"], c.chunk, args.no_cache)
            scores.append({"section": c.chunk.section_id, "bm25_rank": c.rank, "score": sc})
        save_cache(cache)

        gold = q.get("gold_section")
        gold_entry = next((s for s in scores if s["section"] == gold), None)
        llm_order = sorted(scores, key=lambda s: (-s["score"], s["bm25_rank"]))
        gold_rank_llm = next(
            (i for i, s in enumerate(llm_order, 1) if s["section"] == gold), None
        )

        records.append(
            {
                "id": q["id"],
                "layer": q.get("layer"),
                "answerable": q.get("answerable", True),
                "gold": gold,
                "bm25_top1": cands[0].score,
                "llm_max": max(s["score"] for s in scores),
                "llm_scores": [s["score"] for s in scores],
                "gold_score": gold_entry["score"] if gold_entry else None,
                "gold_rank_bm25": gold_entry["bm25_rank"] if gold_entry else None,
                "gold_rank_llm": gold_rank_llm,
            }
        )
        if i % 5 == 0:
            print(f"  已处理 {i}/{len(questions)} 题")

    # ---- 可分性判定 ----
    def stats(rs: list[dict], field: str) -> tuple[float, int, int]:
        vals = [r[field] for r in rs if r.get(field) is not None]
        if not vals:
            return 0.0, 0, 0
        return sum(vals) / len(vals), min(vals), max(vals)

    a_recs = [r for r in records if r["answerable"]]
    n_recs = [r for r in records if not r["answerable"]]

    print()
    print("=" * 84)
    print("可分性对比：BM25 分数 vs LLM 相关性分数")
    print("=" * 84)
    print(f"{'信号':<16}{'可答均值':>12}{'可答最小':>12}{'无覆盖最大':>13}{'可分?':>8}")
    print("-" * 84)
    for field, label in (("bm25_top1", "BM25 top1"), ("llm_max", "LLM 最大分")):
        am, amin, _ = stats(a_recs, field)
        _, _, nmax = stats(n_recs, field)
        sep = bool(n_recs) and bool(a_recs) and nmax < amin
        print(f"{label:<16}{am:>12.3f}{amin:>12}{nmax:>13}{('是' if sep else '否'):>8}")

    print()
    print("各无覆盖题的 LLM 打分明细：")
    for r in n_recs:
        print(f"  {r['id']}  最高分 {r['llm_max']}  各chunk {r['llm_scores']}  {r['bm25_top1']:.1f}(BM25)")

    # ⚠️ 必须要求两组都非空。第一版漏了这个判断：
    # 用 --limit 3 取样时前 3 题全是可答题，noans 为空集，
    # "无覆盖最大分" 算出来是 0，于是脚本报"✅ 两分布完全分离" —— 空集合导致的假阳性。
    h1_ok = False
    a_min = min((r["llm_max"] for r in a_recs), default=0)
    n_max = max((r["llm_max"] for r in n_recs), default=-1)

    print()
    if not n_recs:
        print("→ ⚠️ 本批次不含无覆盖题（no_answer 层），无法检验拒答可分性。")
        print("  请用全集运行（去掉 --limit），或确认取样包含 no_answer 题。")
    elif not a_recs:
        print("→ ⚠️ 本批次不含可答题，无法比较。")
    else:
        # 关键观察：不只看"能否用单一阈值完全分离"，还要看两组分布的整体位置。
        # 完全分离要求过于苛刻 —— 只要有 1 道可答题拿低分就会判定失败，
        # 但真正需要的信息是：两组的**典型位置**是否分开。
        a_mean = sum(r["llm_max"] for r in a_recs) / len(a_recs)
        n_mean = sum(r["llm_max"] for r in n_recs) / len(n_recs)
        low_ans = [r for r in a_recs if r["llm_max"] <= 1]

        print(f"两组分布位置：可答题均值 {a_mean:.2f}（最低 {a_min}）"
              f"｜无覆盖均值 {n_mean:.2f}（最高 {n_max}）｜差距 {a_mean - n_mean:.2f} 档")
        if n_max < a_min:
            print(f"→ ✅ H1 完全成立：阈值 2 可把无覆盖题全部识别（无覆盖最高 {n_max} < 可答最低 {a_min}）。")
            h1_ok = True
        elif a_mean - n_mean >= 0.5:
            print(f"→ 🟡 H1 部分成立：分布明显分离（差距 {a_mean-n_mean:.2f} 档），")
            print(f"  但存在 {len(low_ans)} 道可答题得分 ≤1，单一阈值无法完全分离。")
            print("  需检查这些题是判分噪声，还是本身就该被检索器视为「内容不充分」：")
            for r in low_ans:
                print(f"    {r['id']}（{r['layer']}）gold={r['gold']} 得分={r['gold_score']} "
                      f"各chunk={r['llm_scores']}")
        else:
            print(f"→ ❌ H1 不成立：两组分布位置接近（差距仅 {a_mean-n_mean:.2f} 档）。")

    # ---- H2：rerank 是否提升召回 ----
    print()
    print("=" * 84)
    print("阶段 2：H2 rerank 是否提升召回（相关性分数重排后 gold 的排名）")
    print("=" * 84)

    improved = same = worse = 0
    lost = regained = 0
    by_layer: dict[str, list[tuple[bool, bool]]] = {}

    for r in a_recs:
        rb, rl = r["gold_rank_bm25"], r["gold_rank_llm"]
        hit_b = rb is not None and rb <= STRICT_K
        hit_l = rl is not None and rl <= STRICT_K
        by_layer.setdefault(r["layer"], []).append((hit_b, hit_l))

        if rb and rl:
            if rl < rb:
                improved += 1
            elif rl > rb:
                worse += 1
            else:
                same += 1
        if hit_b and not hit_l:
            lost += 1
        if not hit_b and hit_l:
            regained += 1

    base_hits = sum(1 for r in a_recs if r["gold_rank_bm25"] and r["gold_rank_bm25"] <= STRICT_K)
    new_hits = sum(1 for r in a_recs if r["gold_rank_llm"] and r["gold_rank_llm"] <= STRICT_K)
    n = len(a_recs)

    print(f"命中率 top-{STRICT_K}：BM25 {base_hits}/{n} = {base_hits/n*100:.0f}%"
          f"  →  rerank {new_hits}/{n} = {new_hits/n*100:.0f}%")
    print()
    print(f"  排名提升 {improved} 题｜不变 {same} 题｜下降 {worse} 题")
    print(f"  命中→丢失 {lost} 题｜未命中→命中 {regained} 题")

    print()
    print("按难度层看 rerank 效果（gold 得分 / 命中变化）：")
    print(f"  {'层':<12}{'BM25命中':>10}{'rerank命中':>12}{'变化':>8}")
    print("  " + "-" * 44)
    for lay, pairs in sorted(by_layer.items()):
        b = sum(1 for x, _ in pairs if x)
        l = sum(1 for _, y in pairs if y)
        print(f"  {lay:<12}{b:>10}{l:>12}{(l-b):>+8}")

    print()
    if new_hits > base_hits:
        print("→ ✅ H2 成立：rerank 提升了 top-k 命中率。")
    elif new_hits == base_hits:
        print("→ ⚪ H2 中性：命中率不变（但可能改变了排序质量）")
    else:
        print("→ ❌ H2 不成立：rerank 反而降低了命中率。")

    # ---- 关键交叉检验：rerank 是否让易混题变好、让简单题变坏 ----
    print()
    print("=" * 84)
    print("关键交叉检验：rerank 的收益与代价是否集中在特定层？")
    print("=" * 84)
    for lay, pairs in sorted(by_layer.items()):
        b = sum(1 for x, _ in pairs if x)
        l = sum(1 for _, y in pairs if y)
        flag = "↑" if l > b else ("↓" if l < b else "=")
        print(f"  {flag} {lay:<12} {b}/{len(pairs)} → {l}/{len(pairs)}")

    print()
    print("注意：相关性判定每题需 k 次 API 调用，是 rerank 的真实成本。")
    print(f"本次共 {len(cache)} 条缓存（= 实际 API 调用次数）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
