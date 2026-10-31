# eval/ 目录索引

本目录有 24 个脚本、13 份实验报告、3 套评测集。这份索引说明**哪个是当前在用的、
哪个是历史存档、谁依赖谁** —— 避免把历史脚本当成现行流程。

---

## 一、评测集（3 套，用途不同）

| 文件 | 规模 | 状态 | 被谁使用 |
|---|---|---|---|
| `pilot_set.json` | 10 题 | **历史存档**（区分力已耗尽，其坏题 Q12 已修） | `probe_rerank.py`、`probe_granularity.py`（两份报告的历史基线） |
| `retrieval_set.json` | 35 题 / 23 规则全覆盖 | **当前**：检索层评测 | `run_eval.py` |
| `generation_set.json` | 35 文档 / 40 判定点 | **当前**：生成层手写难例 | `run_generation_eval.py`、`run_hybrid_eval.py`、`test_smoke.py` |
| `generation_set_expanded.json` | 80 文档 / 80 判定点 | **当前**：生成层参数驱动生成（由 `gen_generation_set.py` 产出） | 同上 |

> ⚠️ **不要删 `pilot_set.json`。** 它虽已不是现行评测集，但 `probe_rerank.py` 与
> `probe_granularity.py` 直接依赖它 —— 删掉会让 `rerank_report.md`、
> `granularity_report.md` 的结论无法复现。

### 评测集设计原则（三条硬约束）

1. **期望判定必须由知识库规则的 params 推导**，不参考 agent 输出。
2. **禁止事后修改 `expect`**。若结果与期望不符，按顺序排查：
   ① 匹配逻辑是否命中错点 ② 文档表述是否有歧义 ③ 规则理解是否真的错了。
   顺序不能反，否则评测退化成给模型打掩护。
3. **不能留坏题**。已因此修过 4 道（pilot Q12、G32、G35、E005）。

---

## 二、脚本（按用途分组）

### 校验与门禁（零 API，每次提交都该跑）

| 脚本 | 作用 |
|---|---|
| `test_smoke.py` | **总门禁**：12 项离线不变量。`python -m eval.test_smoke` |
| `validate_kb.py` | 知识库结构校验（id 唯一、字段齐全、正文分节与 title 对齐） |
| `test_parser.py` | front-matter 解析器自测（跨行值 / 嵌套子键） |
| `test_rule_engine.py` | 规则引擎 37 条断言（含 3 个 LLM 判错案例的回归用例） |

### 现行评测（产出报告里的数字）

| 脚本 | 作用 | 调用 API |
|---|---|---|
| `run_eval.py` | 检索层：5 种分块 × 双口径 × 分层指标 | ❌ 纯本地 |
| `run_hybrid_eval.py` | 生成层端到端（**双路，CLI 默认引擎**，119 点） | ✅ 约 10 分钟 |
| `run_generation_eval.py` | 生成层端到端（**纯 LLM 路径，对照用**，119 点）。带重试 + 断点续跑 | ✅ 约 14 分钟 |

> **两条判定路径要分清楚**：CLI 默认 `--engine hybrid`（抽取→属性路由→规则引擎，
> 未知/例外回落 LLM）；`--engine llm` 走原手写 ReAct 循环。
> 两个评测脚本分别测这两条路径，指标不可混用。

### 一次性诊断探针（历史，按需复现）

| 脚本 | 诊断的问题 | 对应报告 |
|---|---|---|
| `probe_experiments.py` | 溯源漂移、预算护栏、反幻觉 | `experiments.md` |
| `run_pilot.py` | 语料规模是否够用（10 题校准） | `pilot_report.md` |
| `probe_rerank.py` | 标题字段加权能否提升排序 | `rerank_report.md` |
| `probe_granularity.py` | 分块粒度是否是瓶颈 | `granularity_report.md` |
| `probe_relevance.py` | LLM 相关性判定能否实现拒答 | `relevance_report.md` |
| `probe_coverage.py` | 正交覆盖度闸门 | `relevance_report.md` |
| `probe_structured.py` | 例外字段能否打破适用性边界 | `structured_report.md` |
| `probe_reject_reason.py` | 拒绝原因分类是否修复回归 | `reject_reason_report.md` |
| `probe_applicability.py` | 判定器 v1 vs v3 判据 | `applicability_report.md` |
| `probe_query_phrasing.py` | **完全离线**：措辞 × 检索方式矩阵 | `query_phrasing_report.md` |
| `probe_decidability.py` | 判定步骤可确定性化程度 | `decidability_report.md` |
| `probe_general_rule.py` | 「通用规则 vs 子对象」误拒的根因（A/B/C 三选一） | `general_rule_report.md` |
| `probe_hybrid.py` | 混合判定：离线路由断言 + 在线抽取 | `hybrid_report.md` |
| `probe_query_phrasing.py` | 见上 | — |
| `run_e2e.py` | 端到端：三类材料 × 闸门开关 | `e2e_report.md` |

### 数据生产

| 脚本 | 作用 |
|---|---|
| `gen_generation_set.py` | 从探针表生成 `generation_set_expanded.json`（80 点） |
| `fix_cases_lesson.py` | 一次性修复：`cases.md` 的 14 条 lesson 字段回填（带完整性闸门） |

---

## 三、运行产物命名约定

`out/` 已 gitignore，日志是本地临时产物。约定：

```
out/logs/geneval-default.log     默认路径（纯 LLM）最近一次全量
out/logs/geneval-hybrid.log      双路并行最近一次全量
out/.cache/geneval/<id>.json     单份文档的结果缓存（供断点续跑）
out/.cache/geneval/_fingerprint.txt   代码/KB 指纹
out/generation_eval.json         最近一次生成层评测的结构化明细
out/hybrid_eval.json             最近一次双路评测的结构化明细
```

**只保留每个 benchmark 的最近一份日志**，历史数字以报告为准 ——
报告里记录了当时的完整数据与结论，比散落的日志更可靠。

### ⚠️ 缓存会因代码或 KB 改动自动失效

`run_generation_eval.py` 的缓存带 `kb/` + `src/` 的内容指纹。
**改了任何知识库文件或源码，下次运行会清空缓存并全部重跑**（约 13 分钟）。

这是刻意设计的：否则会出现"报告写 94%、但代码已经不是那一版"的静默污染 ——
这种测量错误比跑得慢危险得多。要强制重跑用 `--no-resume`。

---

## 四、一条阅读建议

如果你想快速理解这个项目做了什么、为什么这么做，**不要从脚本开始读**。
按这个顺序看报告更高效：

```
1. 根目录 README.md              项目定位与当前指标
2. eval/generation_report.md     生成层 120 点评测怎么做的
3. eval/hybrid_eval_report.md    双路并行为什么没切默认（口径与取舍）
4. eval/decidability_report.md   为什么要把判定交给代码（75% 可确定性）
5. eval/experiments.md           最早那批实验（含排查过程）
```

**报告里保留了被推翻的假设**（`structured_report.md` 推翻 `e2e_report.md`、
`query_phrasing_report.md` 推翻 `query_format_report.md` 的归因），
这是刻意保留的 —— 结论的演进过程比一个"正确"的结论更有信息量。
