"""实验：把「例外」结构字段喂给相关性判定，能否同时保住拒答与覆盖率。

要检验的命题
------------
`e2e_report.md` 的结论是：「规则范围更宽」这个边界**逻辑上不可兼得** ——
要么宽容（拒答失效），要么严格（误拒表格行高）。

**但这个结论建立在一个未验证的前提上：判定器能看到规则的例外条款。**
实际检查发现它看不到：

    TYPO-002 的例外写着「单行文本（按钮标签、徽标、表格单元格）不参与行高倍数校验」
    → "表格行高"的正确答案本来就能**查表得到**，不需要模糊判断
    → 但早期 chunk 只在第一个子块挂结构字段，72 个 chunk 里 49 个不含例外
    → 判定器只看到正文，只能凭措辞猜，于是把表格行高判成 relevance=1

所以本实验分两步修：
  A. 架构修复：每个子块都带结构字段（`repeat_params=True`），让例外始终可见
  B. 判定提示 v2：显式给出「适用 / 例外」，并规定三步判定顺序（先查例外）

### 需要同时满足的三个条件

| 案例 | 期望 | 说明 |
|---|---|---|
| 表格行高 1.6 倍 | relevance ≥ 2（**可判定**） | 例外明确排除表格单元格 → 规则不适用 → 但问题是"该用什么值"… |

⚠️ 注意上面这个案例有个微妙处：例外说"表格单元格不参与行高倍数校验"，
意味着**这条规则对表格不适用**。所以严格来说 relevance 应该判低！
但端到端期望它判 fail。

这说明**该案例的正确处理是"规则明确排除"这个信息本身**，
而不是"给个高分让生成层判 fail"。

因此本实验把判定目标改为更精确的三档语义，并**分两组指标**衡量：

| 指标 | 期望方向 |
|---|---|
| 无覆盖题的 relevance | 应低（保持拒答） |
| `e2e_covered` 类可答题的 relevance | 应高（不误拒） |
| TYPO-002 / 表格行高 | 探针：看 v2 是否识别出"例外排除"并说明 |

v2 的真正价值判断标准不是"给表格行高打高分"，而是
**"它能否给出可解释的判定依据"** —— 无论答案是可判定还是被排除。

用法
----
    python -m eval.probe_structured
    python -m eval.probe_structured --probe-only   # 只跑探针题，便宜
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
from src.relevance import score_relevance, structured_context  # noqa: E402
from src.retrieve import BM25  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "eval" / "retrieval_set.json"
CACHE = ROOT / "eval" / ".cache" / "structured.json"
TOPK = 5

# 探针题：覆盖边界的关键案例
PROBES = [
    ("P1", "表格单元格的行高用 1.6 倍可以吗", "TYPO-002", "例外明确排除表格单元格"),
    ("P2", "数据表格的行内边距可以用 8px 吗", None, "无覆盖：知识库无表格内边距规则"),
    ("P3", "深色模式下容器的颜色值怎么取", None, "无覆盖：知识库无深色模式定义"),
    ("P4", "图标按钮组件必须声明哪些属性", "COMP-001", "范围更宽：'每个组件' 覆盖图标按钮"),
    ("P5", "卡片内边距允许哪几档取值", "SPACING-001", "直接覆盖（下限校验）"),
]


def load_cache() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return {}


def save_cache(c: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")


def score_cached(cache, key, q, chunk, version, no_cache) -> tuple[int, str]:
    if not no_cache and key in cache:
        e = cache[key]
        return e["score"], e["reason"]
    s, r = score_relevance(q, chunk, version=version)
    cache[key] = {"score": s, "reason": r}
    return s, r


def main() -> int:
    ap = argparse.ArgumentParser(description="结构字段增强判定实验")
    ap.add_argument("--probe-only", action="store_true", help="只跑探针题")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    sections = load_sections()
    chunks = build_chunks("heading_split", sections, max_chars=300)
    index = BM25(chunks)
    cache = load_cache()

    # 架构修复验证
    with_exc = sum(1 for c in chunks if "例外:" in c.content)
    print("=" * 84)
    print("步骤 A：架构修复验证（结构字段是否每个子块都带）")
    print("=" * 84)
    print(f"  chunk 总数          : {len(chunks)}")
    print(f"  含「例外:」的 chunk : {with_exc}  ({with_exc*100//len(chunks)}%)")
    print(f"  （修复前为 23/{len(chunks)} = 32%，49 个 chunk 看不到例外条款）")

    # ---- 探针题 ----
    print()
    print("=" * 84)
    print("步骤 B：探针题 —— v1（只看正文） vs v2（含适用/例外）")
    print("=" * 84)
    for qid, q, gold, note in PROBES:
        print(f"\n【{qid}】{q}")
        print(f"      期望 gold={gold or '（无覆盖）'}｜{note}")
        cands = index.search(q, top_k=TOPK)
        for c in cands:
            seen = ""
            if gold and c.chunk.section_id == gold:
                seen = " ←gold"
            s1, r1 = score_cached(cache, f"v1|{qid}|{c.chunk.section_id}", q, c.chunk, 1, args.no_cache)
            s2, r2 = score_cached(cache, f"v2|{qid}|{c.chunk.section_id}", q, c.chunk, 2, args.no_cache)
            save_cache(cache)
            print(f"      {c.chunk.section_id:12} bm25={c.score:6.1f}  v1={s1}  v2={s2}{seen}")
            if s1 != s2 or s2 >= 2:
                print(f"                   v1理由: {r1}")
                if s2 != s1:
                    print(f"                   v2理由: {r2}")

    if args.probe_only:
        print("\n[--probe-only] 跳过全量评测")
        return 0

    # ---- 全量：拒答可分性 ----
    questions = json.loads(DATASET.read_text(encoding="utf-8"))["questions"]
    print()
    print("=" * 84)
    print("步骤 C：全量评测集 —— 两种版本的拒答可分性对比")
    print("=" * 84)

    def collect(version: int) -> tuple[list[int], list[int]]:
        ans, noans = [], []
        for q in questions:
            cands = index.search(q["q"], top_k=TOPK)
            if not cands:
                continue
            mx = 0
            for c in cands:
                s, _ = score_cached(
                    cache, f"v{version}|F|{q['id']}|{c.chunk.section_id}",
                    q["q"], c.chunk, version, args.no_cache,
                )
                mx = max(mx, s)
            (ans if q.get("answerable", True) else noans).append(mx)
        save_cache(cache)
        return ans, noans

    print("  正在对两种版本打分（v1 部分可命中已有缓存）…")
    a1, n1 = collect(1)
    a2, n2 = collect(2)

    print()
    print(f"{'版本':<28}{'可答均值':>10}{'可答最小':>10}{'无覆盖最大':>12}{'可分?':>8}")
    print("-" * 84)
    for label, a, n in (("v1 只看正文", a1, n1), ("v2 含适用/例外", a2, n2)):
        amin = min(a) if a else 0
        nmax = max(n) if n else 0
        print(
            f"{label:<28}{sum(a)/max(1,len(a)):>10.2f}{amin:>10}{nmax:>12}"
            f"{('是' if n and amin > nmax else '否'):>8}"
        )

    # 误拒率：可答题里 max<2 的比例
    print()
    print(f"{'版本':<28}{'误拒(可答<2)':>14}{'误收(无覆盖>=2)':>16}")
    print("-" * 84)
    for label, a, n in (("v1 只看正文", a1, n1), ("v2 含适用/例外", a2, n2)):
        fr = sum(1 for x in a if x < 2)
        fa = sum(1 for x in n if x >= 2)
        print(f"{label:<28}{f'{fr}/{len(a)}':>14}{f'{fa}/{len(n)}':>16}")

    print()
    fr1 = sum(1 for x in a1 if x < 2)
    fr2 = sum(1 for x in a2 if x < 2)
    fa1 = sum(1 for x in n1 if x >= 2)
    fa2 = sum(1 for x in n2 if x >= 2)
    print(f"误拒：v1 {fr1} → v2 {fr2}｜误收：v1 {fa1} → v2 {fa2}")
    if fr2 < fr1 and fa2 <= fa1:
        print("→ ✅ 命题成立：结构字段提升了覆盖率，且未牺牲拒答。")
    elif fr2 < fr1 and fa2 > fa1:
        print("→ ⚠️ 此消彼长：覆盖率提升但拒答变差（与 v2 提示词版本同样的权衡）。")
    elif fr2 == fr1:
        print("→ ⚪ 无变化。")
    else:
        print("→ ❌ 覆盖反而变差。")
    print(f"\n注意：本实验对 33 道可答题 × top-5 打分，是新产生的 API 调用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
