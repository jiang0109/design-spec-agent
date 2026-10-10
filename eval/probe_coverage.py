"""实验：用「覆盖度」这道正交判定解决拒答（相关性阈值的替代方案）。

为什么需要第二次实验
--------------------
第一次实验（同文件的前序版本）证明了：
  - BM25 分数**完全无法**拒答：无覆盖题的 top1 分数（34.5）反而高于可答题最小值（13.9）
  - LLM 相关性打分**能大幅分离**两组：可答均值 2.91 vs 无覆盖均值 1.00（差 1.9 档）

但三种不同措辞的打分标准都无法做到"单一阈值完全分离"：

    版本                        可答均值  无覆盖最大  差距
    v1 原始严格                 2.88      1         1.88
    v2 放宽"范围更宽也适用"      2.91      2         1.41   ← 变差
    v3 决定性档位（3=明确提到）   2.79      2         1.29   ← 更差

关键观察：**v2 修好了 Q23（可答均值 2.88→2.91），却让 Q31 从 1 涨到 2。**
同一处修改一边堵住误拒、一边开了误收 —— 说明这不是措辞问题。

### 根因：相关性与覆盖度是两个独立维度

| 维度 | 问题 | 例子 |
|---|---|---|
| **相关性/适用性** | 这条规则适用于问题所指的对象吗？ | "颜色必须用语义 token" 适用于"深色模式容器颜色" → 适用 |
| **覆盖度** | 知识库里有专门讲这个对象的规则吗？ | 知识库没有任何深色模式主题的定义 → 不覆盖 |

Q31（深色模式容器颜色）在维度一上是"适用"，在维度二上是"无覆盖"。
所以**用单一相关性分数同时回答两个问题必然失败** —— 不是提示词没写好，是维度混淆。

### 本实验的假设

> H3：把"覆盖度"做成一道**独立的二值判定**（知识库里是否有专门针对该对象的规则），
> 可以在相关性分数不能分离的地方实现分离。

判定设计：不看检索结果，直接问"请检查知识库，是否存在专门针对 X 的规则"。
与相关性判定的区别：
  - 相关性：给资料 + 问题 → 这段资料能回答吗
  - 覆盖度：只给问题 → 知识库里**有没有**讲这个对象的规则

用法
----
    python -m eval.probe_coverage
    python -m eval.probe_coverage --analyze-only   # 只用缓存，不调用 API
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.chunking import load_sections  # noqa: E402
from src.llm import ask_llm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "eval" / "retrieval_set.json"
CACHE = ROOT / "eval" / ".cache" / "coverage.json"

# 覆盖度判定的提示词。刻意与相关性判定分离 —— 它不看任何检索结果，
# 只判断"知识库里有没有专门讲这个对象的规则"。
COVERAGE_SYSTEM = """你在检查一份设计规范知识库的**覆盖盲区**。

给定一个用户问题，判断知识库里是否存在**专门针对问题所指对象**的规则。

只输出 JSON：
{"covered": true/false, "rule": "<最相关规则的 id 或名称，没有则填 none>", "reason": "<20字以内>"}

判断标准：
- covered=true：知识库里有一条规则的**适用对象正好是问题问的那个东西**
  例：问「卡片内边距允许哪几档」→ 有「卡片内边距取值」规则 → true
- covered=false：知识库只有讲**别的对象**的规则，或者完全没有相关主题
  例：问「表格行内边距」→ 只有「卡片内边距」规则，没有表格的 → false
  例：问「深色模式主题怎么定义」→ 知识库没有任何主题/深色模式规则 → false

