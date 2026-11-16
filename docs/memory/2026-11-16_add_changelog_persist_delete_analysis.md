# 2026-11-16 ADD_COMPONENT / ChangeLog 持久化 / 删除静态分析

## ADD_COMPONENT

- 白名单：Button / Input / Text / Card，全部使用原生 JSX，不引入第三方
  UI 库，避免绕过架构确认生成业务组件。
- `ai_core/direct_ops/templates.py`：
  - 每个模板声明 jsx、imports、node_count；
  - `ensure_imports()` 在最后一个 import 后确定性补全，已存在则跳过；
  - 当前模板无外部 import，但补全能力已实现并有单测。
- Babel `insert` 现在返回 `nodeCountBefore / nodeCountAfter`；
  Direct Ops 校验节点增量必须等于模板声明值，否则阻断写入。
- 服务端流程：解析目标容器 → L2 公共容器拦截 → insert → 节点增量校验
  → import 补全 → apply_patch → Git commit → ChangeLog。
- 前端 ➕ 打开白名单下拉，选择组件后直接插入所在容器末尾。

## ChangeLog 持久化

- 新增 `JsonlChangeLogStore`：
  - 默认 `data/changelog.jsonl`（容器内 data/ 已挂载到宿主机）；
  - 启动时加载，追加写 + flush；
  - 单行损坏跳过，不影响其它记录；
  - 落盘失败降级为内存，绝不阻断已成功的代码写入。
- 旧 `MemoryChangeLogStore` 保留给单元测试。
- 服务重启后实测：`store_count=1`、`architect_count=1`，
  Architect 仍能读取历史 Direct Op。

## DELETE_NODE 静态分析

- `resolve_target.mjs` 新增：
  - `inMap`：祖先链中存在 `.map(...)` 调用；
  - `isRouteRoot`：没有 JSX 祖先的顶层根节点；
  - `deleteSafe` / `deleteReasons`。
- `ai_core/direct_ops/static_analysis.py` 汇总共享组件、map、路由根
  三类风险；`POST /direct-ops/analyze-delete` 只分析不删除。
- 前端 ❌ 调用分析接口：安全则提示“可安全删除（执行待接入）”，
  不安全则展示拦截原因。
- 实测：
  - `<h1>` → safe=true；
  - `<main>` → safe=false，原因“页面顶层 JSX 根节点”。

## 验证

- Python 590 项 unittest 全绿；前端 16 项 Vitest 全绿；BFF 单测通过。
- 真实 ADD_COMPONENT：1959ms，文件插入 Button，commit
  `797baa9b`，ChangeLog 写入 `data/changelog.jsonl`。
- 重启 python-ai 后 ChangeLog 仍可被 Architect 读取。
- DELETE 静态分析直连与经 BFF 均返回正确判定。

## 剩余

- DELETE_NODE 执行尚未接入（仅前置分析）。
- ChangeLog 追加文件会持续增长，后续可按大小压缩。
- ADD 模板的缩进仍沿用既有 insert 行为，会多一个空白行；不影响
  解析与构建，留待与 M3 的 exec 合并优化一起处理。
