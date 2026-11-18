# 14 · 源码级可视化编辑（Onlook 工程模型 + Utopia 不变量）

> 2026-11-17 · 不 fork Utopia；复用 Direct Ops；复杂操作工具化

## 1. 决策

```text
不 fork Utopia，不复刻它的编辑器运行时。
学习 Utopia 的核心不变量：
  源码唯一事实源 / 稳定节点身份 / 语义命令层 / 命令可回放。

工程模型采用 Onlook：
  iframe 运行应用 + 构建期注入节点 ID
  + DOM ↔ AST 映射 + Babel 回写源码。

实现复用现有 Direct Ops：
  目标解析 / ScopePolicy / revision 锁 / AST Gate / ChangeLog / undo。
```

## 2. 不做什么

- 不把 React 源码搬到编辑器自己的 React 树里执行；
- 不 monkey-patch `React.createElement`；
- 不引入 Utopia 的 TypeScript parser-printer / VSCode / 完整布局策略；
- 不让前端 Agent 直接写文件、拼接 JSX、操作 anchorRange；
- 不做 VisualDocument 与源码双写。

## 3. 分层

```text
L0 执行器（内部，不对 Agent 暴露）
  DirectOperationService
  apply_ast_edit.mjs
  resolve_target.mjs
  AST Gate / ScopePolicy / revision / Git checkpoint

L1 语义工具（前端 Agent 可调用）
  update_text / move_node / insert_component / delete_node
  get_frontend_revision / undo_last_frontend_change

L2 工作流工具（后续）
  preview_change / verify_build / checkpoint / request_human_decision

L3 只读上下文（后续）
  inspect_node / find_nodes / get_design_tokens / get_component_catalog
```

L1 工具是 `DirectOperationService` 的门面，不新增执行器。新增能力必须
先落到 Direct Ops，再由工具层暴露。

沙盒内联 UI（`inspect_sdk.js`）已提供 hover 高亮、悬浮 ❌ 删除、
内联 ➕ 新增、双击文本编辑与拖拽；Visual CodeGen 必须把这份 SDK
写入生成工程并在 `index.html` 注入，否则选择按钮会静默失效。

## 4. 现有复用映射

| 能力 | 现有实现 | 工具层复用方式 |
|---|---|---|
| 目标解析 | `resolve_target.mjs` + `SandboxProvider.resolve_targets` | 传 `node_id` / `selector` / `file:line` |
| 文本修改 | `UPDATE_TEXT` | `update_text` |
| 移动 | `MOVE_NODE` + 同父/容器落位 | `move_node` |
| 新增基础组件 | `ADD_COMPONENT` + 模板白名单 | `insert_component` |
| 删除 / 占位替换 | `DELETE_NODE` + ScopePolicy | `delete_node` |
| 并发控制 | `baseRevision` + 项目写锁 | 自动读取或显式传入 revision |
| 回滚 | Git checkpoint + ChangeLog | `undo_last_frontend_change` |
| 结构校验 | Babel AST / 节点数量 / 同构校验 | 工具层不重复实现 |
| 错误信封 | `DirectActionError` + `ErrorEnvelope` | 统一返回 `ok/code/message/requiresHuman` |

## 5. 身份模型

```text
data-oid       构建期稳定源码身份（inspect_plugin / CodeGen 注入）
vibe-source-map.json  oid -> {file,start,end,line,column,parentOid,container,...}
callSiteOid    组件调用点身份（不注入 props，只写 importer graph）
importerGraph  component -> {definitionOid, callSites:[{callSiteOid,file,...}]}
data-node-id   VisualDocument 逻辑节点 ID（CodeGen 同时注入）
data-source    file:line:column，仅作旧项目兜底 / 调试
instancePath   运行时父 oid 链 + 同模板序号（SDK 已上报）
```

约束：

- 写操作主键是 `data-oid`（旧项目可用 `data-node-id`），不是行列；
- `data-source` 在编辑后可能失效，只用于投影与兜底；
- 构建期插件按「文件 + 组件 + JSX 结构路径」生成 oid，行号只写进地图；
- resolver 优先 oid → 地图区间；地图与 AST 不一致时 fail-closed，不再猜；
- 同一源码多实例、循环项、共享组件继续走 ScopePolicy / HITL。

## 6. 前端 Agent 工具契约

所有 L1 工具返回 JSON：

```json
{
  "ok": true,
  "status": "SUCCEEDED",
  "risk": "L0_DIRECT",
  "resolvedTarget": {},
  "revisionBefore": "",
  "revisionAfter": "",
  "changeId": "",
  "warnings": []
}
```

失败时：

```json
{
  "ok": false,
  "code": "DIRECT_UNSAFE_OPERATION",
  "message": "",
  "diagnostic": {},
  "requiresHuman": true,
  "retryable": false
}
```

Agent 只能根据 `code` / `requiresHuman` / `retryable` 决定重试、换工具
或请求用户决策，不能自行扩大修改范围。

## 7. 后续阶段

### P0

- 工具上下文绑定到前端 Agent Run（已接入 `AgentGraphRunner.run`）；
- 工具单测与注册回归；
- 生成 Vite 工程必须自带：
  - `public/inspect_sdk.js` 与 `index.html` 注入；
  - `/api -> 127.0.0.1:3000` 代理；
  - 否则选择按钮与真实 API 会同时失效；
- 项目源码持久化（MySQL 元数据 + MinIO 正文，已落地）；
- Direct Ops commit → BFF 快照同步（已落地，失败不回滚用户修改）；
- 构建期 `data-oid` 与 `vibe-source-map.json`（插件 + CodeGen 已落地）；
- Importer Graph + `callSiteOid` 双索引（已落地，单调用点 Props 编辑）；
- `UPDATE_PROPS` Direct Ops：只改调用点 JSX，不触碰组件定义；
- Direct Ops `oid / callSiteOid → 源码区间` 解析（已落地，旧 file/line 仅兜底）；
- 运行时 `instancePath` 上报；模板 / 多实例修改返回 HITL 范围选择；
- SourceSync 删除感知 + 本地 JSON 补偿队列（失败后台重试）；
- 跨文件父容器移动（containerScope）、多调用点运行时精确消歧（后续）。

### P1

- `UPDATE_CLASS` / `UPDATE_STYLE` / `UPDATE_PROPS`；
- `wrap_node` / `unwrap_node` / `duplicate_node`；
- 变更预览、构建验证、截图对比；
- 属性面板 / 组件目录 / 设计令牌。

### P2

- 跨文件 / 组件实例编辑；
- flex 顺序调整；
- grid / absolute / 响应式布局；
- 页面 / 路由级工具。

## 8. 验收标准

- 前端 Agent 不接触源码文本即可完成文本、移动、插入、删除；
- 所有写操作经过同一套 Direct Ops 门禁与 ChangeLog；
- 任一工具失败都能回滚，且不会留下半写入文件；
- 未绑定上下文、revision 冲突、共享组件、循环项都能返回稳定错误码；
- 不引入第二套 AST 执行器或第二套项目状态。
