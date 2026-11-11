# vibe-platform · 2026-11-11 技术总结

> 主题：从真实部署事故止血，到责任隔离、缺陷归因、LLM 自愈与可观测性闭环。
>
> 本文记录今日实际使用的技术栈、遇到的问题、根因分析与解决方案，便于后续复盘和交接。

---

## 1. 今日技术栈

### 1.1 前端

- React 18 + Vite 5
- 原生 JavaScript / CSS，无额外组件库
- SSE 消费与后台 Run 状态同步
- Admin 看板 CSS 进度条、KPI 卡片、轮询刷新
- 设计约束：状态颜色固定、指标来源可追溯、监控失败独立降级

### 1.2 Java BFF

- Java 21 / Spring Boot 3.3.5
- `RestClient` 短超时客户端：健康检查、指标透传
- `RestClient` 长超时客户端：全栈 Run 同步执行
- `WebClient`：SSE 流式代理
- `@IsAdmin` + `AdminAuthorizationAspect`：管理端 RBAC 切面
- 统一错误结构：`code / message / requestId`
- 测试：Spring `@WebMvcTest` + MockMvc + 可编程 Stub Bean

### 1.3 Python AI Core

- Python 3.12 + FastAPI + Uvicorn
- LangGraph：全栈生成多节点编排
- Pydantic：契约、请求、诊断结构化校验
- 六边形分层：`domain / ports / adapters / fullstack / architect`
- DeepSeek Chat：通过 `ModelClient` 端口调用，支持任务级模型路由
- 事件机制：SSE + `RunEventBuffer`

### 1.4 生成代码运行时

- Express + Sequelize
- SQLite：沙盒零依赖预览
- MySQL / PostgreSQL：生产目标，通过 `DATABASE_URL` 注入
- Node 22 ES Module
- 迁移脚本 `server/migrate.mjs`，应用启动不自动 `sync()`

### 1.5 代码质量与安全门禁

- Babel Parser：`server/db.js` / `server/index.js` AST 静态校验
- 自研 `schema_ast_check.mjs`：静态求值常量、拼接、`JSON.parse`
- SQLite MCP：真实执行 DDL 并查询 `sqlite_master` / `PRAGMA table_info`
- `Applier`：所有 `server/` 产物落盘前的唯一绝对门禁
- `contract_audit`：契约自洽性审计与缺陷归因
- `black`：Python 格式化
- `unittest`：Python 测试
- Maven Surefire：Java 测试
- `vite build`：前端生产构建

### 1.6 基础设施

- Docker Compose 管理应用容器
- Docker Socket 创建隔离沙盒容器
- 复用 `~/Workspace/infra` 常驻 MySQL / Redis / MinIO
- 不重复部署中间件
- `.env` 注入密钥，禁止提交真实凭据

---

## 2. 核心架构链路

```text
React / Vite
    │
    ▼
Java BFF（鉴权、SSE 代理、Admin 指标透传）
    │
    ▼
Python AI Core（LangGraph）
    │
    ├─ Architect：澄清 → 契约 → 校验 → 子资源/主从表声明
    ├─ DBACoder：DDL → schema.core.json → Sequelize ORM / 迁移
    ├─ BackendCoder：模板优先 → Applier 拦截后 LLM 带诊断修复
    ├─ FrontendCoder：React 页面与 API 交互
    ├─ InfraCoder：Dockerfile / 部署配置
    ├─ ConsistencyVerifier：路由 / ORM / JOIN / SQLite 结构验收
    ├─ BuildVerifier：install + build
    └─ SandboxRestart：健康检查与预览
    │
    ▼
Docker 沙盒（Express + Sequelize + SQLite/MySQL）
```

---

## 3. 今日遇到的问题与解决方案

### 3.1 真实部署后 `500 Unknown column 'name'`

**现象**

- `GET/POST /api/v1/resumes` 返回 500。
- `/api/health` 仍返回 200，健康检查完全没有发现业务表字段错误。

**根因**

1. 多表 DDL 被当作单表解析，`users` 与 `resumes` 的列发生串扰。
2. 生成器存在 `users` / `name` 兜底逻辑，契约缺字段时会静默生成错误 ORM。
3. 生成期只检查“代码能生成”，没有检查 ORM 字段是否真实存在于迁移 DDL。
4. 健康检查只探测进程存活，没有探测业务 CRUD。

