# LangGraph 控制流迁移报告

**结论：图版与手写版在 119 点评测集上行为等价 —— 跨实现差异不超过同一实现两次运行之间的差异。**

| | 手写版 | 图版（第 1 次） | 图版（第 2 次） |
|---|---|---|---|
| 基线文件 | `baselines/pre-langgraph.json` | `baselines/post-langgraph.json` | `baselines/post-langgraph-r2.json` |
| LLM 路径实现 | `manual` | `graph` | `graph` |
| commit | `e9fe907` | `1a351be` | `daf6948` |
| 耗时 | 390.1s | 380.5s | 393.1s |
| 正确 | 116/119 | 116/119 | 116/119 |
| 误报 / 漏报 | 0 / 0 | 0 / 0 | 0 / 0 |
| 过度拒答 / 过度自信 | 0 / 2 | 0 / 2 | 0 / 2 |
| 判定来源 | engine 91 / llm 31 | engine 91 / llm 31 | engine 91 / llm 31 |

---

## 一、为什么迁移

迁移的动机全部来自**手写 `for` 循环做不到的事**，不是「用框架更时髦」：

1. **断点续跑。** 本项目真实事故：全量评测跑到第 42 篇时 `APIConnectionError`，
   14 分钟全废。当时的解法是在**评测层**加 retry + 缓存 —— 那是治标，粒度是
   「整篇文档」，且只有评测受益。checkpointer 把粒度降到「每个节点之后」，
   崩了从最后一个检查点继续，连已检索到的原文都不用重新取。
2. **人工介入。** 这个工具的终点是进设计团队 CI，判 `fail` 的条目要「打回图纸」，
   必须先过人眼确认。手写循环里做人审等于在 `for` 里塞 `input()`，没法被 CLI /
   评测 / 未来的 Web 层复用。图级别的 `interrupt_before` 是同一个图三种跑法。
3. **可观测。** 预算耗尽、报告重试这类分支，手写版藏在 `if` 里，只有读代码才知道
   存在；迁移后它们是图里可见的边。

## 二、做了什么

只搬**编排**，不碰**判定**。`SYSTEM_PROMPT`、`REPORT_INSTRUCTION`、工具分发、
溯源校验全部从 `agent.py` **直接导入**而不是复制 —— 否则基线对比会把
「控制流是否等价」和「prompt 有没有被改坏」混成一件事，测出来的差异无法归因。

| 手写版（`agent.py`） | 图版（`agent_graph.py`） |
|---|---|
| `for step in range(max_steps)` | `agent` 与 `tools` 之间的**条件回边** |
| `for tc in msg.tool_calls` | `tools` 节点内部循环 |
| `if not msg.tool_calls: return` | 条件边 `agent → report_prep` |
| 预算耗尽后 `continue` | `tools` 内保留同一守卫，`budget_exhausted` 进状态 |
| `build_report` 的 `for attempt` | `report` 节点的**自环**（隐式重试 → 可见的边） |
| `working = list(messages)` 局部副本 | 独立的 `report_messages` 状态键 |
| `run_agent()` | `run_agent_graph()`，同签名同返回类型 |

```
START → agent ─┬─(无 tool_calls)→ report_prep → report ─┬→ finalize → END
        ▲      └─(有 tool_calls)→ tools ─┬→ report_prep  ├→ abort    → END
        └──────────────────────────────┘ │              └→ report（自环）
                          (step < max)     │
                          (step ≥ max) ────┘
```

接入方式用**显式参数注入**：`evaluate_document_dual(agent_runner=run_agent_graph)`。
不用环境变量、不改模块全局 —— 项目原纪律是「不改写模块全局，避免隐藏状态污染」
（见 `agent.py` 里 `llm_verify` 的处理）。若靠环境变量切换实现，两次运行的区别
就不在运行记录里了。

## 三、怎么证明等价

