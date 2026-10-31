"""冻结一份可复核的评测基线，用于「迁移前后」回归比对。

为什么不直接跑 run_hybrid_eval.py
----------------------------------
1. 它没有缓存/断点续跑 —— 全量约 10 分钟，一次网络抖动整轮作废；
2. 它只往终端打印，**没有把「代码 + 数据 + 配置」的指纹一起钉住**。
   离开指纹的基线不可复用：你无法证明两次对比用的是同一把尺子。
3. 它内部的 BASELINE 常量是 G20 合并前的 120 点口径，与当前 119 点不同分母，
   直接拿来对比等于拿 119 的分母比 120 的分子。

本脚本做的事
------------
- **复用** `run_hybrid_eval.run_case`，保证匹配/错误分类口径全项目唯一；
- 记录每个输入文件的 SHA256（kb / src / eval 脚本 / 评测集）+ 汇总指纹
  + git commit + 模型配置，任何一项变了都能查出来；
- 逐点明细落盘，迁移后可以 diff 出「哪个判定点变了」而不是只看总分；
- 单份缓存 + 重试，可以断点续跑。

用法
----
    python -m eval.freeze_baseline --tag pre-langgraph        # 冻结当前基线
    python -m eval.freeze_baseline --limit 3 --tag smoke      # 先小样本验管道
    python -m eval.freeze_baseline --tag post-langgraph --no-resume

产物
----
    eval/baselines/<tag>.json     结构化基线（建议提交，作为回归基准）
    out/.cache/baseline/          单份文档缓存（gitignore）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.run_generation_eval import _code_fingerprint  # noqa: E402
from eval.run_hybrid_eval import DATASETS, run_case  # noqa: E402
from src.llm import model_name  # noqa: E402
from src.rule_engine import RuleEngine  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# 上一次全量运行的已知结果（119 点，见 eval/hybrid_eval_report.md）。
# 用途：如果本次跑出来差得远，先怀疑「我的度量写错了」，而不是「系统退化了」。
KNOWN_ANSWER = {"total_points": 119, "correct": 116, "fp": 0, "fn": 0, "over_reject": 0, "over_confidence": 2}


# ----------------------------------------------------------------------
# 指纹：每个输入文件单独记 SHA256，不信任何汇总值
# ----------------------------------------------------------------------
def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def collect_inputs() -> dict[str, str]:
    files: list[Path] = []
    files += sorted((ROOT / "kb").glob("*.md"))
    files += sorted((ROOT / "src").glob("*.py"))
    files += sorted((ROOT / "eval").glob("*.py"))
    files += sorted((ROOT / "eval").glob("generation_set*.json"))
    return {p.relative_to(ROOT).as_posix(): _sha256(p) for p in files}


def cache_stamp(inputs: dict[str, str]) -> str:
    """缓存键：实测路径的输入 + 参与度量的 eval 脚本。

    为什么不能直接用 `_code_fingerprint()`：它只覆盖 kb/ + src/ + 评测集，
    而**匹配与错误分类逻辑在 eval/run_hybrid_eval.py 里**。改匹配口径不会让缓存失效，
    于是会出现「报告写 97%、但尺子已经换了」—— 这正是本项目反复踩过的静默污染。
    所以缓存键必须显式包含度量脚本本身。
    """
    keys = [
        k for k in sorted(inputs)
        if k.startswith(("kb/", "src/"))
        or k.startswith("eval/generation_set")
        or k in ("eval/run_hybrid_eval.py", "eval/run_generation_eval.py", "eval/freeze_baseline.py")
    ]
    h = hashlib.sha256()
    for k in keys:
        h.update(k.encode("utf-8"))
        h.update(inputs[k].encode("utf-8"))
    return h.hexdigest()[:16]


def git_info() -> dict:
    def run(*args: str) -> str:
        try:
            r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=15)
            return r.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(run("status", "--porcelain")),
    }


def run_config() -> dict:
    return {
        "engine": "hybrid",
        "llm_fallback": True,
        "model": model_name(),
        "base_url": os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
        "note": "temperature 由 src 内固定；temperature=0 不消除方差，故需重复运行估计噪声",
    }


# ----------------------------------------------------------------------
# 带缓存的单份评测
# ----------------------------------------------------------------------
def run_case_cached(case: dict, engine: RuleEngine, cache_dir: Path, stamp: str, resume: bool) -> dict:
    stamp_file = cache_dir / "_fingerprint.txt"
    if resume and cache_dir.exists():
        old = stamp_file.read_text(encoding="utf-8").strip() if stamp_file.exists() else ""
        if old != stamp:
            print(f"  （输入已变，缓存失效：{old or '(无)'} → {stamp}）")
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
            r = run_case(case, engine, True)
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
            stamp_file.write_text(stamp, encoding="utf-8")
            return r
        except Exception as e:  # noqa: BLE001 —— 网络/限流等瞬时错误，重试后仍失败才记账
            last = e
            time.sleep(2 ** attempt)
    print(f"  [error] {case['id']} 连续 3 次失败：{type(last).__name__}: {last}")
    return {
        "id": case["id"],
        "points": [
            {**p, "actual": None, "correct": False, "error": "api_error（未完成评测）"}
            for p in case["points"]
        ],
        "by_engine": 0, "by_llm": 0, "fallback": 0,
        "extract_calls": 0, "llm_runs": 0,
        "api_error": f"{type(last).__name__}: {last}",
    }


# ----------------------------------------------------------------------
# 指标：与 run_hybrid_eval.py 的定义保持一致（覆盖率/准确率/四类错误分列）
# ----------------------------------------------------------------------
def summarize(results: list[dict]) -> dict:
    pts = [p for r in results for p in r["points"]]
    total = len(pts)
    matched = [p for p in pts if p["actual"] is not None]
    correct = [p for p in pts if p["correct"]]

    def cnt(prefix: str) -> int:
        return sum(1 for p in pts if str(p.get("error", "")).startswith(prefix))

    sources = {"engine": 0, "llm": 0, "fallback": 0}
    for r in results:
        sources["engine"] += r.get("by_engine", 0)
        sources["llm"] += r.get("by_llm", 0)
        sources["fallback"] += r.get("fallback", 0)

    api_errors = [r["id"] for r in results if r.get("api_error")]
    return {
        "total_points": total,
        "covered": len(matched),
        "coverage": round(len(matched) / total, 4) if total else 0.0,
        "correct": len(correct),
        "accuracy": round(len(correct) / total, 4) if total else 0.0,
        "fp": cnt("误报"),
        "fn": cnt("漏报"),
        "over_reject": cnt("过度拒答"),
        "over_confidence": cnt("过度自信"),
        "unmatched": sum(1 for p in pts if p["actual"] is None),
        "verdict_mismatch": cnt("判定不符"),
        "sources": sources,
        "engine_ratio": round(sources["engine"] / total, 4) if total else 0.0,
        "api_error_docs": api_errors,
    }


def flatten(results: list[dict]) -> list[dict]:
    out = []
    for r in results:
        for p in r["points"]:
            out.append({
                "id": r["id"],
                "rule": p.get("rule"),
                "keyword": p.get("keyword"),
                "expect": p.get("expect"),
                "actual": p.get("actual"),
                "correct": bool(p.get("correct")),
                "error": p.get("error", ""),
                "source": p.get("source", ""),
                "attribute": p.get("attribute", ""),
            })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="冻结可复核的评测基线")
    ap.add_argument("--tag", default="pre-langgraph", help="基线名称，决定输出文件名")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 份文档（验管道用）")
    ap.add_argument("--no-resume", action="store_true", help="忽略缓存，全部重跑")
    ap.add_argument("--out-dir", default="eval/baselines")
    args = ap.parse_args()

    cases: list[dict] = []
    for p in DATASETS:
        if p.exists():
            cases.extend(json.loads(p.read_text(encoding="utf-8"))["cases"])
    if args.limit:
        cases = cases[: args.limit]

    inputs = collect_inputs()
    aggregate = _code_fingerprint()
    stamp = cache_stamp(inputs)
    engine = RuleEngine()
    k, n = engine.coverage()

    print("=" * 92)
    print(f"冻结基线｜tag={args.tag}｜文档 {len(cases)} 份")
    print("=" * 92)
    print(f"  缓存键（含度量脚本）: {stamp}")
    print(f"  代码/KB 指纹        : {aggregate}")
    print(f"  输入文件数      : {len(inputs)}")
    print(f"  引擎覆盖规则    : {k}/{n}")
    print(f"  模型            : {model_name()}")
    print()

    cache_dir = ROOT / "out" / ".cache" / "baseline"
    started = time.perf_counter()
    results = []
    for i, case in enumerate(cases, 1):
        r = run_case_cached(case, engine, cache_dir, stamp, not args.no_resume)
        results.append(r)
        ok = sum(1 for p in r["points"] if p["correct"])
        if ok != len(r["points"]) or i % 20 == 0:
            print(f"  [{i:>3}/{len(cases)}] {r['id']:<8} {ok}/{len(r['points'])} 正确"
                  f"（engine {r.get('by_engine', 0)} / llm {r.get('by_llm', 0)} / fallback {r.get('fallback', 0)}）")
    elapsed = time.perf_counter() - started

    m = summarize(results)
    print()
    print("总体指标")
    print(f"  判定点总数   : {m['total_points']}")
    print(f"  覆盖率       : {m['covered']}/{m['total_points']} = {m['coverage']*100:.0f}%")
    print(f"  准确率       : {m['correct']}/{m['total_points']} = {m['accuracy']*100:.0f}%")
    print(f"  误报 FP      : {m['fp']}")
    print(f"  漏报 FN      : {m['fn']}")
    print(f"  过度拒答     : {m['over_reject']}")
    print(f"  过度自信     : {m['over_confidence']}")
    print(f"  未命中       : {m['unmatched']}")
    print(f"  判定来源     : engine {m['sources']['engine']} / llm {m['sources']['llm']}"
          f" / fallback {m['sources']['fallback']}（引擎占比 {m['engine_ratio']*100:.0f}%）")
    print(f"  耗时         : {elapsed:.0f}s")

    # 已知答案校验：差得远时先怀疑度量本身。
    # 小样本运行必须跳过 —— 拿 4 点比 119 点的已知答案，比了必然误报。
    diffs = {} if args.limit else {
        k2: (KNOWN_ANSWER[k2], m[k2]) for k2 in KNOWN_ANSWER if m[k2] != KNOWN_ANSWER[k2]
    }
    if args.limit:
        print("  （小样本运行，跳过已知答案校验）")
    elif diffs:
        print()
        print("  ⚠ 与上次已知结果不一致（先怀疑度量/数据，再怀疑系统）：")
        for k2, (want, got) in diffs.items():
            print(f"      {k2:<16} 已知 {want} → 本次 {got}")
    else:
        print("  ✓ 与上次已知结果一致")

    if m["api_error_docs"]:
        print(f"\n  ✗ {len(m['api_error_docs'])} 份文档 API 失败，结果不可信：{m['api_error_docs']}")

    record = {
        "tag": args.tag,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "purpose": "迁移前后回归比对的基准；只允许用同一脚本、同一输入重跑",
        "git": git_info(),
        "config": run_config(),
        "fingerprint": {"cache_stamp": stamp, "aggregate": aggregate, "inputs": inputs},
        "dataset": {
            "documents": len(cases),
            "points": sum(len(c["points"]) for c in cases),
            "files": [p.name for p in DATASETS if p.exists()],
        },
        "engine_coverage": {"covered": k, "total": n},
        "metrics": m,
        "elapsed_sec": round(elapsed, 1),
        "known_answer_check": {"expected": KNOWN_ANSWER, "matches": not diffs},
        "points": flatten(results),
    }

    out_dir = ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.tag}.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n基线已写出：{out.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
