# 2026-11-05 Agent OS 数据中台 / 管理中台架构升级

## 1. 背景与目标

平台从单项目 Vibe Coding 走向商业化的 Agent OS，需要支撑：

- 企业多租户与知识库隔离；
- Agent 运行指标、Token 消耗与成功率可运营；
- System Prompt 可版本化、灰度、热更新与回滚；
- 基于用户拒绝理由的 Agent 自优化，但**不做大模型微调**。

本轮只完成**架构、数据模型与接口契约**，不实现底层逻辑；实现按 M3.1–M3.4 推进。

## 2. 交付内容

### 2.1 新增文档

`docs/design/06.Agent_OS_Data_Management.md`（v1.0）：

- 决策摘要与目标 / 非目标；
- 总体 Mermaid 拓扑（Web / BFF / AI Core / Knowledge Service / Admin / 数据层）；
- 多租户与 RBAC：JWT 推导租户、内部 `X-Tenant-Id` 必填、跨租户 404；
- Knowledge Service：摄取状态机、切分 / Embedding / 检索时序、公共与内部 API、
  错误码、降级策略、MinIO / Redis 布局；
- `RAGProvider` 升级：`tenant_id` 必填，`KnowledgeRAGProvider` 生产、
  `MarkdownRAGProvider` 仅本地兜底；
- 管理中台：`/admin`、Agent 指标口径、Prompt Registry 状态机、灰度 / 热更新 / 回滚 / Diff；
- Feedback Collector：Architect `REVISE` 事件模型、幂等、脱敏、内部投递；
- M3 优化任务：聚类 → LLM 候选 Prompt → 离线评估 → 人工审批 → 灰度 → 回滚；
- 数据表、SLO、限流、可观测、部署、里程碑与验收清单。

### 2.2 同步更新

| 文档 | 变更 |
|---|---|
| `docs/design/README.md` | 索引加入 06，阅读顺序补充中台契约优先级 |
| `docs/design/01.功能模块梳理.md` | 角色扩展；M38–M41；中台模块图、流程、规则与验收 |
| `docs/design/02.整体架构设计.md` | v1.2；系统上下文 / 逻辑架构 / 分层 / 信任边界 / 数据所有权 / 表目录 / 部署 / ADR |
| `docs/design/03.API接口设计.md` | v1.2；§14 中台端点索引、请求响应摘要、错误码、验收 |
| `docs/memory/00_project_brief.md` | M3 中台目标、技术栈、模块、约束与文档状态 |
| `README.md` / `TECH_STACK.md` / `PROJECT_STATUS.md` | 标注 M3 为“设计完成、未实现” |
| `.env.example` | 新增 Knowledge / Prompt / Feedback 配置占位（当前未读取） |

## 3. 关键契约

### 3.1 租户隔离

```text
JWT -> BFF 解析 tenantId
BFF/AI Core -> Knowledge Service: X-Tenant-Id 必填
缺失 -> 400 TENANT_CONTEXT_MISSING
跨租户资源 -> 404（不返回 403，避免枚举）
```

- 公共 API 不接受 body 中的 `tenantId`；
- 仅 `platform_admin` 可在 `/api/v1/admin/**` 用 `?tenantId=`，且写审计。

### 3.2 检索

```http
POST /internal/v1/knowledge/retrieve
X-Internal-Token: ...
X-Tenant-Id: ...
X-Actor-Id: architect
```

响应包含 `chunks[]`（`chunkId / documentId / versionId / title / section / path /
content / score / citation`）与 `degraded / latencyMs / tenantId`。

- 向量不可用 → BM25 兜底 `degraded=true`；
- 无兜底 → `503 KNOWLEDGE_UNAVAILABLE`，禁止空数组冒充成功；
- 检索内容是不可信引用，不能覆盖系统指令。

### 3.3 RAGProvider

```python
def retrieve(
    self,
    query: str,
    *,
    tenant_id: str,
    top_k: int = 5,
    filters: dict[str, Any] | None = None,
    knowledge_base_ids: list[str] | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
    prompt_version_id: str | None = None,
    timeout_ms: int | None = None,
) -> list[RetrievedChunk]:
    ...
```

`tenant_id` 必填；实现不得在缺失时回退到本地或全局索引。

### 3.4 Admin / Prompt

- `GET /api/v1/admin/agents`：runs、successRate、token、cost、P50/P95、
  clarificationRate、reviseRate、topErrors；指标每 60s 聚合。
- `POST /api/v1/admin/prompts/{agentId}/versions/{versionId}/publish`：
  `If-Match` + reason + canaryPercent；发布 ≤ 5s 生效；Run 内 pin 版本。
- 已发布版本不可修改；回滚只切指针；全程审计。

### 3.5 Feedback / 自优化

- 触发：Architect `REVISE`（comment 必填）；
- 幂等键：`(tenantId, runId, type, commentHash)`；
- 优化任务：每周 / 手动，样本 ≥ 20；LLM 只输出候选 diff；
- 评估：schema 合法率、澄清率、REVISE 率、成本、安全用例；
- 发布：管理员审批 → 灰度 5%–10% → 观察 24–72h → 全量或回滚；
- **不做 SFT / LoRA / RLHF；不自动发布。**

## 4. 明确未做

- `services/knowledge` 服务代码、Qdrant 接入、Embedding / Rerank 推理；
- `RAGProvider` 代码签名变更与调用方迁移；
- Admin API、`/admin` 前端页面、Prompt Registry 实现；
- Feedback Collector / Optimization Worker；
- 数据库迁移与真实指标聚合。

`TECH_STACK.md` 与 `PROJECT_STATUS.md` 已将这些标记为“仅契约 / 未实现”，
避免把设计目标误当成已落地能力。

## 5. 下一步

1. **M3.1**：`/admin` 入口 + 管理员 RBAC + Agent 指标日聚合 + 审计。
2. **M3.2**：Knowledge Service 摄取流水线 + 租户隔离检索 + `KnowledgeRAGProvider`。
3. **M3.3**：Prompt Registry（版本 / 校验 / 灰度 / 热更新 / 回滚 / Diff）。
4. **M3.4**：Feedback Collector + 优化任务 + 离线评估 + 人工审批 + 灰度回滚。

每阶段先补契约测试与租户隔离用例，再接入真实依赖。
