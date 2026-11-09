# 2026-11-05 M2.5：应用形态判别与全栈实施契约

## 1. 阶段目标

在局部 UI 修改链路之外，新增独立的 **全局架构澄清 / 全栈 Path 生成** 模块：

1. 用户可从工作台显著入口 `下一步：构建全栈方案` 发起 Architect Run；
2. 基于沙盒中的真实前端源码，而不是前端 `project.files` 初始模板；
3. 先判别应用形态，再按形态动态追问，避免轻量工具被强迫选择后端与数据库；
4. 输出带置信度、知识库引用和分级校验的《全栈实施契约》；
5. 支持 `CONFIRM / REVISE / CANCEL` 决策，并与局部 UI Run 的事件、状态、存储完全隔离。

## 2. 应用形态与动态槽位

| `app_shape` | 中文 | 必填槽位 | 禁止内容 |
|---|---|---|---|
| `lightweight_web` | 轻量网页工具 | `ui.primary_flow`（可含前端数据模型） | 后端框架、数据库引擎、REST API、DDL、脚本规格 |
| `script_automation` | 脚本自动化 | `script.runtime/input/output/logic` | UI 流程、REST API、数据库 DDL |
| `full_stack_system` | 全栈系统 | `backend.framework`、`database.engine`、`business.key_flows`、`data.entities` | 物理外键 |

首轮永远不猜测形态，固定问题文案：

> 您是希望做一个只有网页特效的轻量工具，还是需要能保存数据的全栈系统，或者是一个后台自动运行的脚本？

前端表单不预选应用形态，必须显式点击卡片；后端未知形态置信度封顶 0.42。

### 2.1 置信度守卫

- 缺失槽位按形态动态计算，不再固定要求全栈四件套；
- 形态未定 `<= 0.42`；
- 轻量只缺 UI 流程 `0.72`；脚本 / 全栈按缺失项阶梯下降；
- 存在 `assumptions` 时最高 `0.84`，确保会继续提问；
- 达到或超过阈值（默认 `0.85`）才起草契约；
- 最多 5 轮，耗尽后 `CONFIDENCE_EXHAUSTED`，不无限追问。

## 3. Architect Graph 状态机

```text
START
  → ingest_context（沙盒 tar.gz 源码，失败回退请求 files）
  → retrieve_knowledge（Markdown + BM25，失败不影响主流程）
  → architect_analyze（形态 / 槽位 / 缺失项 / 置信度）
  → confidence_gate
      ├─ 形态 + 槽位齐全 + 置信度达标 → draft_contract
      ├─ 达到最大轮次 → confidence_exhausted → failed
      └─ 其他 → clarify_gate → interrupt() → architect_analyze
  → draft_contract
  → validate_contract（分级校验）
      ├─ 通过 → contract_ready
      ├─ 可修复且预算未用尽 → revise_contract（确定性修复反馈）→ validate_contract
      └─ 失败 → failed
  → contract_ready（emit architect.contract.ready）
  → contract_gate → interrupt()
      ├─ CONFIRM → confirmed
      ├─ REVISE（必须 comment）→ revise_contract → validate_contract → contract_ready
      └─ CANCEL → cancelled
```

事件命名空间为 `architect.*`，复用 `RunEventBuffer` 的 `seq / afterSeq / Last-Event-ID / 心跳` 语义：

```text
architect.started
context.ingested
rag.retrieved
app_shape.detected
analysis.completed
clarification.required
clarification.answered
contract.drafting
contract.repair.requested
contract.ready
contract.decision.received
contract.revision_requested
contract.confirmed
cancelled
failed
```

暂停 / 终态事件：`clarification.required`、`contract.ready`、`contract.confirmed`、`cancelled`、`failed`。

## 4. 分级契约

统一 `ArchitectContract` 结构，但按形态约束内容：

- **轻量**：允许 UI 逻辑、前端数据模型、组件交互；`apiRoutes=[]`、`databaseDdl=""`、`scriptSpec={}`；技术栈不得含 `backend/database`。
- **脚本**：必须含 `runtime/entry/dependencies/input/output/exitCodes`；禁止 UI、REST API、DDL。
- **全栈**：必须含至少一条 `/api/` 路由、`CREATE TABLE` DDL、组件交互、`backend/database` 技术栈；禁止物理外键（`FOREIGN KEY`）。

