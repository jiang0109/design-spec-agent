---
doc_id: color
title: 色彩规范
version: 1.0
scope: 文字与背景的对比度下限、语义色的引用方式与状态色的使用边界

rules:
  - id: COLOR-001
    title: 正文文字对比度
    statement: >
      正文文字（字号小于 18px，或任意字号但字重低于 600 的文字）与其直接背景的
      对比度必须不低于 4.5:1，低于该值的组合判定为不合规。
    params:
      ratio:
        min: 4.5
        unit: contrast_ratio
      applies_below_font_size: 18
      standard: "WCAG 2.1 AA 正文文本"
    applies_when:
      - 文字承载信息内容，且字号小于 18px
      - 文字字号不小于 18px 但字重低于 600（未加粗），按正文标准判定
    exceptions:
      - 纯装饰性文字（背景水印、纹理文字）不承载信息，不受此限制
      - 禁用态（disabled）控件内的文字可放宽至 3:1
    positive_case:
      desc: "主文字使用 color-text-primary（#1F2329）置于白色背景 #FFFFFF，对比度 15.8:1"
      value: 15.8
      expected_verdict: pass
    negative_case:
      desc: "次要说明文字使用 #9AA0A6 置于白色背景 #FFFFFF，对比度仅 2.6:1"
      value: 2.6
      expected_verdict: fail
    rationale: >
      4.5:1 是 WCAG 2.1 AA 对正文文本的下限，它保证在常见屏幕亮度、户外强光与
      轻度视力受损条件下字形轮廓仍能稳定辨识。正文是信息传递的主通道，把它交给
      "看起来还能看清"的浅灰，等于把可读性押在用户的显示器质量上。

  - id: COLOR-002
    title: 大号文字对比度
    statement: >
      大号文字（字号不小于 18px，或字号不小于 14px 且字重不低于 600）与其直接
      背景的对比度必须不低于 3:1。
    params:
      ratio:
        min: 3
        unit: contrast_ratio
      large_text_definition:
        - font_size_gte: 18
        - font_size_gte: 14
          font_weight_gte: 600
      standard: "WCAG 2.1 AA 大号文本"
    applies_when:
      - 文字字号不小于 18px
      - 文字字号为 14px 或 16px，且字重为 600
    exceptions:
      - 无
    positive_case:
      desc: "20px 标题使用 color-text-secondary（#878787）置于白色背景，对比度 3.6:1"
      value: 3.6
      expected_verdict: pass
    negative_case:
      desc: "24px 标题使用 #B0B4BA 置于白色背景，对比度仅 2.1:1"
      value: 2.1
      expected_verdict: fail
    rationale: >
      字号越大、笔画越粗，字形的辨识冗余越高，因此 WCAG 允许大号文字把对比度
      下限从 4.5:1 降到 3:1。这条放宽只针对"尺寸或字重已经提供额外辨识度"的
      文字；把它误用到 14px 常规正文上，等于取消了正文的可读性底线。

  - id: COLOR-003
    title: 语义色的使用
    statement: >
      所有颜色引用必须使用语义 token（形如 color-<用途>-<语义>），禁止直接引用
      原始色值变量（如 blue-500）或硬编码十六进制色值（如 #1F2329）。
    params:
      required_pattern: "^color-(text|bg|border|icon|fill)-(primary|secondary|success|warning|danger|info)(-[a-z]+)*$"
      forbidden_pattern: "^([a-z]+-[0-9]{2,3}|#[0-9A-Fa-f]{3,8})$"
      checked_in: [design_token_file, component_style, design_spec_annotation]
    applies_when:
      - 设计稿标注、token 文件或组件样式中出现任何颜色引用
    exceptions:
      - 品牌 Logo、插画与位图资源内部的多色取值不受此限制
      - 阴影与蒙层的色值不受此限制，但其引用方式仍须为语义 token
    positive_case:
      desc: 危险操作按钮背景引用 color-bg-danger，语义明确且可随主题整体切换
      value: color-bg-danger
      expected_verdict: pass
    negative_case:
      desc: 危险操作按钮背景直接引用 blue-500，绕过语义层
      value: blue-500
      expected_verdict: fail
    rationale: >
      原始色值描述的是"它是什么颜色"，语义 token 描述的是"它为什么是这个颜色"。
      只有后者能在换肤、暗色模式或品牌色调整时被一次性替换；直接引用色阶变量的
      地方，每一次视觉调整都要靠人工搜索替换，且必然遗漏。

  - id: COLOR-004
    title: 语义色类别枚举
    statement: >
      语义色只允许 primary / secondary / success / warning / danger / info 六类，
      新增任何第七类语义必须走规范变更流程，不得在实现中自行扩展。
    params:
      allowed: [primary, secondary, success, warning, danger, info]
      count: 6
      token_shape: "color-<用途>-<语义>(-<强度>)?"
      strength_suffixes: [subtle, hover, active]
    applies_when:
      - 新增、重命名或修改语义 token 定义
      - token 文件中的语义分组发生增减
    exceptions:
      - 无
    positive_case:
      desc: 警告提示条背景引用 color-bg-warning-subtle，属于已枚举语义
      value: warning
      expected_verdict: pass
    negative_case:
      desc: 新增 color-bg-error，与 danger 语义完全重复
      value: error
      expected_verdict: fail
    rationale: >
      六类是用户可稳定区分、且产品语义上互不重叠的最小集合。每新增一类，设计、
      开发、测试三侧都要重新对齐映射关系，而收益往往只是命名偏好。error 与
      danger 并存会让同一份设计稿出现两种红色，评审时无法判定哪种才正确。

  - id: COLOR-005
    title: 状态色的误用
    statement: >
      success / warning / danger / info 四类状态色只能用于表达其对应的状态语义，
      不得用于纯装饰、品牌强调或视觉层级区分。
    params:
      status_colors: [success, warning, danger, info]
      allowed_contexts:
        danger: [错误提示, 危险操作, 校验失败]
        warning: [风险提示, 待确认, 容量临界]
        success: [操作成功, 校验通过, 健康状态]
        info: [中性提示, 补充说明, 操作引导]
      forbidden_contexts: [纯装饰, 品牌强调, 层级区分, 分类色]
    applies_when:
      - 界面中出现 success / warning / danger / info 任一状态色的引用
    exceptions:
      - 数据可视化中表示真实涨跌或阈值越界的图表配色，可复用状态色语义
    positive_case:
      desc: "表单校验失败时输入框描边使用 color-border-danger，并同时展示错误文案"
      value: color-border-danger
      expected_verdict: pass
    negative_case:
      desc: "卡片顶部纯装饰色条使用 danger 色 #E5484D，不表达任何错误状态"
      value: "#E5484D"
      expected_verdict: fail
    rationale: >
      状态色的价值来自稀缺性——只有当红色始终意味着"出错了"，用户才会对它形成
      条件反射。红色一旦被用作装饰，这种条件反射就被稀释，真正的错误提示会因为
      "看起来又是一个装饰条"而被直接忽略。
