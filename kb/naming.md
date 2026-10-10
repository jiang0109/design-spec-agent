---
doc_id: naming
title: 命名规范
version: 1.0
scope: 设计文件中的图层名、组件名与设计令牌名

rules:
  - id: NAME-001
    title: 图层命名格式
    statement: >
      图层名必须使用「组件名/修饰/状态」格式，由斜杠分隔的英文段组成，
      必须匹配正则 ^[A-Za-z][A-Za-z0-9]*(\/[A-Za-z0-9]+){1,2}$。
    params:
      pattern: "^[A-Za-z][A-Za-z0-9]*(\\/[A-Za-z0-9]+){1,2}$"
      separator: "/"
      segment_count: 2-3
      mapping:
        segment_1: 组件名
        segment_2: 修饰
        segment_3: 状态
    applies_when:
      - 图层属于设计系统中的组件实例或其内部具名图层
    exceptions:
      - 一次性草稿画板（Draft / Scratch）内的图层不受此限制
    positive_case:
      desc: 按钮默认态图层命名为 Button/Primary/Default
      value: Button/Primary/Default
      expected_verdict: pass
    negative_case:
      desc: 按钮禁用态图层命名为 btn-primary-disabled，未使用斜杠分段
      value: btn-primary-disabled
      expected_verdict: fail
    rationale: >
      斜杠在图层树里是唯一会自动生成层级结构的分隔符。用「组件名/修饰/状态」
      命名，图层面板会自动折叠出可读的三层结构，同时让图层名成为一个可检索
      的路径：任何一次"找出所有禁用态按钮"的盘点都能靠字符串匹配完成，而不
      需要人工逐个点开检查。

  - id: NAME-002
    title: 组件命名格式
    statement: >
      组件名必须使用 PascalCase 英文单词，必须匹配正则 ^[A-Z][a-zA-Z0-9]*$，
      禁止空格、连字符、下划线与中文。
    params:
      pattern: "^[A-Z][a-zA-Z0-9]*$"
      case_style: PascalCase
      forbidden: [空格, 连字符, 下划线, 中文]
      min_words: 1
    applies_when:
      - 图层被登记为组件（Component）或组件集（Component Set）
    exceptions:
      - 无
    positive_case:
      desc: 图标按钮组件命名为 IconButton
      value: IconButton
      expected_verdict: pass
    negative_case:
      desc: 图标按钮组件命名为 icon_button，使用下划线且首字母未大写
      value: icon_button
      expected_verdict: fail
    rationale: >
      PascalCase 是组件名在代码中的原样形态，组件名与代码里的组件名完全一致，
      检索时一次命中，不需要维护"设计叫 A、代码叫 B"的对照表。此外组件名往往
      被直接用于图标与资源导出路径，空格与连字符会在导出环节被工具替换成不可
      控的字符，导致同名资源在不同平台上命名不一致。

  - id: NAME-003
    title: 禁止默认名称
    statement: >
      禁止保留设计工具自动生成的默认名称，凡匹配
      ^(Frame|Rectangle|Group|Ellipse|Line|Vector|Path)\s+\d+$ 的图层名均判定为不合规。
    params:
      pattern: "^(Frame|Rectangle|Group|Ellipse|Line|Vector|Path)\\s+\\d+$"
      forbidden_examples: ["Frame 123", "Rectangle 45", "Group 7"]
      require_rename_before_handoff: true
    applies_when:
      - 图层参与交付（Handoff）或进入评审
    exceptions:
      - 明确标记为废弃（deprecated）且不参与交付的历史图层
    positive_case:
      desc: 原 Group 7 已重命名为 Button/Primary/Default
      value: Button/Primary/Default
      expected_verdict: pass
    negative_case:
      desc: 图层仍保留自动生成的默认名称 Frame 123
      value: Frame 123
      expected_verdict: fail
    rationale: >
      默认名称是"无人对这块内容作出决定"的信号。它无法表达意图，也无法被
      检索，交付后开发只能靠坐标猜测图层职责。要求改名并非形式主义，而是强制
      作者在命名的那一刻回答"这个图层是干什么的"，这是最低成本的意图留存手段。

  - id: NAME-004
    title: 设计令牌命名
    statement: >
      设计令牌必须使用小写字母与连字符（kebab-case），按「类别-语义-变体」组织，
      必须匹配正则 ^[a-z]+(-[a-z0-9]+){2,}$。
    params:
      pattern: "^[a-z]+(-[a-z0-9]+){2,}$"
      case_style: kebab-case
      mapping:
        segment_1: 类别
        segment_2: 语义
        segment_3: 变体
      examples_of_categories: [color, space, radius, font, shadow]
    applies_when:
      - 数值或颜色被抽为可复用令牌并在组件中引用
    exceptions:
      - 由第三方库导入且无法改名的令牌
    positive_case:
      desc: 正文主色令牌命名为 color-text-primary
      value: color-text-primary
      expected_verdict: pass
    negative_case:
      desc: 正文主色令牌命名为 colorTextPrimary，未使用连字符且未分段
      value: colorTextPrimary
      expected_verdict: fail
    rationale: >
      kebab-case 是 CSS 自定义属性的原生写法，令牌名可以不加转换地落到
      --color-text-primary 上，让设计与实现共享同一个名字。固定的三段结构
      则保证令牌集可被前缀检索：查 color- 得到全部颜色，查 space-card-
      得到卡片相关的全部间距，令牌规模增长后仍然可控。
