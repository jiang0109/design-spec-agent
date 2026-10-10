"""端到端验证：检索层拒答接入后，幻觉率是否真的下降。

背景
----
Week 1 已验证**生成层**的反幻觉：无依据时判 uncertain（`eval/experiments.md` 实验 3）。
本周验证了**检索层**：BM25 分数无法拒答，但 LLM 相关性判定可以
（`eval/relevance_report.md`，阈值 2 完全分离）。

本脚本把两者串起来做端到端检验。

⚠️ 第一版实验设计失败，记录在此避免重犯
----------------------------------------
第一版用了三份文档，未覆盖对象都是「圆角、投影、动效」——
结果两种模式**都没有下结论**（全部判 uncertain），看不出任何差异。

原因不是接入无效，而是**测试材料选错了**：
「圆角」「投影」这类概念与知识库的词面距离极大，BM25 本来就召不回任何东西，
生成层自然也无从编造。**两种模式在"词法距离大"的场景下天然等价。**

真正能区分的是**同术语不同对象**（如"表格行内边距" vs "卡片内边距"）：
BM25 因词面重合而高分召回 → 生成层有材料可依 → 才可能编造。
所以本版改为三类对照，显式标注每题的期望判定。

三类测试材料
------------
| 文档 | 类型 | 期望判定 | 能区分两种模式吗 |
|---|---|---|---|
| `e2e_covered.md` | 知识库**有**适用的规范 | 应判 fail | ❌ 两组都应正确（回归检查） |
| `e2e_confusable.md` | **同术语不同对象**，且术语极相近 | 应判 uncertain | ✅ **这是关键对照** |
| `e2e_novel.md` | 术语全新，知识库无对应概念 | 应判 uncertain | ❌ 两组都不该编造 |

`e2e_confusable.md` 的设计意图：
- "表格行的**内边距**" —— 术语与"卡片内边距"高度重合，BM25 必然召回该规范
- "表头与首行数据的**间距**" —— 同理命中"卡片间距"
- "**行高** 1.6 倍" —— 这条**故意可判定**：TYPO-002 对全部文字生效，1.6 违规
  → 用来检验接入后是否"矫枉过正"、把本该判定的也拒答了

用法
----
    python -m eval.run_e2e
    python -m eval.run_e2e --docs e2e_confusable.md
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

from src.agent import render_markdown, run_agent  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
OUT = ROOT / "out"

# ---------------------------------------------------------------
# 期望判定清单：显式声明每份文档里每个判定点该判什么。
# 为什么不用关键词自动分类：第一版用关键词判断"未覆盖项"，结果把
# "表格行高"（其实 TYPO-002 覆盖）也算成未覆盖，指标被污染。
# 显式标注是唯一可靠的做法 —— 这与评测集里标注 gold 是同一个原则。
# ---------------------------------------------------------------
EXPECT = {
    "e2e_covered.md": {
        "covered": ["内边距", "字号", "间距"],
        "expected": "fail",  # 三处都是覆盖范围内的真实违规
        "note": "回归检查：接入后是否仍能正确判 fail",
    },
    "e2e_confusable.md": {
        "covered": ["行高"],  # 行高可判定（TYPO-002 覆盖全部文字）
        "confusable": ["内边距", "间距"],  # 同术语不同对象，应判 uncertain
        "note": "关键对照：词法相近但对象不同",
    },
    "e2e_novel.md": {
        "covered": [],
        "confusable": ["圆角", "过渡"],
        "note": "词法距离大，两组都不该编造",
    },
}


def classify(target: str, docname: str) -> str:
    """把一条判定分类为 covered / confusable / other。"""
    spec = EXPECT.get(docname, {})
    for k in spec.get("covered", []):
        if k in target:
            return "covered"
    for k in spec.get("confusable", []):
        if k in target:
            return "confusable"
    return "other"


def run_one(doc_path: Path, llm_verify: bool) -> dict:
    doc = doc_path.read_text(encoding="utf-8")
    mode = "verified" if llm_verify else "bm25"
    ar = run_agent(doc, doc_name=f"{doc_path.stem}.{mode}", llm_verify=llm_verify, verbose=False)

    # 落盘便于人工复核（run_agent 本身不写盘，只有 CLI 写）
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{doc_path.stem}.{mode}.report.json").write_text(
        ar.model_dump_json(indent=2), encoding="utf-8"
    )
    (OUT / f"{doc_path.stem}.{mode}.report.md").write_text(
        render_markdown(ar), encoding="utf-8"
    )

    issues = [
        {
            "target": i.target,
            "verdict": i.verdict,
            "has_evidence": bool(i.evidence),
            "evidence_head": (i.evidence or "")[:50],
            "kind": classify(i.target, doc_path.name),
        }
        for i in ar.report.issues
    ]
    return {
        "mode": mode,
        "issues": issues,
        "steps": ar.steps,
        "tool_calls": ar.tool_calls,
        "prompt_tokens": ar.prompt_tokens,
        "completion_tokens": ar.completion_tokens,
        "elapsed": ar.elapsed_sec,
        "drift": ar.provenance_drift,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="端到端幻觉率验证")
    ap.add_argument("--docs", nargs="*", default=None, help="只跑指定文档（文件名）")
    args = ap.parse_args()

    docs = sorted(SAMPLES.glob("e2e_*.md"))
    if args.docs:
        docs = [d for d in docs if d.name in args.docs]
    if not docs:
        print("未找到 e2e_*.md 测试文档", file=sys.stderr)
        return 1

    print("端到端验证：三类材料 × 2 种模式（唯一差异 = 相关性闸门）")
    print("对照 = bm25（接入前）｜实验 = verified（接入后）\n")

    results = []
    for doc in docs:
        spec = EXPECT.get(doc.name, {})
        print("=" * 92)
        print(f"文档 {doc.name}   [{spec.get('note','')}]")
        print("=" * 92)
        row = {}
        for verify in (False, True):
            r = run_one(doc, verify)
            row[r["mode"]] = r
            conf = [i for i in r["issues"] if i["kind"] == "confusable"]
            fab = [i for i in conf if i["verdict"] in ("pass", "fail") and not i["has_evidence"]]
            decided = [i for i in conf if i["verdict"] in ("pass", "fail")]
            print(
                f"  [{r['mode']:<8}] 判定 {len(r['issues'])} 条｜"
                f"未覆盖型 {len(conf)}（判 uncertain {sum(1 for i in conf if i['verdict']=='uncertain')}"
                f"／下结论 {len(decided)}／其中无依据 {len(fab)}）"
                f"｜token {r['prompt_tokens']}+{r['completion_tokens']}｜{r['elapsed']}s"
            )
            for i in decided:
                flag = "⚠️ 无依据" if not i["has_evidence"] else "有依据"
                print(f"        下结论: {i['target']} → {i['verdict']}（{flag}）")
        print()
        results.append((doc.name, row["bm25"], row["verified"]))

    # ---- 汇总 ----
    def agg(rs, kind_filter) -> dict:
        tot = dec = fab = 0
        for r in rs:
            items = [i for i in r["issues"] if kind_filter(i)]
            tot += len(items)
            d = [i for i in items if i["verdict"] in ("pass", "fail")]
            dec += len(d)
            fab += sum(1 for i in d if not i["has_evidence"])
        return {"total": tot, "decided": dec, "fabricated": fab}

    print("=" * 92)
    print("汇总：核心指标（只统计「未覆盖型」判定点）")
    print("=" * 92)
    print(f"{'文档':<22}{'模式':<10}{'未覆盖点':>9}{'下结论':>8}{'无依据编造':>11}  下结论明细")
    print("-" * 92)
    tot = {"bm25": {"total": 0, "decided": 0, "fabricated": 0},
           "verified": {"total": 0, "decided": 0, "fabricated": 0}}
    for name, b, v in results:
        for r in (b, v):
            a = agg([r], lambda i: i["kind"] == "confusable")
            for k in tot[r["mode"]]:
                tot[r["mode"]][k] += a[k]
            det = [f"{i['target']}→{i['verdict']}" for i in r["issues"]
                   if i["kind"] == "confusable" and i["verdict"] in ("pass", "fail")]
            print(f"{name:<22}{r['mode']:<10}{a['total']:>9}{a['decided']:>8}{a['fabricated']:>11}  {det}")
    print("-" * 92)
    for mode in ("bm25", "verified"):
        t = tot[mode]
        print(f"{'合计':<22}{mode:<10}{t['total']:>9}{t['decided']:>8}{t['fabricated']:>11}")

    # ---- 回归检查 ----
    print()
    print("=" * 92)
    print("回归检查：对**已覆盖**项，接入后是否仍能正确判 fail？（防止矫枉过正）")
    print("=" * 92)
    print(f"{'文档':<22}{'模式':<10}{'已覆盖点':>9}{'判fail':>8}  明细")
    print("-" * 92)
    reg_lost = 0
    for name, b, v in results:
        for r in (b, v):
            items = [i for i in r["issues"] if i["kind"] == "covered"]
            fails = [i for i in items if i["verdict"] == "fail"]
            if name == "e2e_covered.md" and len(fails) < len(items):
                reg_lost += 1
            det = [f"{i['target']}→{i['verdict']}" for i in items]
            print(f"{name:<22}{r['mode']:<10}{len(items):>9}{len(fails):>8}  {det}")

    # ---- 成本 ----
    print()
    print("=" * 92)
    print("成本对比（拒答能力不是免费的）")
    print("=" * 92)
    for mode in ("bm25", "verified"):
        rs = [r for _, b, v in results for r in [b if mode == "bm25" else v]]
        pt = sum(r["prompt_tokens"] for r in rs)
        ct = sum(r["completion_tokens"] for r in rs)
        el = sum(r["elapsed"] for r in rs)
        tc = sum(r["tool_calls"] for r in rs)
        print(f"  {mode:<10} token {pt}+{ct}｜工具调用 {tc} 次｜总耗时 {el:.1f}s"
              f"（每份 {el/len(rs):.1f}s）")

    # ---- 结论 ----
    print()
    print("=" * 92)
    print("结论")
    print("=" * 92)
    bd, vd = tot["bm25"]["decided"], tot["verified"]["decided"]
    bf, vf = tot["bm25"]["fabricated"], tot["verified"]["fabricated"]
    print(f"对未覆盖型判定点下结论：bm25 {bd} 条 → verified {vd} 条")
    print(f"其中无依据编造：        bm25 {bf} 条 → verified {vf} 条")
    print(f"已覆盖项漏判（回归）：  {'无' if reg_lost == 0 else f'{reg_lost} 组出现退化'}")
    print()
    if bf == 0 and vf == 0:
        print("→ ⚪ 未观察到编造，两种模式在本次材料上无差异。")
        print("  最可能的原因：生成层的判定纪律（Week 1 已验证）已经足够强，")
        print("  在检索层给出'看起来相关'的内容时，它仍会拒绝据此下结论。")
        print("  要观察到差异，需要能诱发生成层误信的更强材料。")
    elif vf < bf:
        print(f"→ ✅ 拒答接入有效：无依据编造从 {bf} 条降到 {vf} 条。")
    else:
        print("→ ❌ 反效果。")
    if reg_lost:
        print(f"→ ⚠️ 但接入带来了回归：{reg_lost} 组已覆盖项出现漏判（矫枉过正）。")
    print()
    print(f"产物：{OUT}\\e2e_*.report.json / .md（可人工复核每条判定的依据）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
