"""验证：判定器 v3（aspect 覆盖判据）能否修好剩余 3 处误拒，且不破坏拒答。

背景：一次诊断反转（务必保留）
------------------------------
120 点评测剩余 3 处「过度拒答」，我最初判定为**判定器适用性判断太严格**。
但把评测记录里 agent 实际发出的查询语句拉出来看，情况不同：

  E016 问「按钮高度允许哪几档取值」        SPACING-005 管的是高度下限 ≥40，不是档位
  E024 问「分隔线颜色引用应使用哪个色阶变量」 COLOR-003 恰恰**禁止**色阶变量
  E060 问「图层名称是否必须与内容一致」      NAME-003 管的是默认名称，不管一致性

也就是说：**问题本身预设了知识库不支持的框架，判定器拒得对。**
真正的缺陷在查询表述（agent 怎么问），不是适用性判断。

但从产品角度，正确行为应是：规则仍然**回答了**这个问题（"你不该用色阶变量"）。
所以值得让 v3 接受它 —— v3 的判据是"规则是否规制了问题所问的**方面**"，
而 E024 问的就是"颜色如何引用"这个方面。

本脚本对照 v1 与 v3 在三组上的表现
----------------------------------
  targets    3 条：期望 v3 能接受（max >= 2）
  allowlist  3 条：本应被接受（排除型/正常），期望 v3 仍接受
  reject    10 条：无覆盖，期望 v3 仍拒（max <= 1）

三组必须同时满足才算成功 —— 只修误拒而抬高误收，是净损失。

用法
----
    python -m eval.probe_applicability
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

from src.chunking import build_chunks, load_sections  # noqa: E402
from src.relevance import score_relevance  # noqa: E402
from src.retrieve import BM25  # noqa: E402

# (标签, 查询, 期望, 说明) —— 查询语句取自 eval 结果里 agent 实际发出的内容
TARGETS = [
    ("E016", "按钮高度允许哪几档取值", ">=", "SPACING-005 的 statement 明写可点击元素高度 ≥40"),
    ("E024", "分隔线（Divider）的颜色引用应使用哪个色阶变量", ">=", "COLOR-003 正好规制颜色引用方式"),
    ("E060", "图标图层名称命名规范，图层名称是否必须与内容一致", ">=", "NAME-003 正则直接匹配 Rectangle 45"),
]
ALLOWLIST = [
    ("N1", "卡片内边距允许哪几档取值", ">=", "SPACING-001 正常接受（常规路径回归）"),
    ("N2", "字号允许使用哪几档", ">=", "TYPO-001 正常接受"),
    ("N3", "正文文字对比度最低要达到多少", ">=", "COLOR-001 正常接受"),
]
REJECT = [
    ("G26", "卡片圆角允许使用哪些取值", "<=", "无覆盖：圆角"),
    ("G27", "Tooltip 悬浮提示的投影规范要求", "<=", "无覆盖：投影"),
    ("G28", "抽屉 Drawer 出现动效时长 淡入 规范要求", "<=", "无覆盖：动效"),
    ("G33", "数据表格行内边距允许哪几档", "<=", "无覆盖：同术语不同对象"),
    ("G34", "表头与首行数据之间的间距允许哪几档", "<=", "无覆盖：间距术语重叠"),
    ("G35", "卡片边框宽度允许使用哪些取值", "<=", "无覆盖：边框"),
    ("Q30", "数据表格的行内边距可以用 8px 吗", "<=", "无覆盖：表格内边距"),
    ("Q31", "深色模式下容器的颜色值怎么取", "<=", "无覆盖：深色模式（易被规则部分回应）"),
    ("E071", "卡片圆角允许使用哪几档取值", "<=", "无覆盖：圆角变体"),
    ("G32", "深色模式下容器之间的分隔线样式，1px 实线是否合规", "<=", "无覆盖：分隔线样式"),
]

MAX_JUDGE = 10


def max_score(index: BM25, query: str, version: int) -> tuple[int, str, int]:
    """按 BM25 顺序判定，>=2 即停；返回 (max_score, 命中的 section, 判定次数)。"""
    cands = index.search(query, top_k=MAX_JUDGE)
    best, best_sec, n = 0, "", 0
    for c in cands:
        s, _ = score_relevance(query, c.chunk, version=version)
        n += 1
        if s > best:
            best, best_sec = s, c.chunk.section_id
        if best >= 2:
            break
    return best, best_sec, n


def main() -> int:
    sections = load_sections()
    index = BM25(build_chunks("heading_split", sections, max_chars=300))

    print("=" * 92)
    print("判定器 v1（原） vs v3（aspect 覆盖）—— 三组必须同时满足才算成功")
    print("=" * 92)

    total_ok = 0
    total = 0

    for group_name, rows, want in (
        ("targets（应被接受）", TARGETS, ">="),
        ("allowlist（应被接受）", ALLOWLIST, ">="),
        ("reject（应被拒绝）", REJECT, "<="),
    ):
        print(f"\n【{group_name}】期望 max {want} {'2' if want == '>=' else '1'}")
        print(f"{'标签':<8}{'v1':>4}{'v3':>4}{'变化':>6}  {'命中 section（v3）':<16}{'判定次数':>9}")
        print("-" * 92)
        for tid, q, _, note in rows:
            v1, _, _ = max_score(index, q, 1)
            v3, sec, n = max_score(index, q, 3)
            ok = (v3 >= 2) if want == ">=" else (v3 <= 1)
            total += 1
            total_ok += bool(ok)
            mark = "✅" if ok else "❌"
            arrow = "→" if v1 == v3 else ("↑" if v3 > v1 else "↓")
            print(f"{tid:<8}{v1:>4}{v3:>4}{arrow:>5} {mark} {sec:<16}{n:>9}")
            print(f"        {note}")

    print()
    print("=" * 92)
    print(f"通过 {total_ok}/{total}")
    print("=" * 92)
    print("判据：targets 与 allowlist 必须 >=2，reject 必须 <=1。")
    print("只修误拒而抬高误收（reject 组出现 >=2）= 净损失，不能上线。")
    return 0 if total_ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
