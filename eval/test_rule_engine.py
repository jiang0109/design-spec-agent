"""规则引擎单元测试：确定性判定的离线验证（零 API 调用）。

为什么需要这套测试
------------------
规则引擎的价值在于**可复现** —— 同一输入永远同一结论。
但"可复现"只有在被测试锁定时才有意义，否则改一行解析就会静默判错。

所以这里用断言覆盖每一种约束类型，并且**专门收录 3 个 LLM 判错的真实案例**：

    E009  组内 24 / 组间 24   → LLM 判 pass   引擎判 fail（组间未严格大于组内）
    E013  内容区最大宽度 1440 → LLM 判 uncertain 引擎判 fail（超 1200 上限）
    E043  正文默认字号 12px   → LLM 判 pass   引擎判 fail（默认值须为 14）

这 3 条正是"把可计算的事交给模型去猜"造成的错误，本测试同时是它们的回归用例。

用法
----
    python -m eval.test_rule_engine
"""

from __future__ import annotations

import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.rule_engine import RuleEngine  # noqa: E402

# (说明, rule_id, claim, 期望 verdict)
CASES: list[tuple[str, str, dict, str]] = [
    # ---- allowed 白名单 ----
    ("内边距 32 不在 {12,16,24}", "SPACING-001", {"value": 32}, "fail"),
    ("内边距 16 合规", "SPACING-001", {"value": 16}, "pass"),
    ("内边距 12 合规（紧凑档）", "SPACING-001", {"value": 12}, "pass"),
    ("字号 18 不在六档内", "TYPO-001", {"value": 18}, "fail"),
    ("字号 20 合规（标题档）", "TYPO-001", {"value": 20}, "pass"),
    ("变体 tertiary 不在枚举内", "COMP-005", {"value": "tertiary"}, "fail"),
    ("变体 ghost 合规", "COMP-005", {"value": "ghost"}, "pass"),
    # ---- step / min_touch_target（多约束）----
    ("间距 10 不是 4 的倍数", "SPACING-005", {"gap": 10}, "fail"),
    ("间距 8 与高度 40 均合规", "SPACING-005", {"gap": 8, "height": 40}, "pass"),
    ("可点击高度 32 低于 40", "SPACING-005", {"height": 32}, "fail"),
    ("间距 8 合规但高度 36 违规 → 仍应 fail", "SPACING-005", {"gap": 8, "height": 36}, "fail"),
    # ---- multiplier 倍数 ----
    ("行高 26 / 字号 20 = 1.3 ≠ 1.5", "TYPO-002", {"size": 20, "line_height": 26}, "fail"),
    ("行高 21 / 字号 14 = 1.5", "TYPO-002", {"size": 14, "line_height": 21}, "pass"),
    ("行高 48 / 字号 32 = 1.5", "TYPO-002", {"size": 32, "line_height": 48}, "pass"),
    # ---- constraint 关系式（含 LLM 判错的 E009）----
    ("组内 24 = 组间 24 → 未严格大于【E009 LLM 判 pass】",
     "SPACING-003", {"inside": 24, "between": 24}, "fail"),
    ("组内 16 / 组间 24 → 满足",
     "SPACING-003", {"inside": 16, "between": 24}, "pass"),
    # ---- max / range（含 LLM 判错的 E013）----
    ("内容区宽度 1440 超 1200 上限【E013 LLM 判 uncertain】",
     "SPACING-004", {"max_width": 1440}, "fail"),
    ("内容区宽度 1200 恰好在上限", "SPACING-004", {"max_width": 1200}, "pass"),
    ("页面外边距 12 不在区间 16-24", "SPACING-004", {"value": 12}, "fail"),
    ("页面外边距 16 合规", "SPACING-004", {"value": 16}, "pass"),
    # ---- default 默认值（含 LLM 判错的 E043）----
    ("正文默认字号 12 ≠ 14【E043 LLM 判 pass】", "TYPO-004", {"value": 12}, "fail"),
    ("正文默认字号 14 合规", "TYPO-004", {"value": 14}, "pass"),
    # ---- required_pattern / forbidden_pattern（语义 token）----
    ("gray-300 是原始色阶，禁止", "COLOR-003", {"value": "gray-300"}, "fail"),
    ("#F5F7FA 硬编码十六进制，禁止", "COLOR-003", {"value": "#F5F7FA"}, "fail"),
    ("color-text-primary 是语义 token", "COLOR-003", {"value": "color-text-primary"}, "pass"),
    # ---- pattern 命名格式 ----
    ("placeholder_text 非 camelCase", "COMP-002", {"name": "placeholder_text"}, "fail"),
    ("placeholderText 合规", "COMP-002", {"name": "placeholderText"}, "pass"),
    ("showIcon 缺 is/has/can 前缀", "COMP-003", {"name": "showIcon"}, "fail"),
    ("hasIcon 合规", "COMP-003", {"name": "hasIcon"}, "pass"),
    ("icon_button 非 PascalCase", "NAME-002", {"name": "icon_button"}, "fail"),
    ("DataTable 合规", "NAME-002", {"name": "DataTable"}, "pass"),
    ("Rectangle 45 是自动默认名", "NAME-003", {"name": "Rectangle 45"}, "fail"),
    ("Button/Primary/Disabled 符合斜杠格式", "NAME-001", {"name": "Button/Primary/Disabled"}, "pass"),
    # ---- required 存在性 ----
    ("只声明 variant，缺 size", "COMP-001", {"declared": ["variant"]}, "fail"),
    ("size 与 variant 均已声明", "COMP-001", {"declared": ["size", "variant"]}, "pass"),
    # ---- defaults 默认值存在性 ----
    ("只有 variant 默认值，缺 size", "COMP-004", {"defaults": {"variant": "primary"}}, "fail"),
    ("size 与 variant 均有默认值", "COMP-004",
     {"defaults": {"size": "medium", "variant": "primary"}}, "pass"),
]


def main() -> int:
    engine = RuleEngine()
    k, n = engine.coverage()

    print("=" * 96)
    print("规则引擎单元测试（离线，零 API 调用）")
    print("=" * 96)
    print(f"可确定性判定的规则：{k}/{n}")
    print(f"测试用例：{len(CASES)}\n")

    fails: list[tuple[str, str, str, str]] = []
    for desc, rid, claim, want in CASES:
        r = engine.evaluate(rid, claim)
        ok = r.verdict == want
        mark = "✅" if ok else "❌"
        print(f"  {mark} {desc}")
        print(f"      {rid} {claim} → {r.verdict}（期望 {want}）")
        if r.field_name:
            print(f"      字段={r.field_name} 期望值={r.expected} 实际={r.actual}")
        print(f"      {r.detail}")
        if not ok:
            fails.append((desc, rid, want, r.verdict))

    print()
    print("=" * 96)
    if fails:
        print(f"❌ {len(fails)}/{len(CASES)} 失败：")
        for d, rid, want, got in fails:
            print(f"   - {d}：{rid} 期望 {want} 实际 {got}")
        return 1
    print(f"✅ 全部 {len(CASES)} 项通过")
    print()
    print("注意：本测试只验证『判定步骤』。从自然语言文档里抽取 claim 仍由 LLM 完成，")
    print("      抽取质量不在本测试范围内。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
