---
skillId: ui.interaction_states
name: 交互状态完整性
agentId: ui_graph
description: 任何按钮 / 表单 / 异步动作都必须具备 Loading、Success、Error 三态，禁止生成"点了没反应"的静态按钮。
version: 1.0.0
enabled: true
priority: 25
tools:
  - read_file
  - apply_code_edit
  - create_file
  - run_build
keywords:
  - 交互
  - 点击
  - 按钮
  - 提交
  - loading
  - 加载中
  - 错误提示
  - 网络请求
ragRefs: []
---

# 交互状态完整性（强制）

你生成的每一个可点击元素都必须有明确、可观察的结果。禁止出现"按钮点了
没有任何反应"的占位交互；如果需求没有定义交互，必须输出静态占位样式并在
代码注释中标注 `TODO(interaction)`，而不是伪造一个点击事件。

## 1. 每个异步交互必须实现三态

1. **Loading**：点击后立即把按钮置为 `disabled`，文案切到"提交中…"，
   并保留原按钮宽度避免布局抖动；同时显示 `role="status"` 的加载提示。
2. **Error**：请求失败时恢复按钮可点击，在表单 / 卡片内显示可读错误
   文案（含 HTTP 状态或后端 message），并提供"重试"入口。
3. **Success**：请求成功后更新列表 / 详情数据，或用明确的成功提示替换
   表单内容；禁止只在控制台打印。

## 2. 状态建模硬性要求

- 使用独立的布尔 / 枚举状态建模：`status: 'idle' | 'loading' | 'success' | 'error'`，
  不要用同一个 `loading` 布尔值同时表达成功与失败。
- 请求必须包在 `try / catch / finally` 中，`finally` 负责解除 loading，
  避免异常路径把按钮永久锁死。
- 并发点击必须幂等：loading 期间直接 `return`，不要重复发起请求。
- 空数据、无权限、网络断开都要有独立的空状态 / 错误状态，不能白屏。

## 3. 可访问性要求

- Loading 容器使用 `role="status"` + `aria-live="polite"`；
- 错误容器使用 `role="alert"`；
- 按钮的 `disabled` 状态必须配合 `aria-disabled="true"` 与视觉降级；
- 按钮文案变化时不要让焦点丢失。

## 4. 网络契约

- 请求路径必须来自 Architect 契约中的 `component_interactions[].api_route`，
  或复用后端已经生成的路由清单；禁止凭空编造 `/api/xxx`。
- `fetch` 必须检查 `response.ok`，并解析后端统一错误体 `{code, message}`。
- 请求体字段必须与后端 DTO 一致；没有后端支持时降级为本地提示，
  并在代码中写明原因，而不是请求一个不存在的接口。

## 5. 交付前自检

1. 列出本次新增 / 修改的所有交互元素。
2. 逐个确认：Loading、Error、Success、空状态是否可见。
3. 确认没有 `onClick={() => {}}`、`console.log` 式假交互。
4. 在总结中输出 `Interactions:` 清单，说明每个按钮的真实行为与请求路径。
