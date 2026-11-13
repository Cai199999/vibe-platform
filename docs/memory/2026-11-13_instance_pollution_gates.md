# 实例污染取证与 P0 硬门禁（2026-11-13）

## 取证结论

1. **SDK 字段完整**：多实例目标 `instance/instanceCount/instanceKey/instanceProps/callSite` 覆盖率 4/4；`_build_annotation_context` 后端保留率 100%，不是信息丢失问题。
2. **旧字符串守卫仅 40% 拦截率**：10 条矩阵中 4 条绕过——无关条件误命中、改动离 callSite ±4 行外、无结构化截断、callSite 缺失时回退错误行号。
3. **审批前已写盘**：AWAITING_APPROVAL 时 `git status` 已出现 `M src/App.jsx` 且 SHA 变化；拒绝可回滚，但 `VIBE_CONTEXT.md` 等未跟踪文件残留。方案 C 草稿沙盒待后续推进。

## P0 门禁一：AST 实例作用域（fail-closed）

- `sandbox_template/ast_scope_check.mjs`：Babel 结构差分，忽略 loc/注释/引号风格；JSXText 归一化。
- 三条规则：`INSTANCE_AST_BASE_COMPONENT_CHANGED`、`INSTANCE_AST_UNGUARDED_JSX`、`INSTANCE_AST_SHARED_DATA_CHANGED`。
- 守卫判定只认真实祖先链：`&&` / 三元 / `if` / `filter().map()`，且必须引用实例身份（key / props / instance）。
- 检查范围收敛到“实例容器”（map 调用或渲染目标组件的 JSX 父节点），避免页头等无关编辑误报。
- 接入：`_applier` 在实例守卫之后调用 `SandboxProvider.ast_scope_check`；检查器缺失或退出码 2 一律拒绝写入。
- 验证：8/8 矩阵 OK；Adapter 级 files 与 patch 双通道命中；`python-ai` 镜像已重建固化。

## P0 门禁二：禁止无结构化范围删除

- 阈值：单文件删除 ≥5 行且 ≥30%，或 ≥80 行，必须携带 `payload.deleteRange`。
- `deleteRange` 必须覆盖实际删除区间，否则 `DELETE_RANGE_MISMATCH`。
- 删除导出符号（`export function/const/class`）即使行数很少也拦截。
- diff 模式只统计 `-` 行并按源文件行号合并区间，避免新增 hunk 误判。
- 错误码：`UNSTRUCTURED_DELETE_BLOCKED` / `DELETE_RANGE_MISMATCH`。

## 已知边界

- AST 守卫以“新增 JSX”为主要目标；既有节点的文案修改不在 P0 范围。
- 旧实例守卫先于 AST 门禁执行，因此基础组件冲突会先返回旧错误码；AST 门禁负责补漏。
- 5 个既有测试失败与本改动无关（MCP 子进程不可用、技能路径按 tmp cwd 解析）。
