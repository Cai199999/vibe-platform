# vibe-platform 技术栈全记录

> **快照日期**：2026-11-05  
> **覆盖范围**：M1 前端闭环 → M2 SSE / HITL → M3-A Diff 审批与取消 → M3-B `CodeEditor` 抽象 → M2.5 应用形态与全栈契约 → Supervisor 多智能体路由 / 前端统一入口 / both 强门控
> **统计口径**：以仓库当前源码、`pom.xml`、`package.json`、`requirements.txt`、Dockerfile 和实际运行容器为准。  
> **重要说明**：设计文档中的目标技术栈与当前已落地技术栈并不完全一致，本文严格区分「当前已使用」「已接入但未启用」「设计预留 / 未落地」。

---

## 1. 项目定位与当前阶段

vibe-platform 是一个以画布标注为输入、以多轮对话为意图、以 LangGraph 编排 Agent、以 Docker 沙盒执行代码修改的 Vibe Coding 平台。

当前已跑通的主链路：

1. 用户在 React 画布上圈选元素并填写修改意图；
2. 前端通过 SSE 启动 Run，接收规划、执行、构建、审批等事件；
3. Java BFF 透传请求与事件流，不阻塞 Servlet 线程；
4. Python AI Core 用 LangGraph 完成 Planner → Coder → Applier → Verifier → Evaluator → Approval 的闭环；
5. 代码修改以 `CodeEdit` 经 `EditorRegistry` 落到沙盒；
6. 沙盒内执行 `npm run build` 验证，生成 Git checkpoint 与 unified diff；
7. 用户审批后应用更改，预览 iframe 刷新；也可取消 Run 或回滚到指定 commit。

M2.5 另有一条**独立的 Architect 链路**，与局部 UI Run 解耦：

1. 工作台点击 `下一步：构建全栈方案`；
2. Architect Graph 从沙盒导出真实源码，先判别 `lightweight_web / script_automation / full_stack_system`；
3. 按应用形态动态追问，不强迫轻量工具提供后端 / 数据库；
4. 检索 `knowledge/fullstack`（Markdown + BM25）生成带引用的《全栈实施契约》；
5. 用户 `CONFIRM / REVISE / CANCEL`，确认结果保留在 Architect 状态中。

顶层新增 **Supervisor 总调度**（M2.5）：

1. `Supervisor Graph` 只做意图路由，不写代码；
2. 根据自然语言、标注与关键词判断 `ui_graph` / `architect_graph` / `both`；
3. `AgentRegistry` 启动扫描 `ai_core/*/manifest.json`，声明内置 / 预留 Agent，
   并加载企业 CustomAgent；`SkillRegistry` 汇总 Manifest 中的 Skill 声明；
4. `route` 返回 Handoff 计划，`dispatch` 才启动子图并返回独立 SSE 路径；
5. 前端“发送 / 提交 / 下一步”统一走 `route → dispatch`，按
   `decision.handoff` 切换局部时间轴 / Architect 面板 / rejected Toast；
6. `handoff_both` 先只启动 Architect，契约 `CONFIRMED` 后
   `POST /gates/{gateId}/release` 才启动 UI，项目锁与门控状态机拒绝乱序执行。

当前仍是**单前端应用（React/Vite）沙盒**。契约尚未驱动全栈代码生成；多服务沙盒、Redis checkpoint、真实鉴权与持久化尚未落地。

---

## 2. 实际运行拓扑

```text
┌──────────────────────────────────────────────────────────────┐
│ Browser                                                      │
│ http://127.0.0.1:5173                                        │
└──────────────────────────┬───────────────────────────────────┘
                           │ 同源 /api
                           ▼
┌──────────────────────────────────────────────────────────────┐
│ platform-web                                                 │
│ React 18.3.1 + Vite 5.4.21                                   │
│ 容器：vibe-platform/platform-web:dev（Node 20.20.2 Alpine）  │
│ 端口：5173；Vite 将 /api 代理到 java-bff:8080                 │
└──────────────────────────┬───────────────────────────────────┘
                           │ HTTP / SSE
                           ▼
┌──────────────────────────────────────────────────────────────┐
│ java-bff                                                     │
│ Java 21.0.13 + Spring Boot 3.3.5                             │
│ Spring MVC + WebClient / Reactor Netty（SSE 非阻塞透传）      │
│ 端口：8080                                                   │
└──────────────────────────┬───────────────────────────────────┘
                           │ REST + text/event-stream
                           ▼
┌──────────────────────────────────────────────────────────────┐
│ python-ai                                                    │
│ Python 3.12.16 + FastAPI 0.115.6 + LangGraph 0.2.62          │
│ 端口：8100                                                   │
│                                                              │
│  ├─ DeepSeek Chat（OpenAI 兼容接口）/ Mock LLM               │
│  ├─ LangGraph StateGraph + MemorySaver                       │
│  ├─ 进程内 ThreadMemoryStore + RunEventBuffer                 │
│  └─ Docker SDK 7.1.0 ──► 宿主 Docker Engine                  │
│                              └─ sandbox-* 容器               │
│                                 React 18 + Vite 5            │
│                                 Git checkpoint / 构建验证     │
└──────────────────────────────────────────────────────────────┘

基础设施（复用 ~/Workspace/infra，不由本项目 Compose 部署）：
  MySQL 8.0 :3306   Redis 7.0 :6379   MinIO :9000/:9001
```

### 2.1 运行单元