---

# 命名规范

命名是设计文件里唯一能被机器读取的语义。本规范约束图层、组件与设计令牌三类名字的
写法，目标是让设计文件在交付时具备可检索、可对齐代码、可自动校验三项能力，
而不是依赖作者的个人习惯。

## 图层命名格式

图层名必须使用「组件名/修饰/状态」格式，由斜杠分隔的英文段组成，必须匹配
`^[A-Za-z][A-Za-z0-9]*(\/[A-Za-z0-9]+){1,2}$`，即 2 到 3 段。

| 段位 | 含义 | 示例 |
|---|---|---|
| 第一段 | 组件名 | `Button` |
| 第二段 | 修饰 | `Primary` |
| 第三段 | 状态 | `Default`、`Disabled` |

**为什么这么规定。** 斜杠在图层树里是唯一会自动生成层级结构的分隔符。用三段式
命名，图层面板会自动折叠出可读结构；同时图层名成为一个可检索路径，任何一次
"找出所有禁用态按钮"的盘点都能用字符串匹配完成，不必逐个点开检查。

> 例外：一次性草稿画板（Draft / Scratch）内的图层不受此限制。

**正例**：`Button/Primary/Default`、`Button/Primary/Disabled`。

**反例**：`btn-primary-disabled`，未使用斜杠分段，既无法折叠也无法按段检索。

## 组件命名格式

组件名必须使用 **PascalCase 英文单词**，必须匹配 `^[A-Z][a-zA-Z0-9]*$`，
禁止空格、连字符、下划线与中文。

**为什么这么规定。** PascalCase 正是组件名在代码里的原样形态。设计与代码用
同一个名字，检索时一次命中，不需要维护"设计叫 A、代码叫 B"的对照表。此外组件名
常被直接用于图标与资源导出路径，空格与连字符会在导出环节被工具替换成不可控字符，
导致同一资源在不同平台上命名不一致。

**正例**：`IconButton`、`DataTable`。

**反例**：`icon_button`（下划线且首字母未大写）、`Data Table`（含空格），
均不匹配命名正则。

## 禁止默认名称

禁止保留设计工具自动生成的默认名称。凡匹配
`^(Frame|Rectangle|Group|Ellipse|Line|Vector|Path)\s+\d+$` 的图层名均判定为不合规，
必须在交付前重命名。

**为什么这么规定。** 默认名称是"无人对这块内容作出决定"的信号：它既不表达意图，
也无法被检索，交付后开发只能靠坐标猜测图层职责。要求改名不是形式主义，而是强制
作者在命名的那一刻回答"这个图层是干什么的"，这是成本最低的意图留存手段。

> 例外：明确标记为废弃（deprecated）且不参与交付的历史图层。

**正例**：原 `Group 7` 已重命名为 `Button/Primary/Default`。

**反例**：图层仍保留 `Frame 123`、`Rectangle 45`、`Group 7` 这类默认名称。

## 设计令牌命名

设计令牌必须使用小写字母与连字符（kebab-case），按「**类别-语义-变体**」三段组织，
必须匹配 `^[a-z]+(-[a-z0-9]+){2,}$`。常见类别前缀包括 `color`、`space`、
`radius`、`font`、`shadow`。

**为什么这么规定。** kebab-case 是 CSS 自定义属性的原生写法，令牌可以不经过任何
转换地落到 `--color-text-primary` 上，让设计与实现共享同一个名字。固定的三段结构
还保证令牌集可被前缀检索：查 `color-` 得到全部颜色，查 `space-card-` 得到卡片相关的
全部间距。令牌规模从几十条涨到几百条之后，唯一还能维持秩序的机制就是命名结构本身。

> 例外：由第三方库导入且无法改名的令牌。

**正例**：`color-text-primary`、`space-card-padding`。

**反例**：`colorTextPrimary`（camelCase，无法直接作为 CSS 变量名）、
`Color-Text-Primary`（含大写，破坏 kebab-case 约定），均不匹配命名正则。