**方法**：同一脚本（`freeze_baseline.py`）、同一数据、同一度量，只把
`--agent-impl` 从 `manual` 换成 `graph`，然后用 `compare_baselines.py` 逐点 diff。

只看总分是不够的：总数相同可能是 3 个变好 + 3 个变坏互相抵消。所以逐点比
`actual` / `correct` / `source` / `attribute`。

**结果**：

| 对比 | 判定点差异 | 正确→错误回归 |
|---|---|---|
| 手写 vs 图（第 1 次） | **0 处** | 0 |
| 手写 vs 图（第 2 次） | 1 处（`E070` 的 `attribute`） | 0 |
| 图（第 1 次）vs 图（第 2 次） | 1 处（**同一个点、同一个字段**） | 0 |

**噪声底数校准——这是让结论成立的关键一步。**

LLM 路径本身有方差，所以「跨实现 0 差异」不能直接解读为等价证明。为此专门让
**同一个实现**（图版）跑两次，得到噪声底数 = 1 处（`E070` 的 `attribute` 在
`unknown` 与 `颜色引用方式` 之间摇摆，属抽取层方差，两个方向的 `correct` 都是 False，
不影响任何判定）。

跨实现差异（1 处）**等于**噪声底数（1 处），且是同一个点同一个字段 ——
因此正确的表述是「**差异不可区分**」，而不是「图版更准」或「图版更差」。

## 四、踩到的坑

1. **`recursion_limit` 默认 25 不够。** 6 步循环 + 3 次报告重试约需 18 步，余量太小，
   长文档会在跑到一半时抛 `GraphRecursionError`。已显式提到 100。
   这类「跑一半才炸」的故障最贵，因为前面的 API 花费全废。
2. **`messages` 与 `report_messages` 必须分开。** 原版 `build_report` 用
   `working = list(messages)` 是局部副本，报告重试追加的消息不污染 `messages`。
   若图里共用一个键，`steps = len([m for m in messages if role=="assistant"])`
   会把重试的 assistant 消息算进轮次 —— 指标就不等价了。
3. **`engine_ratio` 分母 bug（继承自 `run_hybrid_eval.py`）。** 分子是「判定条数」，
   分母写的是「判定点总数」，两个分母不同，小样本上算出 `117%`。已改为按判定条数算，
   `compare_baselines.py` 一律从 `sources` 重算，保证新旧基线该指标可对比。
   > 因此 `pre-langgraph.json` 里存的 `engine_ratio=0.7647` 是旧口径的错误值，
   > 正确值是 `91/122 = 74.6%`。该文件**故意没有回填** —— 回填等于篡改测量记录，
   > 而正确值可以从已存的 `sources` 重算，不丢失信息。
4. **装 langgraph 动了共享依赖。** 新增 27 个包（含 httpx / certifi / urllib3）。
   装完做了环境回归：8 份文档 12 个判定点，与冻结基线逐点对比 0 差异，
   确认装包没有改变行为。若跳过这一步，后面所有对比都无法归因。

## 五、刻意没做的事

- **没有切换默认路径。** CLI 仍走手写版。切换是独立的一步，需要先确认噪声底数
  （本报告已完成），再改默认并重跑一次全量。
- **没有接持久化 checkpointer。** 目前只用 `InMemorySaver`（进程内）。
  真正跨进程续跑需要 `SqliteSaver` 或 Postgres，是额外依赖与额外设计。
- **没有接入实际的人审流程。** `interrupt_before=["report"]` 只是提供了能力，
  审批界面 / 审批记录 / 谁来批，都还没设计。
- **没有处理 `E070` 的抽取波动。** 它属于抽取层的方差，与控制流迁移无关，
  留作独立问题。

## 六、下一步

1. 切换 CLI 默认到图版，重跑全量基线，逐点对比后作为新基准。
2. 给 checkpointer 加一个可复现的续跑演示：跑到中途 kill，再从断点恢复，
   证明「不必重跑前面的检索」。
3. `SqliteSaver` 落盘，让续跑跨进程可用。
