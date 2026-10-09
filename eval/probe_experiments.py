"""对照实验 1：度量并修复"溯源漂移"缺陷。

缺陷定义
--------
模型在 evidence_sections 字段里会把检索返回的 section 改写掉：
    检索返回:  color.md#语义色
    模型输出:  color.md#语义色的使用      ← 扩写了
核心判据（evidence 原文）没问题，但"结论 → 出处"这条引用链断了，
规范更新后无法批量定位失效结论。

方法
----
同一份文档、同一温度，两组各跑 N 次：
    对照组  --loose-prompt  修复前的宽松表述
    实验组  （默认）        加严表述，明确禁止改写并给出正确方向的示例

漂移是间歇性的，单次跑没有统计意义，必须重复采样。

⚠️ 关于本实验的第一次失败（重要记录，不要删）
------------------------------------------------
第一次跑出来是"修复前 40% → 修复后 100%"，方向完全反了。排查发现
根本不是模型行为，而是两个自身 bug：
  1. 加严提示词里的示例方向写反了：知识库里实际是 "color.md#语义色"，
     我却在示例里写 "返回 color.md#语义色的使用 时不得写成 color.md#语义色"，
     等于亲手教模型去扩写。
  2. 提取器字段名写错（hit.get("section")，实际是 "section_id"），
     导致"实际检索到的 section"集合本身不可信 —— 指标是假的。
把示例方向改正后，实验才得出可信结论。
教训：**先验证指标，再相信指标。** 一个错误的度量比没有度量更危险，
因为它会让你朝错误的方向优化。

用法
----
    python -m eval.probe_experiments              # 两组各 5 次
    python -m eval.probe_experiments -n 8
    python -m eval.probe_experiments --only budget

注意：会真实调用 API，N 次 × 2 组产生 token 费用。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 与 cli.py 同理：避免 Windows 控制台 GBK 在打印 ✅/❌ 时崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.agent import run_agent  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "sample_doc.md"


def drift_probe(runs: int, loose: bool) -> dict:
    """重复采样，统计溯源漂移率。"""
    label = "对照组(宽松表述)" if loose else "实验组(加严表述)"
    print(f"\n{'=' * 62}\n{label} —— {runs} 次\n{'=' * 62}")

    doc = SAMPLE.read_text(encoding="utf-8")
    drifts = 0
    unverified = 0
    details: list[list[str]] = []
    total_tokens = 0

    for i in range(1, runs + 1):
        ar = run_agent(doc, doc_name=SAMPLE.name, loose_instruction=loose, verbose=False)
        total_tokens += ar.prompt_tokens + ar.completion_tokens

        if not ar.provenance_checked:
            unverified += 1
            print(f"  第 {i} 次: 指标不可信（检索结果解析失败），本次不计入")
            continue
        if ar.provenance_drift:
            drifts += 1
            details.append(ar.provenance_drift)
            print(f"  第 {i} 次: DRIFT  {ar.provenance_drift}")
        else:
            print(f"  第 {i} 次: ok     {len(ar.report.issues)} 条判定, 无漂移")

    valid = runs - unverified
    rate = drifts / valid * 100 if valid else 0.0
    print(f"\n  → 漂移 {drifts}/{valid} 次 = {rate:.0f}%（指标不可信 {unverified} 次）")
    print(f"  → 累计 token {total_tokens}")
    return {
        "label": label,
        "runs": runs,
        "valid": valid,
        "drifts": drifts,
        "rate": rate,
        "unverified": unverified,
        "details": details,
    }


def budget_probe(budget: int) -> dict:
    """验证工具调用总预算护栏：预算不足时，未查证的点是否敢判 uncertain。"""
    print(f"\n{'=' * 62}\n实验 2：预算护栏 —— tool_calls 上限 = {budget}\n{'=' * 62}")

    doc = SAMPLE.read_text(encoding="utf-8")
    ar = run_agent(doc, doc_name=SAMPLE.name, max_tool_calls=budget, verbose=True)

    print(f"\n  实际工具调用: {ar.tool_calls}（上限 {budget}）")
    print(f"  预算是否用尽: {ar.budget_exhausted}")
    print(f"  溯源漂移: {ar.provenance_drift or '无'}")
    print(f"  token: {ar.prompt_tokens}+{ar.completion_tokens}｜耗时 {ar.elapsed_sec}s")
    print("\n  逐条判定：")
    for issue in ar.report.issues:
        print(f"    - {issue.target}: {issue.verdict} | evidence={'有' if issue.evidence else '空'}")

    fabricated = [
        i.target for i in ar.report.issues if i.verdict != "uncertain" and not i.evidence
    ]
    print(f"\n  有判定但无依据（疑似编造）: {fabricated or '无'}")
    return {
        "budget": budget,
        "tool_calls": ar.tool_calls,
        "budget_exhausted": ar.budget_exhausted,
        "drift": ar.provenance_drift,
        "fabricated": fabricated,
        "verdicts": {i.target: i.verdict for i in ar.report.issues},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="溯源漂移与预算护栏对照实验")
    parser.add_argument("-n", "--runs", type=int, default=5, help="每组采样次数，默认 5")
    parser.add_argument(
        "--only", choices=["drift", "budget", "all"], default="all", help="只跑某一组实验"
    )
    args = parser.parse_args()

    if args.only in ("drift", "all"):
        loose = drift_probe(args.runs, loose=True)
        strict = drift_probe(args.runs, loose=False)
        print(f"\n{'=' * 62}\n实验 1 结论：溯源漂移\n{'=' * 62}")
        print(f"  修复前（宽松表述）: {loose['drifts']}/{loose['valid']} = {loose['rate']:.0f}%")
        print(f"  修复后（加严表述）: {strict['drifts']}/{strict['valid']} = {strict['rate']:.0f}%")

    if args.only in ("budget", "all"):
        budget_probe(2)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