契约只输出形态允许的内容；校验器会裁剪错误形态字段并记录 issue，避免“轻量契约里混入 DDL”这类过度设计泄漏。

## 5. RAG 抽象

- `RAGProvider` 协议：`retrieve(query, top_k) -> RetrievedChunk[]`；
- 当前实现 `MarkdownRAGProvider`：读取 `knowledge/fullstack/*.md`，按标题分块，BM25 排序；
- 中文使用 unigram + bigram 分词，避免无分词依赖；
- 引用结构：`docId/title/section/path/score`；
- `chroma.py` 仅占位并抛 `RAGProviderError`，未来替换向量库不影响契约层；
- RAG 失败记录 warning，不中断 Architect Run。

## 6. API 协议

Python 内部路由前缀：`/api/v1/internal/architect`；Java BFF 公开路由：`/api/v1/architect`。

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/runs/stream` | 启动并返回 SSE |
| `GET` | `/runs/{runId}/events/stream?afterSeq=` | 重放 / 续流 |
| `POST` | `/runs/{runId}/clarifications/{clarificationId}/answers` | 提交答案，返回 202 |
| `POST` | `/runs/{runId}/contracts/{contractId}/decision` | `CONFIRM/REVISE/CANCEL`，返回 202 |
| `POST` | `/runs/{runId}/cancel` | 取消暂停或运行中的 Run |
| `GET` | `/runs/{runId}` | 快照（状态、澄清、契约、lastSeq） |

## 7. 前端

- `useArchitect.js`：按项目维护 Architect 状态，`sessionStorage` 持久化；
- `architectStream.js`：`fetch + ReadableStream` 解析 SSE，支持断线 `afterSeq` 重放；
- `ArchitectPanel.jsx`：右侧全局抽屉，含进度、形态徽章、澄清表单、契约、决策；
- `ContractViewer.jsx`：按形态显示概览 / 接口 / DDL / 前端设计 / 脚本规格 / 实施路径 / Markdown 原文，使用 React 节点渲染，无 `dangerouslySetInnerHTML`；
- `ProjectWorkspace.jsx` 工具栏新增主 CTA；`WorkspacePage.jsx` 负责组装源码、历史与选项。
- 关闭项目 Tab 时，`dispose(projectId)` 会取消仍在运行的 Architect Run，避免服务端悬挂。

## 8. 本轮修复的两个竞态

1. **暂停事件先于状态可见**：`architect.clarification.required` / `contract.ready` 在 `graph.invoke` 返回前发射，用户快速提交会命中 404/409。修复：Graph 提供 `pause_hook`，服务层在事件写入 SSE 缓冲前先写入 `active.clarification/contract/status`。
2. **旧 outcome 覆盖新状态**：用户回答上一轮后，上一段图的 `_apply_outcome` 可能迟到，把已进入下一轮的澄清覆盖回旧 ID。修复：`ActiveArchitectRun.execution_epoch` 单调递增，`_apply_outcome` 只接受当前 epoch 的结果。

## 9. 验证

```bash
# 全量 46 个 Python 单元测试（本轮新增 RAG + Architect 21 个）
docker run --rm -v "$PWD/services/orchestrator:/app" -w /app \
  -e PYTHONPATH=. vibe-platform/python-ai:dev \
  python -m unittest tests.test_editor tests.test_run_options \
    tests.test_architect tests.test_rag -v

# API E2E：三种形态、确认、修订、取消；直连 AI Core
python3 scripts/architect_m2_5_e2e_test.py

# 同一脚本验证 BFF 透传
AI_BASE_URL=http://127.0.0.1:8080 python3 scripts/architect_m2_5_e2e_test.py

# 浏览器 E2E：显式形态选择、动态追问、分级契约、确认与恢复
/tmp/vibe-browser/bin/python scripts/architect_ui_e2e_test.py
```

截图：

- `docs/screenshots/m2-5-architect-contract.png`
- `docs/screenshots/m2-5-architect-confirmed.png`

## 10. 已知边界

- 契约暂未真正进入代码生成执行，`CONFIRMED` 只代表契约确认；
- RAG 仍是 Markdown + BM25，未接向量库；
- Architect 状态仍是进程内存储，进程重启会丢失进行中的 Run；
- 多服务沙盒、数据库迁移验证尚未由契约驱动。
