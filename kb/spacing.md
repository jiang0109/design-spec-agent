---
doc_id: spacing
title: 间距规范
version: 1.0
scope: 所有界面组件的间距取值与用法

rules:
  - id: SPACING-001
    title: 卡片内边距取值
    statement: >
      卡片内边距只允许 12 / 16 / 24 三档，按卡片类型映射，禁止其他档位。
    params:
      allowed: [12, 16, 24]
      mapping:
        compact_list: 12
        regular_content: 16
        marketing: 24
    applies_when:
      - 组件类型为卡片（Card）
    exceptions:
      - 全屏沉浸式视图内的卡片不受此限制
    positive_case:
      desc: 常规内容卡片设置内边距 16px
      value: 16
      expected_verdict: pass
    negative_case:
      desc: 常规内容卡片设置内边距 20px
      value: 20
      expected_verdict: fail
    rationale: >
      三档阶梯分别覆盖紧凑、常规、宽松三种信息密度。引入第四档会让同层级
      卡片在没有设计意图的情况下产生视觉差异，并破坏栅格对齐。

  - id: SPACING-002
    title: 卡片间距取值
    statement: >
      卡片之间的间距只允许 16 或 24，同一组卡片必须使用同一间距，禁止混用。
    params:
      allowed: [16, 24]
      mapping:
        inside_group: 16
        between_groups: 24
    applies_when:
      - 页面中存在两个及以上并列卡片
    exceptions:
      - 无
    positive_case:
      desc: 同一组内三张卡片间距均为 16px
      value: 16
      expected_verdict: pass
    negative_case:
      desc: 同一组卡片间距混用 16px 与 24px
      value: [16, 24]
      expected_verdict: fail
    rationale: >
      间距是分组关系的视觉信号。组内 16、组间 24 形成 1.5 倍反差，
      让用户无需分割线即可识别分组边界。混用则分组关系失效。

  - id: SPACING-003
    title: 分组的间距区分
    statement: >
      分组的间距必须严格大于组内间距，且不得低于 24。
    params:
      min_between_groups: 24
      constraint: between_groups > inside_group
    applies_when:
      - 页面使用了卡片分组或多段式内容布局
    exceptions:
      - 无
    positive_case:
      desc: 组内 16px、组间 24px
      value: { inside_group: 16, between_groups: 24 }
      expected_verdict: pass
    negative_case:
      desc: 组内 16px、组间 16px，分组关系无法辨认
      value: { inside_group: 16, between_groups: 16 }
      expected_verdict: fail
    rationale: >
      分组的可识别性来自"组间明显大于组内"。一旦两者相等或倒置，
      用户会读成同一组，信息架构被破坏。

  - id: SPACING-004
    title: 页面外边距取值
    statement: >
      页面左右外边距在桌面端为 24，窄屏（<768px）为 16；
      内容区最大宽度不超过 1200。
    params:
      range: 16-24
      mapping:
        desktop: 24
        narrow: 16
      max_content_width: 1200
    applies_when:
      - 页面级容器布局
    exceptions:
      - 全出血（full-bleed）横幅与背景图不受外边距限制
    positive_case:
      desc: 桌面端页面左右外边距 24px
      value: 24
      expected_verdict: pass
    negative_case:
      desc: 桌面端页面左右外边距 12px
      value: 12
      expected_verdict: fail
    rationale: >
      24px 是栅格基准 8 的整数倍，且与卡片内边距形成视觉呼应；
      窄屏收窄到 16 以保留内容可用宽度。

  - id: SPACING-005
    title: 组件内部元素间距
    statement: >
      组件内部元素间距取值为 4 的整数倍（4 / 8 / 12 / 16），
      且任一可取点击元素的高度不低于 40。
    params:
      allowed: [4, 8, 12, 16]
      step: 4
      min_touch_target: 40
    applies_when:
      - 组件由两个及以上内部元素构成
    exceptions:
      - 纯装饰性分隔线不参与间距计算
    positive_case:
      desc: 图标与文字间距 8px，按钮高度 40px
      value: { gap: 8, height: 40 }
      expected_verdict: pass
    negative_case:
      desc: 图标与文字间距 6px，按钮高度 36px
      value: { gap: 6, height: 36 }
      expected_verdict: fail
    rationale: >
      4 的倍数保证缩放到任意像素密度都不产生半像素；
      40 是桌面端鼠标可达性与视觉密度之间的平衡点。
