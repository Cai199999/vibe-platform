# vibe-platform · 当前状态简报

> 2026-11-18 · 历史细节见 docs/memory/

## 架构
React+Vite → Java21 BFF → Python3.12 FastAPI/LangGraph → Docker 沙盒；中间件复用 ~/Workspace/infra；六边形分层。

## 已闭环
- 画布/Direct Ops：圈选、拖拽、HITL、Diff、回滚；NodeIdentity/ScopePolicy、错误信封。
- 生成链路：Architect 契约、多 Agent、语义路由、RAG、审计门禁。
- 离线可视化：多页面 VisualDocument + 页面 Tab + 高保真/模拟预览 + 冻结/审查。
- 阶段三 CodeGen：Tailwind JSX + node-id/source；Babel 门禁；自带 inspect SDK 与 /api 代理；写回源码、v0 快照、ChangeLog。
- 存量只读投影：Babel 分析 → 导入 .jsx/.tsx 只读树；不转虚拟树。
- 双轨制：前后端分流；新建强制选从零/导入；空工作区可直达导入；Resolver 以 files/sandbox 事实判定（仅声明存量不再生效）；Fullstack Run 带 projectMode，存量禁止 frontend/fullstack CodeGen，仅后端增量；Architect 首屏切换为存量文案。
- 源码持久化：导入先 PUT BFF，MySQL 存元数据/文件树，MinIO 存正文；重开/清缓存从后端回灌，ReadOnly 投影可直接补拉。
- 写回闭环：Direct Ops Git commit 成功后自动把最新源码同步回 BFF，快照 revision 递增；同步失败不回滚代码，响应返回 sourceSync=FAILED。
- 两阶段：V0 只出虚拟树 → 画布 delta → BACKEND_BINDING；最终契约带冻结树。
- 审查门禁：FROZEN repeat 未绑定仅 warning；CodeGen 用示例数据；二次 Architect 全量审查交互/repeat+delta。
- 方案文档 01–05；质量分级一次；禁物理外键、分层。
- 源码级可视化：前端 Agent 语义工具复用 Direct Ops（文本/移动/增删/撤回），不 fork Utopia。
- 构建期身份：inspect_plugin / CodeGen 注入 data-oid + vibe-source-map.json；SDK 与 resolver oid 优先，旧 file/line 仅兜底，地图过期 fail-closed。
- 跨文件调用点：importerGraph + callSiteOid 双索引；UPDATE_PROPS 只改调用点 JSX（App.jsx），不触碰组件定义（UserCard.jsx）。
- 多调用点消歧：静态调用点按源码顺序 + 运行时实例序号自动匹配；仅限非 map / 非条件渲染，否则保持 HITL。
- 多实例：SDK 上报 instancePath（父 oid 链 + 同模板序号）；模板/重复实例修改返回 HITL_REQUIRED + 范围选项。
- 容器作用域：Source Map 标注 containerScope=local/component_definition/repeated_template/call_site_slot；local 与唯一调用点 children 插槽自动执行，组件定义/重复模板落点走 HITL。
- SourceSync 边界：删除文件从快照移除；失败任务落盘 JSON 队列，后台指数退避重试。
- 本地 CI：`scripts/ci_local.sh` 串行跑 Python 726 / 前端 68 + build / BFF 23；`RUN_E2E=1` 可追加浏览器 E2E。

## 最新验证
前端 68 项 Vitest；Python 727 项全通过；BFF 23 项 Maven 测试；浏览器 E2E PASS：OID 4/4、删除无 422、sourceSync 353ms、刷新回灌 3.6s；跨文件 Props E2E 命中 App.jsx 调用点、UserCard.jsx 未改；多实例/组件定义落点返回 HITL 范围选择。

## 真假边界
DeepSeek 真实调用；鉴权 Mock；BFF 仅源码快照持久化（Run/指标仍无持久化）；沙盒无 gVisor；未云端部署。

## 下一步
P0：多调用点运行时消歧、rename/add 全量同步、身份模型正式文档；P1：class/style、wrap、预览/构建验证；存量 Phase 2；Redis/云端。
