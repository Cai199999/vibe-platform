# vibe-platform 技术栈增量记录（2026-11-05）

> **区间起点**：`d1ba02f`（2026-11-05 09:41）`docs: 记录项目全量技术栈` —— 上一次技术栈文档产出点  
> **区间终点**：`da2b2ff`（2026-11-05 16:59）`feat: 新增真实沙盒 Eval 引擎与 Admin 评测契约`  
> **文档生成**：2026-11-05 17:10 CST  
> **基线文档**：[`TECH_STACK.md`](TECH_STACK.md)（项目全量技术栈，区间内仍在持续增补）  
> **统计口径**：以当前仓库源码、`pom.xml`、`package.json`、`requirements.txt`、Dockerfile、`.env.example` 以及本机实际运行容器为准；不把设计稿里的目标技术写成已落地技术。  
> **状态图例**：✅ 已落地运行 ｜ ◐ 已接入但默认关闭 / 未启用 ｜ ○ 仅契约或设计预留

---

## 0. 一句话结论

这 7 个多小时里，平台从“单 UI Agent + Architect 雏形”演进为“**Supervisor 统一调度 + 声明式 Agent/Skill + 多格式导入 + 管理中台 + 双引擎评测**”。

但底层技术栈几乎没有膨胀：

- **前端没有新增任何 npm 依赖**，管理看板图表用纯 CSS/SVG 手写；
- **Java BFF 只新增 `spring-boot-starter-aop`**，指标仍在 JVM 内存聚合；
- **Python 只新增 `python-multipart` 与 `PyYAML`**，Supervisor 路由是确定性规则，不调用大模型；
- **没有新增任何中间件、Dockerfile 或 docker-compose 服务**，继续复用 `~/Workspace/infra` 的 MySQL / Redis / MinIO；
- 评测器刻意保持“轻依赖”：离线引擎复用现有 `python-ai` 镜像，真实沙盒引擎只依赖 Python 标准库 + PyYAML。

---

## 1. 区间里程碑与技术引入时间线

| 时间 | Commit | 里程碑 | 本轮首次引入的技术 / 机制 |
|---|---|---|---|
| 09:41 | `d1ba02f` | 技术栈基线 | React 18 / Vite 5 / Spring Boot 3.3.5 / FastAPI / LangGraph / Docker 沙盒 |
| 10:08 | `29f0241` | 人工审批设置开关 | React Context + `localStorage` 持久化、`RunOptions.requireManualApproval`、LangGraph 显式 `succeeded` 终态节点 |
| 13:31 | `bacf025` | M2.5 Architect 全栈契约 | 独立 Architect Graph、`ChatOpenAI` 接 DeepSeek OpenAI 兼容接口、Pydantic 结构化契约、Markdown + 自研 BM25 RAG、置信度守卫 |
| 13:41 | `804d306` | Supervisor 多智能体路由 | 确定性规则路由、`AgentRegistry` 启动扫描 `manifest.json`、Handoff 计划、Gate Registry |
| 14:01 | `aa9bdc7` | 前端统一入口与 both 强门控 | 前端 `route → dispatch` 统一调用、按 `decision.handoff` 切换时间轴 / Architect 面板、Gate 释放 |
| 14:12 | `713cdb3` | 声明式 Manifest 与 Skill 预留 | `manifest.json` JSON Schema 校验、`SkillRegistry`、Tool / Skill / Agent 三层边界 |
| 14:23 | `d7f1325` | 决策可视化与 CustomAgent | `SupervisorTrace` 决策轨迹、企业 CustomAgent Manifest 动态扫描、分类色板 |
| 16:04 | `25ab502` | 导入 / 空白 ADD / Tools-Skills-MCP | `multipart/form-data`、zip 安全解压、OpenAI 兼容 VLM、Node 静态服务器、`ToolRegistry` + 白名单 shell、MCP 配置槽 |
| 16:16 | `b7bf718` | 浮层卸载竞态修复 | `AbortController` 取消在途请求、卸载后不再 setState |
| 16:26 | `f86d660` | Agent OS 数据中台架构 | Knowledge Service / Qdrant / BGE-M3 / Prompt Registry / Feedback Collector **契约预留** |
| 16:39 | `c3ffc7d` | M3.1 管理中台 | Spring AOP `@IsAdmin`、Mock RBAC、`GET /api/v1/admin/metrics`、内存有界指标聚合、纯 CSS/SVG 条形图 |
| 16:46 | `b9972bf` | Eval Suite v0.1 | `PyYAML` 评测集、`eval_suite` 包、console / JSON / Markdown / JUnit XML 报告 |
| 16:59 | `da2b2ff` | Eval Suite v0.2 + Admin Eval 契约 | 真实沙盒评测、tar.gz 源码导出、线程隔离、空白不敏感正则断言、`POST /api/v1/admin/eval/run` 契约预留 |

区间代码量：`209 files changed, 33328 insertions(+), 526 deletions(-)`。

---

## 2. 当前实际运行拓扑

