---
doc_id: component
title: 组件属性规范
version: 1.0
scope: 设计系统中所有可复用组件的属性声明、命名、默认值与取值

rules:
  - id: COMP-001
    title: 必填属性约定
    statement: >
      每个组件必须显式声明 size 与 variant 两个属性，二者缺任意一个即判定为不合规。
    params:
      required: [size, variant]
      min_required_count: 2
      allow_inherited_from_parent: false
    applies_when:
      - 组件被登记进设计系统的组件库
      - 评审对象为可复用组件（Component）而非一次性图层组
    exceptions:
      - 纯布局容器（Container / Divider）不声明 size 与 variant
    positive_case:
      desc: 组件 ClipboardCard 的属性列表同时声明 size 与 variant
      value: { declared: [size, variant] }
      expected_verdict: pass
    negative_case:
      desc: 组件 ClipboardCard 只声明了 variant，属性列表中没有 size
      value: { declared: [variant] }
      expected_verdict: fail
    rationale: >
      size 与 variant 是页面组装时的两个稳定调用面。属性一旦缺失，使用方只能
      复制一份组件再改样式，组件库会以副本形式分裂；要求显式声明，是为了让
      "这个组件有哪些可调档位"在属性面板里就能读完，而不用追问作者。

  - id: COMP-002
    title: 组件属性命名格式
    statement: >
      组件属性名一律使用 camelCase，必须匹配正则 ^[a-z][a-zA-Z0-9]*$；
      禁止 kebab-case，禁止中文属性名。
    params:
      pattern: "^[a-z][a-zA-Z0-9]*$"
      case_style: camelCase
      forbidden_styles: [kebab-case, snake_case, PascalCase, chinese]
    applies_when:
      - 组件声明任意属性（含布尔、枚举、文本、插槽类属性）
    exceptions:
      - 无
    positive_case:
      desc: 组件属性名为 showDivider，全部由小写字母与单词内大写组成
      value: showDivider
      expected_verdict: pass
    negative_case:
      desc: 属性名写成 is-loading（kebab-case），不匹配命名正则
      value: is-loading
      expected_verdict: fail
    rationale: >
      属性名会被自动转换为代码里的 props 与 design token 路径。camelCase 是
      JS/TS 生态的原生形态，转换零成本；kebab-case 进入代码需要一次转换，
      中文属性名则完全无法参与代码生成与自动校验，会让结构化审查失效。

  - id: COMP-003
    title: 布尔属性前缀
    statement: >
      布尔属性必须以 is、has、can 三者之一开头，且后接大写字母，命名必须匹配
      正则 ^(is|has|can)[A-Z]。
    params:
      pattern: "^(is|has|can)[A-Z]"
      allowed_prefixes: [is, has, can]
      mapping:
        is: 表示组件当前处于某状态
        has: 表示组件是否包含某子元素
        can: 表示组件是否具备某能力
    applies_when:
      - 属性取值为布尔类型（true / false）
    exceptions:
      - 无
    positive_case:
      desc: 布尔属性命名为 isLoading，匹配 ^(is|has|can)[A-Z]
      value: isLoading
      expected_verdict: pass
    negative_case:
      desc: 布尔属性命名为 loading，缺少 is/has/can 前缀
      value: loading
      expected_verdict: fail
    rationale: >
      读到一个属性名时，判断成本最低的信息是"它是不是开关"。前缀把类型信息
      前置到名字第一个字符，使用方不必展开属性定义就能知道该传 true 还是传
      一个字符串；同时把 exists / visible 这类模糊语义收敛到 is、has、can 三
      个动词，避免同一个含义被拆成多种写法。

  - id: COMP-004
    title: 属性默认值要求
    statement: >
      size 与 variant 必须提供默认值，任一属性缺少默认值即判定为不合规。
    params:
      defaults_required: [size, variant]
      mapping:
        size: medium
        variant: primary
    applies_when:
      - 组件在属性面板中存在 size 或 variant 属性
    exceptions:
      - 无
    positive_case:
      desc: size 默认 medium、variant 默认 primary，组件拖入画布即为可用状态
      value: { size: medium, variant: primary }
      expected_verdict: pass
    negative_case:
      desc: variant 未设置默认值，组件拖入画布后变体为空
      value: { size: medium, variant: null }
      expected_verdict: fail
    rationale: >
      默认值决定了组件的"无人配置时长相"。没有默认值，组件一落到画布就是空
      壳或首个枚举值的随机结果，页面里会散落大量未被作者意识到的不一致实例；
      固定默认值让默认状态成为唯一事实来源，改动默认值即可全局调整基线。

  - id: COMP-005
    title: 变体属性取值
    statement: >
      variant 的取值只允许 primary、secondary、ghost、danger 四者之一，禁止第五种取值。
    params:
      allowed: [primary, secondary, ghost, danger]
      allow_multiple: false
      exclusive_with: []
    applies_when:
      - 组件声明了 variant 属性
    exceptions:
      - 品牌定制组件在业务方指定下可扩展取值，但必须在组件文档中登记新取值
    positive_case:
      desc: 卡片组件 variant 取值为 primary
      value: primary
      expected_verdict: pass
    negative_case:
      desc: 卡片组件 variant 取值为 tertiary，不在枚举内
      value: tertiary
      expected_verdict: fail
    rationale: >
      四个取值对应四种明确的语义强度：主操作、次操作、弱操作、破坏性操作。
      枚举封顶的目的是让"按钮有多重"成为一个可被穷举的有限集合，评审时能
      一次判完；每新增一个变体都会让视觉权重体系的重心发生偏移，必须有人
      为它负责，而不是随手加一个更浅的颜色。
