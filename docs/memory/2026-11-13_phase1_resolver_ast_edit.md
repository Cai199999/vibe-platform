# Phase 1：确定性 Resolver + apply_ast_edit 工具协议

> 2026-11-13 · 阶段总结

## 目标

彻底废除 Coder“自由输出 diff / 全量文件”的写权限，改为：

```
AnnotationEnvelope + 沙盒 AST
   → Resolver（无 LLM，Babel）
   → TargetResolution（锚点 / 白名单 / 保护标识）
   → Coder 只输出 tool_calls: apply_ast_edit
   → 工具硬门禁 + Babel 精确切片
   → Applier（沿用草稿隔离 + 实例作用域门禁）
```

## 落地内容

### 1. Resolver 节点（确定性）

- 新节点位于 `style_check` 与 `coder` 之间；`resolver → coder` 条件路由。
- 输入：`_resolution_requests()` 从标注提取 file / line / column / selector /
  component / instance / instanceCount / callSiteFile。
- 沙盒脚本 `sandbox_template/resolve_target.mjs`：
  - 选择器（`[attr="v"]` / `[attr]` / `#id` / `.class` / tag）优先，
    多命中时用实例序号或行列消歧；
  - 行列回退取最内层 JSXElement / JSXFragment；
  - 仍不唯一 → `TARGET_AMBIGUOUS` + 候选（candidateId / range / preview）；
  - 共享判定：同位置渲染多次、callSite 与定义文件分离、被其它文件 import；
  - 共享即 `protected`，并给出 `forbiddenRanges`（渲染函数体）与
    `allowedFiles`（共享组件仅允许 callSite 文件）。
- 领域契约：`ai_core/domain/resolution.py`（纯 JSON、可 Checkpoint）。
- 歧义：`interrupt()` + `clarification.required`，用户选择候选后带
  `selectedRange` 重新解析；连续 `max_clarification_rounds` 仍歧义 →
  `RESOLUTION_LIMIT`。澄清关闭或用户未选 → `unresolved`，降级旧 Coder 路径，
  绝不猜测。
- Provider 不支持（返回 None）或脚本异常 → `SKIPPED`，全部旧测试保持可用。

### 2. 唯一写工具

- `ai_core/tools/ast_edit_tool.py` 注册 `apply_ast_edit`（danger_level=write）。
- 入参：`anchor_id`、`operation`、`target_range`、`jsx_code`、`insert_position`。
- `execute_ast_edit()` 与 LangChain Tool 解耦，便于单测注入 `AstEditContext`。
- 沙盒执行器 `apply_ast_edit.mjs`：
  - 只按锚点 offset 做源码切片，保持原有缩进；
  - insert 的父级必须是 JSX 容器，兄弟节点使用锚点同级缩进；
  - delete 前深拷贝 AST 剪掉目标子树，与新代码解析结果做结构哈希比对
    （忽略 loc / 注释 / 纯空白 JSXText），不等价 → `DELETE_SCOPE_MISMATCH`。

### 3. Coder 改造

- `DeepSeekAgentLLM._generate_edits()`：当解析结果含 RESOLVED 锚点时启用。
  Prompt 明确：无直接写文件能力、唯一写操作是 `apply_ast_edit`、禁止代码块 /
  diff / 全量文件；只输出 `{"summary", "tool_calls"}`。
- `_parse_edit_result()` 拒绝 `files` / `patch` / `mode`，校验工具名、anchorId、
  operation、范围一致性、新增组件 `data-vibe-created` 标记。
- 同文件多编辑按锚点 offset 降序执行；后续编辑由服务端注入
  `content_override`（模型不可见、不可伪造）。
- Mock / 不支持解析的 Provider 继续走旧 files 路径。

### 4. 工具内部硬门禁

错误码：`ANCHOR_NOT_FOUND`、`ANCHOR_RANGE_MISMATCH`、`FILE_NOT_ALLOWED`、
`RANGE_NOT_ALLOWED`、`PROTECTED_COMPONENT`、`OPERATION_NOT_ALLOWED`、
`INVALID_OPERATION`、`INVALID_JSX_SNIPPET`、`INVALID_INSERTION_POINT`、
`DELETE_SCOPE_MISMATCH`、`EDIT_CONTEXT_REQUIRED`、`AST_EDIT_FAILED`。

- modify / delete 的目标范围必须与锚点完全一致；
- 公共组件定义文件与 forbiddenRanges 重叠一律拒绝；
- 工具错误转 `apply_failed`，复用 evaluator 修复预算与同错指纹终止，
  禁止无反馈盲目重试。

## 验证

- `tests/test_phase1_resolver_ast_edit.py`：25 项（解析脚本 / 编辑脚本 /
  工具门禁 / Graph E2E / 歧义 interrupt 重解析）。
- 全量 Python：494 passed（原 469 + Phase 1 新增，另修正 style_check
  路由断言）。
- 真实 Docker 沙盒冒烟：`resolve_targets` 返回 RESOLVED 锚点，
  `apply_ast_edit` 修改成功，脚本自动投放到 `/opt` 后可执行。

## 关键决策 / 遗留

- Phase 1 只打通“解析 → 工具 → 落盘”链路；`PROTECTED_COMPONENT` 的安全路径
  选择（改 callSite / 新建变体 / 取消）留给 Phase 2 决策分流。
- 共享组件当前在定义文件上直接拒绝，实例级修改需 Phase 2 的 callSite 锚点。
- 删除根节点（如 `<main>` 作为 return 唯一子节点）会因 AST 剪枝失败而
  fail-closed，符合预期。
