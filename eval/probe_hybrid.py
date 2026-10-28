"""验证：混合判定流程（抽取 → 路由 → 引擎优先）。

两级验证
--------
  A. 离线：手写 (attribute, claim) 直接走路由 + 引擎，零 API 调用。
     验证"路由表 + 引擎"这条确定性链路是否正确，含 3 个 LLM 判错的历史案例。
  B. 在线：对评测集里的小样本跑真实抽取（每份 1 次 LLM 调用），
     比较混合判定结果与人工标注的期望判定。

为什么要分开
------------
A 失败 = 我的路由/引擎写错了（可离线定位，零成本）。
B 失败 = 抽取层没抽出正确形态（需要改抽取提示词或词表）。
**混在一起测就分不清是哪一层的锅** —— 这个项目已经多次栽在这里。

用法
----
    python -m eval.probe_hybrid           # A + B（B 约 12 次 API 调用）
    python -m eval.probe_hybrid --offline # 只跑 A，零成本
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

from src.claim_extractor import Claim, route  # noqa: E402
from src.hybrid import evaluate_document, judge_claim  # noqa: E402
from src.rule_engine import RuleEngine  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EXPANDED = ROOT / "eval" / "generation_set_expanded.json"
HAND = ROOT / "eval" / "generation_set.json"

# ---- A. 离线路由/引擎断言：(说明, attribute, claim, 期望 rule, 期望 verdict) ----
OFFLINE: list[tuple[str, str, dict, str, str]] = [
    ("内边距 32 违规", "卡片内边距", {"value": 32}, "SPACING-001", "fail"),
    ("内边距 16 合规", "卡片内边距", {"value": 16}, "SPACING-001", "pass"),
    ("卡片间距 20 违规", "卡片间距", {"value": 20}, "SPACING-002", "fail"),
    ("组内=组间 【E009 LLM 判 pass】", "分组间距",
     {"inside": 24, "between": 24}, "SPACING-003", "fail"),
    ("组内16/组间24 合规", "分组间距",
     {"inside": 16, "between": 24}, "SPACING-003", "pass"),
    ("内容区 1440 超限 【E013 LLM 判 uncertain】", "内容区最大宽度",
     {"max_width": 1440}, "SPACING-004", "fail"),
    ("内容区 1200 合规", "内容区最大宽度", {"max_width": 1200}, "SPACING-004", "pass"),
    ("页面外边距 12 越界", "页面外边距", {"value": 12}, "SPACING-004", "fail"),
    ("可点击高度 32 偏低", "可点击高度", {"height": 32}, "SPACING-005", "fail"),
    ("元素间距 10 非 4 倍数", "元素间距", {"gap": 10}, "SPACING-005", "fail"),
    ("正文对比度 4.1 不达标", "文字对比度",
     {"value": 4.1, "font_size": 14}, "COLOR-001", "fail"),
    ("大号文字 3.2 达标（应按字号分流到 COLOR-002）", "文字对比度",
     {"value": 3.2, "font_size": 20}, "COLOR-002", "pass"),
    ("gray-300 原始色阶", "颜色引用方式", {"value": "gray-300"}, "COLOR-003", "fail"),
    ("语义 token 合规", "颜色引用方式",
     {"value": "color-text-primary"}, "COLOR-003", "pass"),
    ("字号 18 不在阶梯", "字号", {"value": 18}, "TYPO-001", "fail"),
    ("行高 20/26 = 1.3", "行高", {"size": 20, "line_height": 26}, "TYPO-002", "fail"),
    ("行高 14/21 = 1.5", "行高", {"size": 14, "line_height": 21}, "TYPO-002", "pass"),
    ("字重 300 过低", "字重", {"value": 300}, "TYPO-003", "fail"),
    ("正文默认字号 12 【E043 LLM 判 pass】", "正文默认字号",
     {"value": 12}, "TYPO-004", "fail"),
    ("只声明 variant", "属性声明", {"declared": ["variant"]}, "COMP-001", "fail"),
    ("placeholder-text 非 camelCase", "属性命名",
     {"name": "placeholder-text"}, "COMP-002", "fail"),
    ("showIcon 缺前缀", "布尔属性命名", {"name": "showIcon"}, "COMP-003", "fail"),
    ("缺 size 默认值", "属性默认值",
     {"defaults": {"variant": "primary"}}, "COMP-004", "fail"),
    ("变体 tertiary 非法", "变体取值", {"value": "tertiary"}, "COMP-005", "fail"),
    ("btn-primary 非斜杠格式", "图层命名",
     {"name": "btn-primary-disabled"}, "NAME-001", "fail"),
    ("icon_button 非 PascalCase", "组件命名",
     {"name": "icon_button"}, "NAME-002", "fail"),
    ("Rectangle 45 默认名", "图层默认名", {"name": "Rectangle 45"}, "NAME-003", "fail"),
    ("colorTextPrimary 非 kebab", "令牌命名",
     {"name": "colorTextPrimary"}, "NAME-004", "fail"),
]


def part_a(engine: RuleEngine) -> tuple[int, int]:
    print("=" * 96)
    print("A. 离线路由 + 引擎断言（零 API）")
    print("=" * 96)
    print(f"{'':2}{'说明':<44}{'rule':<13}{'期望':<7}{'实际':<7}")
    print("-" * 96)
    ok_n = 0
    for desc, attr, claim, want_rule, want_verdict in OFFLINE:
        rid, routed, note = route(attr, claim)
        j = judge_claim(Claim(desc, attr, claim), engine)
        ok = (rid == want_rule) and (j.verdict == want_verdict)
        ok_n += ok
        mark = "✅" if ok else "❌"
        print(f"{mark} {desc:<44}{str(rid):<13}{want_verdict:<7}{j.verdict:<7}")
        if not ok:
            print(f"     期望 rule={want_rule} 实际 rule={rid}｜{note}")
            print(f"     detail: {j.detail}")
    print("-" * 96)
    print(f"通过 {ok_n}/{len(OFFLINE)}")
    return ok_n, len(OFFLINE)


def part_b(engine: RuleEngine, limit: int) -> tuple[int, int]:
    print()
    print("=" * 96)
    print(f"B. 在线抽取 + 混合判定（{limit} 份样本，每份 1 次抽取调用）")
    print("=" * 96)

    cases: list[dict] = []
    for p in (EXPANDED, HAND):
        if p.exists():
            cases.extend(json.loads(p.read_text(encoding="utf-8"))["cases"])
    # 只挑「单一判定点、期望 pass/fail、非排除型」的样本
    pick = [c for c in cases
            if len(c["points"]) == 1
            and c["points"][0]["expect"] in ("pass", "fail")
            and "例外" not in c["points"][0].get("rule", "")][:limit]

    print(f"{'案例':<7}{'期望':<7}{'混合判定':<9}{'来源':<10}{'属性':<14}{'rule':<13}说明")
    print("-" * 96)
    ok_n = 0
    for c in pick:
        want = c["points"][0]["expect"]
        rep = evaluate_document(c["doc"], engine)
        if not rep.judgments:
            print(f"{c['id']:<7}{want:<7}{'(无抽取结果)':<9}{'-':<10}{'-':<14}{'-':<13}抽取失败")
            continue
        j = rep.judgments[0]
        ok = j.verdict == want
        ok_n += ok
        mark = "✅" if ok else "❌"
        print(f"{mark}{c['id']:<6}{want:<7}{j.verdict:<9}{j.source:<10}{j.attribute:<14}{j.rule_id:<13}"
              f"{j.detail[:40]}")
        if len(rep.judgments) > 1:
            print(f"      ⚠️ 抽取产出 {len(rep.judgments)} 条，只比较第一条")
    print("-" * 96)
    print(f"通过 {ok_n}/{len(pick)}")
    return ok_n, len(pick)


def main() -> int:
    ap = argparse.ArgumentParser(description="混合判定流程验证")
    ap.add_argument("--offline", action="store_true", help="只跑离线部分（零 API）")
    ap.add_argument("--limit", type=int, default=12, help="在线样本数")
    args = ap.parse_args()

    engine = RuleEngine()
    k, n = engine.coverage()

    print("混合判定：抽取(LLM) → 路由(闭集) → 引擎优先，未知回落")
    print(f"引擎覆盖 {k}/{n} 条规则\n")

    a_ok, a_n = part_a(engine)
    if args.offline:
        print("\n[--offline] 跳过在线部分")
        return 0 if a_ok == a_n else 1

    b_ok, b_n = part_b(engine, args.limit)

    print()
    print("=" * 96)
    print(f"合计：离线 {a_ok}/{a_n}｜在线 {b_ok}/{b_n}")
    print("=" * 96)
    print("判据：A 必须全过（否则是路由/引擎的锅）；B 反映抽取层质量。")
    return 0 if a_ok == a_n else 1


if __name__ == "__main__":
    raise SystemExit(main())
