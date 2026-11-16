# 2026-11-16 NodeIdentity 与 ScopePolicy

## NodeIdentity 协议

新增 `ai_core/domain/node_identity.py`：

- kind：leaf / container / map_item / component_root / route_root / unknown
- 事实字段：file/line/column、instance、instanceCount、callSiteFile、
  importerCount、inMap、isRouteRoot、isShared、isProtected、
  hasCallSite、allowedInsertPositions、range
- `editable_scope`：
  - map_item → data_source
  - route_root → replace_root
  - callSite → instance
  - 同源码多实例且非 import → all_instances
  - component_root → definition
  - 其它 → instance
- SDK readSource 增加 `identity` 基础事实，经前端 payload 传给 Resolver。

## ScopePolicy

新增 `ai_core/direct_ops/scope_policy.py`：

- DELETE：
  - 普通 / callSite / 同源码多实例 → 直接执行
  - map item → HITL：删除数据项(AI) / 仅视觉隐藏 / 取消
  - route_root → replace_placeholder 策略
  - import 公共组件定义 → HITL：分离实例 / 改定义 / 取消
- MOVE：
  - 普通 / callSite / 同源码多实例 → 直接执行
  - map item → 数据源排序 HITL
  - route_root → 建议先包裹容器
  - import 定义 → HITL
- UPDATE_TEXT：
  - map item → 数据源/视觉覆盖 HITL
  - 公共定义无 callSite → HITL
- ADD_COMPONENT：
  - map 容器 → 数据源新增 HITL
  - 公共容器 → 分离/定义 HITL

## 执行链路

- `DirectOperationService` 现在先生成 NodeIdentity，再由 ScopePolicy 决定
  allowed / scope / strategy。
- 根节点删除不再报错：Babel 使用 `modify` 替换为
  `<div className="empty-placeholder">空节点</div>`，保持 JSX 合法。
- analyze-delete 返回 identity / scope / strategy / options / safe。
- 普通可控操作不再走旧的 `_risk` 启发式拦截。

## 验证

- 新增 NodeIdentity / ScopePolicy 单测：叶子、map item、route root、
  多实例移动。
- Python 620 项 unittest 全绿；前端 16 项 Vitest、构建通过。
- sandbox / python-ai 镜像已重建。

## 尚未完成

- 前端尚未把 ScopePolicy 返回的 HITL options 渲染成选择面板；
  当前 map/item 仍只展示拦截原因。
- OperationExecutor 仍集中在 DirectOperationService，尚未按动作拆分。
- WorkspacePage / SDK 编排拆分尚未开始。