| 单元 | 镜像 / 运行方式 | 端口 | 当前职责 |
|---|---|---|---|
| `platform-web` | `vibe-platform/platform-web:dev`，基于 `node:20-alpine` | 5173 | 画布、标注、对话、预览、审批 UI |
| `java-bff` | `vibe-platform/java-bff:dev`，Temurin 21 JRE | 8080 | 对外 API、参数校验、SSE 透传、沙盒导出代理 |
| `python-ai` | `vibe-platform/python-ai:dev`，基于 `python:3.12-slim` | 8100 | Agent 编排、代码编辑、沙盒生命周期、事件缓冲 |
| `sandbox-*` | `vibe-platform/sandbox-react:dev`，基于 `node:20-alpine` | 随机宿主端口，仅绑定 `127.0.0.1` | 运行用户 React/Vite 项目、构建校验、Git 快照 |

Docker Compose 使用自定义 bridge 网络 `vibe-platform-net`，三个平台服务通过服务名互访。

---

## 3. 技术栈总表

| 层 | 技术 / 组件 | 版本 | 当前用途 | 状态 |
|---|---|---|---|---|
| 前端框架 | React | 18.3.1 | 页面与组件 | 已使用 |
| 前端渲染 | React DOM | 18.3.1 | 浏览器渲染 | 已使用 |
| 前端路由 | React Router DOM | 6.28.0 | 登录页 / 工作台路由 | 已使用 |
| 前端拖拽 | `@dnd-kit/core` | 6.1.0 | 工作区 Tab 拖拽排序 | 已使用 |
| 前端拖拽 | `@dnd-kit/sortable` | 8.0.0 | 可排序 Tab 列表 | 已使用 |
| 前端拖拽 | `@dnd-kit/utilities` | 3.2.2 | 拖拽样式变换 | 已使用 |
| 前端构建 | Vite | 5.4.21 | Dev Server、HMR、生产构建 | 已使用 |
| 前端插件 | `@vitejs/plugin-react` | 4.3.4 | React JSX 转换与 Fast Refresh | 已使用 |
| 前端语言 | JavaScript + JSX | ES2022 | 全部业务代码 | 已使用 |
| 前端样式 | 原生 CSS | — | `styles.css`、CSS 变量、响应式布局 | 已使用 |
| 前端 HTTP | Fetch API | 浏览器原生 | REST 调用 | 已使用 |
| 前端流式 | Fetch + `ReadableStream` + `TextDecoder` | 浏览器原生 | 解析 Run / Architect SSE，支持断线重放 | 已使用 |
| 前端预览 | HTML `<iframe>` | 浏览器原生 | 沙盒预览与缩放 | 已使用 |
| 前端状态 | React Context + Hooks | React 18 | 鉴权、工作区、视口状态 | 已使用 |
| 前端持久化 | `localStorage` / `sessionStorage` | 浏览器原生 | Mock 会话、项目草稿、Run 快照 | 已使用 |
| BFF 语言 | Java | 21（运行时 21.0.13 LTS） | 全部 BFF 代码 | 已使用 |
| BFF 框架 | Spring Boot | 3.3.5 | 应用基座、自动配置 | 已使用 |
| BFF Web | Spring MVC / Tomcat | Boot 3.3.5 管理 | REST 接口与 Servlet 容器 | 已使用 |
| BFF 异步客户端 | Spring WebFlux / WebClient | Boot 3.3.5 管理 | 调用 AI Core 并透传 SSE | 已使用 |
| BFF 响应式运行时 | Reactor Netty | Boot 3.3.5 管理 | WebClient 连接与流式读取 | 已使用 |
| BFF 校验 | Jakarta Bean Validation | Boot 3.3.5 管理 | 请求 DTO 校验 | 已使用 |
| BFF JSON | Jackson | Boot 3.3.5 管理 | 序列化 / 反序列化 | 已使用 |
| BFF 对象存储 | MinIO Java SDK | 8.5.12 | 懒加载客户端 Bean | 已接入未启用 |
| BFF Redis | Spring Data Redis | Boot 3.3.5 管理 | 依赖与连接配置 | 已接入未启用 |
| BFF 数据库驱动 | `mysql-connector-j` | Boot BOM 管理 | JDBC 驱动依赖 | 仅依赖 |
| 构建 | Maven | 3.9.9 | BFF 编译与镜像构建 | 已使用 |
| AI 语言 | Python | 3.12.16（镜像 `3.12-slim`） | AI Core 全部代码 | 已使用 |
| AI Web 框架 | FastAPI | 0.115.6 | 内部 API、SSE、参数模型 | 已使用 |
| AI ASGI Server | Uvicorn `[standard]` | 0.34.0 | 启动服务 | 已使用 |
| AI 配置 | pydantic-settings | 2.7.1 | 环境变量与 `.env` 解析 | 已使用 |
| AI 环境变量 | python-dotenv | 1.0.1 | `.env` 加载 | 已使用 |
| Agent 编排 | LangGraph | 0.2.62 | StateGraph、checkpoint、interrupt | 已使用 |
| 多 Agent 调度 | `SupervisorGraphRunner` + `AgentRegistry` | 自研，基于 LangGraph | 意图路由、能力注册、子图 Handoff | 已使用（路由层） |
| LLM 框架 | LangChain | 0.3.14 | Prompt、消息与工具生态 | 已使用 |
| LLM 接入 | langchain-openai | 0.3.0 | 通过 OpenAI 兼容协议调用 DeepSeek | 已使用 |
| 模型 | DeepSeek `deepseek-chat` | 远端 API | 真实生成与规划 | 可选 |
| 模型 | Mock LLM | 内置 | 无 Key、确定性 E2E | 已使用 |
| HTTP 客户端 | httpx | 0.28.1 | 健康检查 / 测试调用依赖 | 已使用 |
| 架构 RAG | Markdown + BM25（自研，无外部依赖） | — | 检索 `knowledge/fullstack` 规范与引用 | 已使用 |
| 容器 SDK | Docker SDK for Python | 7.1.0 | 创建、执行、导出、销毁沙盒 | 已使用 |
| 沙盒运行时 | Docker Engine | 宿主安装 | 隔离执行用户项目 | 已使用 |
| 沙盒基础镜像 | `node:20-alpine` | Node 20 | React/Vite 构建与 dev server | 已使用 |
| 沙盒框架 | React / React DOM | 18.3.1 | 生成项目的运行框架 | 已使用 |
| 沙盒构建 | Vite / plugin-react | 5.4.21 / 4.3.4 | 用户项目构建 | 已使用 |
| 沙盒测试 | Vitest | 2.1.9 | 用户项目测试脚本占位 | 已使用 |
| 沙盒版本控制 | Git | Alpine 包 | 基线、每轮 checkpoint、回滚 | 已使用 |
| 业务数据库 | MySQL | 8.0 | 预留业务持久化，schema 已定义 | 仅配置 |
| 缓存 / 事件 | Redis | 7.0 | 预留 checkpoint / 事件总线 | 仅配置 |
| 对象存储 | MinIO | RELEASE.2025-09-07T16-13-09Z | 预留源码 / 快照 / 导出产物 | 仅配置 |
| 容器编排 | Docker Compose | v2 | 本地三服务编排 | 已使用 |

