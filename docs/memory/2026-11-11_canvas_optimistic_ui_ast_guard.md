# 画布视觉暂存（Optimistic UI）与 AST 写盘门禁

> 2026-11-11 · 对应事故：Coder 生成 `<Plus />` 未 import，esbuild 通过但运行时
> ReferenceError 导致整个 React 树卸载，前端白屏。

## 事故结论

- 语法合法 ≠ 可运行。JSX 组件未绑定属于运行时错误，Vite HMR 会把坏代码
  推进 iframe，白屏后用户所有标注与画布状态一起丢失。
- 止血：沙盒 `sbx_3a5f9ab7cc4e4509bdc3` 已 `git reset --hard` 回基线 `330a95a`。

## 写盘前 AST 门禁

`services/orchestrator/sandbox_template/ast_check.mjs`

- `@babel/parser` 解析 JS/JSX，语法错误直接 exit 1；
- `@babel/traverse` 检查所有大写 JSX 标签（含 `JSXMemberExpression`
  根节点）是否在作用域内绑定；未 import / 未声明的组件 exit 1；
- 依赖独立安装到镜像 `/opt/vibe-ast`，避免导入项目改写
  `/app/package.json` 后检查器失效；旧镜像缺依赖时 exit 2，调用方 fail-open。

`LocalDockerSandbox._preflight_code_check`

- 写盘前把 `/app` 快照复制到 `/tmp/vibe-ast-stage`（排除 node_modules /
  dist / .git / .vite），在副本上叠加 files 或 `git apply --recount`；
- 只检查本次变更的脚本文件；
- 检查失败抛 `SandboxPatchError`，`/app` 完全未被触碰，也就不会触发 HMR；
- 有 checker 但依赖缺失时只告警放行，保证旧镜像不中断业务。

## 前端视觉暂存

目标：用户怎么画，页面都不会白屏。

- `WorkspacePage` 维护 `pendingAdditionsRef`：`annotationId → {rect,
  documentRect, componentType, intent, ...}`；
- CREATE_NEW 只记录标注与草稿，不调用普通 Run；
- `CanvasStage` 在 `.annotation-layer` 内用绝对定位渲染
  `.pending-addition-ghost`（半透明按钮预览），只读前端状态；
- 右下角浮窗展示「📝 当前有 N 个未保存的组件变更」，可逐项移除 / 撤销；
- 只有点击【保存布局】或进入架构澄清 / 全栈生成时，才把
  `pendingAdditions + pendingLayoutChanges` 打包为一条 `BATCH_LAYOUT`。

## BATCH_LAYOUT 统一提交

请求：`{ action: "BATCH_LAYOUT", payload: { moves?: [...], additions: [
{annotationId, intent, annotation} ] } }`

后端 `AgentGraphRunner._coder_layout_batch`：

1. 先用原始 `current_files` 调一次 Coder 生成全部新增组件的 JSX / diff；
2. Python `_apply_unified_patch` 把模型 diff 合并进工作副本（容忍行号偏移）；
3. 再用 `move_component` 为每个位移追加确定性 `transform` CSS；
4. 合并成一份 `generated_files` + 一份 patch 交给 Applier；
5. Verifier 穿透 `payload.additions` 逐个检查 `data-vibe-created` 标记。

失败时前端保留全部草稿与幽灵预览，用户可直接重试。

## 架构师上下文

进入架构澄清 / 全栈生成前先 `await handleSaveLayout`；保存成功后把
`describePendingChanges()` 生成的摘要以 `system` 消息注入 `uiHistory`，
Architect 可以主动提及画布上刚新增的组件。

## 验证

- `tests/test_layout_batch.py`（协议 / diff 合并 / 单补丁 / 标记校验）10 项；
- 相关套件 `test_tools_mcp_skills`、`test_sandbox_snapshots`、
  `test_create_new_actions`、`test_relative_coordinates` 共 53 项全绿；
- 前端 `vite build` 通过；
- 真实沙盒 AST 探针：未绑定 `<Plus />` → exit 1，正常 `src/App.jsx` → exit 0。