```text
┌────────────────────────────────────────────────────────────────────┐
│ Browser                                                            │
│ 生产式本地入口 http://127.0.0.1:5173                               │
│ 本轮联调入口   http://127.0.0.1:5174（Vite dev → demo BFF :18080） │
└───────────────────────────┬────────────────────────────────────────┘
                            │ 同源 /api
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│ platform-web  React 18.3.1 + Vite 5.4.21                           │
│ 容器 node:20-alpine；业务工作台 + /admin 管理看板 + Playwright 可驱动 │
└───────────────────────────┬────────────────────────────────────────┘
                            │ HTTP / SSE / multipart
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│ java-bff  Spring Boot 3.3.5（Java 21 目标）                        │
│ Spring MVC + WebClient(Reactor Netty) + Spring AOP                 │
│ 端口 8080；本轮另有 18080 演示实例（宿主机 JDK 25 运行）            │
└───────────────────────────┬────────────────────────────────────────┘
                            │ REST + text/event-stream
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│ python-ai  Python 3.12.16 + FastAPI 0.115.6 + LangGraph 0.2.62     │
│ 端口 8100                                                          │
│  ├─ Supervisor Graph（规则路由，不调 LLM）                          │
│  ├─ UI Graph（DeepSeek / Mock LLM）                                │
│  ├─ Architect Graph（ChatOpenAI → DeepSeek）                       │
│  ├─ Markdown + BM25 RAG；Chroma 仅占位                             │
│  └─ Docker SDK 7.1.0 ──► sandbox-react 容器                        │
└───────────────────────────┬────────────────────────────────────────┘
                            │ Docker socket
                            ▼
┌────────────────────────────────────────────────────────────────────┐
│ sandbox-*  Node 20 Alpine + React 18 + Vite 5                      │
│ 仅绑定 127.0.0.1 随机端口；内存 768MB / 1 CPU 上限                  │
└────────────────────────────────────────────────────────────────────┘

基础设施（复用 ~/Workspace/infra，不由本项目部署）：
  geo-mysql   MySQL 8.0                                    :3306
  geo-redis   Redis 7.0                                    :6379
  geo-minio   MinIO RELEASE.2025-09-07T16-13-09Z           :9000/:9001
```

本轮新增的是**逻辑运行单元**，不是新的基础设施：

| 单元 | 运行方式 | 端口 | 本轮职责 |
|---|---|---|---|
| `platform-web` | 容器 / Vite dev | 5173 / 5174 | 工作台、Supervisor 轨迹、Architect 面板、`/admin` 看板 |
| `java-bff` | 容器 / `java -jar` | 8080 / 18080 | 管理指标聚合、Supervisor 透传、Architect SSE、multipart 导入 |
| `python-ai` | 容器 | 8100 | Supervisor、Architect、UI、导入、沙盒、评测离线引擎 |
| `sandbox-*` | 按 Run 创建、结束销毁 | 随机宿主端口（仅 127.0.0.1） | 用户项目运行、构建校验、Git checkpoint、源码导出 |
| Eval Runner | 宿主机 Python / 一次性容器 | 无 | 评测集加载、路由断言、真实沙盒断言、报告生成 |

---

## 3. 技术栈增量总表

| 层 | 技术 / 组件 | 版本 | 本轮用途 | 状态 |
|---|---|---|---|---|
| 前端框架 | React | 18.3.1 | 不变；新增管理页与 Supervisor UI | ✅ |
| 前端路由 | React Router DOM | 6.28.0 | `/admin` 路由与 `AdminRoute` 守卫 | ✅ |
| 前端拖拽 | `@dnd-kit/*` | 6.1.0 / 8.0.0 / 3.2.2 | 不变；多 Tab 工作区 | ✅ |
| 前端构建 | Vite | 5.4.21 | 不变；dev / preview / build | ✅ |
| 前端图表 | 原生 CSS + SVG | — | Agent 调用占比条形图，**未引入图表库** | ✅ |
| 前端状态 | React Context + `localStorage` | — | 审批设置、Mock 登录态、管理角色 | ✅ |
| 前端流式 | Fetch + `ReadableStream` + `TextDecoder` | 浏览器原生 | Supervisor / Run / Architect SSE | ✅ |
| 前端取消 | `AbortController` | 浏览器原生 | 卸载与切换时的在途请求取消 | ✅ |
| BFF 基础 | Java（语言级别） | 21 | 编译目标 | ✅ |
| BFF 运行 | Temurin JDK | 21 | 容器运行时；宿主机 JDK 25 仅跑本地演示 jar | ✅ |
| BFF 框架 | Spring Boot | 3.3.5 | MVC / Validation / WebFlux WebClient | ✅ |
| BFF 切面 | `spring-boot-starter-aop` | 随 Boot 3.3.5 | `@IsAdmin` Mock RBAC 切面 | ✅ |
| BFF 流式 | Reactor Netty + WebClient | 随 Boot 3.3.5 | SSE 非阻塞透传 | ✅ |
| BFF 测试 | JUnit 5 + MockMvc | 随 Boot 3.3.5 | 管理指标接口测试 | ✅ |
| AI Web | FastAPI | 0.115.6 | 全部内部 API | ✅ |
| AI Server | Uvicorn | 0.34.0 | ASGI 服务 | ✅ |
| AI 配置 | pydantic-settings / python-dotenv | 2.7.1 / 1.0.1 | 环境变量强类型配置 | ✅ |
| AI 上传 | `python-multipart` | 0.0.20 | 项目导入 `multipart/form-data` | ✅ |
| AI 评测 | PyYAML | 6.0.3 | 评测集与 fixture 加载 | ✅ |
| AI 编排 | LangGraph | 0.2.62 | UI / Architect / Supervisor 图 | ✅ |
| AI LLM | LangChain + langchain-openai | 0.3.14 / 0.3.0 | `ChatOpenAI` 指向 DeepSeek OpenAI 兼容端点 | ✅ |
| AI HTTP | httpx | 0.28.1 | 内部服务调用 | ✅ |
| AI 沙盒 | Docker SDK for Python | 7.1.0 | 创建 / 构建 / 预览 / 导出 / 销毁沙盒 | ✅ |
| AI 检索 | 自研 Markdown + BM25 | — | `knowledge/fullstack` 契约知识检索 | ✅ |
| AI 检索 | ChromaDB | — | 适配器占位，未安装依赖 | ○ |
| AI 视觉 | OpenAI 兼容 VLM | 默认 `gpt-4o` | 图片导入生成 React 页面 | ◐ |
| AI 工具 | `ToolRegistry` + 白名单 shell | — | `shlex` + 参数数组，默认关闭 | ◐ |
| AI 协议 | MCP | — | `MCP_SERVERS` 配置槽与挂载注释 | ○ |
| 沙盒 OS | Node Alpine | 20 | React / Vite 运行环境 | ✅ |
| 沙盒静态服务 | `static_server.mjs` | — | 纯 HTML 导入项目的零依赖预览服务器 | ✅ |
| 沙盒版本控制 | Git | 容器内 | checkpoint、单标注回滚、导出 | ✅ |
| 评测报告 | JUnit XML / JSON / Markdown | — | 离线评测多格式输出 | ✅ |
| E2E | Playwright（Python Sync API） | 本机 venv | 管理台、Supervisor、导入、审批等 E2E | ✅ |
| 格式化 | Black | 临时容器安装 | Python 代码格式化 | ✅ |
| 数据库 | MySQL | 8.0 | 复用 `geo_ai_db`；本轮未新增表 | ✅ |
| 缓存 | Redis | 7.0 | 复用；本轮未新增持久化用途 | ✅ |
| 对象存储 | MinIO | RELEASE.2025-09-07T16-13-09Z | 复用；本轮未新增桶 | ✅ |