**解决方案**

- `parse_ddl_tables` 改为括号配对解析，保证每张表字段完全隔离。
- `core_table_for_contract` 按 API 集合资源选择核心业务表，排除 `users` / sessions 等系统表。
- 删除 `users` + `name` 硬编码兜底；缺失 DDL 时使用明确的 `app_records` 占位结构。
- DBACoder 产出 `server/schema.core.json`，BackendCoder 强制读取并绑定。
- 写盘前断言 ORM / 迁移 / 契约字段一致。
- 根据 NOT NULL / DEFAULT / UNIQUE 生成默认值，避免 `Column 'content' cannot be null`。
- ConsistencyVerifier 新增 ORM ↔ 迁移字段比对，字段漂移自动回退 DBACoder，最多 2 次。
- 验收脚本增加静态 ORM/DDL 对比，以及“核心表 GET/POST 不因缺字段 500”运行时断言。

**结果**

- SQLite CRUD、MySQL 迁移、直连查行、重复发布幂等全部通过。
- 同类问题从“运行期事故”前移为“生成期 / 构建期拦截”。

---

### 3.2 多实例组件重复、全局样式污染

**现象**

- 同一组件多实例修改时，补丁命中所有实例。
- 修改一个按钮导致全局样式被污染。

**根因**

- Coder 只看到组件名，没有实例身份。
- Applier 只校验语法，没有校验“多实例补丁必须带身份判断”。

**解决方案**

- SDK 从 React Fiber 提取 `instanceKey / instanceProps / instanceId / callSite`。
- 实例身份贯穿 Coder 上下文与 Diff 生成。
- Applier 新增 `INSTANCE_GUARD_MISSING`：
  - 多实例补丁必须在 callSite 或根组件源码写入真实身份比较；
  - 禁止无条件写进 `map` 返回体；
  - Diff 模式同样校验新增行。
- 基础组件修改直接打回 Coder，避免跨实例污染。

---

### 3.3 正则门禁漏判 ORM / DDL 不一致

**现象**

- 早期用正则检查 Sequelize 字段，遇到常量、字符串拼接、`JSON.parse` 就会漏判。
- 误判会直接导致生产 500。

**根因**

- 正则无法理解 JavaScript 语义，也无法识别运行期动态修改。

**解决方案**

- 引入 Babel Parser，新增 `schema_ast_check.mjs`。
- 静态求值常量、字符串拼接、`JSON.parse`。
- 阻断 `COLUMN_DEFS.push` 等运行期变更。
- Python 封装 `schema_ast.py`。
- AST 校验器不可用时直接硬失败，禁止回退正则。
- 生成期、ConsistencyVerifier、验收脚本共用同一套 AST 引擎。

---

### 3.4 主从表关系只靠隐式推断

**现象**

- `users → resumes` 这类主从关系无法稳定生成：
  - 子表缺失；
  - 外键列猜错；
  - 嵌套路由与 `include` 别名不一致。

**解决方案**

- 契约新增 `relations`，支持显式声明，也兼容 `<parent>_id` 推导。
- DBA 生成 `RELATED_MODELS` / `RELATIONS`。
- Backend 生成 `hasMany` / `belongsTo`、`include + separate` 预加载与嵌套子资源路由。
- AST 门禁扩展校验关系两端、外键列、JOIN 别名。
- 关联系统表的敏感列自动剥离。

---

### 3.5 契约缺陷被误当成代码缺陷反复重试

**现象**

- 契约本身缺表、缺字段、缺 JOIN 时，系统仍然反复重跑 BackendCoder。
- 同一输入必然得到同一错误，浪费模型调用与构建时间。

**解决方案**

- 新增 `contract_audit.py`，做确定性审计：
  - 缺 DDL / 核心表；
  - 集合路由没有对应表；
  - 非法关系；
  - 嵌套路由缺子表 / 缺 JOIN；
  - 未声明的交互路由。
- 缺陷归因：
  - `CONTRACT_DEFECT`：立即停止，要求修改契约，不浪费代码重试；
  - `CODE_DEFECT`：只重跑 BackendCoder，最多 2 次。
