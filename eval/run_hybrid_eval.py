"""全量评测：双路并行（引擎 + LLM）vs 现状 LLM 路径，120 个判定点。

对照基线
--------
`eval/query_format_report.md` 记录的现状（v5，纯 LLM 路径，120 点）：

    覆盖率 100%｜准确率 95%（114/120）｜误报 0｜漏报 0｜过度拒答 4｜过度自信 2

本脚本用完全相同的评测集、匹配规则与指标定义，跑双路并行，看是否更好。

设计
----
每份文档：
  1. 抽取 claim（1 次 LLM 调用）
  2. 闭集属性 → 规则引擎判定（确定性）
  3. 未知属性 → 只把未知陈述拼成迷你文档，走一次 agent（LLM 路径）
  4. 两条路径的结果合并成 issues，再套用同一套匹配/指标

为什么未知才走 LLM：引擎已判过的部分不重复计费；而未知属性里有
"规则明确排除某对象"这类必须读例外条款的案例，只有 LLM 判得对。

用法
----
    python -m eval.run_hybrid_eval
    python -m eval.run_hybrid_eval --limit 10      # 先跑 10 份验证管道
    python -m eval.run_hybrid_eval --no-llm        # 只测引擎路径（不调 LLM 兜底）
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

from eval.run_generation_eval import _keywords, category, match  # noqa: E402
from src.hybrid import evaluate_document_dual  # noqa: E402
from src.rule_engine import RuleEngine  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATASETS = [
    ROOT / "eval" / "generation_set.json",
    ROOT / "eval" / "generation_set_expanded.json",
]
OUT = ROOT / "out" / "hybrid_eval.json"

# 对照基线（v5 纯 LLM 路径，来自 eval/query_format_report.md）
BASELINE = {
    "name": "v5 纯 LLM 路径",
    "coverage": 1.00,
    "accuracy": 114 / 120,
    "fp": 0, "fn": 0, "over_reject": 4, "over_confidence": 2,
}


def run_case(case: dict, engine: RuleEngine, llm: bool) -> dict:
    rep = evaluate_document_dual(case["doc"], engine, llm_fallback=llm)
    # 统一成与 LLM 路径相同的 issues 形状，复用同一套匹配
    issues = [
        {
            "target": j.statement,
            "verdict": j.verdict,
            "has_evidence": j.evidence,
            "reason": j.detail,
            "source": j.source,
            "rule_id": j.rule_id,
            "attribute": j.attribute,
        }
        for j in rep.judgments
    ]

    used: set[int] = set()
    single = len(case["points"]) == 1
    points_out = []
    for p in case["points"]:
        kws = _keywords(p)
        got = match(issues, kws, used)
        if got is None and single:
            unused = [i for i, x in enumerate(issues) if i not in used]
            if len(unused) == 1:
                got = (unused[0], issues[unused[0]])
        if got is None:
            points_out.append({
                "keyword": kws[0], "keywords": kws, "rule": p["rule"],
                "expect": p["expect"], "actual": None, "correct": False,
                "error": "未命中（未就该点给出判定）", "why": p["why"],
                "source": "", "attribute": "",
            })
            continue
        idx, issue = got
        used.add(idx)
        actual = issue["verdict"]
        ok = actual == p["expect"]
        if ok:
            err = ""
        elif p["expect"] in ("pass", "fail") and actual == "uncertain":
            err = "过度拒答（该判却 uncertain）"
        elif p["expect"] == "uncertain" and actual in ("pass", "fail"):
            err = "过度自信（无覆盖却下结论）"
        elif p["expect"] == "pass" and actual == "fail":
            err = "误报（合规判成违规）"
        elif p["expect"] == "fail" and actual == "pass":
            err = "漏报（违规判成合规）"
        else:
            err = f"判定不符（期望 {p['expect']}）"
        points_out.append({
            "keyword": kws[0], "keywords": kws, "rule": p["rule"],
            "expect": p["expect"], "actual": actual, "correct": ok,
            "error": err, "why": p["why"],
            "source": issue["source"], "attribute": issue["attribute"],
            "target": issue["target"],
        })

    return {
        "id": case["id"],
        "points": points_out,
        "by_engine": rep.by_engine,
        "by_llm": rep.by_llm,
        "fallback": rep.fallback,
        "extract_calls": rep.extract_calls,
        "llm_runs": rep.llm_runs,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="双路并行全量评测")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 份文档")
    ap.add_argument("--no-llm", action="store_true", help="关闭 LLM 兜底（只测引擎路径）")
    args = ap.parse_args()

    cases: list[dict] = []
    for p in DATASETS:
        if p.exists():
            cases.extend(json.loads(p.read_text(encoding="utf-8"))["cases"])
    if args.limit:
        cases = cases[: args.limit]

    n_points = sum(len(c["points"]) for c in cases)
    mode = "仅引擎路径" if args.no_llm else "双路并行（引擎 + LLM）"
    print(f"混合判定全量评测｜模式：{mode}")
    print(f"文档 {len(cases)} 份｜判定点 {n_points} 个\n")

    engine = RuleEngine()
    k, n = engine.coverage()
    print(f"引擎覆盖 {k}/{n} 条规则\n")

    results = []
    for i, case in enumerate(cases, 1):
        r = run_case(case, engine, not args.no_llm)
        results.append(r)
        ok = sum(1 for p in r["points"] if p["correct"])
        if ok != len(r["points"]) or i % 20 == 0:
            print(f"  [{i:>3}/{len(cases)}] {r['id']:<6} {ok}/{len(r['points'])} "
                  f"正确（engine {r['by_engine']} / llm {r['by_llm']} / fallback {r['fallback']}）")

    # ---------------- 汇总 ----------------
    pts = [p for r in results for p in r["points"]]
    total = len(pts)
    matched = [p for p in pts if p["actual"] is not None]
    correct = [p for p in pts if p["correct"]]

    def cnt(prefix: str) -> int:
        return sum(1 for p in pts if p["error"].startswith(prefix))

    print()
    print("=" * 92)
    print("总体指标")
    print("=" * 92)
    print(f"  判定点总数        : {total}")
    print(f"  覆盖率            : {len(matched)}/{total} = {len(matched)/total*100:.0f}%")
    print(f"  **端到端准确率**  : {len(correct)}/{total} = {len(correct)/total*100:.0f}%")
    print(f"  误报 FP           : {cnt('误报')}")
    print(f"  漏报 FN           : {cnt('漏报')}")
    print(f"  过度拒答          : {cnt('过度拒答')}")
    print(f"  过度自信          : {cnt('过度自信')}")
    print(f"  未命中            : {sum(1 for p in pts if p['actual'] is None)}")

    # ---------------- 与基线对比 ----------------
    print()
    print("=" * 92)
    print(f"与基线对比（{BASELINE['name']}，来自 eval/query_format_report.md）")
    print("=" * 92)
    acc = len(correct) / total
    print(f"{'指标':<16}{'基线':>14}{'本次':>14}{'差':>10}")
    print("-" * 92)
    rows = [
        ("覆盖率", BASELINE["coverage"], len(matched) / total, True),
        ("准确率", BASELINE["accuracy"], acc, True),
        ("误报 FP", BASELINE["fp"], cnt("误报"), False),
        ("漏报 FN", BASELINE["fn"], cnt("漏报"), False),
        ("过度拒答", BASELINE["over_reject"], cnt("过度拒答"), False),
        ("过度自信", BASELINE["over_confidence"], cnt("过度自信"), False),
    ]
    for name, base, now, is_rate in rows:
        if is_rate:
            print(f"{name:<16}{base*100:>13.0f}%{now*100:>13.0f}%{(now-base)*100:>+9.0f}pp")
        else:
            print(f"{name:<16}{base:>14}{now:>14}{now-base:>+10}")

    # ---------------- 来源分布 ----------------
    src = {"engine": 0, "llm": 0, "fallback": 0}
    for r in results:
        src["engine"] += r["by_engine"]
        src["llm"] += r["by_llm"]
        src["fallback"] += r["fallback"]
    print()
    print("=" * 92)
    print("判定来源分布（引擎路径占比越高，判定越可复现）")
    print("=" * 92)
    for kk in ("engine", "llm", "fallback"):
        print(f"  {kk:<10}{src[kk]:>5}  {(src[kk]/max(1,total))*100:>5.0f}%")

    # ---------------- 分层 ----------------
    cats: dict[str, list[dict]] = {}
    for p in pts:
        cats.setdefault(category(p["rule"]), []).append(p)
    print()
    print("=" * 92)
    print("按规则类别分层")
    print("=" * 92)
    print(f"{'类别':<24}{'点数':>6}{'正确':>6}{'准确率':>8}{'引擎':>7}{'LLM':>6}")
    print("-" * 92)
    for cat in sorted(cats):
        ps = cats[cat]
        c = sum(1 for p in ps if p["correct"])
        e = sum(1 for p in ps if p.get("source") == "engine")
        l = sum(1 for p in ps if p.get("source") == "llm")
        print(f"{cat:<24}{len(ps):>6}{c:>6}{c/len(ps)*100:>7.0f}%{e:>7}{l:>6}")

    # ---------------- 错误明细 ----------------
    errs = [p for p in pts if not p["correct"]]
    if errs:
        print()
        print("=" * 92)
        print(f"错误明细（{len(errs)} 处）")
        print("=" * 92)
        for p in errs:
            print(f"  [{p['rule']}] 期望 {p['expect']} → 实际 {p['actual']}  {p['error']}")
            print(f"     来源={p.get('source')} 属性={p.get('attribute')} 依据：{p['why'][:50]}")
            if p.get("target"):
                print(f"     target: {str(p['target'])[:70]}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"mode": mode, "accuracy": acc, "results": results},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写出：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
