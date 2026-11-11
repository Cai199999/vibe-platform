# 2026-11-11 · 契约缺陷 vs 代码缺陷分流

## 背景

自愈重试原本把 ConsistencyVerifier 的所有失败都当成代码缺陷。若契约
本身矛盾（DDL 没有业务表、交互调用未声明接口、关系引用不存在的列），
重试只会重复同一错误，浪费模型调用并掩盖真正问题。

## 确定性审计

`ai_core/fullstack/contract_audit.py`：

- 不调用模型、不读取沙盒产物，纯结构检查。
- 检查项：
  - `missing_ddl` / `missing_core_table`；
  - `collection_table_missing`：API 集合资源在 DDL 中无对应表；
  - `invalid_relation`：父子表或外键列不存在；
  - `missing_api_routes` / `invalid_api_path`；
  - `undeclared_interaction_route`：组件交互引用未声明接口；
  - `invalid_interaction_route`：交互路径不在 `/api/` 下。
- 输出 `ContractAudit`：`ok`、`failure_kind`（`CONTRACT` / `CODE`）、
  `summary()`、`clarification_prompt()`、`payload()`。
- `mode` 区分 `fullstack` / `backend` / `dba`，DBA 模式不要求 API 路由。

## 分流落点

1. **生成前**（`FullstackRunService._run_worker`）：
   审计失败立即 `_complete_failure`，不启动 LangGraph。
2. **Graph 内**（`ConsistencyVerifier`）：
   - 进入节点先审计，矛盾直接抛
     `FullstackContractContradictionError`；
   - 自愈重试耗尽时重新审计：若为契约问题，抛同类异常而不是
     `FullstackConsistencyError`。
3. **失败载荷**：`failureKind`、`needsClarification`、`clarification`
   贯穿 SSE 事件、运行快照、前端 reducer 与 sessionStorage。

## 前端闭环

- `ArchitectPanel` 在 `failureKind=CONTRACT` 时：
  - 错误卡显示黄色提示「重试不会修复契约矛盾」；
  - 自动弹出「契约需要重新澄清」对话框，列出每条问题与建议；
  - 「回到契约并重新澄清」调用 `onDecide('REVISE', prompt)`，
    把修正要求交回 Architect；
  - 生成按钮在 `needsClarification` 期间禁用，避免无意义重试。
- 样式：`.architect-clarify-dialog` / `.architect-fullstack-error-hint`。

## 验证

- `tests/test_contract_audit.py` 9 项；
- Graph/Service 测试覆盖「预测到契约矛盾时不进入重试」与
  「失败载荷携带 clarification」。
