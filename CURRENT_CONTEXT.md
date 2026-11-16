# vibe-platform · 当前状态简报

> 2026-11-16 · 历史细节见 docs/memory/

## 架构
React+Vite → Java21 BFF → Python3.12 FastAPI/LangGraph → Docker 沙盒；中间件复用 ~/Workspace/infra；AI Core 六边形分层。

## 已闭环
- 画布圈选/ADD/Inspect/批量拖拽、双屏同步、HITL 澄清、Diff 审批、取消、Git 快照回滚。
- 全栈生成：Architect 契约、多 Agent、语义路由自愈、SQLite/MySQL 验收、导入导出。
- 混合 RAG、分级决策、Reflection、Token 成本、骨架防脑补、Agent/知识库管理。
- AST/ORM/DDL 门禁、Applier 隔离、契约审计、缺陷归因、风险门禁 L1/L2/L3。
- Phase1：确定性 Resolver + apply_ast_edit 硬门禁；Phase2：AWAITING_DECISION + decision.required + 3 轮上限。
- 暂存组件自由拖拽：视口 clamp、SDK stage-hit 父容器高亮、Drop 写 placement；同父容器连续插入按拖入顺序确定性定序，inside_index 由服务端计算（不采信模型序号）；拖拽中 Esc / 路由切换均清空幽灵。
- 孤儿 Draft 回收：启动即扫 + 6h 周期，7 天 TTL；begin_draft 前 60s 宽限回收同沙盒残留 worktree，避免重启后 5174 端口被占。
- 契约资源/动作分离：Prompt 禁止 /auth、/session、/account 作为集合根；审计自动映射 users / 跳过动作端点。修复死锁改为 conflict_gate 中断 → AWAITING_DECISION，前端三选一（映射已有表 / 新建表 / 取消），决策后确定性改写契约并复验。

## 最新验证
前端 16 项 Vitest、Python 552 项 unittest 通过；Java BFF 镜像构建通过。真实浏览器 E2E 全绿（Diff 顺序/父容器/无绝对定位、Esc、路由切换），产物在 docs/screenshots/staged-drag-*。

## 真假边界
DeepSeek 真实调用；鉴权 Mock；BFF 无持久化；沙盒无 gVisor；未云端部署。

## 下一步
Redis checkpoint；云端部署。