---

# 组件属性规范

组件属性是设计系统与代码之间的接口。本规范约束组件对外暴露的属性有哪些、
叫什么、默认长什么样、能取哪些值，目标是让页面组装者在不阅读组件源码的前提下
就能正确调用组件，也让自动化审查能对属性面板做逐字比对。

## 必填属性约定

每个进入组件库的组件，必须在属性列表中**显式声明 size 与 variant 两个属性**。
二者缺任意一个即视为不合规，不允许通过继承父级或由使用方另行补足。

**为什么这么规定。** size 与 variant 是页面组装时的两个稳定调用面：一个决定
视觉体量，一个决定语义强度。属性缺失时，使用方唯一的替代方案是复制组件再手改
样式，于是组件库会以副本的形式分裂成十几个近似组件。要求显式声明，是为了让
"这个组件有哪些可调档位"在属性面板里一次读完。

> 例外：纯布局容器（Container / Divider）不参与此约束。

**正例**：组件 ClipboardCard 的属性列表为 `size` + `variant`，两个属性齐备。

**反例**：组件 ClipboardCard 只声明了 `variant`，属性列表中没有 `size`，
使用方为了得到一个更小的卡片只能复制出一个名为 `ClipboardCard copy 3` 的新组件。

## 组件属性命名格式

组件属性名一律使用 **camelCase**，必须匹配 `^[a-z][a-zA-Z0-9]*$`。
禁止 kebab-case、snake_case、PascalCase，禁止中文属性名。

**为什么这么规定。** 属性名会被直接转换为代码中的 props 与令牌路径。camelCase
是 JS/TS 生态的原生形态，转换成本为零；kebab-case 进入代码需要一次额外映射，
每次映射都是一次出错机会；中文属性名则完全无法参与代码生成，会让结构化审查
通道直接失效——校验脚本拿不到可比对的键名。

**正例**：属性名为 `showDivider`。

**反例**：属性名写成 `is-loading`（kebab-case），或写成 `是否加载中`（中文属性名），
二者都不匹配命名正则。

## 布尔属性前缀

布尔属性必须以 **is、has、can** 三者之一开头，且后接大写字母，
整体命名必须匹配 `^(is|has|can)[A-Z]`。三个前缀各有分工：

| 前缀 | 含义 | 示例 |
|---|---|---|
| `is` | 组件当前处于某状态 | `isLoading`、`isDisabled` |
| `has` | 组件是否包含某子元素 | `hasIcon`、`hasDivider` |
| `can` | 组件是否具备某能力 | `canClose`、`canDismiss` |

**为什么这么规定。** 读到一个属性名时，判断成本最低的信息是"它是不是开关"。
前缀把类型信息前置到名字的第一个字符，使用方不必展开属性定义就知道该传
`true` 还是传一个字符串。同时，三个动词把 `exists`、`visible`、`enabled`
这类同义写法收敛到同一种表达，避免同一含义在组件库里有多种拼法。

**正例**：布尔属性命名为 `isLoading`、`hasIcon`、`canClose`。

**反例**：布尔属性命名为 `loading`（缺前缀），或写成 `isloading`（前缀后未接大写），
二者都不匹配 `^(is|has|can)[A-Z]`。

## 属性默认值要求

`size` 与 `variant` **必须提供默认值**，任一属性缺少默认值即视为不合规。
默认值取 `size = medium`、`variant = primary`。

**为什么这么规定。** 默认值决定了组件"无人配置时的长相"。没有默认值时，组件
落到画布上要么是空壳、要么是枚举顺序里的随机首个值，页面中会散落大量连作者
都没意识到的不一致实例。把默认值固定下来，默认状态就成了一条唯一事实来源，
将来调整视觉基线只需要改这一处。

**正例**：`size` 默认 `medium`、`variant` 默认 `primary`，组件拖入画布即为可用状态。

**反例**：`variant` 未设置默认值（空值），组件拖入画布后变体为空，
渲染结果取决于使用方是否记得手动选择。

## 变体属性取值

`variant` 的取值只允许 **primary / secondary / ghost / danger** 四者之一，
每次只能取一个值，禁止第五种取值。

**为什么这么规定。** 四个取值对应四种明确的语义强度：主操作、次操作、弱操作、
破坏性操作。枚举封顶的目的是让"这个组件有多重"成为一个可穷举的有限集合，
评审时能一次判完；每新增一个变体，都会让视觉权重体系的重心发生偏移，
必须有人为它负责，而不是随手加一个更浅的灰色。

> 例外：品牌定制组件在业务方指定下可以扩展取值，但新取值必须登记在组件文档中。

**正例**：卡片组件 `variant` 取值为 `primary`。

**反例**：卡片组件 `variant` 取值为 `tertiary`，不在四类枚举内，
使用方与审查方都无法判断它与 `secondary` 的权重关系。
