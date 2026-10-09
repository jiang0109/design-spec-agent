"""手写 ReAct 循环 —— Week 1 的核心，也是本阶段唯一不该被框架替代的部分。

一个 agent 的全部真相就四件事：
    LLM + 工具调用 + 记忆（messages 列表） + 循环终止条件

这里刻意不用 LangGraph / LangChain，目的是把每一行都握在手里。
第 3 周会用 LangGraph 重写一遍，那时你才有资格对比"框架替你做了什么"。

两个阶段：
  阶段 A  工具循环：模型自主决定查什么 → 真实检索 → 结果回填 → 直到它给出结论
  阶段 B  结构化输出：把结论压成 Pydantic 契约，校验失败就把错误原样回喂重试
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from pydantic import ValidationError

from .llm import ask_llm
from .schemas import AgentReport, SpecReport
from .tools import TOOL_IMPLS, TOOLS_SCHEMA

# 防死循环 + 防烧钱的最低要求：
#   max_steps     限制"轮次" —— 模型每轮都可能发起多个并行工具调用
#   max_tool_calls 限制"调用总数" —— 这才是真正的成本护栏
# 实验证明：max_steps=1 时模型在一轮里发了 6 个调用，只靠步数上限拦不住。
MAX_STEPS = 6
MAX_TOOL_CALLS = 12
MAX_REPORT_ATTEMPTS = 3

SYSTEM_PROMPT = """你是设计规范审查助手，负责把设计文档和设计规范知识库做逐条比对。

工作流程：
1. 读用户给出的设计文档内容。
2. 对每一个需要判定合规性的点，必须先调用 search_spec 工具检索规范原文。
3. 只有在检索到明确依据时，才能判 pass 或 fail；检索不到依据时判 uncertain，绝不凭记忆猜测。
4. 全部判定完成后，直接给出简短的总结性结论，结束本轮。

判定纪律：
- evidence 字段必须逐字引用检索结果里的规范原文，不得改写、不得编造。
- fail 必须给出可执行的 suggestion，且建议要落在规范允许的取值范围内。
- 宁判 uncertain，不判错。误报和漏报都是质量问题。

检索纪律：
- 一次检索就能覆盖的点，不要重复查询；同一主题不要反复换措辞重查。
- 系统有工具调用次数上限。如果被告知已达上限，必须停止检索，
  并仅基于已有依据作判定；仍无依据的点直接判 uncertain，不得凭记忆补全。"""


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _strip_code_fence(text: str) -> str:
    """模型经常把 JSON 包在 ```json ... ``` 里，剥掉它。"""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    return m.group(1).strip() if m else t


def _usage_add(acc: dict, r) -> None:
    acc["prompt_tokens"] += r.prompt_tokens
    acc["completion_tokens"] += r.completion_tokens
    acc["llm_calls"] += 1


# ----------------------------------------------------------------------
# 阶段 A：工具调用循环
# ----------------------------------------------------------------------
def run_tool_loop(
    doc: str,
    max_steps: int = MAX_STEPS,
    max_tool_calls: int = MAX_TOOL_CALLS,
    verbose: bool = True,
) -> tuple[str, list[dict], dict]:
    """返回 (模型最终文本, 完整消息历史, 用量统计)。

    两道护栏：
      外层 max_steps      —— 限制轮次，防"反复查同一条"的死循环
      内层 max_tool_calls —— 限制调用总数，防"一轮并发几十个调用"的费用失控
    两者都不能少：实测 max_steps=1 时模型仍在单轮里发出 6 个调用。
    """
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"请审查下面这份设计文档：\n\n<document>\n{doc}\n</document>"},
    ]
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0, "tool_calls": 0}
    budget_exhausted = False

    for step in range(1, max_steps + 1):
        result = ask_llm(messages, tools=TOOLS_SCHEMA, temperature=0.2)
        _usage_add(usage, result)
        msg = result.message

        # 关键点 1：助手消息必须原样回填。
        # 只有 role="tool" 的消息能对应上 tool_call_id，所以 tool_calls 字段不能丢。
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in (msg.tool_calls or [])
                ],
            }
            if msg.tool_calls
            else {"role": "assistant", "content": msg.content or ""}
        )

        # 没有工具调用 = 模型认为可以收尾了
        if not msg.tool_calls:
            if verbose:
                print(f"[step {step}] 模型结束工具循环，进入结构化输出")
            usage["budget_exhausted"] = budget_exhausted
            return msg.content or "", messages, usage

        # 执行每个工具调用（逐个检查预算，超出则回填"拒绝"而不是真去执行）
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args: dict[str, Any] = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            if usage["tool_calls"] >= max_tool_calls:
                budget_exhausted = True
                payload = (
                    f"已达工具调用总预算（{max_tool_calls} 次），本次调用被拒绝。"
                    "请立即停止检索，仅基于已获得的依据完成判定；"
                    "仍无依据的点判 uncertain 并把 evidence 留空。"
                )
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": payload})
                continue

            try:
                fn = TOOL_IMPLS.get(name)
                if fn is None:
                    payload = f"不存在名为 {name} 的工具。可用工具：{list(TOOL_IMPLS)}"
                else:
                    payload = fn(**args)
            except TypeError as e:
                payload = f"参数不符合工具签名：{e}"
            except Exception as e:  # noqa: BLE001 —— 故意兜住所有工具异常，避免整程序崩溃
                payload = f"工具执行失败：{type(e).__name__}: {e}"

            usage["tool_calls"] += 1
            if verbose:
                q = args.get("query") if isinstance(args, dict) else args
                print(f"[step {step}] 调用 {name}({q!r})")

            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(payload)})

    # 走到这里说明达到了步数上限
    if verbose:
        print(f"[warn] 达到 max_steps={max_steps}，强制终止工具循环")
    usage["budget_exhausted"] = budget_exhausted
    return "达到最大步数上限，未能得出完整结论。", messages, usage


# ----------------------------------------------------------------------
# 阶段 B：结构化输出 + 校验重试
# ----------------------------------------------------------------------
REPORT_INSTRUCTION = """现在把上面的审查结论整理成严格符合下面 JSON Schema 的对象。

