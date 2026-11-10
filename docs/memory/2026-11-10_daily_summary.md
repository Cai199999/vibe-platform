# 2026-11-10 · 当日工作与技术栈变更

> 当日 12 个本地 commit：`9c6479d` → `7814d13`。未推送远程。
> 详细实现见同日各专题 memory；本文只做当天总览。

## 1. 当日完成的工作

### 1.1 稳定性与真实模型基线

- 修复前端卡顿：SSE 100ms 合并提交、组件 memo、非激活 Tab 卸载 iframe、
  runStates / Architect 800ms 防抖、移除页面级计时器。
- 接入真实 DeepSeek Eval 基线：10 例 7 过（70%），57s，
  26,223 tokens / ¥0.050088；新增每节点 usage 采集与 `LLM_PRICING` 成本计算。
- Coder / Planner 校验失败回传原始输出与错误重试（最多 2 次）。
- 防脑补骨架：抽象需求在 `skeleton_gate` 确定性拦截，不调用 LLM，
  只生成 Header + Sidebar + Main + 唯一占位。

### 1.2 画布与交互闭环

- 六项体验断层修复：契约确认后对话栏常驻、Inspect 可用、全栈前后端共用
  `collectionPath`、侧边栏真实切换、拖拽痕只在画布、移动抽象为 Agent Tool。
- 画布固定 1440×900 逻辑视口 + 原生滚动；点击页面/画布切换焦点，
  滚轮分别滚动页面/缩放画布；双屏 scroll-to 与点击重放同步 React 状态。
- 直接拖拽静默同步：SDK 接管 pointer 手势，500ms 防抖合并为 `MOVE`，
  后端只写 `transform: translate3d`，失败/超时回滚并 Toast。
- Architect 面板收尾：移除双 Tab，统一时间轴；正式推进与只读提问分离；
  全栈 SUCCEEDED 且一致性通过后成功卡一键导出 tar.gz。

### 1.3 全栈生成与一致性

- 企业级五项升级：画布 flex 链与自适应缩放、AgentBuilder、知识库上传、
  Architect 强制交互澄清、前端真实 Loading/Error/Success。
- BackendCoder 为契约每个业务动作注册独立 Express 路由，不再折叠为通用 CRUD。
- 新增 ConsistencyVerifier：比对前端 Method/Path/Payload 与后端路由，
  缺失时回传 BackendCoder 自愈最多 2 次，耗尽才阻断。
- 全栈产物验收脚本：生成 → 导出 tar.gz → 安全解压 → 部署三件套 →
  前后端路由比对 → 纯净容器 npm build → docker build → 无 DB env SQLite CRUD。
- 修复 Dockerfile 权限 Bug：保留 `server/` 层级，SQLite 落到可写的
  `/app/data`，消除 `EACCES: permission denied, mkdir '/data'`。

### 1.4 业务语义与评测

- 路由解析加固：Express `app.use` 二级路由、`route().get().post()`；
  前端 fetch / axios / 自定义 client / 路径常量 / 模板字符串。
- `check_business_semantics()`：契约路由必须真实注册、DDL 表必须落盘、
  上传契约必须有 multer 类中间件与文件字段、有 API 交互必须有成功态。
- 新增 `config/eval_suite/business_cases.yaml` 与 `scripts/business_eval.py`：
  4 条真实 DeepSeek 用例，区分业务通过率 / 契约理解准确率 / 产物语义通过率。
- 首次运行发现真实产物缺陷：契约要求上传但生成后端没有 multer；
  修复为按需注入 `multer@2.5.0`，并在 BuildVerifier 前安装根项目与
  `server/` 依赖，避免沙盒重启 `ERR_MODULE_NOT_FOUND`。

## 2. 当日评测结果

| 项目 | 结果 |
|---|---|
| Business Eval | 业务 4/4、契约理解 4/4、产物语义 3/3 = 100% |
| 业务 Eval 成本 | 36,586 tokens / ¥0.102416 / 123.5s |
| 全栈产物验收 | PASS：导出 69.8KB、路由一致、npm/docker build、SQLite CRUD |
| 全栈单测 | 28 passed（含 multer 用例） |
| 全量 Python 单测 | 281 个用例 |
| 真实 DeepSeek 基线（Supervisor / UI） | 10 例 7 过 = 70% |

## 3. 技术栈变更

### 新增 / 升级

- 沙盒镜像：`node:20-alpine` → `node:22-alpine`，使用内置 `node:sqlite`。
- 生成后端：Express 4.21.2 + Sequelize 6.37.5 + mysql2 3.11.5 / pg 8.13.1。
- 按需上传：multer 2.5.0，磁盘存储、5MB 限制、`/uploads` 静态目录。
- 生成部署：Dockerfile + render.yaml + `.env.example` + `DEPLOY.md`。
- 评测：YAML 业务用例 + 真实 DeepSeek Architect SSE + 契约/产物双层断言。
- 路由一致性：LangGraph `ConsistencyVerifier` 自愈环（≤2 次）。
- 成本可观测：节点 usage 聚合、`LLM_PRICING`、SSE 下发 tokens/cost。
- 验收：tar.gz 安全解压、Docker-in-Docker 构建、无 DB env 运行。

### 仍为边界

- 生成的 Express 与 Vite 运行在同一沙盒容器，不是 web/api/db 多服务。
- 未生成 Spring Boot；生产 MySQL 仅按 `DATABASE_URL` 切换，验收未连真实库。
- 业务 Eval 只有 4 例静态语义；上传尚未真实发送 multipart 请求。
- Redis checkpoint、真实鉴权、BFF 持久化、MinIO 落盘仍未实现。

## 4. 相关 memory

- `2026-11-10_frontend_perf_deepseek_baseline.md`
- `2026-11-10_anti_hallucination_skeleton.md`
- `2026-11-10_enterprise_platform_upgrade.md`
- `2026-11-10_ux_continuity_canvas_move.md`
- `2026-11-10_phase_b_canvas_sync_helper.md`
- `2026-11-10_direct_drag_silent_sync.md`
- `2026-11-10_fullstack_semantic_routes_self_heal.md`
- `2026-11-10_architect_ux_export_unified_chat.md`
- `2026-11-10_fullstack_acceptance_script.md`
- `2026-11-10_business_eval.md`