---

## 4. 前端 `apps/platform-web`

### 4.1 运行时依赖

```json
{
  "react": "18.3.1",
  "react-dom": "18.3.1",
  "react-router-dom": "6.28.0",
  "@dnd-kit/core": "6.1.0",
  "@dnd-kit/sortable": "8.0.0",
  "@dnd-kit/utilities": "3.2.2"
}
```

### 4.2 开发与构建依赖

```json
{
  "vite": "5.4.21",
  "@vitejs/plugin-react": "4.3.4"
}
```

- 容器基础镜像：`node:20-alpine`，实测 Node `20.20.2`、npm `10.8.2`。
- npm registry：`https://registry.npmmirror.com`。
- Vite Dev Server：`host=0.0.0.0`、`port=5173`、`strictPort=true`。
- `/api` 代理目标由 `VITE_BFF_PROXY_TARGET` 控制，Compose 中为 `http://java-bff:8080`。
- 源码通过 bind mount 挂载，启用 HMR；`node_modules` 使用匿名卷避免被宿主覆盖。

### 4.3 前端实现特征

| 能力 | 实现方式 |
|---|---|
| 画布 | 原生 Pointer Events + CSS `transform` 平移缩放 |
| 圈选标注 | 原生指针拖拽 + SVG / 覆盖层椭圆 |
| 多标注 | 最多 3 个独立圈与独立输入框，批量提交 `AnnotationEnvelope[]` |
| 预览 | `<iframe>` 固定 1280×720 内部视口，外层等比缩放 |
| 工作区 Tab | `@dnd-kit` 拖拽排序，可多项目并行 |
| SSE | 自研 `runStream.js`，解析 `id/event/data` 帧，记录 `lastSeq`，断线后 `afterSeq` 重放 |
| 鉴权 | Mock 登录，任意账号密码或一键体验，Token 存 `localStorage` |
| 项目持久化 | 工作区文件与标注存 `localStorage`；Run 结果快照存 `sessionStorage` |
| 样式 | 单一 `styles.css` + CSS 变量，无 UI 组件库 |

### 4.4 当前未使用

- 未使用 TypeScript，全部为 `.jsx` / `.js`。
- 未使用 Tailwind CSS、CSS-in-JS 或组件库。
- 未使用 Redux、Zustand、MobX、React Query 等状态 / 数据请求库。
- 未使用 Axios，统一使用原生 `fetch`。
- 未使用 `EventSource`：因为启动 Run 需要 POST body 与自定义 Header，改用 `fetch` 流式读取。
- `platform-web` 自身没有单元测试框架；浏览器 E2E 在仓库根目录 `scripts/` 中用 Playwright 编写。

---

## 5. Java BFF `apps/platform-bff`

### 5.1 基线

| 项目 | 值 |
|---|---|
| Java | 21 |
| Spring Boot | 3.3.5 |
| 构建工具 | Maven 3.9.9 |
| 构建镜像 | `maven:3.9.9-eclipse-temurin-21` |
| 运行镜像 | `eclipse-temurin:21-jre-jammy` |
| JVM 参数 | `-XX:MaxRAMPercentage=75` |
| 应用模型 | Spring MVC（Servlet） |

### 5.2 Maven 依赖

| 依赖 | 作用 |
|---|---|
| `spring-boot-starter-web` | REST Controller、Tomcat、Jackson |
| `spring-boot-starter-webflux` | 仅引入 WebClient / Reactor Netty，用于 SSE 非阻塞透传；应用仍是 MVC |
| `spring-boot-starter-validation` | `@Valid` 请求校验 |
| `spring-boot-starter-data-redis` | Redis 客户端依赖，为事件 / 会话 / 租约预留 |
| `mysql-connector-j` | MySQL JDBC 驱动，runtime scope |
| `io.minio:minio:8.5.12` | 对象存储客户端 |
| `spring-boot-configuration-processor` | 配置元数据 |
| `spring-boot-starter-test` | 测试依赖 |

### 5.3 关键实现