> 说明：表中「版本」来自依赖清单或实际镜像；本轮没有升级 React、Vite、Spring Boot、LangGraph 等主版本。

---

## 4. 前端 `apps/platform-web`

### 4.1 依赖没有变化

`package.json` 在区间内**零改动**：

```text
dependencies:
  react 18.3.1 / react-dom 18.3.1 / react-router-dom 6.28.0
  @dnd-kit/core 6.1.0 / @dnd-kit/sortable 8.0.0 / @dnd-kit/utilities 3.2.2
devDependencies:
  vite 5.4.21 / @vitejs/plugin-react 4.3.4
```

没有引入 TypeScript、Tailwind、Ant Design、ECharts、Recharts、Zustand、TanStack Query 等。所有新增 UI 均使用原生 JavaScript + JSX + 原生 CSS。

### 4.2 新增页面与组件

| 文件 | 作用 | 关键技术点 |
|---|---|---|
| `src/pages/AdminPage.jsx` | `/admin` 管理看板 | KPI 卡、Agent 占比条形图、最近执行表、30s 定时刷新 |
| `src/components/AdminHeader.jsx` | 管理台页头 | 角色展示、返回工作台、退出 |
| `src/components/AdminForbidden.jsx` | 非管理员整页 403 | 路由守卫的兜底 UI，不发起管理请求 |
| `src/admin/adminClient.js` | 管理 API 客户端 | `fetch` + `X-User-Role` + `AbortController` |
| `src/components/SupervisorTrace.jsx` | Supervisor 决策可视化 | 展示 handoff、目标 Agent、置信度、门控状态 |
| `src/components/AgentCatalogMenu.jsx` | Agent 目录 | 读取 `/api/v1/supervisor/agents` |
| `src/components/ArchitectPanel.jsx` | 全栈契约面板 | SSE 事件驱动、澄清 / 确认 / 修订 |
| `src/components/ContractViewer.jsx` | 契约 Markdown / 路由 / DDL 展示 | 纯前端渲染 |
| `src/components/SettingsDialog.jsx` | 审批开关 | `role="switch"` + `aria-checked` |
| `src/settings/SettingsContext.jsx` | 全局设置 | `localStorage` 键 `vibe.settings.v1` |
| `src/workspace/supervisorClient.js` | 统一调度客户端 | `route → dispatch → gate release` |
| `src/workspace/architectStream.js` | Architect SSE | `ReadableStream`、断线重放、取消 |
| `src/workspace/importClient.js` | 项目导入 | `FormData` + multipart |
| `src/workspace/useArchitect.js` | Architect 状态 Hook | React Hooks 状态机 |
| `src/components/ErrorBoundary.jsx` | 渲染错误兜底 | React 类组件边界 |

### 4.3 管理看板的实现方式

- **图表不用库**：Agent 调用占比是普通 `div` 进度条 + 直接数值标注，颜色与实体固定绑定；
- 配色使用 dataviz 校验过的深色分类槽位：`#3987e5`（UI）、`#d95926`（Architect）、`#199e70`（Backend）、`#c98500`（Script）；
- 数字 / 时间格式化使用原生 `Intl.NumberFormat`、`Intl.DateTimeFormat`；
- 刷新周期 30 秒，`useEffect` 清理时 abort；演示数据在接口返回 `demo: true` 时显示“演示数据”徽标；
- `AuthContext` 用 `localStorage` 持久化 Mock 会话，`isAdmin` 由角色 `platform_admin` / `tenant_admin` 决定；`AdminRoute` 未登录跳 `/login`，非管理员渲染 403，不请求管理接口。

### 4.4 流式与竞态处理

- Run / Architect / Supervisor 均使用 `fetch` 读取 `text/event-stream`，`TextDecoder` 增量解析；
- 支持 `afterSeq` 断线重放；
- 本区间修复了标注浮层卸载后的请求竞态：组件卸载即 `abort()`，并用存活标记阻止后续 `setState`；
- 没有使用 `EventSource`（无法携带自定义 Header / 请求体），统一用 `fetch` 流。

---

## 5. Java BFF `apps/platform-bff`

### 5.1 依赖变化

`pom.xml` 区间内仅新增：

```xml
<dependency>
  <groupId>org.springframework.boot</groupId>
  <artifactId>spring-boot-starter-aop</artifactId>
</dependency>
```

用途：`@IsAdmin` 注解 + `AdminAuthorizationAspect` 切面。其余 `spring-boot-starter-web`、`webflux`、`validation`、`data-redis`、`mysql-connector-j`、`minio`、`starter-test` 均为基线已有。

### 5.2 M3.1 管理中台

