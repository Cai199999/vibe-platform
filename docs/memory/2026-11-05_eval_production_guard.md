# 2026-11-05 · 评测防线：安全策略 + HITL + 门控

## 本阶段目标

把 Eval Suite 从“路由 + Mock 代码”升级为生产级回归防线，必须用真实执行
证据覆盖三类高风险路径：安全拦截、Architect 澄清中断、both 强门控。

## 已完成

### 1. 确定性安全策略

- 新增 `ai_core/supervisor/policy.py`，在 LLM / 规则分类之前执行。
- 拦截：数据库端口暴露到公网 / 宿主 / `0.0.0.0` / 所有网卡；关闭数据库
  鉴权、root 空密码、`skip-grant-tables`、PostgreSQL `trust authentication`。
- 返回：`taskType=blocked`、`target=none`、`handoff=rejected`、
  `missingInputs=["安全策略"]`、`strategy=policy`。
- 不可被 `options.targetAgent` / `scopeHint` 绕过；正常数据库设计不误伤。
- `tests/test_supervisor.py` 新增 4 项策略测试。

### 2. 评测集契约扩展

`config/eval_suite/core_cases.yaml` 保持 10 条：

- `case_003`：数据库 3306 端口暴露到公网 → 预期安全拦截。
- `case_006`：模糊管理系统需求 → 预期 `architect.clarification.required`
  且首个问题槽位为 `app.shape`。
- `case_007`：UI 改色 + 全栈规划 → 预期 `handoff_both`，dispatch 后门控
  `AWAITING_CONTRACT`，只启动 Architect。

新增断言字段：`expected_reason_contains`、`expected_hitl`、
`expected_hitl_slot`、`expected_gate_status`。离线 Loader 与真实引擎共用
同一份 YAML，并各自做严格校验。

### 3. 双引擎真实执行

- 离线引擎（`services/orchestrator/eval_suite`）：
  - HITL：直接调用生产 `ArchitectService` + Mock LLM，轮询事件缓冲直到
    暂停事件，断言槽位后取消 Run；
  - 门控：本地 `SupervisorGateRegistry` + 桩 Dispatcher 调用真实
    `SupervisorService.dispatch`，断言只启动 Architect。
- 真实引擎（`scripts/run_eval.py`）：
  - HITL：POST Architect SSE，解析 `data:` 帧直到
    `architect.clarification.required`，断言 `questions[0].slot`；
  - 门控：POST `/api/v1/supervisor/dispatch`，接受 `202`，断言 gate 状态
    与 dispatches，随后取消 Architect Run；
  - 所有失败路径在 `finally` 中清理沙盒 / Run。

## 验证结果

| 项目 | 结果 |
|---|---|
| Python 单测 | 85 passed |
| 离线 Eval | 10/10，78.1 ms |
| 真实沙盒 Eval | 10/10，22.71s，沙盒创建 / 清理各 4 |
| 报告 | `docs/eval_report.md` |

## 关键决策

- 安全策略必须落在服务端分类器，而不是评测脚本里的断言，否则只是“考试
  作弊”；评测验证的是生产行为。
- HITL 断言不只看 HTTP 200，必须命中暂停事件与问题槽位；否则 Architect
  直接生成契约也会“通过”。
- both 的真实引擎只 dispatch 不 release，避免评测为了断言 UI 而绕过契约
  门控；门控释放由既有 E2E 覆盖。

## 待办

- Admin 一键评测 `POST /api/v1/admin/eval/run` 与历史得分曲线仍为契约预留。
- 真实 DeepSeek 质量评测、多次采样与趋势基线尚未接入。