- 统一错误码 `CONTRACT_REVISION_REQUIRED`。
- 错误载荷携带 `failureKind=CONTRACT`、`defectKind`、`needsClarification`、`clarification`。
- 前端据此引导 `REVISE`。

---

### 3.6 确定性模板重试必然产生同一结果

**现象**

- 模板是确定性的，同一输入重试不会产生新代码。
- `CODE_DEFECT` 虽然被正确归因，但“自愈”没有真实收益。

**解决方案**

新增 `LLMBackendCoder`：

- 模板仍是默认生成路径，保证快速、稳定、可离线。
- 仅当 Applier 判定 `CODE_DEFECT`，或 ConsistencyVerifier 要求重建 JOIN 时，才调用 LLM。
- 把 Applier 的明确诊断原文传入 Prompt，例如：
  - `嵌套路由引用了不存在的 JOIN 别名 resumez`
  - `include 别名必须与 RELATIONS 的 as 完全一致`
- Prompt 同时包含完整失败文件、`schema.core.json`、DDL、契约路由、显式 `relations`。
- 要求模型输出完整文件，而不是 diff 或片段。
- LLM 失败时回退模板，不能把环境失败伪装成修复成功。
- 事件 `fullstack.applier.llm_repair` 记录 `SUCCEEDED / SKIPPED / FAILED`。

---

### 3.7 LLM 可能越权修改 DDL / ORM / 迁移

**风险**

- 让模型“自由修后端”可能改表名、字段、迁移甚至路由清单。
- 数据库结构事实必须唯一来自 DBACoder 与契约。

**解决方案**

- LLM 只允许修改 `server/index.js`。
- 明确禁止输出：
  - `server/db.js`
  - `server/migrations/**`
  - `server/schema.core.json`
  - `server/api-routes.json`
  - `package.json`
- 输出强校验：
  - 必须是 JSON 对象；
  - 路径必须 `posixpath.normpath` 后命中白名单；
  - 拒绝空内容、超过 512KB、非 Express 文件；
  - 必须包含 `server/index.js`。
- 即使 LLM 输出合法，也仍由 Applier 重新执行 Babel AST 门禁，不存在“LLM 生成即豁免”。

---

### 3.8 监控指标只存在于后端，无法看到缺陷拦截效果

**问题**

- 四个核心指标只有 Python 内部接口，Admin 看板看不到。
- 如果指标接口故障却返回全零数据，会被误判为“系统健康”。

**解决方案**

- Java BFF 新增 `GET /api/v1/admin/fullstack/metrics`，透传 AI Core 指标并转 camelCase。
- 上游失败显式返回 `502 AI_CORE_METRICS_UNAVAILABLE`，禁止伪造健康数据。
- Admin 新增“系统健康与缺陷拦截”卡片：
  - Applier 拦截率进度条；
  - 契约缺陷误重试次数；
  - 多表 AST 通过率；
  - JOIN / 其他 ORM 拦截明细。
- 前端与主看板并行请求，使用 `Promise.allSettled`：
  - 健康接口失败只降级卡片；
  - 不影响主指标和 Run 列表。

---

### 3.9 嵌套子资源契约靠隐式推断

**风险**

- `/users/{user_id}/profile` 这类路由如果没有独立子表，Coder 会猜表名和外键。
- 结果通常是悬空路由、JOIN 失败或运行时 500。

**解决方案**

Architect 起草与修订 Prompt 同时加入铁律：

- 存在嵌套子资源路由时，必须在 `databaseDdl` 中显式创建对应子表；
- 必须在 `relations` 中声明父表、子表、外键；
- 禁止让 Coder 猜表名或外键列；
- 没有独立子表时，改为父资源详情查询，不得生成悬空嵌套路由。

---

### 3.10 Java 25 下 `@MockBean` 失败

**现象**

```text
Java 25 (69) is not supported by the current version of Byte Buddy
which officially supports Java 23 (67)
```

**根因**

- 测试运行环境是 Java 25，Mockito / Byte Buddy 版本不支持。

**解决方案**

- 不使用 `@MockBean`。
- 在 `@TestConfiguration` 中提供可编程 Stub Service：
  - 可设置返回数据；
  - 可设置抛出 502 异常；
  - 每个测试前重置状态。