- **SSE 透传**：`RunStreamService` 使用专用 `WebClient` 调用 AI Core `POST /internal/runs/execute/stream`，返回 `Flux<ServerSentEvent<String>>`，由 Spring MVC `ReactiveTypeHandler` 异步写回。
- **断线重放**：`GET /api/v1/runs/{runId}/events/stream?afterSeq=`，兼容 `Last-Event-ID` 请求头。
- **长任务超时**：普通 AI Core 读超时 3s；Run 执行读超时默认 300s。
- **沙盒注册表**：`RunSandboxRegistry` 在 BFF 进程内维护 Run 与 sandboxId 的映射。
- **导出代理**：`SandboxExportService` 从 AI Core 拉取 `tar.gz` 并转发浏览器。
- **健康检查**：`/api/v1/health` 会继续调用 Python AI Core 的 `/internal/ping`，属于端到端健康检查。

### 5.4 当前未使用 / 仅配置

| 技术 | 现状 |
|---|---|
| MyBatis-Plus | `pom.xml` 中不存在，未使用 |
| Flyway / Liquibase | 未使用；schema 由 `infra/migrations/schema.sql` + `scripts/init-db.sh` 手工执行 |
| Spring Security | 未引入；当前只有前端 Mock 鉴权 |
| JDBC / JPA / HikariCP | 只有 `mysql-connector-j` 驱动和 `application.yml` 数据源配置，未引入 `spring-boot-starter-jdbc` / `spring-boot-starter-data-jpa`，因此当前没有实际数据库访问层 |
| Spring Data Redis | 依赖与配置已存在，但代码中未注入 `RedisTemplate` / `StringRedisTemplate` |
| MinIO | 仅 `MinioConfig` 创建懒连接客户端 Bean，主链路尚未写入对象 |

> 结论：Java BFF 当前是**无状态代理层**，业务数据仍由前端本地存储与 Python 进程内状态承载。

---

## 6. Python AI Core `services/orchestrator`

### 6.1 精确依赖

```text
fastapi==0.115.6
uvicorn[standard]==0.34.0
pydantic-settings==2.7.1
python-dotenv==1.0.1
langgraph==0.2.62
langchain==0.3.14
langchain-openai==0.3.0
httpx==0.28.1
docker==7.1.0
```

运行镜像：`python:3.12-slim`，非 root 用户，安装 `curl`、`git`，启动命令：

```bash
uvicorn main:app --host 0.0.0.0 --port 8100 --proxy-headers
```

### 6.2 Agent 图

`langgraph.graph.StateGraph` 当前节点：

```text
START
  → planner
  → clarify_gate
      ├─ confidence < 阈值 → 澄清中断 → planner
      └─ coder
  → coder
  → applier
  → verifier
  → evaluator
      ├─ 失败且未超预算 → coder（自动修复，最多 3 次）
      ├─ 需要审批 → approval_gate
      └─ 成功 → END
  → approval_gate
      ├─ 待审批 → awaiting_approval → END
      └─ 拒绝 → rejected → END
  → failed → END
```

### 6.3 关键机制

| 机制 | 实现 |
|---|---|
| 状态图 | LangGraph `StateGraph(AgentState)` |
| HITL 澄清 | LangGraph `interrupt()` + `Command(resume=...)` |
| Diff 审批 | 构建通过后生成 unified diff，`approval_gate` 暂停；`MemorySaver` 保存中断点 |
| Checkpoint | `CheckpointerProvider` 协议；M2 实现为 `MemoryCheckpointerProvider`，内部使用 LangGraph `MemorySaver` |
| 事件流 | 进程内 `RunEventBuffer`，`collections.deque(maxlen=2000)`，单调 `seq`，支持 `afterSeq` 重放 |
| 后台执行 | `threading.Thread` 运行长任务；HTTP 层用 FastAPI `StreamingResponse` 输出 `text/event-stream` |
| 会话记忆 | `ThreadMemoryStore` 进程内字典，按 threadId 保存聊天历史、Run、checkpoint、源码快照 |
| 模型路由 | `DEEPSEEK_API_KEY` 非空时用 `ChatOpenAI` 指向 `https://api.deepseek.com`；为空时回退 Mock |
| 代码编辑 | `CodeEditor` 协议 + `EditorRegistry` + `AnchorCodeEditor` + `FileCreator`；`TreeSitterEditor` 占位 |
| 沙盒 | `LocalDockerSandbox` 通过 Docker SDK 管理容器 |
| 构建校验 | 容器内执行 `npm run build`，失败则回传日志给 Coder 修复 |
| Git 快照 | 沙盒内 `git add -A` / `git commit` / `git reset --hard`，commit hash 白名单校验 |
| 取消 | 协作式取消：标记 Run 状态，节点边界检查并停止 |

### 6.4 代码编辑抽象

| 组件 | 职责 | 状态 |
|---|---|---|
| `CodeEditor` Protocol | 统一编辑接口 | 已实现 |
| `AnchorCodeEditor` | 锚点替换、全量替换、前后插入、occurrence、缩进 | 已实现 |
| `FileCreator` | 新建 / 重写文件，路径穿越防护 | 已实现 |
| `TreeSitterEditor` | 跨文件结构化编辑 | 占位，调用即抛 `EditorNotAvailableError` |
| `EditorRegistry` | 按 `EditKind` 路由，批量应用并计算净变化 | 已实现 |

### 6.5 当前未使用

- 未安装 `redis` Python 客户端，未连接 Redis。
- 未使用 SQLAlchemy / Alembic；AI Core 当前不直接访问 MySQL。
- 未使用 pytest；编辑器 / Architect 测试使用标准库 `unittest`。
- 未使用 tree-sitter 依赖。
- 未实现 `RedisSaver`、Redis Stream 事件总线、跨进程 checkpoint 恢复。
- Supervisor 路由层已实现，但 BackendCoder / DBA / Reviewer / ScriptAgent 仍是预留能力，尚未接入代码生成 Runtime。
- 当前 Agent 仍运行在同一 AI Core 进程内；没有独立 Agent 进程、消息总线或跨进程 Agent 隔离。

