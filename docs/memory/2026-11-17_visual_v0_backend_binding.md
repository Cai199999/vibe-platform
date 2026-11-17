# 2026-11-17 两阶段产出：Visual V0 → 增量后端绑定

## 目标

新建项目不再一次生成“虚拟树 + 完整契约”，而是拆成两阶段：

```text
Architect 多轮澄清
  → VISUAL_V0：只产出 VisualDocument，API/DDL/relations 必须为空
  → 画布离线编辑
  → 冻结并计算 v0 → current delta
  → BACKEND_BINDING：只产出 API/DDL/relations，复用冻结虚拟树
  → 契约确认
  → FullstackGraph（后端 + 用冻结树编译前端）
```

## 后端

### 请求 / 状态

- `ArchitectRunRequest.outputPhase`：`VISUAL_V0 | BACKEND_BINDING | FULL`；
- `visualDocument`：冻结虚拟树（BACKEND_BINDING 必传）；
- `visualDelta`：v0→current 增量包；
- `ArchitectContext` / `ArchitectState` / `ArchitectContract` / snapshot
  全链路透传 `outputPhase`。

### Prompt

- `VISUAL_V0`：
  - 只输出 VisualDocument；
  - API/DDL/relations/interactions 必须为空；
  - 不追问后端框架/数据库；
  - contract_markdown 只描述视觉原型。
- `BACKEND_BINDING`：
  - visual_document 必须为空对象；
  - 只基于冻结 VisualDocument + visualDelta 生成后端字段；
  - 不重新设计页面、不追问 ui.layout。
- 冻结虚拟树 + delta 以 24k 截断注入 human prompt。

### 校验 / 装配

- `validate_draft(output_phase=...)`：
  - VISUAL_V0：要求 visual_document；模型多写的 API/DDL/relations/
    interactions **确定性剥离**，不进入 repair；跳过前端骨架要求；
  - BACKEND_BINDING：跳过前端骨架要求、组件交互要求、
    relations “父键必须存在”的阻塞错误；
  - 存量项目继续跳过 DDL relations 自检。
- `assemble_contract(output_phase=..., frozen_visual_document=...)`：
  - VISUAL_V0：只保留视觉树，不生成 databaseEngine；后端字段防御性清空；
  - BACKEND_BINDING：使用冻结虚拟树，不重新生成/覆盖；
  - 最终契约同时携带冻结 visualDocument，供 CodeGen 使用。
- 图新增 `visual_v0_ready` 终态：状态 `AWAITING_VISUAL_EDIT`，
  不进入契约确认 interrupt。

### 槽位

- VISUAL_V0：只要求 `ui.layout`、`ui.primary_flow`；
- BACKEND_BINDING：要求后端/数据库/业务流/数据实体，不再要求 `ui.layout`。

## 前端

- 新建项目首次 Architect start：`outputPhase=VISUAL_V0`。
- V0 contract.ready：
  - reducer 直接置为 `AWAITING_VISUAL_EDIT`；
  - ArchitectPanel 显示“v0 虚拟原型已生成”与画布入口；
  - 自动打开画布；
  - localStorage 保存 `vibe.visual.v0.<projectId>` 基线。
- 画布冻结：
  - `buildSemanticReviewPacket` 读取 v0 基线，计算 `visualDelta`；
  - 新 Architect run：`outputPhase=BACKEND_BINDING`、
    `visualDocument=frozen`、`visualDelta=delta`；
  - 最终契约携带冻结视觉树，ArchitectPanel 显示 CodeGen 入口。
- 若用户手动再次启动 Architect 且已有冻结树：同样走 BACKEND_BINDING。
- 最终 BACKEND_BINDING 契约不再自动跳回画布。

## 验证

- Python 672 项全量（7 项既有环境失败）；新增：
  - V0 graph 只产出 visual、无 API/DDL；
  - BACKEND_BINDING graph 复用 frozen visual 并产出 API/DDL；
  - V0/BACKEND_BINDING 槽位与契约装配测试。
- 前端 55 项 Vitest；新增 visualDelta 新增/修改/移动/删除测试。
- 浏览器 E2E：
  - 新建 → 从零开始 → mock 澄清 → V0 自动打开画布；
  - 添加文本节点 → 冻结；
  - 第二次 Architect 请求 `outputPhase=BACKEND_BINDING`，
    `visualDelta.added=1`，`visualDocument.status=FROZEN`；
  - 0 浏览器错误。

## 修复记录（2026-11-17）

- 真机出现“VISUAL_V0 阶段禁止生成 API / DDL / relations / 交互契约”
  修复循环耗尽，最终落成通用内部错误。
- 根因：V0 把“模型多写后端字段”当阻塞校验，而模型在修复时仍可能
  输出完整契约。
- 修复：V0 在 `validate_draft` 与 `assemble_contract` 两处确定性剥离
  后端字段，不再报错；只有 visual_document 本身不合法才进入 repair。
- 修复耗尽错误码改为 `VISUAL_V0_INVALID`，返回可重试友好文案，
  不再落 `ARCHITECT_INTERNAL_ERROR`。

## 已知限制

- V0 的 contract_markdown 由 mock 简化；真实 DeepSeek 由 Prompt 约束。
- 最终 BACKEND_BINDING 仍需完整确认契约后才进入 CodeGen。
- 跨文件/循环/复杂组件抽取仍属后续阶段。