---

# 色彩规范

色彩在界面里承担两件事：守住可读性底线，以及承载语义。本规范约束的正是这两件事的边界——它不规定品牌色板本身长什么样，但规定色板被引用时必须经过语义层，以及文字与背景的组合必须达到的对比度下限。

## 正文文字对比度

正文文字与背景的对比度是本规范中唯一不能凭观感判断的指标，必须按 WCAG 2.1 的相对亮度公式计算。判定标准是：**字号小于 18px 的文字，或任何未加粗的文字，与其直接背景的对比度不得低于 4.5:1**。

"直接背景"指文字实际叠加的那一层，而不是容器最外层。半透明蒙层、渐变背景、图片上的文字，均按文字正下方约一个行高范围内的实际像素取值计算。

| 文字角色 | 参考 token | 前景色 | 背景色 | 对比度 | 判定 |
|---|---|---|---|---|---|
| 主文字 | color-text-primary | #1F2329 | #FFFFFF | 15.8:1 | 通过 |
| 次要说明 | color-text-secondary | #878787 | #FFFFFF | 3.6:1 | 不通过（正文场景） |
| 弱化说明 | color-text-tertiary | #9AA0A6 | #FFFFFF | 2.6:1 | 不通过 |

**为什么这么规定。** 4.5:1 是 WCAG 2.1 AA 对正文文本的下限，它保证在常见屏幕亮度、户外强光以及轻度视力受损的条件下，字形轮廓依然稳定可辨。正文是信息传递的主通道，把它交给"看起来还能看清"的浅灰，等于把可读性押在用户的显示器质量上。

**正例**：主文字使用 color-text-primary（#1F2329）置于白色背景 #FFFFFF，对比度 15.8:1，高于 4.5:1 下限。

**反例**：次要说明文字使用 #9AA0A6 置于白色背景 #FFFFFF，对比度仅 2.6:1，低于 4.5:1 下限。同类场景应改用 #6B7280（4.8:1）或更深的值。

## 大号文字对比度

大号文字享受更宽松的下限：**字号不小于 18px，或字号不小于 14px 且字重不低于 600 时，对比度只需达到 3:1**。这两个条件是"或"的关系，满足任意一条即按大号文字判定。

需要注意 14px 这条分支附带字重要求。14px 常规字重的正文即使字号落在分支的描述范围内，也不适用 3:1，仍按正文的 4.5:1 判定。

| 场景 | 是否属于大号文字 | 对比度下限 |
|---|---|---|
| 24px semibold 标题 | 是 | 3:1 |
| 20px regular 标题 | 是（字号 ≥ 18px） | 3:1 |
| 16px semibold 小标题 | 否（字号 < 18px） | 4.5:1 |
| 14px semibold 强调文字 | 是（字号 ≥ 14px 且字重 ≥ 600） | 3:1 |
| 14px regular 正文 | 否 | 4.5:1 |