### 6.6 M2.5 Architect 模块

| 组件 | 文件 | 职责 |
|---|---|---|
| Graph | `ai_core/architect/graph.py` | 独立 `StateGraph`：采集 → RAG → 形态判别 → 动态澄清 → 分级契约 → 决策 |
| Confidence Guard | `ai_core/architect/confidence.py` | 按 `app_shape` 计算必填槽位、确定性置信度上限与 fallback 问题 |
| Contract | `ai_core/architect/contract.py` | 分级校验、字段裁剪、确定性修复反馈、契约装配 |
| LLM | `ai_core/architect/llm.py` | DeepSeek / Mock Architect LLM；Mock 可离线生成三种形态契约 |
| Source | `ai_core/architect/source.py` | 从沙盒 tar.gz 读真实源码；预算、路径与二进制过滤 |
| RAG | `ai_core/rag/base.py`、`markdown.py`、`chroma.py` | `RAGProvider` 协议；Markdown + BM25 已实现，Chroma 占位 |
| Service | `ai_core/architect/service.py` | 后台线程、SSE 缓冲、暂停态预热、epoch 防旧结果回写、决策幂等 |
| API | `ai_core/api/architect.py` | 内部 REST/SSE；BFF 暴露公开 `/api/v1/architect/**` |

应用形态与动态槽位：

| 形态 | 必填槽位 | 明确禁止 |
|---|---|---|
| `lightweight_web` | `ui.primary_flow` | 后端、数据库、API、DDL、脚本规格 |
| `script_automation` | `script.runtime/input/output/logic` | UI、API、DDL |
| `full_stack_system` | `backend.framework/database.engine/business.key_flows/data.entities` | 物理外键 |

分支契约校验：

- 轻量：`apiRoutes=[]`、`databaseDdl=""`、`scriptSpec={}`；
- 脚本：必须含 `runtime/entry/dependencies/input/output/exitCodes`；
- 全栈：至少一条 `/api/` 路由、`CREATE TABLE`、组件交互、`backend/database` 技术栈。

前端 `useArchitect.js` 按项目持久化 Architect 状态到 `sessionStorage`，`architectStream.js`
复用 `afterSeq` 重放；`ContractViewer.jsx` 用 React 节点渲染 Markdown，不使用 HTML 注入。

### 6.7 Supervisor 多智能体总调度

| 组件 | 文件 | 职责 |
|---|---|---|
| Graph | `ai_core/supervisor/graph.py` | LangGraph 顶层路由：分类 → Registry 解析 → Handoff → 终态 |
| Classifier | `ai_core/supervisor/router.py` | 确定性规则；`targetAgent` / `scopeHint` 显式意图优先 |
| Registry | `ai_core/supervisor/registry.py` | 启动扫描 `ai_core/*/manifest.json`；能力、版本、SSE 路径、可用性与 Skill 汇总 |
| Manifest | `ai_core/supervisor/manifest.py` | Agent Manifest Pydantic Schema、JSON Schema 根节点校验、重复能力 / Skill 校验 |
| SkillRegistry | `ai_core/supervisor/skills.py` | M3 预留：登记 Skill（Prompt / RAG / Tool 子集），当前不执行 |
| CustomAgent | `ai_core/supervisor/custom.py` | 受限 JSON Manifest 加载；路径穿越与任意 import 防护 |
| Gate Registry | `ai_core/supervisor/gates.py` | both 项目锁；`AWAITING_CONTRACT → RELEASING → RELEASED/FAILED` 状态机与幂等释放 |
| Service | `ai_core/supervisor/service.py` | `route` 计划、`dispatch` 真实启动、契约校验与 gate release |
| API | `ai_core/api/supervisor.py` | 内部 `/api/v1/internal/supervisor/**`，含 gates 查询 / 释放 |
| BFF | `bff/supervisor/*` | 公开 `/api/v1/supervisor/**` 透传 |
| 前端客户端 | `workspace/supervisorClient.js` | route / dispatch / gate release / gate query |
| 前端接入 | `pages/WorkspacePage.jsx` | 统一提交入口、handoff 分支、自动释放门控、attach 预启动 Run |

**内置能力**：

| Agent | Manifest | 能力 | 状态 |
|---|---|---|---|
| `ui_graph` | `agent/manifest.json` | `ui_local_edit` | 可用 |
| `architect_graph` | `architect/manifest.json` | `global_architecture` | 可用 |
| `backend_coder` | `backend_coder/manifest.json` | `backend_generation` | 预留（无 Graph） |
| `script_agent` | `script_agent/manifest.json` | `script_automation` | 预留（无 Graph） |

**Skill**：每个 Manifest 的 `skills[]` 声明 `skill_id`、`prompt_ref`、`rag_refs`、
`tools`；`SkillRegistry` 只汇总展示，M3 才实现 Agent + Skill 动态拼装。
**Memory**：仅 LangGraph 线程级短期记忆；长期记忆 / 用户画像明确留待 M3。
**MCP**：架构图虚线框预留 `MCP Client Adapter`，M2.5 不实现。

**路由规则**：显式 `targetAgent` > 显式 `scopeHint` > UI + 架构关键词 = both >
架构关键词 = Architect > 标注 / UI 关键词 = UI > 无信号 = rejected。

