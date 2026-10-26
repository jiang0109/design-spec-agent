"""实验：拒绝原因分类（reject_reason）能否修复端到端回归。

要解决的问题
------------
`e2e_report.md` 观察到的回归：**表格行高 1.6 倍** 由 `fail` 退化为 `uncertain`。

`structured_report.md` 查明真实根因不是检索层误拒（相关性判定本就把该 gold
传到生成层，relevance=3），而是**工具 schema 的措辞**：
把 `found=false` 单一信号等同于"必须判 uncertain"，
于是生成层无法区分下面两种本质上不同的情况：

| 情况 | 语义 | 生成层应做什么 |
|---|---|---|
| 规则**明确排除**该对象 | 该对象不受约束 → **有结论** | 判 pass |
| 知识库**无覆盖**该对象 | 无法判定 | 判 uncertain |

### 修复方案

给检索结果增加**结构化的拒绝原因**（独立于相关性分数的第二道判定）：

```
excluded        例外条款明确点出该对象 → 该对象不受约束 → 判 pass
not_applicable  规则讲的是另一个对象   → 判 uncertain
no_coverage     只是沾到主题边缘/无关   → 判 uncertain
```

同时改掉工具描述，把三种情况分别说明（原来只说"found=false → uncertain"）。

### 预注册判据（先写下来，避免事后编解释）

| 检查项 | 通过条件 |
|---|---|
| P1 表格行高 | 分类为 `excluded` |
| P2 表格行内边距 | 分类为 `not_applicable` 或 `no_coverage`（不是 excluded） |
| P3 深色模式容器颜色 | 分类为 `not_applicable` 或 `no_coverage` |
| 端到端回归 | `e2e_confusable.md` 的表格行高从 uncertain 恢复为 pass/fail |
| 幻觉不回归 | `e2e_novel.md` 与 P2/P3 仍不得判 pass/fail |

用法
----
    python -m eval.probe_reject_reason
    python -m eval.probe_reject_reason --classify-only   # 只测分类，不跑端到端
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
from src.relevance import classify_rejection  # noqa: E402
from src.retrieve import BM25  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
CACHE = ROOT / "eval" / ".cache" / "reject_reason.json"

# 预注册的探针与期望
PROBES = [
    ("P1", "表格单元格的行高用 1.6 倍可以吗", {"excluded"}, "例外明确排除表格单元格"),
    ("P2", "数据表格的行内边距可以用 8px 吗", {"not_applicable", "no_coverage"}, "无覆盖：无表格内边距规则"),
    ("P3", "深色模式下容器的颜色值怎么取", {"not_applicable", "no_coverage"}, "无覆盖：无深色模式定义"),
]


def load_cache() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return {}


def save_cache(c: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="拒绝原因分类实验")
    ap.add_argument("--classify-only", action="store_true", help="只测分类，不跑端到端")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    sections = load_sections()
    chunks = build_chunks("heading_split", sections, max_chars=300)
    index = BM25(chunks)
    cache = load_cache()

    # ---- 阶段 1：分类正确性 ----
    print("=" * 86)
    print("阶段 1：拒绝原因分类是否与预注册判据一致")
    print("=" * 86)
    all_ok = True
    for pid, q, expected, note in PROBES:
        cands = index.search(q, top_k=3)
        lead = cands[0].chunk if cands else None
        if lead is None:
            print(f"  {pid} 无候选，跳过")
            continue
        key = f"{pid}|{lead.section_id}"
        if not args.no_cache and key in cache:
            reason, detail = cache[key]["reason"], cache[key]["detail"]
        else:
            reason, detail = classify_rejection(q, lead)
            cache[key] = {"reason": reason, "detail": detail}
            save_cache(cache)
        ok = reason in expected
        all_ok = all_ok and ok
        mark = "✅" if ok else "❌"
        print(f"  {mark} {pid}  {q}")
        print(f"       首候选 {lead.section_id}（{lead.section_title}）")
        print(f"       分类={reason}  期望∈{sorted(expected)}  {detail}")
        print(f"       {note}")
    print()
    print(("→ ✅ 分类全部符合预注册判据" if all_ok else "→ ❌ 有分类不符合预注册判据"))

    if args.classify_only:
        return 0

    # ---- 阶段 2：端到端回归是否修复 ----
    print()
    print("=" * 86)
    print("阶段 2：端到端回归检查（接入 reject_reason 后）")
    print("=" * 86)

    from src.agent import run_agent  # 延迟导入，避免 classify-only 时依赖

    checks = []
    for docname in ("e2e_confusable.md", "e2e_covered.md", "e2e_novel.md"):
        path = SAMPLES / docname
        if not path.exists():
            print(f"  跳过（文件不存在）：{docname}")
            continue
        doc = path.read_text(encoding="utf-8")
        ar = run_agent(doc, doc_name=docname, llm_verify=True, verbose=False)
        print(f"\n  【{docname}】verified 模式")
        for i in ar.report.issues:
            tag = "有依据" if i.evidence else "无依据"
            print(f"    [{i.verdict:9}] {i.target:22} ({tag})")
            if docname == "e2e_confusable.md" and "行高" in i.target:
                checks.append(("回归项：表格行高不再是 uncertain", i.verdict != "uncertain", i.verdict))
            if docname == "e2e_novel.md":
                checks.append(
                    (f"幻觉项：{i.target} 不得下结论", i.verdict == "uncertain", i.verdict)
                )

    print()
    print("=" * 86)
    print("预注册判据核对")
    print("=" * 86)
    for name, ok, actual in checks:
        print(f"  {'✅' if ok else '❌'} {name}  （实际 {actual}）")
    print()
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"通过 {passed}/{len(checks)} 项")
    if passed == len(checks):
        print("→ ✅ reject_reason 修复了回归，且未引入新的幻觉。")
    else:
        print("→ ⚠️ 仍有未通过的判据，见上方明细。")
    print()
    print("注意：本轮改了分类逻辑与工具描述，若相关性判定有缓存，")
    print("      请用 --no-cache 重跑以反映最新提示词。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
