# vibe-platform · 当前状态简报

> 2026-11-11 · 详细历史见 `docs/memory/`（旧版全文：`2026-11-11_context_archive.md`）

## 架构底座

React + Vite → Java 21 BFF → Python 3.12 FastAPI/LangGraph → Docker 沙盒；中间件复用 `~/Workspace/infra`；AI Core 六边形分层。

## 已闭环能力

- 画布圈选 / ADD / Inspect / 批量拖拽、多标注、双屏同步、HITL 澄清、Diff 审批、取消、Git 快照回滚。
- 全栈生成：Architect 契约、Frontend/Backend/DBA/Infra 多 Agent、语义路由自愈、SQLite/MySQL 验收、生产级产物导出与导入。
- 混合 RAG（BM25 + Chroma MiniLM + RRF）、分级决策、Reflection、Token 成本采集、骨架防脑补、Agent 管理与知识库上传。
- **UI Coder 解耦**：只生成 UI/Tailwind；大文件仅发送目标 JSX 窗口；超时降级 `coder_fast`；业务/API/DDL 不进入上下文。

## 最新里程碑（2026-11-11）

- **修复添加按钮假成功**：空 selector 走 `app-main` 结构锚点；SDK 精确行号优先；Coder 强制新增节点携带 `data-vibe-created`；Verifier 构建通过后再校验标记，缺失即失败。
- **修复 Applier 落盘失败**：模型 hunk 行数常写错，`git apply --recount` 容忍计数错误与行号偏移；真实回归已成功把按钮写入 `src/App.jsx` 并构建通过。
- **标注坐标系下放**：SDK 基于真实 DOM 覆盖判定 MODIFY/CREATE，回传 `documentRect`、相对坐标与滚动快照；父页面用文档坐标 + 实时 `frameScroll` 重投影，滚出视口隐藏。
- **页面切换清空**：SDK 监听 `pushState/replaceState/popstate/hashchange/reload`，`route-change` 通知父页面清除标注与草稿，不回滚代码。
- 验证：Chromium CDP 实测滚动不变性 + 路由事件 PASS；前端 build 通过；orchestrator 346 tests 中 345 通过，唯一失败为既有挂载目录权限问题。

## 真假边界

DeepSeek 真实调用；鉴权 Mock；BFF 无持久化；沙盒无 gVisor；未真实部署云端。

## 下一步

圈选精度评测、Redis checkpoint、迁移回滚脚本、多服务编排、真实云端部署。
