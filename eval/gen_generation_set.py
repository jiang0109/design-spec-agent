"""生成层评测集扩充器：从规则 params 批量生成判定点。

为什么用生成器而不是手写
------------------------
手写 70 个判定点有两个问题：极度冗长，且容易在措辞上引入歧义
（今天已经出现过两次：G32 "单独定义一套色值"、G35 "单独取值" 都隐含了违规，
导致期望值标错）。

生成器的做法：**探针表只写数据**（规则 / 取值 / 期望判定 / 推导依据），
文档由固定模板渲染。于是：
  - 期望判定的来源可审计（每行都写着为什么）
  - 措辞是确定性的，不会因人而异引入歧义
  - 扩充只需加表项，不需要写文章

代价：措辞比手写单调。所以**手写的 40 个难例（generation_set.json）保留不动**，
两者合并评测 —— 手写负责"难与自然"，生成负责"广与可复现"。

设计硬约束
----------
1. 每个探针的 expect 必须能由规则的 params 直接推出（allowed / range / pattern / 例外）。
2. 一份文档只放一个判定点（少量两点），避免模型合并 target 导致对齐失败。
3. 模板块交替使用两种结构，避免全套文档格式完全一致。

用法
----
    python -m eval.gen_generation_set            # 生成 generation_set_expanded.json
    python -m eval.gen_generation_set --check    # 只统计不写文件
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

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "eval" / "generation_set_expanded.json"

# ---------------------------------------------------------------
# 探针表：(规则, 期望, 文档标题, 判定点标签, 陈述句, 推导依据)
# 期望判定的依据一律来自 kb/ 下规则的 params 或例外条款。
# ---------------------------------------------------------------
# 元组可为 6 项 (规则,期望,标题,标签,陈述,依据) 或 7 项（末项为额外关键词列表）。
# 额外关键词用于模型把一句话拆成多个 target 时仍能对齐。
PROBES: list = [
    # ---------------- SPACING-001 卡片内边距 allowed {12,16,24} ----------------
    ("SPACING-001", "pass", "紧凑列表卡片", "内边距", "内边距使用 12px", "12 在 allowed {12,16,24}（compact_list 档）"),
    ("SPACING-001", "pass", "营销位大卡片", "内边距", "内边距使用 24px", "24 在 allowed {12,16,24}（marketing 档）"),
    ("SPACING-001", "fail", "常规内容卡片", "内边距", "内边距使用 32px", "32 不在 allowed {12,16,24}"),
    ("SPACING-001", "fail", "紧凑列表卡片", "内边距", "内边距使用 8px", "8 不在 allowed {12,16,24}"),
    # ---------------- SPACING-002 卡片间距 allowed {16,24} ----------------
    ("SPACING-002", "pass", "分组内容区", "卡片间距", "不同分组之间的间距为 24px", "between_groups 映射为 24，且在 allowed {16,24}"),
    ("SPACING-002", "fail", "分组内容区", "卡片间距", "同组卡片之间的间距为 12px", "12 不在 allowed {16,24}"),
    ("SPACING-002", "pass", "分组内容区", "卡片间距", "同组卡片之间的间距为 16px 且均相同", "16 在 allowed {16,24}，且同组一致"),
    # ---------------- SPACING-003 组间距必须 > 组内且 ≥24 ----------------
    ("SPACING-003", "pass", "仪表盘分组", "分组间距", "组内间距 16px，组间间距 24px", "24 > 16 且 ≥ min_between_groups 24", ["组间", "组内"]),
    ("SPACING-003", "fail", "仪表盘分组", "分组间距", "组内间距 24px，组间间距 24px", "组间未严格大于组内", ["组间", "组内"]),
    # ---------------- SPACING-004 页面外边距 range 16-24 / max width 1200 ----------------
    ("SPACING-004", "pass", "窄屏布局", "页面外边距", "窄屏左右外边距为 16px", "窄屏映射为 16，在 range 16-24"),
    ("SPACING-004", "fail", "桌面端布局", "页面外边距", "桌面端左右外边距为 8px", "8 低于 range 下限 16"),
    ("SPACING-004", "pass", "内容区布局", "内容区最大宽度", "内容区最大宽度为 1200px", "等于 max_content_width 1200"),
    ("SPACING-004", "fail", "内容区布局", "内容区最大宽度", "内容区最大宽度为 1440px", "超过 max_content_width 1200"),
    # ---------------- SPACING-005 内部间距 4 的倍数 / 点击高度 ≥40 ----------------
    ("SPACING-005", "pass", "搜索框", "图标与文字间距", "图标与文字的间距为 12px，点击区高度 44px", "12 是 4 的倍数，44 ≥ min_touch_target 40"),
    ("SPACING-005", "fail", "搜索框", "图标与文字间距", "图标与文字的间距为 10px", "10 不是 4 的倍数"),
    ("SPACING-005", "fail", "标签按钮", "按钮高度", "按钮高度为 32px", "32 < min_touch_target 40"),
    # ---------------- COLOR-001 正文对比度 ≥4.5 ----------------
    ("COLOR-001", "pass", "正文文本", "文字对比度", "正文文字与背景对比度 4.5:1", "4.5 ≥ 4.5 门槛"),
    ("COLOR-001", "fail", "正文文本", "文字对比度", "正文文字与背景对比度 3.8:1", "3.8 < 4.5"),
    ("COLOR-001", "pass", "正文文本", "文字对比度", "正文文字与背景对比度 7.2:1", "7.2 ≥ 4.5"),
    # ---------------- COLOR-002 大号文字 ≥3:1 ----------------
    ("COLOR-002", "pass", "数据大屏主指标", "对比度", "字号 32px 的主指标对比度 3.5:1", "32px ≥ 18px 属大号文字，3.5 ≥ 3"),
    ("COLOR-002", "fail", "数据大屏主指标", "对比度", "字号 32px 的主指标对比度 2.4:1", "大号文字门槛 3:1，2.4 < 3"),
    ("COLOR-002", "pass", "卡片标题", "对比度", "字号 14px 且字重 600 的标题对比度 3.1:1", "14px+600 属大号文字，3.1 ≥ 3"),
    # ---------------- COLOR-003 语义 token ----------------
    ("COLOR-003", "pass", "分隔线", "颜色引用", "颜色引用 color-border-subtle 语义令牌", "使用语义 token，符合"),
    ("COLOR-003", "fail", "分隔线", "颜色引用", "颜色引用 gray-300 色阶变量", "gray-300 是原始色值变量，禁止直接引用"),
    ("COLOR-003", "fail", "卡片背景", "颜色引用", "背景色硬编码为 #F5F7FA", "硬编码十六进制色值，禁止"),
    # ---------------- COLOR-004 语义色六类 ----------------
    ("COLOR-004", "pass", "提示条", "语义色类别", "使用 info 语义色表示一般提示", "info 属于允许的六类语义色"),
    ("COLOR-004", "fail", "提示条", "语义色类别", "使用 brand 作为额外语义色类别", "允许的语义色只有六类，brand 不在其中"),
    # ---------------- COLOR-005 状态色误用 ----------------
    ("COLOR-005", "fail", "活动角标", "状态色", "活动角标使用 warning 黄色作为装饰底色", "状态色不得用于非状态语义的装饰"),
    ("COLOR-005", "pass", "表单校验", "状态色", "校验失败提示使用 danger 红色", "danger 用于错误状态语义，合规"),
    # ---------------- TYPO-001 字号阶梯 ----------------
    ("TYPO-001", "pass", "文章正文", "字号", "正文使用 14px", "14 在 allowed {12,14,16,20,24,32}"),
    ("TYPO-001", "pass", "区块标题", "字号", "区块标题使用 20px", "20 在 allowed 且用于标题档"),
    ("TYPO-001", "fail", "区块标题", "字号", "区块标题使用 22px", "22 不在 allowed 六档内"),
    ("TYPO-001", "fail", "辅助文字", "字号", "辅助文字使用 13px", "13 不在 allowed 六档内"),
    # ---------------- TYPO-002 行高 = 字号 × 1.5 ----------------
    ("TYPO-002", "pass", "段落正文", "行高", "字号 16px，行高 24px", "24 / 16 = 1.5，严格等于"),
    ("TYPO-002", "pass", "展示型标题", "行高", "字号 32px，行高 48px", "48 / 32 = 1.5，严格等于"),
    ("TYPO-002", "fail", "段落正文", "行高", "字号 16px，行高 20px", "20 / 16 = 1.25，不等于 1.5"),
    ("TYPO-002", "fail", "区块标题", "行高", "字号 20px，行高 34px", "34 / 20 = 1.7，不等于 1.5"),
    # ---------------- TYPO-003 字重白名单 ----------------
    ("TYPO-003", "pass", "正文文本", "字重", "正文使用 400 字重", "400 在允许范围且正文不低于 400"),
    ("TYPO-003", "pass", "区块标题", "字重", "标题使用 600 字重", "600 在允许范围且标题不高于 600"),
    ("TYPO-003", "fail", "区块标题", "字重", "标题使用 700 字重", "700 超出自重上限 600"),
    ("TYPO-003", "fail", "正文文本", "字重", "正文使用 200 字重", "200 低于正文下限 400"),
    # ---------------- TYPO-004 正文默认 14 ----------------
    ("TYPO-004", "pass", "列表项", "正文默认字号", "正文默认字号为 14px", "等于规定的 14"),
    ("TYPO-004", "fail", "列表项", "正文默认字号", "正文默认字号为 12px", "不等于规定的 14"),
    # ---------------- COMP-001 必填属性 ----------------
    ("COMP-001", "pass", "抽屉组件 Drawer", "属性声明", "同时显式声明了 size 与 variant", "两者均已显式声明"),
    ("COMP-001", "fail", "抽屉组件 Drawer", "属性声明", "声明了 size，但未声明 variant", "缺 variant，二者必须同时声明"),
    # ---------------- COMP-002 属性命名 camelCase ----------------
    ("COMP-002", "pass", "输入框 TextField", "属性名", "属性名使用 placeholderText", "camelCase 合规"),
    ("COMP-002", "fail", "输入框 TextField", "属性名", "属性名使用 placeholder_text", "下划线写法不符合 camelCase"),
    ("COMP-002", "fail", "输入框 TextField", "属性名", "属性名使用 placeholder-text", "kebab-case 明确禁止"),
    # ---------------- COMP-003 布尔前缀 ----------------
    ("COMP-003", "pass", "卡片 Card", "布尔属性", "布尔属性使用 hasIcon", "以 has 开头，合规"),
    ("COMP-003", "pass", "弹窗 Modal", "布尔属性", "布尔属性使用 canClose", "以 can 开头，合规"),
    ("COMP-003", "fail", "卡片 Card", "布尔属性", "布尔属性使用 showIcon", "缺 is/has/can 前缀"),
    # ---------------- COMP-004 默认值 ----------------
    ("COMP-004", "pass", "标签 Tag", "属性默认值", "size 默认 medium，variant 默认 primary", "两个必填属性均有默认值"),
    ("COMP-004", "fail", "标签 Tag", "属性默认值", "为 variant 设置了默认值，但 size 无默认值", "size 缺默认值", ["size"]),
    # ---------------- COMP-005 变体取值枚举 ----------------
    ("COMP-005", "pass", "按钮 Button", "变体取值", "variant 取值使用 ghost", "ghost 在 allowed 四类中"),
    ("COMP-005", "fail", "按钮 Button", "变体取值", "variant 取值使用 outline", "outline 不在 allowed 四类中"),
    # ---------------- NAME-001 图层命名 ----------------
    ("NAME-001", "pass", "按钮图层", "图层命名", "图层命名为 Button/Primary/Disabled", "符合「组件名/修饰/状态」格式"),
    ("NAME-001", "fail", "按钮图层", "图层命名", "图层命名为 Button Primary Disabled", "未使用斜杠分层"),
    # ---------------- NAME-002 组件名 PascalCase ----------------
    ("NAME-002", "pass", "表格组件", "组件命名", "组件命名为 DataTable", "PascalCase 合规"),
    ("NAME-002", "fail", "表格组件", "组件命名", "组件命名为 dataTable", "首字母未大写，不符合 PascalCase"),
    # ---------------- NAME-003 禁止默认名 ----------------
    ("NAME-003", "fail", "图标图层", "图层名称", "图层名称仍为 Rectangle 45", "Rectangle 45 是自动生成的默认名称"),
    ("NAME-003", "pass", "图标图层", "图层名称", "图层名称已改为 Icon/Search/Default", "已重命名为语义化格式"),
    # ---------------- NAME-004 令牌 kebab-case 三段 ----------------
    ("NAME-004", "pass", "间距令牌", "令牌命名", "间距令牌命名为 space-card-padding", "kebab-case 三段式合规"),
    ("NAME-004", "fail", "间距令牌", "令牌命名", "间距令牌命名为 space_card_padding", "下划线不符合 kebab-case"),
    # ---------------- 排除型：规则明确排除该对象 ----------------
    ("SPACING-001 例外", "pass", "全屏播放页", "卡片内边距", "全屏沉浸式视图内卡片内边距使用 8px", "SPACING-001 例外：全屏沉浸式视图内的卡片不受此限制"),
    ("SPACING-004 例外", "pass", "全出血横幅", "页面外边距", "全出血横幅左右外边距为 0", "SPACING-004 例外：全出血横幅与背景图不受外边距限制"),
    ("COMP-001 例外", "pass", "分割线 Divider", "属性声明", "分割线 Divider 未声明 size 与 variant", "COMP-001 例外：纯布局容器（Container / Divider）不声明"),
    ("TYPO-002 例外", "pass", "按钮标签", "行高", "按钮单行标签未设置 1.5 倍行高", "TYPO-002 例外：单行文本（按钮标签、徽标、表格单元格）不参与行高倍数校验"),
    ("NAME-001 例外", "pass", "草稿画板", "图层命名", "一次性草稿画板内图层沿用任意命名", "NAME-001 例外：一次性草稿画板（Draft / Scratch）内的图层不受此限制"),
    ("NAME-003 例外", "pass", "历史图层", "图层名称", "已标记废弃且不参与交付的历史图层仍为 Group 7", "NAME-003 例外：明确标记为废弃且不参与交付的历史图层"),
    ("COLOR-003 例外", "pass", "品牌标识", "颜色引用", "品牌 Logo 位图内部使用多种原始色值", "COLOR-003 例外：品牌 Logo、插画与位图资源内部的多色取值不受此限制"),
    # ---------------- 无覆盖：应判 uncertain ----------------
    ("(无覆盖)", "uncertain", "卡片视觉", "圆角", "卡片圆角使用 6px", "知识库无任何圆角规则"),
    ("(无覆盖)", "uncertain", "阴影层级", "投影", "卡片投影使用 0 2px 8px", "知识库无投影规范"),
    ("(无覆盖)", "uncertain", "过渡动效", "动效时长", "侧边栏滑入动效时长 250ms", "知识库无动效时长规范"),
    ("(无覆盖)", "uncertain", "空状态", "插画尺寸", "空状态插画尺寸为 240px", "知识库无插画尺寸规范"),
    ("(无覆盖)", "uncertain", "字体族", "字体", "界面字体族使用 Inter", "知识库无字体族规范"),
    ("(无覆盖)", "uncertain", "图标", "图标尺寸", "图标尺寸使用 20px", "知识库无图标尺寸规范"),
    ("(无覆盖)", "uncertain", "表格行内边距", "行内边距", "表格行内边距使用 8px", "术语与卡片内边距重叠，但对象不同，无适用规则"),
    ("(无覆盖)", "uncertain", "表格表头间距", "表头间距", "表头与首行间距使用 12px", "SPACING-002 只管卡片间距，无表格表头间距"),
    ("(无覆盖)", "uncertain", "卡片边框宽度", "边框宽度", "卡片边框宽度使用 1px", "知识库无描边宽度规范"),
    ("(无覆盖)", "uncertain", "无障碍标签", "无障碍标签", "所有图标按钮需提供无障碍标签", "知识库无无障碍标签规范"),
]

# 模板块：交替使用，避免全套文档格式完全一致
TEMPLATES = [
    "# {title}\n\n**{label}**：{label}{text}。\n",
    "# {title}\n\n## 规格\n\n{label}：{label}{text}。\n",
]


def build() -> list[dict]:
    cases = []
    for i, probe in enumerate(PROBES, start=1):
        rule, expect, title, label, text, why = probe[:6]
        extra_kws = list(probe[6]) if len(probe) > 6 else []
        tpl = TEMPLATES[i % len(TEMPLATES)]
        doc = tpl.format(title=title, label=label, text=text)
        # 关键词：标签本身 + 文本里最有辨识度的前 4 字
        kws = [label] + extra_kws
        tail = text[:6]
        if tail and tail not in label:
            kws.append(tail)
        cases.append(
            {
                "id": f"E{i:03d}",
                "doc": doc,
                "points": [
                    {
                        "keywords": kws,
                        "rule": rule,
                        "expect": expect,
                        "why": why,
                    }
                ],
            }
        )
    return cases


def main() -> int:
    ap = argparse.ArgumentParser(description="生成层评测集扩充器")
    ap.add_argument("--check", action="store_true", help="只统计不写文件")
    args = ap.parse_args()

    cases = build()
    n = sum(len(c["points"]) for c in cases)

    by_expect: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    for c in cases:
        for p in c["points"]:
            by_expect[p["expect"]] = by_expect.get(p["expect"], 0) + 1
            key = p["rule"].split(" ")[0]
            by_rule[key] = by_rule.get(key, 0) + 1

    print(f"生成 {len(cases)} 份文档 / {n} 个判定点")
    print(f"期望分布：{by_expect}")
    print(f"规则覆盖：{len(by_rule)} 类")
    print()
    for k in sorted(by_rule):
        print(f"  {k:<18}{by_rule[k]}")

    # 自检：每条文档只有一个判定点、why 非空、expect 合法
    errs = []
    for c in cases:
        if len(c["points"]) != 1:
            errs.append(f"{c['id']} 判定点数不为 1")
        for p in c["points"]:
            if p["expect"] not in ("pass", "fail", "uncertain"):
                errs.append(f"{c['id']} expect 非法")
            if not p["why"]:
                errs.append(f"{c['id']} 缺 why")
            if not p["keywords"]:
                errs.append(f"{c['id']} 缺 keywords")
    if errs:
        print(f"\n❌ 自检失败：{errs[:5]}")
        return 1
    print("\n✅ 自检通过（单点文档 / expect 合法 / why 与 keywords 齐全）")

    if args.check:
        print("\n[--check] 未写入文件")
        return 0

    payload = {
        "_note": "由 eval/gen_generation_set.py 从探针表生成；期望判定全部由知识库规则的 params 推导。",
        "_design": [
            "手写难例在 generation_set.json，本文件负责「广与可复现」，两者合并评测。",
            "生成器只渲染固定模板，措辞确定性来自模板而非人工，避免引入歧义。",
            "新增判定点只需在 PROBES 表加一行数据。",
        ],
        "cases": cases,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