---

# 间距规范

间距是界面中成本最低、收益最高的秩序来源。本规范定义全局间距阶梯及其使用边界，
目标是在不增加分割线的前提下，让分组关系与信息层级从间距本身就能读出来。

## 卡片内边距取值

卡片内边距用于建立卡片内容与卡片边界之间的呼吸空间。全站只允许 **12、16、24** 三档，
并按下表与卡片类型一一对应：

| 卡片类型 | 内边距 | 典型场景 |
|---|---|---|
| 紧凑型列表卡片 | 12 | 设置列表、消息列表 |
| 常规内容卡片 | 16 | 数据卡片、概览卡片 |
| 营销位大卡片 | 24 | 首屏推荐位、活动入口 |

除上述三档外的任何取值均视为不合规，包括看起来"差不多"的 20 与 32。

**为什么这么规定。** 三档阶梯分别覆盖紧凑、常规、宽松三种信息密度。
一旦引入第四档，同层级卡片会在没有设计意图的情况下产生视觉差异；
同时非阶梯取值会破坏栅格对齐，导致卡片边缘无法与页面基准线对齐。

**正例**：常规内容卡片内边距设为 16px。

**反例**：常规内容卡片内边距设为 20px。20 不在阶梯内，且与 16 的差异
不足以传达任何设计意图，只会让卡片看起来"歪了 4px"。

## 卡片间距取值

卡片之间的间距只允许 **16 或 24**，其中同一组卡片之间使用 16，
不同分组之间使用 24。同一组内必须使用同一间距，禁止混用。

**为什么这么规定。** 间距本身就是分组关系的视觉信号。组内 16、
组间 24 形成 1.5 倍反差，用户无需分割线即可识别分组边界。
一旦组内混用两种间距，分组关系立刻失效，用户会开始寻找并不存在的逻辑。

**正例**：同一组三张卡片，间距均为 16px。

**反例**：同一组卡片中，前两张间距 16px、后两张间距 24px。

## 分组的间距区分

分组的间距必须**严格大于**组内间距，且最低不得小于 24。

**为什么这么规定。** 分组的可识别性完全来自"组间明显大于组内"这一反差。
当两者相等时，用户会把两组合并阅读；当组间小于组内时，视觉分组与
信息分组完全倒置，这是比"不美观"严重得多的信息架构错误。

**正例**：组内 16px、组间 24px。

**反例**：组内 16px、组间 16px，分组关系无法辨认。

## 页面外边距取值

页面左右外边距在桌面端为 **24**，窄屏（宽度小于 768px）为 **16**；
内容区最大宽度不超过 **1200**。

**为什么这么规定。** 24 是栅格基准 8 的整数倍，并且与卡片内边距形成视觉呼应，
让页面留白与卡片留白读起来是同一套系统；窄屏收窄到 16 是为了在有限宽度下
保留足够的内容可用宽度。内容区限制 1200 则避免超宽屏下行长过长影响阅读。

**正例**：桌面端页面左右外边距 24px。

**反例**：桌面端页面左右外边距 12px，导致内容贴边、与卡片内边距失去呼应。

## 组件内部元素间距

组件内部元素间距取值为 **4 的整数倍**（4 / 8 / 12 / 16），
且任意可点击元素的高度不低于 **40**。

**为什么这么规定。** 4 的倍数保证界面缩放到任意像素密度时都不产生半像素模糊；
40 是桌面端鼠标可达性与视觉密度之间的平衡点，低于该值点击命中率会明显下降。

> 例外：纯装饰性分隔线不参与间距计算。

**正例**：图标与文字间距 8px，按钮高度 40px。

**反例**：图标与文字间距 6px，按钮高度 36px。6 不是 4 的倍数，
36 低于最小点击高度。