---

### 3.11 容器内测试环境问题

**现象**

- 运行容器内没有 `pytest`。
- `tests/` 没有打进运行镜像。
- 直接挂载源码后，入口脚本降权导致临时目录不可写。
- MCP 测试依赖 `/app/scripts`，临时容器没挂载会失败。

**解决方案**

- 用 unittest 代替 pytest。
- 临时容器挂载 `services/orchestrator` 到 `/app`。
- 使用 `--entrypoint python` 绕过入口降权脚本。
- 额外只读挂载项目根目录 `scripts/` 到 `/app/scripts`。
- `black` 不在镜像内时，使用临时虚拟环境 `/tmp/blackenv`。

---

### 3.12 GitHub 推送缺少凭据

**现象**

```text
fatal: could not read Username for 'https://github.com'
```

**根因**

- 当前 Shell 是非交互环境，没有 GitHub Token、Credential Helper 或 SSH Key。

**解决方案**

- 打开项目目录交互式终端，执行 `git push origin main` 并完成认证。
- 或在 Claude Code 会话中使用 `! git push origin main`。

---

## 4. 今日验证结果

| 验证项 | 结果 |
|---|---|
| Python 全量单测 | 440 项通过，仅排除历史 `tests.test_eval_suite` |
| 新增 LLM BackendCoder 单测 | 6 项通过 |
| 新增 Graph E2E | 模板连续错误 → LLM 修复 → Applier 复检通过 |
| LLM 降级测试 | LLM 失败 → 回退模板 → Applier 连续拦截并报错 |
| Consistency JOIN 反馈 | 通过注入一致性报告验证反馈链路 |
| Architect 子资源 Prompt | 起草 / 修订双路径断言通过 |
| Java BFF 测试 | 9 项通过 |
| 前端生产构建 | `vite build` 通过 |
| 运行态验证 | Admin 指标接口 200，匿名访问 403 |
| 镜像重建 | `python-ai` / `java-bff` 重建并健康启动 |

---

## 5. 今日提交

- `36ef673`：实例解耦，解决重复按钮 / 全局样式污染
- `d0e396d`：Unknown column 永久止血，ORM/DDL 生成期与构建期门禁
- `e5aeb41`：ORM/DDL 门禁改用 Babel AST
- `23cbdc1`：主从表契约与契约缺陷 / 代码缺陷分流
- `9f8fcbd`：Applier 绝对门禁、缺陷归因与全栈核心指标
- `7606a67`：带诊断反馈的 LLM BackendCoder、监控看板、子资源契约铁律

---

## 6. 经验总结

1. **健康检查不能只探进程存活**：必须覆盖核心业务 CRUD，否则 500 会被 200 掩盖。
2. **结构事实必须有唯一来源**：表名与字段只来自 DDL 和 `schema.core.json`，禁止生成器猜。
3. **门禁必须放在落盘前**：Applier 是统一入口，任何 Coder 都不能绕过。
4. **归因先于重试**：契约缺陷重试代码是纯浪费，必须先区分 `CONTRACT_DEFECT` 与 `CODE_DEFECT`。
5. **LLM 只做它擅长的部分**：让它修 HTTP 编排层，数据库结构仍由确定性组件持有。
6. **LLM 产物必须二次门禁**：模型输出不是可信输入，AST 校验不能豁免。
7. **监控失败要显式失败**：指标接口不可用时返回 502，不能用全零数据伪装健康。
8. **静态门禁优于运行期救火**：能在生成期 / 构建期拦住的问题，不要留到用户点击时。

---

## 7. 当前边界与下一步

当前边界：

- 鉴权仍为 Mock Header，未接入真实 JWT / OIDC。
- BFF 指标为进程内聚合，服务重启归零。
- 未真实部署云端。
- 沙盒未使用 gVisor 等强隔离方案。
- LLM 自愈只覆盖 `server/index.js`；模板在产出任何文件前直接抛异常时，仍按生成失败上报。

下一步建议：

- Run / 指标落库，支持重启后聚合。
- 接入真实鉴权与租户隔离。
- 增加 LLM 修复成功率、P95 修复耗时指标。
- 将 Applier 门禁接入 CI 阻断。
- 扩展真云端部署与多服务编排验证。
