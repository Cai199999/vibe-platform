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
- 写回闭环：Direct Ops Git commit 成功后自动同步快照；sourceSync 异步 + 持久化队列，commit 仍同步完成。Git add/commit/rev-parse 合并为单次容器执行。失败不回滚代码，队列后台重试。
- 删除快路径：安全叶子点击即乐观删除 + 左下角撤回；共享/循环/定义节点仍先确认再 execute，HITL/失败自动恢复。
- 交互规范：`docs/design/15_交互语义与节点身份规范.md` 冻结身份优先级、L0-L3 分级与操作矩阵。
- 两阶段：V0 只出虚拟树 → 画布 delta → BACKEND_BINDING；最终契约带冻结树。
- 审查门禁：FROZEN repeat 未绑定仅 warning；CodeGen 用示例数据；二次 Architect 全量审查交互/repeat+delta。
- 方案文档 01–05；质量分级一次；禁物理外键、分层。
- 源码级可视化：前端 Agent 语义工具复用 Direct Ops（文本/移动/增删/撤回），不 fork Utopia。
- 构建期身份：inspect_plugin / CodeGen 注入 data-oid + vibe-source-map.json；SDK 与 resolver oid 优先，旧 file/line 仅兜底，地图过期 fail-closed。
- 跨文件调用点：importerGraph + callSiteOid 双索引；UPDATE_PROPS 只改调用点 JSX（App.jsx），不触碰组件定义（UserCard.jsx）。
- 高级编辑只走前端 Agent 工具：`update_props` / `update_class` / `update_style`；画布不再暴露 ⚙/🎨/🖌 按钮。
- 低级移动落位使用 manifest.childOids 的 AST 兄弟序号，不再按 DOM children 猜；DOM/AST 不一致时回退。SDK 回传 parentOid，服务端也可从 `[data-oid]` 选择器回补，修复“移动成功但没动 / 找不到落点”。
- 多调用点消歧：优先 React dev `_debugSource` 精确定位；无 Fiber 时按源码顺序 + 运行时实例序号，仍不确定保持 HITL。
- 多实例：SDK 上报 instancePath（父 oid 链 + 同模板序号）；模板/重复实例修改返回 HITL_REQUIRED + 范围选项。
- 数据源删除：静态数组（字面量/同文件变量）可确定性删除对应元素；动态数据仍明确 HANDOFF_TO_AI。
- 容器作用域：Source Map 标注 containerScope=local/component_definition/repeated_template/call_site_slot；local 与 call_site_slot 直接落位，跨文件仍拒绝。仅被 `src/main.*` / `src/index.*` 渲染的入口组件按 local（页面根）处理，存量项目拖拽不再误触 HITL。
- 低级操作不转 Agent：父容器删除/移动连同子树直接执行；共享/循环/多实例不再 handoff，由 SDK 悬停高亮全部子节点提示影响范围。
- SourceSync 边界：删除文件从快照移除；失败任务落盘 JSON 队列，后台指数退避重试。
- 本地 CI：`scripts/ci_local.sh` 串行跑 Python 739 / 前端 68 + build / BFF 23；`RUN_E2E=1` 可追加浏览器 E2E。
- 终极路线：`docs/design/16_平台终极目标与路线图.md` 固化六维目标；M1 闭环进行中。
- 拖拽容错：落点出容器/缝隙时按 48px 几何距离吸附最近容器，不再直接“未能定位父容器”。
- 旧工程兼容：SDK 支持 `file:line` / `file:line:column` 两段与三段锚点，缺失时向上找源码祖先；页面根兜底取真实容器，不再报“标注缺少目标文件”。
- 拖拽实时预览：拖动中按指针求候选父容器与 AST 视觉缝隙，画虚线框 + 插入线；松手复用同一计算，所见即所写。

## 最新验证
前端 68 项 Vitest；Python 739 项全通过；BFF 23 项 Maven 测试；浏览器 E2E PASS：OID 4/4、删除预览消失 257ms、刷新回灌 3.9s、跨文件 Props 命中 App.jsx 调用点且 UserCard.jsx 未改、Direct MOVE 由真实 SDK 拖段落算出 siblingIndex=2，源码与预览同步重排；拖动中候选父容器框与插入线几何断言通过，松手后提示清零。

## 真假边界
DeepSeek 真实调用；鉴权 Mock；BFF 仅源码快照持久化（Run/指标仍无持久化）；沙盒无 gVisor；未云端部署。

## 下一步
P0：多调用点运行时消歧、rename/add 全量同步、身份模型正式文档；P1：class/style、wrap、预览/构建验证；存量 Phase 2；Redis/云端。
