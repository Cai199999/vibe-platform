# 2026-11-05 M2.5：Supervisor 总调度 + Sub-Agents 专家协同

## 1. 阶段目标

在既有局部 UI Run 与全局 Architect Run 之上，新增一个**只负责意图路由**
的 `Supervisor Graph`，让平台拥有统一的“大脑”：

1. 判断用户请求是局部 UI 修改、全局架构设计，还是两者都要；
2. 通过 `AgentRegistry` 解析可用专家 Agent；
3. 生成有序 Handoff 计划，必要时真正启动子图；
4. 为未来 BackendCoder、ScriptAgent 与企业 CustomAgent 预留注册 / 加载槽；
5. **不改变** UI Graph、Architect Graph 的状态机、事件协议与 HITL 语义。

## 2. 架构与状态机

设计文档：`docs/design/05.Supervisor_Multi_Agent.md`，包含：

- Supervisor 总-分拓扑图；
- Supervisor Graph 状态机；
- 仅路由 / 真正派发 / CustomAgent 扩展三张时序图；
- AgentRegistry 与 CustomAgent Manifest 契约；
- 内部与 BFF API、错误码。

状态机：

```text
START
  → classify_intent（显式 targetAgent / scopeHint 优先）
  → resolve_agents（AgentRegistry 能力解析）
  → handoff_ui / handoff_architect / handoff_both / handoff_custom / rejected
  → finalize → END
```

## 3. 路由规则

| 优先级 | 条件 | 结果 |
|---|---|---|
| 1 | `options.targetAgent` 指向已注册可用 Agent | 该 Agent，置信度 0.99 |
| 2 | `options.scopeHint = ui / architect / both` | 显式目标，0.97 |
| 3 | UI + 架构关键词同时命中 | `both`，0.88 |
| 4 | 仅架构关键词 | `architect_graph`，0.90 |
| 5 | 标注 + UI 关键词 | `ui_graph`，0.92 |
| 6 | 仅标注 | `ui_graph`，0.84 |
| 7 | UI 关键词但缺标注 | `ui_graph`，不可派发，0.72 |
| 8 | 无有效信号 | `none / rejected`，≤ 0.30 |

UI Graph 需要至少 1 个标注；`dispatch` 在缺少标注时返回
`SUPERVISOR_UI_CONTEXT_REQUIRED`，不会创建空沙盒。

## 4. AgentRegistry

内置注册项：

| Agent | 能力 | 状态 |
|---|---|---|
| `ui_graph` | `ui_local_edit` | available |
| `architect_graph` | `global_architecture` | available |
| `backend_coder` | `backend_generation` | reserved |
| `script_agent` | `script_automation` | reserved |

预留 Agent 不可被路由；显式指定返回 `AGENT_NOT_AVAILABLE`。

## 5. CustomAgent 扩展槽

- 目录：`SUPERVISOR_CUSTOM_AGENTS_DIR`，默认 `config/custom_agents`；
- 只扫描 `*.json`，单文件 ≤ 256 KB；
- Manifest 字段：`agentId / displayName / taskTypes / keywords /
  systemPromptFile / ragKnowledgeDir / enabled / metadata`；
- 路径必须是 Manifest 目录内相对路径，拒绝绝对路径、反斜杠与 `..`；
- 不允许覆盖内置 Agent；
- 只登记能力，不执行代码；当前返回 `handoff_custom`，运行时尚待接入；
- `CustomAgent` 运行时协议 `handle(task, context) -> AgentResult` 已预留。

## 6. API

内部：`/api/v1/internal/supervisor`

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/route` | 只路由，不启动子图 |
| `POST` | `/dispatch` | 启动目标子图，返回 202 + 子 Run ID / SSE 路径 |
| `GET` | `/agents` | 注册中心列表 |
| `POST` | `/agents/reload` | 重新扫描 Manifest |

BFF 公开：`/api/v1/supervisor/**`，非流式透传；子图 SSE 仍由前端按
`dispatches[].streamPath` 直接订阅：

- UI：`/api/v1/runs/{runId}/events/stream`
- Architect：`/api/v1/architect/runs/{runId}/events/stream`

`both` 当前按 `architect_graph` → `ui_graph` 的顺序派发；两者独立执行，
契约确认后再放行 UI 修改的强门控属于后续阶段。

## 7. 代码位置

| 层 | 文件 |
|---|---|
| Python Graph | `services/orchestrator/ai_core/supervisor/graph.py` |
| 规则分类器 | `services/orchestrator/ai_core/supervisor/router.py` |
| 注册中心 | `services/orchestrator/ai_core/supervisor/registry.py` |
| Manifest 加载 | `services/orchestrator/ai_core/supervisor/custom.py` |
| 路由 / 派发服务 | `services/orchestrator/ai_core/supervisor/service.py` |
| 内部 API | `services/orchestrator/ai_core/api/supervisor.py` |
| Java BFF | `apps/platform-bff/src/main/java/com/vibeplatform/bff/supervisor/*` |
| 单元测试 | `services/orchestrator/tests/test_supervisor.py` |
| API E2E | `scripts/supervisor_e2e_test.py` |

## 8. 验证

```bash
# 全量 Python 单元测试：62 个
docker run --rm -v "$PWD/services/orchestrator:/app" -w /app \
  -e PYTHONPATH=. vibe-platform/python-ai:dev \
  python -m unittest tests.test_editor tests.test_run_options \
    tests.test_architect tests.test_rag tests.test_supervisor -v

# 直连 AI Core：路由、Registry、reserved 拒绝、UI 缺标注拒绝、真实派发 Architect
python3 scripts/supervisor_e2e_test.py

# 经 Java BFF 透传
AI_BASE_URL=http://127.0.0.1:8080 python3 scripts/supervisor_e2e_test.py
```

结果：两轮 API E2E 均通过；`dispatch` 真实启动 Architect 子图，SSE 首事件为
`architect.clarification.required`，随后取消清理。

## 9. 已知边界

- Supervisor 目前只路由，不生成代码，也不合并子图事件流；
- BackendCoder / ScriptAgent 仅注册为 reserved；
- CustomAgent 只加载 Manifest，运行时尚待实现；
- `both` 仍是 MVP 同时启动，不是“契约确认后再改 UI”的强顺序；
- 注册中心为进程内单例，Manifest 更新需调用 `/agents/reload` 或重启服务。
