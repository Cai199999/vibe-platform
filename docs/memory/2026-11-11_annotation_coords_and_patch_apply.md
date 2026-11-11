# 2026-11-11 · 添加按钮“假成功”与标注圈坐标重构

## 事故一：Agent 提示成功但页面没有按钮

排查链路与结论：

1. `read_file` 验证：旧逻辑中代码**根本没有写入**；不是 HMR 或 CSS 隐藏问题。
2. 真实画布传回的 `selector` 为空，`ui_context._find_anchor` 返回 0，Coder 只看到
   `src/App.jsx` 文件头（import 区），于是模型只生成 import 补丁；构建仍然通过，
   Verifier 只看 `npm run build`，形成“假成功”。
3. 修复：
   - `ui_context.py`：空 selector 时按 `data-source="app-main"` / `<main>` /
     `data-source-kind="container"` / `return (` 做结构锚点；SDK 行号优先。
   - `llm.py`：CREATE_NEW/ADD 补丁的新增行必须包含
     `data-vibe-created="<annotationId>"`，只改 import/样式直接拒绝。
   - `graph.py`：Verifier 增加 `created-markers` 检查，构建通过但标记缺失仍判失败。
4. 回归中补丁已正确生成（含按钮 JSX 与标记），但 Applier 报
   `corrupt patch`。根因：模型把插入 35 行写成 `@@ -893,6 +893,24 @@`，
   hunk 计数错误；严格 `git apply` 会拒绝，即使上下文行号偏移本身可容忍。
5. `local_docker._apply_unified_diff` 改为
   `git apply --recount --whitespace=nowarn`；新增
   `test_miscounted_hunk_applies_with_recount` 锁定该行为。
6. 真实回归（`run_cf7a8b2cda534c9aa9e0`）成功：补丁落盘、构建 5.1s 通过、
   `src/App.jsx` 第 903 行出现 `data-vibe-created="ann_verify_fix"` 按钮。

## 事故二：标注圈不跟随页面

坐标系全部下放到 iframe SDK：

- `analyze-selection`：SDK 基于真实 DOM 覆盖面积判断 MODIFY_EXISTING / CREATE_NEW，
  返回 `target` / `addTarget` / `relativeRect` / `documentRect` / `viewport.scroll`。
- 父页面 Annotation 持久化 `documentRect`（文档坐标）与 `frameScroll`；
  渲染时用当前 `frameScroll` 动态投影，滚出视口直接隐藏。
- SDK 监听 `pushState` / `replaceState` / `popstate` / `hashchange` 与整页 reload，
  通过 `route-change` 通知父页面；`CanvasStage` / `ScaledPreview` 收到后作废异步
  分析并清空标注、草稿、澄清状态（不回滚代码）。
- Inspect 拾取同样记录 `documentRect` + `frameScroll`。

验证：

- 沙盒镜像已重建，当前沙盒已注入新 SDK 并重启。
- Chromium 实测：`data-vibe-created="ann_verify_fix"` 按钮真实出现在渲染 DOM 中
  （`display:block` / `visibility:visible` / `opacity:1`，文本“提交”）。
- Chromium CDP 实测：滚动前后同一元素 `documentRect` 不变、`scrollY` 正确上报；
  `history.pushState` 触发 `route-change`，结果 `PASS`。
- `npm run build` 通过；orchestrator 346 个测试中 345 通过，唯一失败为既有
  `test_delete_supports_relative_managed_dir` 的 Docker 挂载目录权限问题。