**CustomAgent**：`SUPERVISOR_CUSTOM_AGENTS_DIR`（默认 `config/custom_agents`）
只扫描 `*.json`；Manifest 声明 `taskTypes`、`keywords`、`systemPromptFile`、
`ragKnowledgeDir`，禁止绝对路径与 `..`，不允许覆盖内置 Agent。当前只返回
`handoff_custom` 路由计划，运行时尚待接入。未来运行时必须使用独立受限沙盒 /
临时 venv：非 root、只读 Prompt/RAG、无宿主网络与密钥、CPU/内存/时间配额，
只向主系统返回结构化 `AgentResult`，详见设计文档 §9。

**both 强门控**：`dispatch` 遇到 `handoff_both` 时先 `reserve_project`，只启动
Architect 并创建 gate；UI 请求保存在 gate 内。`release` 校验
`architect.contract.confirmed` 后才用原始请求启动 UI。重复 release 幂等；
契约未确认 409；同项目并发 dispatch 409；Architect 取消 / 失败自动 FAILED 并
释放项目锁。当前门控为单进程内存实现，多副本部署需替换为 Redis/MySQL 分布式
锁，API 契约不变。

---

## 7. 沙盒 `services/orchestrator/sandbox_template`

### 7.1 模板技术栈

| 组件 | 版本 |
|---|---|
| Node.js | 20（`node:20-alpine`） |
| React | 18.3.1 |
| React DOM | 18.3.1 |
| Vite | 5.4.21 |
| `@vitejs/plugin-react` | 4.3.4 |
| Vitest | 2.1.9 |
| Git | Alpine 包 |

- npm registry 使用 `https://registry.npmmirror.com`。
- 镜像名：`vibe-platform/sandbox-react:dev`。
- 容器内工作目录：`/app`。
- 开发端口：`5173`，映射到宿主随机端口。

### 7.2 容器加固

| 限制 | 值 / 方式 |
|---|---|
| Linux capabilities | `cap_drop=["ALL"]` |
| 提权 | `security_opt=["no-new-privileges"]` |
| 内存 | 默认 768 MB |
| CPU | 默认 1.0 核（`nano_cpus`） |
| PID | 默认 256 |
| 端口暴露 | 宿主端口仅绑定 `127.0.0.1` |
| 文件大小 | 单文件 1 MB、单请求 5 MB、导出 32 MB 上限 |
| Git 操作 | 同一沙盒串行化，commit hash 40 位十六进制白名单 |
| 宿主 Docker Socket | `python-ai` 挂载 `/var/run/docker.sock`，**仅限本地受信任环境** |

### 7.3 沙盒内不使用的技术

- 没有 Docker-in-Docker。
- 没有 Kubernetes / gVisor / Firecracker。
- 没有多服务编排（web / api / db / edge 尚未实现）。
- 没有真实数据库迁移验证。

---

## 8. 基础设施（复用 `~/Workspace/infra`）

| 服务 | 镜像 | 端口 | 当前使用方式 |
|---|---|---|---|
| MySQL | `mysql:8.0` | 3306 | 默认库 `geo_ai_db`；`infra/migrations/schema.sql` 定义 `users` / `projects` / `runs` |
| Redis | `redis:7.0` | 6379 | 仅连接配置，主链路未读写 |
| MinIO | `minio/minio:RELEASE.2025-09-07T16-13-09Z` | 9000（API）/ 9001（Console） | 仅 Java 客户端 Bean，主链路未读写 |

### 8.1 MySQL schema 特征

- 字符集 `utf8mb4`，排序规则 `utf8mb4_0900_ai_ci`。
- 引擎 InnoDB。
- **禁止物理外键**，关联一致性由业务代码、唯一索引与对账保证。
- 所有业务表带 `tenant_id`。
- 当前三张表：
  - `users`：用户账号；
  - `projects`：项目，含 `app_type`（`FRONTEND` / `FULLSTACK`）；
  - `runs`：Run 记录，含 `status`、`sandbox_id`、`last_event_seq`、`clarification_id` 等全栈预留字段。

### 8.2 本地默认连接

```text
MySQL : jdbc:mysql://127.0.0.1:3306/geo_ai_db  (root/root，仅本地开发)
Redis : 127.0.0.1:6379
MinIO : http://127.0.0.1:9000  (admin/admin123，仅本地开发)
```

敏感信息只写入 `.env`；`.env` 已被 `.gitignore` 忽略。仓库提供 `.env.example`。

---

## 9. 协议与运行时机制

### 9.1 API 风格

| 类型 | 说明 |
|---|---|
| 对外 API | Java BFF `/api/v1/**`，JSON |
| 内部 API | Python AI Core `/api/v1/internal/**`，JSON |
| 健康检查 | `GET /api/v1/health`，BFF 会级联检查 AI Core |
| 流式协议 | SSE，`Content-Type: text/event-stream; charset=utf-8` |
| 沙盒导出 | `GET /api/v1/runs/{runId}/export`，返回 `application/gzip` |

