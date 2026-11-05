# M3-B：CodeEditor 抽象与全栈前瞻决策落地（2026-11-05）

## 背景

为全栈生成（长耗时、多文件、跨语言）提前固化四条执行层决策：

1. 同步 `/runs` 保留，新增 SSE `/runs/stream`，Java BFF 异步非阻塞透传；
2. 澄清由 Planner `confidence_score` 触发，Prompt 植入全栈范围引导；
3. checkpoint 统一经 `CheckpointerProvider`，M2 MemorySaver，预留 RedisSaver；
4. 代码编辑统一经 `CodeEditor`，M2 锚点，预留 `FileCreator` / `TreeSitterEditor`。

核对结果：1–3 已在 M2 / M3-A 落地（`RunStreamService`、
`PlanResult.confidence_score` + Planner Prompt、`agent/checkpoint.py`）；
本次补齐第 4 条并更新架构文档。

## 新增：`ai_core/editor/`

| 文件 | 内容 |
|---|---|
| `base.py` | `EditKind`、`EditLayout`、`CodeEdit`、`EditResult`、`CodeEditor` 协议、`EditorError` |
| `anchor.py` | `AnchorCodeEditor`：锚点替换 / 全量替换 / 前后插入；`occurrence`（正数 1-based、负数倒数）、`expected_matches`、`BLOCK` 自动缩进 |
| `file_creator.py` | `FileCreator`：`CREATE_FILE` / `REWRITE_FILE`；拒绝绝对路径、`..`、反斜杠、盘符；覆盖 / 缺失保护 |
| `tree_sitter.py` | `TreeSitterEditor` 占位：`STRUCTURED` 编辑抛 `EditorNotAvailableError`，不引入 tree-sitter 依赖 |
| `registry.py` | `EditorRegistry`：按 `EditKind` 路由；`apply_all()` 返回相对初始工作区的净变化 |

## 接入点

- `agent/llm.py`：Mock 的 CSS 追加、按钮插入改为构造 `CodeEdit` 并调用
  `default_editor_registry()`；不再直接 `str.replace`。`_replace_element_text`
  仍是选择器正则，已在注释中标注为 Mock 启发式，真实实现交给 TreeSitter。
- `agent/graph.py`：Coder `files` 模式经 `_materialize_generated_files()` 落地，
  已存在文件 `REWRITE_FILE`、新文件 `CREATE_FILE`，为全栈多文件生成预留。
- `services/orchestrator/tests/test_editor.py`：15 个 unittest 覆盖锚点定位、
  缩进插入、重复锚点、路径穿越、覆盖保护、注册表路由与净变化。

## 前端修复

审批 / 澄清面板位于预览侧，而对比模式默认停留在画布，导致输入被冻结却看不到
处理入口。未强制切换视图，改为在画布右下角显示琥珀色「查看审批差异」/
「回答澄清问题」CTA（`.view-result-cta.is-pending`），点击后切到预览侧面板，
保留用户的画布上下文。

## 文档

- `docs/design/04.M2_Agent_ToolCalling_SSE_HITL.md`
  - 状态升级 v2.1；模块树改为 `ai_core/editor/`；
  - §3.3.1 更新为实际 `CodeEdit` / `CodeEditor` 协议与实现表；
  - 新增 §12.1 四条决策落地对照。
- `docs/design/02.整体架构设计.md`
  - 顶部增加执行层决策落地说明；
  - checkpoint 行标注 M2 MemorySaver；
  - 新增 ADR-16 ~ ADR-19。

## 验证

- `PYTHONPATH=services/orchestrator python3 -m unittest discover -s services/orchestrator/tests`：15 passed。
- `python3 -m compileall services/orchestrator/ai_core`：通过。
- 重新构建 `python-ai` 镜像并重启。
- E2E 全绿：
  - `scripts/agent_e2e_test.py`
  - `scripts/platform_e2e_test.py`
  - `scripts/approval_cancel_e2e_test.py`
  - `scripts/approval_ui_e2e_test.py`
  - `scripts/add_component_e2e_test.py`（新增按钮走 AnchorCodeEditor）
  - `scripts/multi_annotation_e2e_test.py`
  - `scripts/multi_turn_chat_e2e_test.py`
  - `scripts/ui_e2e_test.py`
  - `scripts/ui_copy_clean_e2e_test.py`
  - `scripts/popover_equal_scale_e2e_test.py`
- `docker exec vibe-platform-web npm run build`：通过。

## 已知限制

1. `TreeSitterEditor` 仅占位；跨文件符号编辑尚未实现。
2. `_replace_element_text` 仍是 Mock 正则，真实 Coder 应输出锚点或结构化编辑。
3. `rollback_e2e_test.py` / `annotation_delete_rollback_e2e_test.py` 仍按旧同步
   `/runs` 响应编写，尚未适配 SSE 与审批暂停。
4. 编辑器抽象目前只覆盖 Mock 与 files 模式；阶段 3 Tool Executor 接入后，
   所有工具调用都必须经 `EditorRegistry`。