要求：
- 只输出 JSON，不要输出任何解释文字，不要用 markdown 代码块包裹。
- evidence 必须是 search_spec 返回的规范原文的逐字片段，不得改写、不得拼接。
- 没有依据的点判 uncertain，并把 evidence 留空。
- evidence_sections 只能逐字复制 search_spec 返回结果里的 section 字段。
  禁止改写、扩写、补充层级或翻译：返回 "color.md#语义色" 就必须原样写
  "color.md#语义色"，不得写成 "color.md#语义色的使用"。
- 只列出真正被 evidence 引用过的 section，不要把检索到但没引用的也列上。
- 如果系统提示检索预算已用尽，仍未判定的点判 uncertain，evidence 留空。

JSON Schema：
{schema}
"""

# 对照组：修复前的宽松表述。仅用于 eval/probe_experiments.py 做 before/after 对比，
# 生产路径永远用上面的加严版本。
REPORT_INSTRUCTION_LOOSE = """现在把上面的审查结论整理成严格符合下面 JSON Schema 的对象。

要求：
- 只输出 JSON，不要输出任何解释文字，不要用 markdown 代码块包裹。
- evidence 必须来自 search_spec 返回的规范原文。
- 没有依据的点一律判 uncertain，并把 evidence 留空。

JSON Schema：
{schema}
"""


def _retrieved_sections(messages: list[dict]) -> tuple[set[str], bool]:
    """从消息历史里还原"检索实际返回过哪些 section"。

    返回 (集合, 是否解析可信)。

    踩过的坑（保留记录，避免重犯）：这里最初写的是 hit.get("section")，
    而工具 schema 里字段名实际是 "section_id"，于是集合恒为空集，
    "溯源漂移"指标变成纯粹的假阳性来源。教训：**指标本身必须先被验证**，
    一个错误的度量比没有度量更危险 —— 它会让你朝错误方向优化。
    """
    found: set[str] = set()
    parse_failures = 0
    saw_hits = False

    for m in messages:
        if m.get("role") != "tool":
            continue
        content = m.get("content") or ""
        if isinstance(content, str) and content.startswith(("已达工具调用总预算", "工具执行失败", "不存在名为")):
            continue  # 这些不是检索结果
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            parse_failures += 1
            continue
        if not isinstance(data, dict):
            continue
        hits = data.get("hits")
        if not hits:
            continue
        saw_hits = True
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            # 字段名容错：不同版本可能叫 section 或 section_id
            sid = hit.get("section_id") or hit.get("section")
            if sid:
                found.add(str(sid))

    # 可信条件：没出现解析失败，且非空（除非确实一条都没检索到）
    trusted = parse_failures == 0 and (bool(found) or not saw_hits)
    return found, trusted


def _provenance_drift(messages: list[dict], report: SpecReport) -> tuple[list[str], bool]:
    """找出模型声称引用、但本次检索从未返回过的 section。

    返回 (漂移列表, 检测是否可信)。检测不可信时列表一律视为无效。
    """
    retrieved, trusted = _retrieved_sections(messages)
    if not trusted:
        return [], False
    return [s for s in report.evidence_sections if s not in retrieved], True


def build_report(
    messages: list[dict],
    usage: dict,
    verbose: bool = True,
    max_attempts: int = MAX_REPORT_ATTEMPTS,
    loose_instruction: bool = False,
) -> tuple[SpecReport, dict]:
    """让模型产出符合 SpecReport 契约的数据；失败则把校验错误回喂重试。

    loose_instruction=True 时使用修复前的宽松表述，仅用于对照实验。
    """
    template = REPORT_INSTRUCTION_LOOSE if loose_instruction else REPORT_INSTRUCTION
    schema_text = json.dumps(SpecReport.model_json_schema(), ensure_ascii=False, indent=2)
    working = list(messages)
    working.append({"role": "user", "content": template.format(schema=schema_text)})

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        result = ask_llm(working, temperature=0.1, json_mode=True)
        _usage_add(usage, result)
        raw = _strip_code_fence(result.message.content or "")

        try:
            report = SpecReport.model_validate_json(raw)
        except (ValidationError, ValueError) as e:
            last_error = str(e)
            if verbose:
                print(f"[report {attempt}/{max_attempts}] 校验失败，回喂错误重试")
                print("    " + last_error.splitlines()[0][:160])
            # 关键动作：把错误原文和它自己的错误输出一起回喂
            working.append({"role": "assistant", "content": raw})
            working.append(
                {
                    "role": "user",
                    "content": (
                        "上面的输出不符合契约，校验器报错如下：\n\n"
                        f"{last_error}\n\n"
                        "请修正后重新输出完整 JSON。只输出 JSON。"
                    ),
                }
            )
            continue

        if verbose:
            print(f"[report {attempt}/{max_attempts}] 校验通过，共 {len(report.issues)} 条判定")
        return report, usage

    raise RuntimeError(f"结构化输出在 {max_attempts} 次尝试后仍不合法：\n{last_error}")


# ----------------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------------
def run_agent(
    doc: str,
    doc_name: str = "document.md",
    max_steps: int = MAX_STEPS,
    max_tool_calls: int = MAX_TOOL_CALLS,
    max_report_attempts: int = MAX_REPORT_ATTEMPTS,
    loose_instruction: bool = False,
    verbose: bool = True,
) -> AgentReport:
    started = time.perf_counter()
    _, messages, usage = run_tool_loop(
        doc, max_steps=max_steps, max_tool_calls=max_tool_calls, verbose=verbose
    )
    report, usage = build_report(
        messages,
        usage,
        verbose=verbose,
        max_attempts=max_report_attempts,
        loose_instruction=loose_instruction,
    )
    elapsed = time.perf_counter() - started

    drift, drift_checked = _provenance_drift(messages, report)
    if verbose and not drift_checked:
        print("[warn] 溯源检测不可信（检索结果解析失败），本次不输出漂移指标")

    return AgentReport(
        doc=doc_name,
        report=report,
        steps=len([m for m in messages if m.get("role") == "assistant"]),
        tool_calls=usage["tool_calls"],
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        elapsed_sec=round(elapsed, 2),
        provenance_drift=drift,
        provenance_checked=drift_checked,
        budget_exhausted=bool(usage.get("budget_exhausted")),
    )


# ----------------------------------------------------------------------
# 渲染：把结构化结果变成人看的 markdown（纯代码生成，不让模型碰格式）
# ----------------------------------------------------------------------
_VERDICT_LABEL = {"pass": "✅ 符合", "fail": "❌ 违反", "uncertain": "❓ 无依据"}


def render_markdown(ar: AgentReport) -> str:
    meta = (
        f"> 轮次 {ar.steps}｜工具调用 {ar.tool_calls} 次｜"
        f"token {ar.prompt_tokens}+{ar.completion_tokens}｜耗时 {ar.elapsed_sec}s"
    )
    if ar.provenance_drift:
        meta += f"｜⚠️ 溯源漂移 {len(ar.provenance_drift)} 处"
    if ar.budget_exhausted:
        meta += "｜⚠️ 检索预算已用尽"

    lines = [
        f"# 设计规范审查报告：{ar.doc}",
        "",
        meta,
        "",
        "## 结论",
        "",
        ar.report.summary,
        "",
        "## 逐条判定",
        "",
    ]
    for i, issue in enumerate(ar.report.issues, 1):
        lines += [
            f"### {i}. {issue.target} —— {_VERDICT_LABEL.get(issue.verdict, issue.verdict)}",
            "",
            f"- **理由**：{issue.reason}",
        ]
        if issue.evidence:
            lines.append(f"- **规范依据**：{issue.evidence}")
        if issue.suggestion:
            lines.append(f"- **修改建议**：{issue.suggestion}")
        lines.append("")

    if ar.report.evidence_sections:
        lines += ["## 引用出处", ""]
        lines += [f"- {s}" for s in ar.report.evidence_sections]
        lines.append("")

    if ar.provenance_drift:
        lines += [
            "## ⚠️ 溯源漂移",
            "",
            "以下 section 被模型引用，但本次检索从未返回过（引用链不可信）：",
            "",
        ]
        lines += [f"- `{s}`" for s in ar.provenance_drift]
        lines.append("")
    return "\n".join(lines)
