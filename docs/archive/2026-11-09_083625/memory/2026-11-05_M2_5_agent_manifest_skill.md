# 2026-11-05 M2.5 声明式 Agent Manifest 与 Skill 预留

## 1. 本阶段目标

在进入前端 Supervisor 联调前完成架构收尾：

1. 用声明式 `manifest.json` 替代 `AgentRegistry` 中的硬编码注册；
2. 预留 `SkillRegistry`，明确 Agent / Tool / Skill 三层职责；
3. 划清 Memory 与 MCP 的演进边界，防止提前引入跨会话记忆与外部协议依赖。

## 2. Agent Manifest

### 2.1 目录约定

```text
services/orchestrator/ai_core/
├── agent/manifest.json          # ui_graph
├── architect/manifest.json      # architect_graph
├── backend_coder/manifest.json  # backend_coder（reserved）
└── script_agent/manifest.json   # script_agent（reserved）
```

`AgentRegistry.__init__()` 启动即扫描 `ai_core/*/manifest.json`：

- 单文件 ≤ 256 KB；
- `agent_id` 必须匹配 `^[a-z][a-z0-9_-]{2,63}$`；
- `capabilities[].id` 与 `skills[].skill_id` 均不允许重复；
- `input_schema` / `output_schema` 根节点必须是 `object`；
- 任一文件失败只记录 warning，不阻断其余 Agent；全部失败才快速失败。

### 2.2 核心字段

| 字段 | 用途 |
|---|---|
| `agent_id` | 注册中心唯一标识 |
| `name` | 前端展示名 |
| `capabilities` | 能力 ID + 描述，`task_types` 由能力 ID 派生 |
| `supported_tools` | Tool 白名单 |
| `input_schema` / `output_schema` | 运行时输入输出 JSON Schema |
| `skills` | Skill 挂载声明（Prompt / RAG / Tool 子集） |
| `status` | `available` / `reserved` |
| `graph` | 运行时图名称声明 |
| `keywords` | 规则分类词表，改 Manifest 即生效 |

### 2.3 注册中心改动

- 删除 `_builtin_descriptors()` 与硬编码 `UI_KEYWORDS` / `ARCHITECT_KEYWORDS`；
- 新增 `reload_builtin_agents()`，与 `reload_custom_agents()` 分离；
- 自定义 Manifest 仍不可覆盖内置 Agent；
- `/agents/reload` 同时重扫内置与企业 Manifest；
- `router.py` 从描述符读取关键词，仅保留兜底词表应对 Manifest 缺失。

## 3. Agent / Tool / Skill

| 概念 | 定义 | M2.5 状态 |
|---|---|---|
| Agent | 执行者，拥有状态机与 Handoff 协议 | UI / Architect 已实现；Backend / Script 仅 Manifest |
| Tool | 底层原子函数，如读写文件、构建、Git | 子图内部实现，Manifest 声明白名单 |
| Skill | 领域封装 = Prompt + RAG + Tool 子集 | 只声明，不加载、不执行 |

`SkillRegistry` 提供：

```text
register / get / list_skills / list_for_agent / find_by_tool / version
```

当前 Prompt 仍硬编码在子图节点中，保证 M2.5 闭环稳定；M3 再按
`选 Agent → 匹配 Skill → 注入 Prompt + RAG → 裁剪 Tool 子集` 动态拼装。

## 4. Memory 与 MCP 边界

### 4.1 Memory

- 当前仅 LangGraph 线程级短期记忆，随 Run / thread 生命周期结束；
- 明确不引入跨会话长期记忆、用户画像、跨租户知识沉淀；
- 原因：规则路由阶段过早记忆会把错误路由与一次性标注固化为偏好；
- 长期记忆规划在 M3。

### 4.2 MCP

- 架构图新增虚线框 `MCP Client Adapter`，位于 Supervisor 侧；
- 未来以标准 MCP 协议连接数据库、工单、代码托管、企业知识库；
- M2.5 不引入依赖、不建立连接、不暴露端口；
- 外部工具未来仍须映射为 Manifest `supported_tools` 才能被 Agent 使用。

## 5. 兼容性

- `AgentView` 新增 `capabilities` / `supportedTools` / `inputSchema` /
  `outputSchema` / `skills` / `manifestPath`，均为增量字段；
- `AgentListResponse` 新增 `skillRegistryVersion` 与扁平 `skills`；
- `AgentDescriptor` 字段全部带默认值，旧调用方式保持可用；
- 原有 66 个 Supervisor / Registry / Gate 单元测试保持通过，新增 4 个
  Manifest / SkillRegistry 测试后共 70 个全部通过。

## 6. 验证

```bash
docker run --rm -v "$PWD":/repo -w /repo/services/orchestrator \
  -e PYTHONPATH=/repo/services/orchestrator vibe-platform/python-ai:dev \
  python -m unittest discover -s tests -v
```

结果：`Ran 70 tests ... OK`。

## 7. 下一步

前端 Supervisor 路由已在上一阶段接入，本阶段完成后需重新验证：

1. `POST /api/v1/supervisor/route` 五种 handoff 分支；
2. `handoff_both` 先 Architect 后 UI 的强门控；
3. 浏览器级混合 E2E（标注 → route → gate → 契约确认 → release → UI Run）。