### 9.2 主要端点

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/v1/runs` | 同步执行 Run，保留兼容 |
| `POST` | `/api/v1/runs/stream` | 启动 Run 并返回 SSE |
| `GET` | `/api/v1/runs/{runId}/events/stream` | 断线重放，`afterSeq` / `Last-Event-ID` |
| `GET` | `/api/v1/runs/{runId}` | 查询 Run 快照 |
| `POST` | `/api/v1/runs/{runId}/clarifications/{clarificationId}/answers` | 提交澄清答案 |
| `GET` | `/api/v1/runs/{runId}/clarifications` | 查询澄清 |
| `POST` | `/api/v1/runs/{runId}/approvals/{approvalId}/decision` | 批准 / 拒绝 Diff |
| `GET` | `/api/v1/runs/{runId}/approvals` | 查询审批 |
| `POST` | `/api/v1/runs/{runId}/cancel` | 取消 Run |
| `POST` | `/api/v1/runs/rollback` | 回滚到指定 commit |
| `GET` | `/api/v1/runs/{runId}/export` | 导出项目 tar.gz |
| `DELETE` | `/api/v1/sandboxes/{sandboxId}` | 销毁沙盒 |
| `POST` | `/api/v1/architect/runs/stream` | 启动 Architect Run 并返回 SSE |
| `GET` | `/api/v1/architect/runs/{runId}/events/stream` | Architect 断线重放 |
| `POST` | `/api/v1/architect/runs/{runId}/clarifications/{clarificationId}/answers` | 提交架构澄清 |
| `POST` | `/api/v1/architect/runs/{runId}/contracts/{contractId}/decision` | 契约 `CONFIRM / REVISE / CANCEL` |
| `POST` | `/api/v1/architect/runs/{runId}/cancel` | 取消 Architect Run |
| `GET` | `/api/v1/architect/runs/{runId}` | Architect 快照 |
| `POST` | `/api/v1/supervisor/route` | 只返回意图路由与 Handoff 计划 |
| `POST` | `/api/v1/supervisor/dispatch` | 启动目标子图，返回子 Run ID 与 SSE 路径 |
| `GET` | `/api/v1/supervisor/agents` | 查询 AgentRegistry、Manifest 能力与 Skill 列表 |
| `POST` | `/api/v1/supervisor/agents/reload` | 重扫内置 Agent Manifest 与企业 CustomAgent Manifest |

### 9.3 SSE 事件模型

- 每个事件带单调递增 `seq`，作为 SSE `id`。
- 事件类型包括 `run.started`、节点进度、`run.succeeded`、`run.failed`、`run.cancelled`、`run.rejected`、`clarification.required`、`approval.required`。
- Architect 事件使用 `architect.*` 命名空间，包括 `architect.started`、`architect.rag.retrieved`、`architect.app_shape.detected`、`architect.clarification.required`、`architect.contract.ready`、`architect.contract.confirmed`、`architect.cancelled`、`architect.failed`。
- 终态事件：`run.succeeded`、`run.failed`、`run.cancelled`、`run.rejected`、`clarification.required`、`approval.required`；Architect 的暂停 / 终态事件同样结束当前 SSE，并可按 `afterSeq` 续流。
- 心跳帧用于保持连接。
- 客户端记录 `lastSeq`，重连时通过 `afterSeq` 补洞。
- 事件缓冲区上限 2000 条，生产环境计划切换 Redis Stream。

### 9.4 数据契约

- 标注：`AnnotationEnvelope`（含 `action`、`target`、`payload.text` 等）。
- 编辑：`CodeEdit` / `EditKind`（`REPLACE_ANCHOR`、`REPLACE_ALL`、`INSERT_BEFORE`、`INSERT_AFTER`、`CREATE_FILE`、`REWRITE_FILE`、`STRUCTURED`）。
- 审批：unified diff + `approvalId`。
- checkpoint：Git commit hash。
- 项目包：`tar.gz`，含源码与 `VIBE_CONTEXT.md`。

---

## 10. 测试、构建与格式化

| 类别 | 工具 / 命令 | 说明 |
|---|---|---|
| Python 单元测试 | `python3 -m unittest discover -s services/orchestrator/tests` | 编辑器、Run 选项、RAG、Architect、Supervisor 等 66 个用例 |
| Python 语法检查 | `python3 -m compileall services/orchestrator/ai_core` | 编译检查 |
| Python 格式化 | `black .` | 当前环境 black 26.10.1 |
| 浏览器 E2E | Playwright 1.64.1，Chromium | Python API，`/tmp/vibe-browser/bin/python` |
| 前端构建 | `docker exec vibe-platform-web npm run build` | Vite 生产构建 |
| Java 构建 | `mvn compile` / Docker 多阶段构建 | Maven 3.9.9 |
| 沙盒验证 | 容器内 `npm run build` | Agent 自动修复循环的一部分 |
| 一键启动 | `docker compose up -d` | 三服务 + 健康检查 |

### 10.1 E2E 覆盖脚本

```text
scripts/agent_e2e_test.py
scripts/platform_e2e_test.py
scripts/approval_cancel_e2e_test.py
scripts/approval_ui_e2e_test.py
scripts/add_component_e2e_test.py
scripts/multi_annotation_e2e_test.py
scripts/multi_turn_chat_e2e_test.py
scripts/ui_e2e_test.py
scripts/ui_copy_clean_e2e_test.py
scripts/popover_equal_scale_e2e_test.py
scripts/architect_m2_5_e2e_test.py
scripts/architect_ui_e2e_test.py
scripts/supervisor_e2e_test.py
```

已知遗留：`rollback_e2e_test.py`、`annotation_delete_rollback_e2e_test.py` 仍按旧同步 `/runs` 响应编写，尚未适配 SSE 与审批暂停。

---

## 11. 设计目标 vs 当前实际

以下技术出现在设计文档或规划中，但**尚未在当前代码中落地**，不能视为已使用：

| 设计目标 | 当前实际 | 差距 |
|---|---|---|
| React 19 + TypeScript | React 18.3.1 + JavaScript/JSX | 未升级、未引入 TS |
| Tailwind CSS 4 | 原生 CSS | 未引入 |
| MyBatis-Plus + Flyway | 无持久化层；手写 schema | BFF 未接数据库 |
| Redis Stream 事件总线 | 进程内 `RunEventBuffer` | 未接 Redis |
| RedisSaver checkpoint | `MemorySaver` | 重启丢失中断状态 |
| WebSocket 双向通道 | SSE 单向 + REST | 未实现 |
| gVisor / Firecracker / K8s | 普通 Docker 容器 | 隔离强度较低 |
| 多服务沙盒（web/api/db/edge） | 单 React/Vite 沙盒 | 全栈未实现 |
| Qdrant / 向量 RAG | 已实现 Markdown + BM25 检索；无向量库 | `chroma.py` 仅占位 |
| OpenAPI / JSON Schema 代码生成 | Pydantic / Jackson DTO 手写 | `packages/protocol` 仅 README |
| Supervisor + 多 Agent 进程 | Supervisor 路由层已落地；UI / Architect 独立子图 | BackendCoder/DBA/Reviewer 未接入；无独立 Agent 进程与消息总线 |
| Tree-sitter 结构化编辑 | 占位类 | 未安装依赖、未实现 |
| 真实认证 / 多租户 | 前端 Mock 登录 | 无 Spring Security、无 JWT |
| MinIO 源码 / 快照存储 | 客户端 Bean | 未写入对象 |
| 配额、审计、管理后台 | 无 | 未实现 |

---

## 12. 关键约束与安全边界

1. **中间件复用**：MySQL / Redis / MinIO 由 `~/Workspace/infra` 常驻，本项目 Compose 不重复部署。
2. **本地凭据**：`root/root`、`admin/admin123` 仅限本地开发；生产必须使用密钥管理。
3. **Docker Socket**：`python-ai` 挂载宿主 Docker Socket，等价于宿主 root 权限，仅限本地受信任环境。
4. **沙盒隔离**：`cap_drop=ALL`、`no-new-privileges`、资源限额、预览端口仅绑定回环地址。
5. **路径安全**：`FileCreator` 拒绝绝对路径、`..`、反斜杠与 Windows 盘符。
6. **Git 安全**：回滚 commit hash 必须匹配 40 位十六进制，并通过 `git cat-file -e` 校验。
7. **审批闸门**：代码落盘前必须经过用户审批；审批超时回滚沙盒。
8. **敏感信息**：`.env` 不提交 Git；文档与日志不得写入真实 API Key。
9. **推送策略**：只做本地 commit，不自动 `git push`。

---

## 13. 本地启动

```bash
cd ~/Workspace/vibe-platform

