---
skillId: ui.frontend_ecosystem
name: 前端工具类生态
agentId: ui_graph
priority: 45
description: 统一前端图标与类名合并方案，优先使用 lucide-react 与 clsx，禁止重复造轮子。
tools:
  - read_file
  - apply_code_edit
  - create_file
  - run_build
keywords:
  - 图标
  - icon
  - lucide
  - clsx
  - className
  - 类名
---

生成或修改 React 组件时，必须遵守以下工具类生态约定：

1. **图标统一使用 `lucide-react`**：优先从该库导入语义化图标，例如
   `import { Plus, Search, Trash2, Pencil, Check } from 'lucide-react'`。
   禁止手写重复的 inline SVG；只有项目尚未安装 `lucide-react` 时，才允许
   使用现有图标方案，并且不得为此新增依赖。
2. **图标尺寸与颜色由当前文本样式继承**：使用 `size={16}` / `size={20}`
   与 `strokeWidth={2}`，颜色使用 `currentColor`；按钮内的图标必须带
   `aria-hidden="true"`，纯图标按钮必须提供 `aria-label`。
3. **className 合并统一使用 `clsx`**：条件样式必须写成
   `clsx('base', active && 'is-active', disabled && 'opacity-50')`，
   禁止用字符串拼接产生 `false` / `undefined` 类名。
4. **不要为了用而用**：静态且只有单一类名的元素可以直接写字符串；
   只有存在条件分支或外部 className 合并时才引入 `clsx`。
5. **依赖安全**：生成 import 前先确认 `package.json` 已包含对应依赖；
   若项目未安装，优先复用已有库或原生 CSS，不得输出必然构建失败的 import。
6. **布局强制 Grid**：页面主容器与卡片列表必须使用响应式 Grid。
   已配置 Tailwind 时使用
   `grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6`；
   未配置 Tailwind 时使用等价 CSS Grid 与媒体查询。禁止把所有卡片写成
   `flex flex-col` 单列纵向堆叠。
