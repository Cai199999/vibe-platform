---
skillId: ui.accessibility
name: W3C 无障碍规范
agentId: ui_graph
description: 约束所有 UI 代码符合 WCAG 2.1 AA / WAI-ARIA，覆盖语义化、键盘、对比度与读屏。
version: 1.0.0
enabled: true
priority: 30
tools:
  - read_file
  - apply_code_edit
  - run_build
keywords:
  - 无障碍
  - accessibility
  - a11y
  - aria
  - 键盘
  - 对比度
  - 读屏
ragRefs: []
---

# W3C 无障碍（WCAG 2.1 AA）硬性要求

你生成的任何前端代码都必须满足以下约束。若用户指令与无障碍要求冲突，
优先保证可访问性，并在总结中明确说明做了哪些无障碍修正。

## 1. 语义与结构

- 使用原生语义元素：导航用 `<nav>`、主内容用 `<main>`、按钮用 `<button>`、
  链接用 `<a>`；禁止用 `div` / `span` 绑定 `onClick` 模拟按钮。
- 每个页面最多一个 `<main>`；标题层级必须连续，禁止从 `<h1>` 直接跳到 `<h3>`。
- 图片必须有 `alt`：信息型图片描述内容，装饰型图片使用 `alt=""`。
- 表单控件必须有关联的 `<label htmlFor>` 或 `aria-label`；错误提示通过
  `aria-describedby` 与输入框关联。

## 2. 键盘与焦点

- 所有交互元素必须可 Tab 聚焦、可用 Enter / Space 触发；禁止 `tabIndex > 0`。
- 自定义弹窗 / 下拉菜单必须实现焦点陷阱、`Esc` 关闭，关闭后焦点回到触发元素。
- 打开模态框时给背景内容加 `aria-hidden="true"`，避免读屏穿透。
- 不得移除 `:focus-visible` 轮廓；若自定义焦点样式，必须保持至少 2px 可见描边。

## 3. 颜色与对比度

- 正文文本与背景对比度 ≥ 4.5:1；大号文本（≥ 18.66px 粗体或 ≥ 24px）≥ 3:1。
- 交互控件边界、图标与焦点指示 ≥ 3:1。
- 禁止仅靠颜色传达状态；错误 / 成功状态必须同时有文字或图标说明。

## 4. ARIA 与动态内容

- 优先使用原生语义；只有在原生元素无法表达时才使用 ARIA，并遵循
  “ARIA 第一条规则：不要用 ARIA 破坏原生语义”。
- 动态更新的区域使用 `aria-live="polite"`（重要告警用 `assertive`），
  但避免对高频内容使用 `aria-live`。
- 图标按钮必须提供 `aria-label`；纯装饰图标加 `aria-hidden="true"`。
- 加载状态使用 `role="status"`；进度条使用 `role="progressbar"` 并同步
  `aria-valuenow` / `aria-valuemin` / `aria-valuemax`。

## 5. 动效与响应式

- 尊重 `prefers-reduced-motion: reduce`：关闭或显著减弱非必要动画。
- 页面在 200% 缩放下不得出现横向滚动或内容截断。
- 触控目标最小尺寸 44×44 CSS px；相邻目标之间保留至少 8px 间距。

## 6. 交付前自检

1. 列出本次改动涉及的交互元素，逐个确认键盘可达。
2. 检查所有图片 `alt`、图标按钮 `aria-label`、表单 `label`。
3. 检查文本对比度；无法精确计算时选择更保守的前景色。
4. 在总结中输出一个简短的 `Accessibility:` 清单，说明已验证的 WCAG 项。