| 组件 | 文件 | 说明 |
|---|---|---|
| 鉴权注解 | `admin/IsAdmin.java` | 标注管理接口 |
| 鉴权切面 | `admin/AdminAuthorizationAspect.java` | 从 `X-User-Role` 读取 Mock 角色；允许 `admin`、`platform_admin`、`tenant_admin`、`tenant_operator` |
| 统一异常 | `admin/AdminAccessDeniedException.java`、`admin/AdminExceptionHandler.java` | 403 `{code: ADMIN_ROLE_REQUIRED, message, requestId}` |
| 指标接口 | `admin/AdminMetricsController.java` | `GET /api/v1/admin/metrics` |
| 指标聚合 | `admin/AdminMetricsService.java` | JVM 内存聚合，最多 5000 条 Run，终态优先淘汰 |
| 演示数据 | `admin/AdminDemoDataSeeder.java` | `ADMIN_METRICS_DEMO_SEED=true` 且真实数据为空时注入 18 条样例 |
| 测试 | `src/test/java/.../AdminMetricsControllerTest.java` | 缺角色 / member → 403；admin + demo → 200；platform_admin / tenant_operator 放行 |

指标口径：

- 活跃沙盒：`RunSandboxRegistry.activeCount()`；
- 今日 Run 数：按日期累计；
- 成功率：分母只算 `SUCCEEDED + FAILED`，避免运行中 Run 拉低；
- 平均耗时：同口径，只算终态成功 / 失败；
- Agent 占比：由 SSE `run.*` 事件与 Supervisor `dispatch` 的 `dispatches[]` 归因；
- 状态映射：`RUNNING / SUCCEEDED / FAILED / CANCELLED / REJECTED / AWAITING_APPROVAL / AWAITING_CLARIFICATION / AWAITING_CONTRACT`；
- 数据不落库：BFF 重启清零，多实例不共享；M3.2 才切换到 run / run_event 聚合表。

