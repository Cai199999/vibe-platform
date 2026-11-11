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

- **白屏事故止血 + AST 门禁**：沙盒已回滚基线；`ast_check.mjs` 用 Babel 检查语法与未绑定 JSX 组件，`_preflight_code_check` 在 `/tmp` 副本上预检，失败直接拦截、不触碰 `/app`、不触发 HMR。
- **画布视觉暂存**：CREATE_NEW 不再边画边写代码；前端 `pendingAdditions` + 半透明幽灵预览 + 右下角「未保存变更」浮窗，怎么画都不会白屏。
- **BATCH_LAYOUT 统一提交**：保存布局 / 进入架构澄清 / 全栈生成时，位移与新增组件打包一次提交；Coder 一次生成新增 JSX，Python 合并 diff 后追加确定性 transform，只产出一份补丁与一次 HMR；Verifier 穿透校验每个 `data-vibe-created`。
- **Architect 上下文**：进入澄清前自动 flush 画布草稿，并以 `system` 消息注入未保存变更摘要。
- 验证：`test_layout_batch` 等 53 项测试全绿；前端 build 通过；真实沙盒 AST 探针（`<Plus />` 拦截 / 正常文件放行）通过。

## 真假边界

DeepSeek 真实调用；鉴权 Mock；BFF 无持久化；沙盒无 gVisor；未真实部署云端。

## 下一步

圈选精度评测、Redis checkpoint、迁移回滚脚本、多服务编排、真实云端部署。