**为什么这么规定。** 字号越大、笔画越粗，字形的辨识冗余越高，因此 WCAG 允许大号文字把对比度下限从 4.5:1 降到 3:1。这条放宽只针对"尺寸或字重已经提供了额外辨识度"的文字；把它误用到 14px 常规正文上，等于直接取消了正文的可读性底线。

**正例**：20px 标题使用 color-text-secondary（#878787）置于白色背景，对比度 3.6:1，达到 3:1 下限。

**反例**：24px 标题使用 #B0B4BA 置于白色背景，对比度仅 2.1:1，即使按大号文字标准也不达标，必须替换为 #878787 或更深的值。

## 语义色的使用

颜色引用只走语义层。**所有颜色必须引用语义 token，形如 `color-<用途>-<语义>`，例如 color-bg-danger、color-text-primary；禁止直接引用原始色值变量（blue-500）或硬编码十六进制色值（#1F2329）**。

这条规则同时覆盖设计侧与代码侧：设计稿的标注、token 定义文件、组件样式表都在检查范围内。Logo、插画、位图资源内部的多色取值不参与判定，因为它们不承担界面语义。

**为什么这么规定。** 原始色值描述的是"它是什么颜色"，语义 token 描述的是"它为什么是这个颜色"。只有后者能在换肤、暗色模式或品牌色调整时被一次性替换；直接引用色阶变量的地方，每一次视觉调整都要靠人工搜索替换，而且必然遗漏——遗漏处就是暗色模式下那块刺眼的白。

**正例**：危险操作按钮背景引用 color-bg-danger，语义明确，无需改动即可随主题整体切换。

**反例**：危险操作按钮背景直接引用 blue-500，绕过了语义层。蓝色既无法表达"危险"这一语义，也让后续主题切换无从下手。

## 语义色类别枚举

语义色的类别是封闭集合：**只允许 primary、secondary、success、warning、danger、info 六类**。新增第七类语义必须走规范变更流程，不允许在实现中自行扩展。

token 的命名形态固定为 `color-<用途>-<语义>`，用途取 text、bg、border、icon、fill 之一，语义取上述六类之一，可选的强度后缀只允许 subtle、hover、active。

| 语义 | 表达的含义 | 典型用途 |
|---|---|---|
| primary | 品牌主色、主操作 | 主按钮、选中态 |
| secondary | 次级操作、辅助信息 | 次按钮、次要标签 |
| success | 成功、通过、健康 | 提交成功提示、校验通过 |
| warning | 风险、待确认 | 额度临界提示、不可逆操作前置确认 |
| danger | 错误、危险 | 校验失败、删除操作 |
| info | 中性提示、引导 | 说明气泡、空态引导 |

**为什么这么规定。** 六类是用户可稳定区分、且产品语义上互不重叠的最小集合。每新增一类，设计、开发、测试三侧都要重新对齐映射关系，而收益往往只是命名偏好。error 与 danger 并存会让同一份设计稿出现两种红色，评审时无法判定哪种才是正确的那一种。

**正例**：警告提示条背景引用 color-bg-warning-subtle，warning 在枚举内，subtle 是允许的强度后缀。

**反例**：新增 color-bg-error，值与 danger 完全相同，仅在命名上做了区分。这属于重复语义，应当合并到 danger。

## 状态色的误用

success、warning、danger、info 四类颜色是状态色，**只能用于表达其对应的状态语义**。把它们用于纯装饰、品牌强调或视觉层级区分，都判定为不合规。

判定"是否表达状态"的简单标准是：如果这个颜色被换成中性灰，信息是否丢失。若信息不丢失，说明它只是装饰，不应使用状态色。

| 状态色 | 允许的场景 | 禁止的场景 |
|---|---|---|
| danger | 错误提示、危险操作、校验失败 | 装饰性色条、卡片强调边框 |
| warning | 风险提示、待确认、容量临界 | 普通分组标题的颜色 |
| success | 操作成功、校验通过、健康状态 | 数据可视化中的随意分类色 |
| info | 中性提示、补充说明、操作引导 | 需要突出层级时的标题着色 |

**为什么这么规定。** 状态色的价值完全来自稀缺性——只有当红色始终意味着"出错了"，用户才会对它形成条件反射。红色一旦被用作装饰，这种条件反射就被稀释，真正的错误提示会因为"看起来又是一个装饰条"而被直接忽略。

**正例**：表单校验失败时，输入框描边使用 color-border-danger，并在字段下方同步给出口头化错误文案。

**反例**：卡片顶部的纯装饰色条使用 danger 色 #E5484D。它不表达任何错误状态，只会让用户误以为该卡片存在问题；应改用中性分隔或品牌色。
