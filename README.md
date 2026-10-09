# design-spec-agent

输入一份设计文档，agent 自动对照**设计规范知识库**做逐条合规判定，输出
**可校验的结构化报告 + 引用出处 + 修改建议**。

> 当前状态：**Week 1（手写 ReAct 循环 + 结构化输出契约）**
> 知识库与检索仍为占位实现，Week 2 接入真实 RAG。

## 它解决什么问题

设计规范的落地检查长期依赖人工 review：慢、不一致、无法规模化。
本项目把"设计规范"变成模型可检索、可判定的知识，让合规检查变成一次可复现的调用。

对能力要求的覆盖：

| 能力 | 在本项目中的落点 |
|---|---|
| 设计理解力 | `src/tools.py` 里把设计规范形式化为可判定条目（带出处、带取值边界） |
| AI 工程力 | `src/agent.py` 手写 ReAct 循环：工具调用、结果回填、终止条件、异常兜底 |
| RAG 与知识管理 | Week 2：分块策略对比 → 混合检索 → rerank → 评测集验证语义质量 |
| 开发与脚本 | Python 工程结构 + CLI + 类型标注 + 日志指标 |
| Prompt Engineering | `src/schemas.py` 输出契约 + 校验失败自动重试；判定纪律写进系统提示 |

## 快速开始

```bash
# 1. 装依赖（任选其一）
uv venv && uv pip install -e ".[rag]"
# 或
python -m venv .venv && .venv\Scripts\activate && pip install -e ".[rag]"

# 2. 配置 API key（重要，见下节）
copy .env.example .env      # macOS/Linux: cp .env.example .env
# 然后编辑 .env，把 LLM_API_KEY 换成你自己的密钥

# 3. 自检连通性
python cli.py --ping

# 4. 跑一份示例文档
python cli.py samples/sample_doc.md -v
```

输出落在 `out/`：`*.report.json`（结构化结果）和 `*.report.md`（人读版本）。

## 配置 API key

**key 只放 `.env`，不写进代码、不提交到 git。**

1. 复制模板：`copy .env.example .env`
2. 打开 `.env`，改这一行：
   ```ini
   LLM_API_KEY=sk-你的真实密钥
   ```
3. 确认 `.gitignore` 已包含 `.env`（本项目已包含）

`.env` 的完整字段说明见 `.env.example`，可切换 DeepSeek / OpenAI 或任何 OpenAI 兼容接口。

## 目录结构

```
src/schemas.py   输出契约：SpecReport / Issue / AgentReport
src/tools.py     工具定义与 JSON Schema（Week 1 为占位检索）
src/llm.py       模型客户端：唯一接触 API 的地方，顺带收 token 与耗时
src/agent.py     手写 ReAct 循环 + 结构化输出校验重试 + markdown 渲染
cli.py           命令行入口
samples/         示例设计文档
kb/              设计规范知识库源文件（Week 2 填充）
eval/            评测集与评测脚本（Week 2 起）
out/             运行产物（已 gitignore）
```

## 设计决策

- **为什么先手写循环再上框架**：agent 的本质只有 LLM + 工具 + 记忆 + 终止条件四件事。
  先手写一遍，再用 LangGraph 重写，才能说清框架究竟封装了什么、代价是什么。
- **为什么要结构化输出契约**：自然语言回答无法被程序可靠消费。用 Pydantic 定义字段与
  取值约束，校验失败时把错误原文回喂给模型自动修正 —— 这就是"鲁棒性"的工程含义。
- **为什么强制引用出处**：判定必须能被溯源，否则无法回答"它凭什么这么说"，
  也无法在规范更新后定位失效结论。
- **为什么区分 pass / fail / uncertain**：检索不到依据时宁可判 uncertain 也不猜。
  误报和漏报都是质量问题，而"承认不知道"是可控性的前提。

## 已知限制

- 知识库为 8 条占位数据，**不是真实可用规模**，检索基于关键词打分而非语义
- 无评测集，当前所有效果均为定性判断（Week 2 补齐 Recall@k 与端到端准确率）
- 单轮执行，无持久化，进程中断即丢失中间状态（Week 3 引入 checkpointer）
- 长文档会被整体塞进上下文，尚无截断/分段策略

## Roadmap

- [x] **Week 1** 手写 ReAct 循环、工具调用、结构化输出与校验重试
- [ ] **Week 2** 真实知识库 + 三种分块策略对比 + 混合检索 + rerank + 30 条检索评测集
- [ ] **Week 3** 迁移到 LangGraph 状态图 + 条件边重试 + checkpointer + 15 条端到端评测
- [ ] **Week 4** Gradio 过程可视化界面 + 评测报告 + 三个失败案例复盘