注意：不要因为"有讲同类主题的规则"就判 true。
问的是表格行内边距，知识库只有卡片内边距 —— 虽然都是内边距，但对象不同，判 false。
判断的是**对象是否被专门覆盖**，不是主题是否相关。"""


def load_cache() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return {}


def save_cache(c: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")


def judge_coverage(question: str) -> tuple[bool, str, str]:
    res = ask_llm(
        [
            {"role": "system", "content": COVERAGE_SYSTEM},
            {"role": "user", "content": f"【知识库目录】\n{kb_index_text()}\n\n【用户问题】\n{question}"},
        ],
        temperature=0.0,
        json_mode=True,
    )
    raw = (res.message.content or "").strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        raw = m.group(0)
    try:
        d = json.loads(raw)
        return bool(d.get("covered")), str(d.get("rule", ""))[:40], str(d.get("reason", ""))[:50]
    except (json.JSONDecodeError, ValueError, TypeError):
        return False, "parse_error", raw[:40]


def kb_index_text() -> str:
    """把知识库的规则清单给模型 —— 判定"是否覆盖"需要知道库里有什么。"""
    lines = []
    for s in load_sections():
        if s.section_id.startswith("CASE"):
            continue
        lines.append(f"- {s.section_id} {s.section_title}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="覆盖度判定实验")
    ap.add_argument("--analyze-only", action="store_true", help="只用缓存分析，不调用 API")
    args = ap.parse_args()

    questions = json.loads(DATASET.read_text(encoding="utf-8"))["questions"]
    cache = load_cache()

    if not args.analyze_only:
        todo = [q for q in questions if q["id"] not in cache]
        print(f"需判定 {len(todo)} 题（已缓存 {len(cache)}）\n")
        for i, q in enumerate(todo, 1):
            cov, rule, reason = judge_coverage(q["q"])
            cache[q["id"]] = {"covered": cov, "rule": rule, "reason": reason}
            save_cache(cache)
            if i % 5 == 0:
                print(f"  已判定 {i}/{len(todo)}")

    # ---- 分析 ----
    ans = [q for q in questions if q.get("answerable", True)]
    noans = [q for q in questions if not q.get("answerable", True)]

    def rate(rs: list[dict]) -> float:
        if not rs:
            return 0.0
        return sum(1 for q in rs if cache.get(q["id"], {}).get("covered")) / len(rs)

    print()
    print("=" * 80)
    print("覆盖度判定的分离效果")
    print("=" * 80)
    a_cov = rate(ans)
    n_cov = rate(noans)
    print(f"可答题被判「已覆盖」   ：{sum(1 for q in ans if cache.get(q['id'],{}).get('covered'))}/{len(ans)}"
          f" = {a_cov*100:.0f}%")
    print(f"无覆盖题被判「已覆盖」 ：{sum(1 for q in noans if cache.get(q['id'],{}).get('covered'))}/{len(noans)}"
          f" = {n_cov*100:.0f}%")
    print(f"分离度（可答覆盖率 - 无覆盖覆盖率）：{(a_cov-n_cov)*100:.0f} 个百分点")
    print()

    perfect = n_cov == 0.0 and a_cov == 1.0
    good = (a_cov - n_cov) >= 0.8
    if perfect:
        print("→ ✅ H3 成立：覆盖度判定**完全分离**两组（无覆盖题全部判 false）。")
        print("  拒答可以实现，且不需要相关性打分参与 —— 它是独立且更可靠的一道闸门。")
    elif good:
        print(f"→ 🟡 H3 基本成立：分离度 {(a_cov-n_cov)*100:.0f}pp，远优于相关性打分的重叠。")
        print("  剩余误差集中在少数题，值得逐题检查。")
    else:
        print(f"→ ❌ H3 不成立：分离度仅 {(a_cov-n_cov)*100:.0f}pp。")

    print()
    print("逐题判定（可答题中被判「未覆盖」的 = 误拒）：")
    false_reject = [q for q in ans if not cache.get(q["id"], {}).get("covered")]
    if false_reject:
        for q in false_reject:
            e = cache[q["id"]]
            print(f"  {q['id']:<5}{q['layer']:<12}gold={q['gold_section']:<12}"
                  f"rule={e['rule']:<12} {e['reason']}")
    else:
        print("  无 —— 可答题全部被正确判为已覆盖")

    print()
    print("无覆盖题判定明细：")
    for q in noans:
        e = cache.get(q["id"], {})
        print(f"  {q['id']}  covered={e.get('covered')}  rule={e.get('rule')}  {e.get('reason')}")

    # ---- 与相关性分数交叉：两闸门组合后的误拒率 ----
    rel_cache = ROOT / "eval" / ".cache" / "relevance.json"
    if rel_cache.exists():
        rel = json.loads(rel_cache.read_text(encoding="utf-8"))
        print()
        print("=" * 80)
        print("两闸门组合：相关性分 ≥2 AND 覆盖度=true 才算「有依据」")
        print("=" * 80)
        ans_ok = noans_ok = 0
        for q in ans:
            scores = [v["score"] for k, v in rel.items() if k.startswith(q["id"] + "|")]
            gate = (max(scores) >= 2 if scores else False) and cache.get(q["id"], {}).get("covered")
            ans_ok += bool(gate)
        for q in noans:
            scores = [v["score"] for k, v in rel.items() if k.startswith(q["id"] + "|")]
            gate = (max(scores) >= 2 if scores else False) and cache.get(q["id"], {}).get("covered")
            noans_ok += bool(gate)
        print(f"可答题通过（应高）：{ans_ok}/{len(ans)} = {ans_ok/len(ans)*100:.0f}%")
        print(f"无覆盖通过（应低）：{noans_ok}/{len(noans)} = {noans_ok/max(1,len(noans))*100:.0f}%")
        if noans_ok == 0 and ans_ok / len(ans) >= 0.8:
            print("→ ✅ 两闸门组合可用：能拒答且保留大部分可答题。")
        else:
            print("→ 需据上面两行判断：无覆盖通过率越低越好，可答题通过率越高越好。")
    else:
        print("\n（未找到相关性缓存，跳过两闸门组合分析）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
