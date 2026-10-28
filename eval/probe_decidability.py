"""统计：120 个判定点里，有多少的「判定步骤」可以交给确定性代码执行。

为什么问这个
------------
生成层评测剩下 5 处错误，其中两处（E009 组间/组内关系、E043 允许字号 vs
允许默认字号）不是"模型不懂"，而是**我们把可计算的事交给了模型去猜**。

知识库里这些约束其实是写死的：
    SPACING-003  constraint: between_groups > inside_group
    TYPO-002     multiplier: 1.5
    SPACING-005  min_touch_target: 40
    TYPO-001     allowed: [12,14,16,20,24,32]

如果"取值比对"这一步换成代码执行，这类错误就不是靠调提示词修，而是**结构性消失**。

必须先讲清的边界
----------------
"规则可确定性判定" ≠ "整条链路可确定性判定"：

    抽取（自然语言 → 具体取值）  仍需 LLM / 正则解析，**本统计假定它已经完成**
    判定（取值 → 合规/违规）      ← 本统计关心的部分

所以下文的"可确定性"一律指**判定步骤**。这个口径直接回答"哪类错误会消失"。

分类桶
------
  enum      取值枚举 / 白名单比对
  numeric   阈值 / 区间比对
  formula   倍数或关系式（行高=字号×1.5、组间>组内）
  format    正则 / 命名格式
  presence  声明或默认值的存在性
  coverage  覆盖度判断（无覆盖 / 排除型）—— 需要语义，**不可确定性**
  judgment  其余语义判断（状态色误用等）—— 需要语义，**不可确定性**

用法
----
    python -m eval.probe_decidability
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
DATASETS = [
    ROOT / "eval" / "generation_set.json",
    ROOT / "eval" / "generation_set_expanded.json",
]

# 每个规则桶归属：依据是规则 params 里有没有可直接执行的约束
RULE_BUCKET: dict[str, str] = {
    "SPACING-001": "enum",       # allowed [12,16,24]
    "SPACING-002": "enum",       # allowed [16,24]
    "SPACING-003": "formula",    # constraint between > inside
    "SPACING-004": "numeric",    # range 16-24 / max_content_width 1200
    "SPACING-005": "formula",    # step 4 / min_touch_target 40
    "COLOR-001": "numeric",      # contrast >= 4.5
    "COLOR-002": "numeric",      # contrast >= 3
    "COLOR-003": "format",       # 语义 token vs 原始色值（模式可判别）
    "COLOR-004": "enum",         # 六类语义色
    "COLOR-005": "judgment",     # 状态色是否用于非状态语义 —— 需语义
    "TYPO-001": "enum",          # allowed 六档
    "TYPO-002": "formula",       # 行高 = 字号 × 1.5
    "TYPO-003": "enum",          # 字重白名单
    "TYPO-004": "numeric",       # 默认字号 == 14
    "COMP-001": "presence",      # 必须声明 size 与 variant
    "COMP-002": "format",        # camelCase 正则
    "COMP-003": "format",        # 布尔前缀正则
    "COMP-004": "presence",      # 必须有默认值
    "COMP-005": "enum",          # 变体取值白名单
    "NAME-001": "format",        # 组件名/修饰/状态
    "NAME-002": "format",        # PascalCase
    "NAME-003": "format",        # 默认名称正则
    "NAME-004": "format",        # kebab-case 三段
}

DETERMINISTIC = {"enum", "numeric", "formula", "format", "presence"}
SEMANTIC = {"coverage", "judgment"}


def bucket_of(rule: str) -> str:
    r = rule.strip()
    if "例外" in r:
        return "coverage"          # 排除型：需要判断对象是否被例外覆盖
    if r.startswith("(无覆盖"):
        return "coverage"
    base = r.split(" ")[0]
    return RULE_BUCKET.get(base, "judgment")


def load_points() -> list[dict]:
    out = []
    for p in DATASETS:
        if not p.exists():
            continue
        for c in json.loads(p.read_text(encoding="utf-8"))["cases"]:
            for pt in c["points"]:
                out.append({**pt, "case_id": c["id"], "doc": c["doc"]})
    return out


def main() -> int:
    points = load_points()
    total = len(points)

    buckets: dict[str, list[dict]] = {}
    for pt in points:
        buckets.setdefault(bucket_of(pt["rule"]), []).append(pt)

    print("=" * 84)
    print(f"判定步骤可确定性化程度分析（共 {total} 个判定点）")
    print("=" * 84)
    print(f"{'桶':<12}{'点数':>6}{'占比':>8}  说明")
    print("-" * 84)
    desc = {
        "enum": "取值白名单比对",
        "numeric": "阈值 / 区间比对",
        "formula": "倍数或关系式（行高=字号×1.5、组间>组内）",
        "format": "正则 / 命名格式匹配",
        "presence": "声明与默认值的存在性检查",
        "coverage": "覆盖度判断（需语义）",
        "judgment": "其余语义判断（需语义）",
    }
    for b in ["enum", "numeric", "formula", "format", "presence", "coverage", "judgment"]:
        ps = buckets.get(b, [])
        if not ps:
            continue
        print(f"{b:<12}{len(ps):>6}{len(ps)/total*100:>7.0f}%  {desc[b]}")

    det = [p for b in DETERMINISTIC for p in buckets.get(b, [])]
    sem = [p for b in SEMANTIC for p in buckets.get(b, [])]
    print("-" * 84)
    print(f"{'可确定性':<12}{len(det):>6}{len(det)/total*100:>7.0f}%")
    print(f"{'需语义判断':<12}{len(sem):>6}{len(sem)/total*100:>7.0f}%")

    # ---- 各桶错误率：这才是最有信息量的读数 ----
    res_path = ROOT / "out" / "generation_eval.json"
    if res_path.exists():
        data = json.loads(res_path.read_text(encoding="utf-8"))

        # 顺序对齐：评测脚本与这里都按「数据集顺序 → 文档内 points 顺序」遍历，
        # 所以两边逐点 zip 一定对得上。第一版用 target 字符串匹配，
        # 结果桶里的点没有 target 字段，全部匹配失败、错误率恒为 0 —— 假绿。
        ordered = load_points()          # 与 run_generation_eval 相同顺序
        results = data["results"]
        errs_detail = []
        idx = 0
        for r in results:
            for p in r["points"]:
                if idx < len(ordered) and not p["correct"]:
                    ordered[idx]["_error"] = p["error"]
                    ordered[idx]["_actual"] = p["actual"]
                    errs_detail.append({**ordered[idx], "case_id": r["id"],
                                        "error": p["error"], "actual": p["actual"]})
                idx += 1
        if idx != len(ordered):
            print(f"⚠️ 对齐数量不符：results {idx} vs 数据集 {len(ordered)}，错误率不可信")

        print()
        print("=" * 84)
        print("各桶错误率 —— 错误集中在「可确定性」的一侧，还是「需语义」的一侧？")
        print("=" * 84)
        print(f"{'桶':<12}{'点数':>6}{'错误':>6}{'错误率':>9}  归类")
        print("-" * 84)
        det_err = det_n = sem_err = sem_n = 0
        for b in ["enum", "numeric", "formula", "format", "presence", "coverage", "judgment"]:
            ps = [p for p in ordered if bucket_of(p["rule"]) == b]
            if not ps:
                continue
            e = sum(1 for p in ps if "_error" in p)
            kind = "可确定性" if b in DETERMINISTIC else "需语义"
            print(f"{b:<12}{len(ps):>6}{e:>6}{e/len(ps)*100:>8.0f}%  {kind}")
            if b in DETERMINISTIC:
                det_n += len(ps); det_err += e
            else:
                sem_n += len(ps); sem_err += e
        print("-" * 84)
        print(f"{'合计·可确定性':<12}{det_n:>6}{det_err:>6}{det_err/max(1,det_n)*100:>8.0f}%")
        print(f"{'合计·需语义':<12}{sem_n:>6}{sem_err:>6}{sem_err/max(1,sem_n)*100:>8.0f}%")

        print()
        print("=" * 84)
        print(f"当前 {len(errs_detail)} 处错误逐个定位")
        print("=" * 84)
        print(f"{'案例':<8}{'桶':<12}{'错误类型':<26}{'真正需要什么'}")
        print("-" * 84)
        for e in errs_detail:
            b = bucket_of(e["rule"])
            if e["error"].startswith("过度拒答"):
                need = "适用性映射（规则管不管这个对象）"
            else:
                need = "取值抽取 + 确定性判定执行"
            print(f"{e['case_id']:<8}{b:<12}{e['error']:<26}{need}")
        print("-" * 84)
        print("⚠️ 关键区分：把判定步骤换成代码，只能解决『取值比对算错』这一类；")
        print("   『规则是否适用于这个对象』属于适用性判断，需要额外的结构化映射。")
        print("   3 处过度拒答属于后者 —— 规则引擎单独修不掉。")

    # ---- 明细（供人工复核分类是否合理）----
    print()
    print("=" * 84)
    print("各桶明细（前 3 条，供复核分类）")
    print("=" * 84)
    for b in ["enum", "numeric", "formula", "format", "presence", "coverage", "judgment"]:
        ps = buckets.get(b, [])
        if not ps:
            continue
        print(f"\n【{b}】{len(ps)} 点")
        for p in ps[:3]:
            print(f"  {p['case_id']:<6}{p['rule']:<16}{p['expect']:<10}{p['why'][:52]}")

    print()
    print("=" * 84)
    print("口径提醒")
    print("=" * 84)
    print("上面的『可确定性』只针对**判定步骤**（取值 → 合规判定）。")
    print("从自然语言文档里抽取取值仍需 LLM/正则，这部分不可省。")
    print("所以混合架构的形态是：LLM 抽取 → 代码判定 → LLM 组织理由与建议。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
