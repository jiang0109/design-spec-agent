"""生成层端到端评测：在人工标注的 47 个判定点上测真实准确率。

为什么需要它
------------
在此之前，检索层有 35 题评测，但**生成层只有一个 6 个判定点的示例**。
也就是说"这个 agent 的判定准确率是多少"这个问题答不出可信数字 ——
这是整个项目最大的空白。

评测集设计
----------
`eval/generation_set.json`：31 份文档 / 35 个判定点，手工编写并标注。

期望判定**全部由知识库规则的 params 推导**，不参考 agent 输出：
  - SPACING-001 的 allowed 是 {12,16,24} → 写 20 就是 fail
  - TYPO-002 规定行高 = 字号 × 1.5 → 20px 字号配 26px 行高就是 fail
  - TYPO-002 的例外写了"表格单元格不参与校验" → 表格行高就是 pass

三类错误必须分开统计，它们的危害完全不同：

| 错误类型 | 含义 | 为什么严重 |
|---|---|---|
| 误报（FP） | 合规判成违规 | 会让设计团队返工，消耗信任 |
| 漏报（FN） | 违规判成合规/无依据 | 规范形同虚设，问题流到线上 |
| 过度拒答 | 有明确依据却判 uncertain | 可用性差，等于没有功能 |
| 过度自信 | 无覆盖却给出 pass/fail | 幻觉，最危险 |

用法
----
    python -m eval.run_generation_eval              # 全部 31 份
    python -m eval.run_generation_eval --limit 5    # 先跑 5 份验证管道
    python -m eval.run_generation_eval --no-verify  # 关闭检索闸门做对照
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent import run_agent  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# 两份数据集合并评测：
#   generation_set.json            手写难例（负责「难与自然」）
#   generation_set_expanded.json   生成器批量产出（负责「广与可复现」）
DATASETS = [
    ROOT / "eval" / "generation_set.json",
    ROOT / "eval" / "generation_set_expanded.json",
]
OUT_JSON = ROOT / "out" / "generation_eval.json"


def category(rule: str) -> str:
    """把规则标识归类，用于分层统计。"""
    if "例外" in rule:
        return "excluded（排除型）"
    if rule.startswith("(无覆盖"):
        return "no_coverage（无覆盖）"
    for p in ("SPACING", "COLOR", "TYPO", "COMP", "NAME"):
        if rule.startswith(p):
            return p.lower()
    return "other"


def match(issues: list[dict], keywords: list[str], used: set[int]) -> tuple[int, dict] | None:
    """按关键词列表在 target 里对齐；跳过已被占用的下标。

    为什么用**关键词列表**而不是单个关键词：
    第一版用单词匹配，结果 3/7 的"错误"其实是匹配失败 —— 模型确实判了，
    只是 target 措辞没命中我预设的那个词：

        期望点 keyword="属性名"  →  模型 target="LoadingButton 的布尔属性 loading"
        期望点 keyword="变体"    →  模型 target="主按钮 variant 取值"
        期望点 keyword="属性"    →  模型 target="纯布局容器 Container 未声明 size 与 variant"

    统计工具本身造成的假阴性比模型错误更危险：它会让准确率被低估，
    并诱导你去"修"一个根本不存在的模型问题。
    """
    for kw in keywords:
        for idx, i in enumerate(issues):
            if idx in used:
                continue
            if kw in i["target"]:
                return idx, i
    return None


def _keywords(point: dict) -> list[str]:
    """兼容单关键词与关键词列表两种写法，主关键词优先。"""
    kws = point.get("keywords")
    if kws:
        return list(kws)
    return [point["keyword"]]


def run_case(case: dict, verify: bool | None) -> dict:
    ar = run_agent(case["doc"], doc_name=f"gen_{case['id']}", llm_verify=verify, verbose=False)
    issues = [
        {
            "target": i.target,
            "verdict": i.verdict,
            "has_evidence": bool(i.evidence),
            "reason": i.reason[:60],
        }
        for i in ar.report.issues
    ]

    used: set[int] = set()
    points_out = []
    single = len(case["points"]) == 1
    for p in case["points"]:
        kws = _keywords(p)
        got = match(issues, kws, used)
        # 单点文档的直接对齐：只有 1 个期望点、且模型只产出 1 条判定时，
        # 对齐是唯一的，再依赖关键词只会引入假阴性。
        # 实测（eval/generation_set_expanded）有 3/8 的"错误"是这类匹配失败：
        #   期望点 "页面外边距" ← 模型 target "窄屏页面左右外边距"
        #   期望点 "颜色引用"   ← 模型 target "卡片背景色"
        #   期望点 "属性声明"   ← 模型 target "Divider 未声明 size 与 variant"
        if got is None and single:
            unused = [i for idx, i in enumerate(issues) if idx not in used]
            if len(unused) == 1:
                got = (issues.index(unused[0]), unused[0])
        if got is None:
            points_out.append(
                {
                    "keyword": kws[0],
                    "keywords": kws,
                    "rule": p["rule"],
                    "expect": p["expect"],
                    "actual": None,
                    "correct": False,
                    "error": "未命中（模型未就该点给出判定）",
                    "why": p["why"],
                }
            )
            continue
        idx, issue = got
        used.add(idx)
        actual = issue["verdict"]
        ok = actual == p["expect"]
        err = ""
        if not ok:
            if p["expect"] == "pass" and actual == "fail":
                err = "误报（合规判成违规）"
            elif p["expect"] == "fail" and actual == "pass":
                err = "漏报（违规判成合规）"
            elif p["expect"] in ("pass", "fail") and actual == "uncertain":
                err = "过度拒答（有依据却判 uncertain）"
            elif p["expect"] == "uncertain" and actual in ("pass", "fail"):
                err = "过度自信（无覆盖却下了结论）"
            else:
                err = f"判定不符（期望 {p['expect']}）"
        points_out.append(
            {
                "keyword": kws[0],
                "keywords": kws,
                "rule": p["rule"],
                "expect": p["expect"],
                "actual": actual,
                "correct": ok,
                "error": err,
                "why": p["why"],
                "target": issue["target"],
                "has_evidence": issue["has_evidence"],
                "reason": issue["reason"],
            }
        )

    extra = [i["target"] for idx, i in enumerate(issues) if idx not in used]

    return {
        "id": case["id"],
        "points": points_out,
        "extra": extra,
        "reject_reasons": ar.reject_reasons,
        "queries": ar.retrieval_queries,
        "prompt_tokens": ar.prompt_tokens,
        "completion_tokens": ar.completion_tokens,
        "elapsed": ar.elapsed_sec,
        "tool_calls": ar.tool_calls,
    }


def _code_fingerprint() -> str:
    """kb/ 与 src/ 的内容指纹。

    为什么需要：断点续跑缓存会在**代码或知识库改动后静默复用旧结果**，
    于是评测数字与当前代码不对应 —— 这是最隐蔽的测量污染，
    报告里写"94%"但代码已经不是那一版了。指纹不匹配就整体忽略缓存。
    """
    import hashlib

    h = hashlib.sha256()
    for d in ("kb", "src"):
        for p in sorted((ROOT / d).glob("*.py" if d == "src" else "*.md")):
            h.update(p.name.encode("utf-8"))
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def _run_case_cached(case: dict, verify: bool | None, resume: bool = True) -> dict:
    """带"重试 + 断点续跑"的单份文档评测。

    为什么需要
    ----------
    120 点全量评测要跑 14 分钟、上百次 API 调用。实测一次网络抖动
    （`openai.APIConnectionError`）就让它跑到第 42 份时整个崩掉，前 40 份全废。
    所以：
      1. 单份最多重试 3 次（指数退避），仍失败则记为 api_error 并继续 ——
         一次抖动不该让整轮评测作废
      2. 每份的结果缓存到 out/.cache/geneval/，重跑时直接跳过已完成的部分
      3. 缓存带代码指纹，kb/src 一改就自动失效（避免用旧结果冒充新结论）
    """
    cache_dir = ROOT / "out" / ".cache" / "geneval"
    stamp_file = cache_dir / "_fingerprint.txt"
    stamp = _code_fingerprint()

    if resume and cache_dir.exists():
        old = stamp_file.read_text(encoding="utf-8").strip() if stamp_file.exists() else ""
        if old != stamp:
            print(f"  （代码/KB 已变，缓存失效：{old or '(无)'} → {stamp}）")
            for f in cache_dir.glob("*.json"):
                f.unlink(missing_ok=True)

    cache_file = cache_dir / f"{case['id']}.json"
    if resume and cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    last: Exception | None = None
    for attempt in range(3):
        try:
            r = run_case(case, verify)
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
            stamp_file.write_text(stamp, encoding="utf-8")
            return r
        except Exception as e:  # noqa: BLE001 —— 网络/限流等瞬时错误
            last = e
            time.sleep(2 ** attempt)

    # 三次都失败：记为 API 错误，继续跑后面的，不中断整轮
    return {
        "id": case["id"],
        "points": [
            {
                "keyword": _keywords(p)[0], "keywords": _keywords(p), "rule": p["rule"],
                "expect": p["expect"], "actual": None, "correct": False,
                "error": f"API 调用失败（{type(last).__name__}）", "why": p["why"],
            }
            for p in case["points"]
        ],
        "extra": [],
        "queries": [],
        "prompt_tokens": 0, "completion_tokens": 0, "elapsed": 0.0, "tool_calls": 0,
        "api_error": True,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成层端到端评测")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 份文档（0=全部）")
    ap.add_argument("--no-verify", action="store_true", help="关闭检索闸门（对照）")
    ap.add_argument("--no-resume", action="store_true",
                    help="忽略 out/.cache/geneval 缓存，强制全部重跑")
    args = ap.parse_args()

    # 合并多份数据集（手写难例 + 生成集）
    cases: list[dict] = []
    for p in DATASETS:
        if not p.exists():
            print(f"⚠️ 跳过不存在的评测集：{p.name}", file=sys.stderr)
            continue
        part = json.loads(p.read_text(encoding="utf-8"))["cases"]
        print(f"  载入 {p.name}：{len(part)} 份文档")
        cases.extend(part)
    if not cases:
        print("没有可用的评测集", file=sys.stderr)
        return 1
    if args.limit:
        cases = cases[: args.limit]

    n_points = sum(len(c["points"]) for c in cases)
    verify = False if args.no_verify else None
    mode = "闸门关闭（纯 BM25）" if args.no_verify else "默认（闸门开启）"

    print(f"生成层端到端评测｜模式：{mode}")
    print(f"文档 {len(cases)} 份｜判定点 {n_points} 个\n")

    results = []
    for i, case in enumerate(cases, 1):
        r = _run_case_cached(case, verify, resume=not args.no_resume)
        results.append(r)
        ok = sum(1 for p in r["points"] if p["correct"])
        flag = "  ⚠️ API 失败" if r.get("api_error") else ""
        print(f"  [{i:>2}/{len(cases)}] {r['id']}  {ok}/{len(r['points'])} 正确  ({r['elapsed']}s){flag}")
        for p in r["points"]:
            if not p["correct"]:
                print(f"        ✗ {p['keyword']}: 期望 {p['expect']} → 实际 {p['actual']}  {p['error']}")

    # ---------------- 汇总 ----------------
    all_points = [p for r in results for p in r["points"]]
    total = len(all_points)
    matched = [p for p in all_points if p["actual"] is not None]
    correct = [p for p in all_points if p["correct"]]

    print()
    print("=" * 88)
    print("总体指标")
    print("=" * 88)
    print(f"  判定点总数        : {total}")
    print(f"  模型给出判定      : {len(matched)}/{total} = {len(matched)/total*100:.0f}%（覆盖率）")
    print(f"  判定正确          : {len(correct)}/{total} = {len(correct)/total*100:.0f}%（端到端准确率）")
    if matched:
        print(f"  已判定中正确      : {len(correct)}/{len(matched)} = {len(correct)/len(matched)*100:.0f}%")

    def count(err_prefix: str) -> list[dict]:
        return [p for p in all_points if p["error"].startswith(err_prefix)]

    fp = count("误报")
    fn = count("漏报")
    over_reject = count("过度拒答")
    over_conf = count("过度自信")
    miss = [p for p in all_points if p["actual"] is None]

    print()
    print(f"  误报 FP（合规→违规）: {len(fp)}")
    print(f"  漏报 FN（违规→合规）: {len(fn)}")
    print(f"  过度拒答（该判却uncertain）: {len(over_reject)}")
    print(f"  过度自信（无覆盖却下结论）: {len(over_conf)}")
    print(f"  未命中（模型未给出该点判定）: {len(miss)}")

    # ---------------- 分层 ----------------
    print()
    print("=" * 88)
    print("按规则类别分层")
    print("=" * 88)
    cats: dict[str, list[dict]] = {}
    for p in all_points:
        cats.setdefault(category(p["rule"]), []).append(p)
    print(f"{'类别':<22}{'点数':>6}{'正确':>6}{'准确率':>8}{'误报':>6}{'漏报':>6}{'拒答':>6}")
    print("-" * 88)
    for cat in sorted(cats):
        ps = cats[cat]
        c = sum(1 for p in ps if p["correct"])
        print(
            f"{cat:<22}{len(ps):>6}{c:>6}{c/len(ps)*100:>7.0f}%"
            f"{sum(1 for p in ps if p['error'].startswith('误报')):>6}"
            f"{sum(1 for p in ps if p['error'].startswith('漏报')):>6}"
            f"{sum(1 for p in ps if p['error'].startswith('过度拒答')):>6}"
        )

    # ---------------- 错误明细 ----------------
    errs = [p for p in all_points if not p["correct"]]
    if errs:
        print()
        print("=" * 88)
        print("全部错误明细（这是最有价值的部分）")
        print("=" * 88)
        for p in errs:
            print(f"\n  [{p['rule']}] keyword={p['keyword']}  期望 {p['expect']} → 实际 {p['actual']}")
            print(f"    依据：{p['why']}")
            if p.get("target"):
                print(f"    模型 target: {p['target']}")
            if p.get("reason"):
                print(f"    模型理由  : {p['reason']}")

    # ---------------- 多余判定 ----------------
    extras = [(r["id"], e) for r in results for e in r["extra"]]
    print()
    print("=" * 88)
    print(f"多余判定（未被期望点匹配的 target）：{len(extras)} 条")
    print("=" * 88)
    if not extras:
        print("  无 —— 模型没有产出期望之外的判定点")
    else:
        print("  注意：多余判定不等于错。若文档里确有未标注的问题，模型多报是合理的；")
        print("        但也可能是把同一问题拆成了两条。需人工判断，故不计入准确率。")
        for cid, e in extras[:12]:
            print(f"  {cid}: {e}")

    # ---------------- 查询格式（验证提示词是否真的生效）----------------
    # 只看最终分数无法判断"约束查询表述"这条指令有没有被执行，
    # 所以统计 agent 实际发出的查询里有多少仍带「预设答案框架」的措辞。
    PRESUPPOSE = [
        "允许哪几档", "允许哪些", "哪几档", "哪几档取值",
        "应使用哪个", "应该用哪个", "哪个变量", "哪个令牌", "哪个档",
        "是否必须", "是否与内容", "是否一致",
    ]
    all_queries = [q for r in results for q in r.get("queries", [])]
    flagged = [q for q in all_queries if any(p in q for p in PRESUPPOSE)]
    print()
    print("=" * 88)
    print("查询格式统计（验证「约束查询表述」是否生效）")
    print("=" * 88)
    print(f"  查询总数            : {len(all_queries)}")
    print(f"  带预设框架措辞的查询 : {len(flagged)}"
          f"（{len(flagged)/max(1,len(all_queries))*100:.0f}%）")
    if flagged:
        print("  仍带预设框架的示例：")
        for q in flagged[:6]:
            print(f"    ✗ {q}")
    ok_q = [q for q in all_queries if q not in flagged]
    if ok_q:
        print("  「对象+属性+取值」式查询示例：")
        for q in ok_q[:4]:
            print(f"    ✓ {q}")
    print()
    print("  说明：本项只统计措辞形态，不判断查询好坏。")
    print("        『对象+属性+取值』式查询更利于规则匹配，也更利于结果复现。")

    # ---------------- 成本 ----------------
    pt = sum(r["prompt_tokens"] for r in results)
    ct = sum(r["completion_tokens"] for r in results)
    el = sum(r["elapsed"] for r in results)
    tc = sum(r["tool_calls"] for r in results)
    print()
    print("=" * 88)
    print("成本")
    print("=" * 88)
    print(f"  token {pt}+{ct}｜工具调用 {tc} 次｜总耗时 {el:.1f}s（每份 {el/len(results):.1f}s）")
    print(f"  平均每个判定点 token ≈ {(pt+ct)/max(1,total):.0f}")

    # ---------------- 落盘 ----------------
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(
            {
                "mode": mode,
                "total_points": total,
                "coverage": len(matched) / total if total else 0,
                "accuracy": len(correct) / total if total else 0,
                "metrics": {
                    "fp": len(fp),
                    "fn": len(fn),
                    "over_reject": len(over_reject),
                    "over_confidence": len(over_conf),
                    "missed": len(miss),
                },
                "by_category": {
                    c: {
                        "total": len(ps),
                        "correct": sum(1 for p in ps if p["correct"]),
                    }
                    for c, ps in cats.items()
                },
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n明细已写出：{OUT_JSON}")

    # ---------------- 结论 ----------------
    print()
    print("=" * 88)
    print("结论")
    print("=" * 88)
    acc = len(correct) / total if total else 0
    if acc >= 0.9:
        print(f"→ 端到端准确率 {acc*100:.0f}%（≥90%）")
    elif acc >= 0.8:
        print(f"→ 端到端准确率 {acc*100:.0f}%（80~90%，可用但有明显改进空间）")
    else:
        print(f"→ 端到端准确率 {acc*100:.0f}%（<80%，需先修主要错误类型）")
    if over_conf:
        print(f"→ ⚠️ 仍有 {len(over_conf)} 处过度自信（无覆盖却下结论）—— 这是最该优先修的")
    if over_reject:
        print(f"→ ⚠️ 有 {len(over_reject)} 处过度拒答（有依据却判 uncertain）—— 影响可用性")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