### 5.3 本轮新增 / 变更的公开接口

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/v1/supervisor/route` | 只路由，不启动子图 |
| `POST` | `/api/v1/supervisor/dispatch` | 启动目标子图，返回 202 + 子 Run / SSE 路径 |
| `GET` | `/api/v1/supervisor/agents` | Agent 注册中心列表 |
| `POST` | `/api/v1/supervisor/agents/reload` | 重新扫描内置 / 企业 Manifest |
| `GET` | `/api/v1/supervisor/gates/{gateId}` | 查询 both 强门控状态 |
| `POST` | `/api/v1/supervisor/gates/{gateId}/release` | 契约确认后放行 UI 子图 |
| `POST` | `/api/v1/architect/runs/stream` | Architect SSE |
| `GET` | `/api/v1/architect/runs/{runId}/events/stream` | Architect 断线重放（`afterSeq` / `Last-Event-ID`） |
| `POST` | `/api/v1/architect/runs/{runId}/clarifications/{clarificationId}/answers` | 回答澄清问题 |
| `POST` | `/api/v1/architect/runs/{runId}/contracts/{contractId}/decision` | `CONFIRM / REVISE / CANCEL` 契约 |
| `POST` | `/api/v1/architect/runs/{runId}/cancel` | 取消 Architect Run |
| `GET` | `/api/v1/architect/runs/{runId}` | 刷新 / 重连后恢复快照 |
| `POST` | `/api/v1/projects/import` | multipart 项目导入 |
| `GET` | `/api/v1/admin/metrics` | 管理看板指标 |
| `POST` | `/api/v1/admin/eval/run` | **仅契约预留，尚未注册路由** |

### 5.4 仍保留的技术边界

- 鉴权是 Mock：只信任 `X-User-Role`，不是真实 JWT / Spring Security；
- 指标是单机内存，不是 Redis / MySQL 聚合；
- MySQL / Redis / MinIO 依赖仍在，但本轮管理指标没有写入；
- BFF 不解析 SSE 业务语义，只做透传与旁路聚合，避免 Servlet 线程阻塞。

---

## 6. Python AI Core `services/orchestrator`

### 6.1 依赖变化

`requirements.txt` 区间内仅新增：

```text
python-multipart==0.0.20   # 项目导入 multipart/form-data
PyYAML==6.0.3              # Eval Suite 评测集加载
```

现有版本保持：

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

没有新增 ORM、向量数据库 SDK、任务队列、缓存客户端或可观测性 SDK。

### 6.2 Supervisor 多智能体调度

**技术特征：确定性规则 + 注册中心，不调用 LLM。**

| 模块 | 文件 | 技术点 |
|---|---|---|
| 规则路由 | `ai_core/supervisor/router.py` | 纯 `re` + 关键词 / 标注 / `options` 优先级；无模型调用 |
| 注册中心 | `ai_core/supervisor/registry.py` | 启动扫描 `ai_core/*/manifest.json`，单文件 ≤ 256KB，字段 / 能力 / Skill 去重校验 |
| Manifest 加载 | `ai_core/supervisor/manifest.py` | JSON Schema 根节点必须为 `object`；内置 Agent 不可被覆盖 |
| 自定义 Agent | `ai_core/supervisor/custom.py` | 扫描 `config/custom_agents/*.json`，拒绝绝对路径、反斜杠、`..` |
| Skill 注册 | `ai_core/supervisor/skills.py` | 只登记 Prompt / RAG / Tool 子集声明，不执行 |
| 门控状态机 | `ai_core/supervisor/gates.py` | `AWAITING_CONTRACT → RELEASING → RELEASED / FAILED`；409 / 404 错误码 |
| 编排图 | `ai_core/supervisor/graph.py` | LangGraph `StateGraph`：`classify_intent → resolve_agents → handoff_* → finalize` |
| 服务层 | `ai_core/supervisor/service.py` | route / dispatch / gate release；项目级互斥锁 |
| API | `ai_core/api/supervisor.py` | `/api/v1/internal/supervisor/**` |

路由优先级：

```text
targetAgent 已注册可用        → 指定 Agent（0.99）
scopeHint = ui/architect/both → 显式目标（0.97）
UI + 架构关键词同时命中        → both（0.88）
仅架构关键词                  → architect_graph（0.90）
标注 + UI 关键词              → ui_graph（0.92）
仅标注                        → ui_graph（0.84）
UI 关键词但缺标注              → ui_graph，不可派发（0.72）
无有效信号                    → rejected（≤ 0.30）
```

- UI 子图至少需要 1 个标注，否则 `dispatch` 返回 `SUPERVISOR_UI_CONTEXT_REQUIRED`，不会创建空沙盒；
- `both` 先启动 Architect，契约 `CONFIRMED` 后通过 Gate 才能启动 UI；
- 预留 Agent：`backend_coder`、`script_agent`，状态 `reserved`，不可路由。

### 6.3 Architect 全栈契约

| 技术点 | 实现 |
|---|---|
| 编排 | 独立 LangGraph `StateGraph`，与 UI Graph 解耦 |
| 模型 | `langchain-openai.ChatOpenAI` 指向 DeepSeek `deepseek-chat`（OpenAI 兼容） |
| 结构化输出 | Pydantic v2 模型校验；LLM 只“分析 + 起草”，约束 / 槽位 / 置信度由确定性代码控制 |
| 应用形态 | `lightweight_web / script_automation / full_stack_system` 三分类 |
| 澄清 | 最多 5 轮；`ARCHITECT_MAX_CLARIFICATION_ROUNDS` |
| 置信度 | 阈值 `0.85`，不达标强制继续澄清 |
| 检索 | `MarkdownRAGProvider`：解析 `knowledge/fullstack/*.md`，自研 BM25（中文按字 + 二元组切分，ASCII 按词） |
| 源码输入 | 从沙盒导出包收集真实源码，限制 200KB / 80 文件 |
| 契约修复 | 最多 1 次重试（`ARCHITECT_CONTRACT_REPAIR_LIMIT`） |
| 事件 | SSE，复用 `RunEventBuffer`，支持 `after_seq` 重放 |
| Mock | 无 `DEEPSEEK_API_KEY` 时使用确定性 `MockArchitectLLM` |

### 6.4 UI Graph 的本轮增强

- 新增真实 `ToolNode` 接入 `ToolRegistry`；
- `requireManualApproval=false` 时构建成功直接进入显式 `succeeded` 终态节点（LangGraph 0.2.x 条件边不能直连 `END`）；
- `requireManualApproval=true` 时仍进入 `approval_gate` 的 `interrupt()`；
- 旧字段 `approvalMode` 保持兼容；
- Prompt 增加 `ADD` 语义：空白区域按父容器相对坐标插入 JSX，并保留 `data-source`。

### 6.5 项目导入与视觉生成

| 能力 | 技术 |
|---|---|
| 上传协议 | `multipart/form-data`（`python-multipart`） |
| React zip | `zipfile` 安全解压 → 识别入口 → `npm install` → `npm run build` |
| HTML zip | 容器内 `node static_server.mjs`，只读 GET / HEAD，SPA fallback |
| 图片 | OpenAI 兼容多模态接口（默认 `gpt-4o`），无 Key 时生成确定性占位页 |
| 安全 | Zip Slip / 符号链接 / 加密包 / 解压炸弹 / 条目数 / 解压体积限制；过滤 `.env` 与密钥文件 |
| 限制 | 压缩包 32MB、图片 12MB、解压后 128MB、4000 条目、超时 300s |

### 6.6 Tools / Skills / MCP

- `ToolRegistry`：Agent 可挂载白名单工具；
- `shell_tool`：`shlex.split` + 参数数组，不使用 `shell=True`；命令白名单、固定 workdir、10s 超时、输出截断 20000 字符；
- 默认 `TOOL_SHELL_ENABLED=false`，属于“已接入未启用”；
- `SkillRegistry`：只做声明与查询，不动态注入 Prompt；
- MCP：`.env.example` 有 `MCP_SERVERS=[]` 配置槽，`agent/graph.py` 保留官方 `mcp` SDK 挂载注释，**运行时尚不可用**；
- 企业 CustomAgent：Manifest 已可加载并参与路由，运行协议 `handle(task, context) -> AgentResult` 已预留，当前返回 `handoff_custom`。

### 6.7 RAG 技术现状

| Provider | 状态 | 说明 |
|---|---|---|
| `MarkdownRAGProvider` | ✅ 默认 | 本地 Markdown 分块 + 自研 BM25，零外部依赖 |
| `ChromaRAGProvider` | ○ 占位 | 构造即抛 `RAGProviderError`，未安装 chromadb |
| Knowledge Service / Qdrant / BGE-M3 | ○ 契约 | 仅 `.env.example` 与架构文档，未实现客户端调用 |

---

## 7. 沙盒 `services/orchestrator/sandbox_template`

### 7.1 技术组成

```text
基础镜像   node:20-alpine
运行框架   React 18 + Vite 5
包管理     npm（构建期装依赖，运行期秒级启动）
版本快照   Git（checkpoint / rollback / diff）
预览       Vite dev server :5173，映射到宿主 127.0.0.1 随机端口
静态兜底   static_server.mjs（纯 Node http，无 npm 依赖）
```

### 7.2 容器加固

- 仅绑定 `127.0.0.1`，不暴露到局域网；
- `mem_limit=768m`、`nano_cpus=1.0`；
- 启动 / 构建 / 导入超时分别为 90s / 180s / 300s；
- Docker SDK 通过挂载的 `/var/run/docker.sock` 管理；
- 用户代码不执行在宿主机，所有构建在容器内完成。

### 7.3 本轮新增能力

| 能力 | 技术 |
|---|---|
| 项目导入 | zip 解压 / 图片生成后写入沙盒 |
| 纯 HTML 项目 | `static_server.mjs`，零依赖 MIME + 路径逃逸防护 |
| 源码导出 | `GET /sandboxes/{id}/export` 输出 tar.gz，供 Eval 读取真实 `src/App.jsx` |
| 上下文导出 | tar.gz 内附 `VIBE_CONTEXT.md`，记录画布标注意图 |

---

## 8. Eval Suite（本轮重点新增）

### 8.1 双引擎架构

```text
config/eval_suite/core_cases.yaml
        │
        ├─ 离线引擎（v0.1）services/orchestrator/eval_suite/
        │     ├─ 真实 SupervisorService / AgentRegistry 路由
        │     ├─ MockAgentLLM(scenario="none") 生成代码
        │     ├─ 不启动 FastAPI、不创建沙盒、不读模型密钥
        │     ├─ 报告：console / JSON / Markdown / JUnit XML
        │     └─ 入口：./scripts/eval_suite.sh run
        │
        └─ 真实沙盒引擎（v0.2）scripts/run_eval.py
              ├─ 调用真实 BFF：route / runs / sandbox export / delete
              ├─ 真实 Docker 沙盒执行 UI 子图
              ├─ 导出 tar.gz 读真实源码
              ├─ 空白不敏感正则断言 expected_code_contains
              ├─ 每条用例独立 threadId，finally 删除沙盒
              └─ 报告：终端 + docs/eval_report.md
```

### 8.2 评测集与执行器技术

| 组件 | 技术 |
|---|---|
| 评测集格式 | YAML（PyYAML 6.0.3） |
| 严格加载 | 拒绝未知字段、重复 ID、fixture 路径穿越；单标注自动继承 `input.text` |
| 路由断言 | 复用真实 `SupervisorService` / `AgentRegistry`，与生产同源 |
| 生成断言 | 离线用 `MockAgentLLM`；真实引擎用 Mock LLM + 真实沙盒 |
| 报告 | `console / json / markdown / junit` |
| 容器化离线跑批 | 复用 `vibe-platform/python-ai:dev`，只读挂载源码 / 配置，可写挂载 `reports/` |
| 单测 | 标准库 `unittest`，`tests/test_eval_suite.py` |

### 8.3 真实沙盒引擎 `scripts/run_eval.py`

刻意做成**宿主机可直接运行**：

```text
依赖：Python 标准库 + PyYAML
不导入 ai_core，不要求宿主机安装 LangGraph / Docker SDK
HTTP：urllib.request
导出包：tarfile 内存解析
断言：re（空白不敏感 literal 正则）
```

关键机制：

1. **API 模式自适应**
   - BFF（8080）：`/api/v1/supervisor/route`、`/api/v1/runs`、`/api/v1/sandboxes/{id}/export`、`DELETE /api/v1/sandboxes/{id}`；
   - 直连 AI Core（8100）：自动切换 `/api/v1/internal/...`。
2. **线程隔离**
   - 每条用例注入 `eval_<runTag>_<caseId>_<index>` 作为 `threadId`，长度 ≤ 64；
   - 避免固定 `annotationId` 命中上一轮 Redis / 进程内会话快照。
3. **真实代码断言**
   - 通过沙盒导出 tar.gz 读取真实 `src/App.jsx`；
   - 不用 Vite 预览地址 `/src/App.jsx`，因为 dev server 返回的是 `jsxDEV(...)` 转换后模块；
   - 导出失败才回退 `generatedFiles` / `generatedPatch`。
4. **空白不敏感匹配**
   - YAML 片段按空白切分后 `re.escape` 再用 `\s*` 连接，避免格式差异误报。
5. **资源清理**
   - 每条用例 `finally` 删除沙盒；支持 `--keep-sandboxes` 调试。

CLI：

```bash
python3 scripts/run_eval.py
python3 scripts/run_eval.py --filter case_001 --verbose
python3 scripts/run_eval.py --base-url http://127.0.0.1:8100
```

最近一次验收结果：

```text
10/10 PASS，通过率 100.0%，总耗时 35.72s
创建沙盒 6，清理沙盒 6
报告：docs/eval_report.md
```

> 注意：沙盒用例使用 `llmProvider=mock`，验证的是“路由契约 + 沙盒执行 + 源码落盘”，不是 DeepSeek 生成质量。

### 8.4 Admin 一键评测契约（仅预留）

`docs/design/03.API接口设计.md` §14.4.2 已冻结契约，但 BFF **尚未注册路由**：

```text
POST /api/v1/admin/eval/run
  → 202 + evalRunId
  ← suiteId / suiteVersion / engine(offline|sandbox) / filter / failFast
    llmProvider / promptVersionId / knowledgeSnapshotId / reason / idempotencyKey

GET  /api/v1/admin/eval/runs/{evalRunId}
GET  /api/v1/admin/eval/runs
GET  /api/v1/admin/eval/trend

状态机：QUEUED → RUNNING → SUCCEEDED | FAILED | CANCELLED
错误码：ADMIN_ROLE_REQUIRED / EVAL_SUITE_NOT_FOUND /
        EVAL_RUN_ALREADY_RUNNING / EVAL_ENGINE_UNAVAILABLE / EVAL_QUOTA_EXCEEDED
```

架构约束：**评测集是“考题”，知识库是“教材”**。评测用例、fixture、期望答案不得进入 RAG 语料；评测记录必须携带 `knowledgeSnapshotId`，与知识库版本解耦。

---

## 9. 协议、数据契约与状态机

### 9.1 通用协议

| 项 | 约定 |
|---|---|
| API 风格 | REST + JSON，字段 camelCase |
| 流式 | `text/event-stream`，`fetch` 读取，`afterSeq` 重放 |
| 请求隔离 | 服务端以 `threadId || annotationId` 作为会话标识 |
| 错误体 | `{code, message, requestId}` |
| 幂等 | 评测接口预留 `idempotencyKey`；Run 未引入通用幂等键 |
| 时间 | ISO-8601；管理看板按 `Asia/Shanghai` 展示 |

### 9.2 关键状态枚举

```text
Run:        RUNNING / SUCCEEDED / FAILED / CANCELLED / REJECTED
            AWAITING_APPROVAL / AWAITING_CLARIFICATION / AWAITING_CONTRACT
Supervisor: handoff_ui / handoff_architect / handoff_both / handoff_custom / rejected
Gate:       AWAITING_CONTRACT / RELEASING / RELEASED / FAILED
Eval:       QUEUED / RUNNING / SUCCEEDED / FAILED / CANCELLED（契约）
```

### 9.3 数据持久化现状

| 数据 | 当前存储 | 说明 |
|---|---|---|
| Run 事件 / 线程记忆 | 进程内 `RunEventBuffer` + LangGraph `MemorySaver` | 重启丢失 |
| Git checkpoint | 沙盒容器内 Git | 随沙盒销毁 |
| 管理指标 | BFF JVM 内存（最多 5000 Run） | 重启清零，多实例不共享 |
| 评测报告 | 文件系统 `reports/` / `docs/eval_report.md` | 已加入 `.gitignore` 的是 `reports/` |
| MySQL / Redis / MinIO | 已连接但本轮未新增业务表 / 键 / 桶 | M3.2 起逐步落地 |

---

## 10. 基础设施与部署

- 继续复用 `~/Workspace/infra` 的 `geo-mysql`、`geo-redis`、`geo-minio`，**没有新增中间件 Compose**；
- 项目 `docker-compose.yml` 只包含 `python-ai`、`java-bff`、`platform-web` 三个平台服务，属于基线已有；
- 沙盒容器由 Docker SDK 动态创建，不写进 Compose；
- 本轮联调使用两个临时实例：
  - Vite dev `5174`，`VITE_BFF_PROXY_TARGET=http://127.0.0.1:18080`；
  - BFF demo `18080`，`ADMIN_METRICS_DEMO_SEED=true`，宿主机 JDK 25 运行 Java 21 目标 jar；
- 正式容器内运行时仍为 Temurin 21 / Node 20 / Python 3.12；**不要把宿主机版本当成部署版本**。

### 10.1 本轮新增 / 重点环境变量

| 变量 | 默认值 | 用途 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 空 | 为空时全部走 Mock LLM |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
| `ARCHITECT_CONFIDENCE_THRESHOLD` | `0.85` | 契约置信度阈值 |
| `ARCHITECT_MAX_CLARIFICATION_ROUNDS` | `5` | 最大澄清轮次 |
| `ARCHITECT_CONTRACT_REPAIR_LIMIT` | `1` | 契约修复重试 |
| `ARCHITECT_RAG_PROVIDER` | `markdown` | `markdown` / `chroma`（未启用） |
| `ARCHITECT_KNOWLEDGE_DIR` | `knowledge/fullstack` | 契约知识目录 |
| `SUPERVISOR_CUSTOM_AGENTS_DIR` | `config/custom_agents` | 企业 Agent Manifest 目录 |
| `SKILLS_DIR` | `config/skills` | Skill 声明目录 |
| `TOOL_SHELL_ENABLED` | `false` | shell 工具总开关 |
| `TOOL_SHELL_TIMEOUT_SECONDS` | `10` | shell 超时 |
| `MCP_SERVERS` | `[]` | MCP 配置槽（未启用） |
| `IMPORT_MAX_ARCHIVE_BYTES` | `33554432` | 导入压缩包上限 |
| `IMPORT_MAX_UNCOMPRESSED_BYTES` | `134217728` | 解压后上限 |
| `IMPORT_MAX_ENTRIES` | `4000` | 压缩包条目上限 |
| `VISION_API_KEY` / `VISION_MODEL` | 空 / `gpt-4o` | 图片导入视觉模型 |
| `ADMIN_METRICS_DEMO_SEED` | `false` | 管理看板演示数据 |
| `ADMIN_METRICS_REFRESH_SECONDS` | `60` | 预留刷新配置 |
| `KNOWLEDGE_SERVICE_*` / `QDRANT_*` | — | M3 数据中台契约预留 |
| `PROMPT_REGISTRY_*` / `FEEDBACK_COLLECTOR_*` | — | M3.3 / M3.4 契约预留 |

---

## 11. 测试、构建与工程化

| 类型 | 工具 | 覆盖点 |
|---|---|---|
| Python 单测 | 标准库 `unittest`（容器内执行） | Editor、RunOptions、Architect、RAG、Supervisor、Eval Suite |
| Java 单测 | JUnit 5 + MockMvc | 管理指标 RBAC 与口径 |
| 前端构建 | `npm run build`（Vite） | JSX / 路由 / 样式 |
| E2E | Playwright Python Sync API + Chromium | 管理台、Supervisor、Architect、导入、空白 ADD、审批开关、浮层竞态、回滚 |
| 评测 | `eval_suite` + `scripts/run_eval.py` | 路由契约 + 真实沙盒代码断言 |
| 格式化 | Black | Python 代码；容器内临时安装，宿主机无需全局安装 |

### 11.1 本轮新增 E2E 脚本

```text
scripts/admin_console_e2e_test.py
scripts/architect_m2_5_e2e_test.py
scripts/architect_ui_e2e_test.py
scripts/blank_area_add_e2e_test.py
scripts/custom_agent_e2e_test.py
scripts/import_project_e2e_test.py
scripts/popover_close_race_e2e_test.py
scripts/settings_manual_approval_e2e_test.py
scripts/supervisor_e2e_test.py
scripts/supervisor_both_e2e_test.py
scripts/supervisor_mixed_ui_e2e_test.py
```

### 11.2 验证命令

```bash
# 依赖事实核对
cat apps/platform-web/package.json
cat apps/platform-bff/pom.xml
cat services/orchestrator/requirements.txt

# Java 单测
mvn -pl apps/platform-bff test

# 前端构建
cd apps/platform-web && npm run build

# Python 全量单测（容器内，避免宿主机缺 LangGraph / Docker SDK）
docker run --rm -v "$PWD/services/orchestrator:/app" -w /app \
  -e PYTHONPATH=. vibe-platform/python-ai:dev \
  python -m unittest discover -s tests -v

# 离线评测
./scripts/eval_suite.sh validate
./scripts/eval_suite.sh run

# 真实沙盒评测
python3 scripts/run_eval.py

# 管理台 E2E（需 5174 + 18080）
ADMIN_WEB_URL=http://127.0.0.1:5174/ \
  /tmp/vibe-browser/bin/python scripts/admin_console_e2e_test.py
```

---

## 12. 本轮“没有采用”的技术（避免误读）

| 未采用 | 原因 / 现状 |
|---|---|
| 图表库（ECharts / Recharts / D3） | 管理看板图表简单，纯 CSS/SVG 足够，避免新增前端依赖 |
| TypeScript | 前端基线为 JavaScript + JSX，本轮不迁移 |
| Tailwind / UI 组件库 | 继续使用原生 CSS 与现有设计变量 |
| Zustand / Redux / TanStack Query | Context + 自定义 Hook 足够；未引入全局状态库 |
| Spring Security / JWT | 管理 RBAC 仅 Mock Header，真实鉴权在 M3.2+ |
| Redis / MySQL 指标持久化 | 管理指标仍是单机内存 |
| Chroma / Qdrant / 向量检索 | 只有 `Markdown + BM25` 落地，Chroma 是占位，Qdrant 是架构预留 |
| Knowledge Service | 只有 API / 环境变量契约，无服务实现，也无可视化界面 |
| MCP 运行时 | 只有配置槽与挂载注释，默认关闭 |
| 跨会话长期记忆 / 用户画像 | 明确不在本轮引入，避免把一次性标注固化为偏好 |
| 真实大模型评测 | 真实沙盒评测仍用 `llmProvider=mock` |
| 大模型微调（SFT / LoRA / RLHF） | 架构约束禁止 |
| 新中间件 / 新 Docker Compose 基础设施 | 禁止重复部署，继续复用 `~/Workspace/infra` |
| CI 流水线 | 目前靠本地脚本与单测；JUnit XML 已具备接 CI 的能力 |

---

## 13. 已知风险与下一阶段

| 风险 | 影响 | 下一步 |
|---|---|---|
| 管理指标仅内存 | BFF 重启清零、多实例不一致 | M3.2 落 `run / run_event` 聚合表 |
| Mock RBAC | 任意客户端可伪造 `X-User-Role` | M3.2 接真实登录态与用户-租户-角色表 |
| Mock LLM 评测 | 不代表真实模型质量 | M3.3 接 Prompt Registry 与多采样 / 基线对比 |
| 会话快照复用 | 固定 `annotationId` 会污染评测 | 已强制每用例注入唯一 `threadId`，需保持 |
| Vite 预览源码被转换 | JSX 断言易误报 | 已改为沙盒 tar.gz 读取真实源码 |
| 宿主机版本漂移 | 本地 JDK 25 / Node 22 / Python 3.14 与容器 21 / 20 / 3.12 不同 | 交付验证以容器版本为准 |
| Eval / Knowledge 耦合风险 | 考题泄漏进 RAG 语料会虚高得分 | 架构层强制解耦，记录 `knowledgeSnapshotId` |
| 数据中台无 UI | 无法直接查看知识库 / 评测曲线 | M3 管理中台逐步补 `/knowledge`、`/eval` 页面 |

---

## 14. 相关文档

| 文档 | 内容 |
|---|---|
| [`TECH_STACK.md`](TECH_STACK.md) | 项目全量技术栈基线（区间内已增补 M2.5 / Agent OS 架构） |
| [`docs/design/02.整体架构设计.md`](docs/design/02.整体架构设计.md) | 整体架构、Admin Eval 契约、Eval / Knowledge 解耦 |
| [`docs/design/03.API接口设计.md`](docs/design/03.API接口设计.md) | API 契约，含 §14.4.1 管理指标、§14.4.2 Admin Eval |
| [`docs/design/05.Supervisor_Multi_Agent.md`](docs/design/05.Supervisor_Multi_Agent.md) | Supervisor 拓扑、状态机、Manifest、门控 |
| [`docs/design/06.Agent_OS_Data_Management.md`](docs/design/06.Agent_OS_Data_Management.md) | 数据中台 / 管理中台 / Eval 中心设计 |
| [`docs/design/07.Eval_Suite.md`](docs/design/07.Eval_Suite.md) | 双引擎评测架构、断言语义、CI 演进 |
| [`docs/eval_report.md`](docs/eval_report.md) | 最近一次真实沙盒评测报告 |
| [`docs/memory/2026-11-05_eval_real_sandbox.md`](docs/memory/2026-11-05_eval_real_sandbox.md) | 真实沙盒评测实现与两个关键调试结论 |
| [`docs/memory/2026-11-05_M3_1_admin_console_metrics.md`](docs/memory/2026-11-05_M3_1_admin_console_metrics.md) | 管理中台实现与验证记录 |

---

> **维护约定**：本文只覆盖 `d1ba02f..da2b2ff` 的增量。后续如继续演进，优先更新 `TECH_STACK.md` 的全量事实；本文保留为这一时间段的快照，避免“增量文档越写越像全量文档”。