# 1. 准备环境变量
cp .env.example .env
# 按需填写 DEEPSEEK_API_KEY；留空则使用 Mock LLM

# 2. 启动平台三服务（中间件已在 ~/Workspace/infra 运行）
docker compose up -d

# 3. 初始化数据库（首次）
./scripts/init-db.sh

# 4. 访问
# Web       http://127.0.0.1:5173
# BFF       http://127.0.0.1:8080/api/v1/health
# AI Core   http://127.0.0.1:8100/api/v1/health
```

---

## 14. 版本升级注意事项

- **React 18 → 19**：需同步升级 `react-dom`、`@vitejs/plugin-react`，并回归全部 Playwright E2E；当前代码未使用 React 19 特性。
- **JavaScript → TypeScript**：建议先固化 `packages/protocol` 类型，再逐步迁移 `workspace/` 与 `components/`，避免一次性重写。
- **MemorySaver → RedisSaver**：只需替换 `CheckpointerProvider` 实现；LangGraph 版本需支持对应 saver，并保证 `thread_id` 与 Run 的映射稳定。
- **进程内事件缓冲 → Redis Stream**：需保留 `seq` 语义与 `afterSeq` 重放接口，前端协议不变。
- **单沙盒 → 多服务沙盒**：需扩展 `SandboxSpec.services[]`、端口分配、edge 反向代理与迁移验证。
- **Mock 鉴权 → 真实鉴权**：前端 Token 存储需从 `localStorage` 切换为 HttpOnly Cookie，BFF 引入 Spring Security。
- **新增 Python 数据访问**：按项目规范使用 SQLAlchemy ORM，禁止手拼原生 SQL；当前 `requirements.txt` 尚未包含 SQLAlchemy。

---

## 15. 相关文档

| 文档 | 内容 |
|---|---|
| `README.md` | 启动方式、API、M1 全链路 |
| `docs/design/02.整体架构设计.md` | 目标架构、ADR、全栈演进 |
| `docs/design/03.API接口设计.md` | 对外 API 契约 |
| `docs/design/04.M2_Agent_ToolCalling_SSE_HITL.md` | SSE、HITL、CodeEditor 实际协议 |
| `docs/design/05.Supervisor_Multi_Agent.md` | Supervisor 总调度、声明式 Manifest、SkillRegistry、Memory / MCP 边界、both 强门控、CustomAgent 沙盒隔离 |
| `docs/memory/00_project_brief.md` | 项目简报与里程碑 |
| `docs/memory/2026-11-04_M2_SSE_HITL.md` | M2 阶段总结 |
| `docs/memory/2026-11-05_M3A_diff_approval_cancel.md` | Diff 审批与取消 |
| `docs/memory/2026-11-05_M3B_code_editor_abstraction.md` | CodeEditor 抽象 |
| `docs/memory/2026-11-05_M2_5_app_shape_contract.md` | M2.5 应用形态与全栈契约 |
| `docs/memory/2026-11-05_M2_5_supervisor_multi_agent.md` | Supervisor 总调度与 AgentRegistry |
| `docs/memory/2026-11-05_M2_5_supervisor_frontend_gate.md` | 前端统一调度、both 强门控与混合 E2E |
| `docs/memory/2026-11-05_M2_5_agent_manifest_skill.md` | 声明式 Agent Manifest、SkillRegistry 与 Memory / MCP 边界 |

---

> 本文是**技术栈事实清单**，不是路线图。新增依赖或替换基础设施时，应同步更新本文件，并保持「已使用 / 仅配置 / 设计预留」三种状态不混淆。
