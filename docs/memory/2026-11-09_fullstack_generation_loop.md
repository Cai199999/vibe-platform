# 2026-11-09 · 契约确认 → 全栈生成 → 沙盒联调闭环

## 目标

Architect 契约 `CONFIRMED` 后不再停在面板：一键生成 DDL、后端与前端接入代码，
写入现有沙盒并重启前后端，预览区直接调用真实 API。

## 关键改动

- **Python AI Core**
  - `ai_core/fullstack/coders.py`：确定性生成 `server/schema.sql`、
    `server/index.mjs`（零依赖 `node:http` REST + CORS + 健康检查）、
    `server/package.json`、`src/fullstack-api.jsx`、`scripts/dev.mjs`；
    并 patch `src/main.jsx` / `vite.config.js` / `package.json`。
  - `ai_core/fullstack/graph.py`：LangGraph 拓扑
    `DBACoder → BackendCoder → FrontendCoder → BuildVerifier → SandboxRestart`，
    每节点推送 `fullstack.node.started/output/completed/failed`。
  - `ai_core/fullstack/service.py` + `api/fullstack.py`：后台 Run、事件缓冲
    重放、终态 `fullstack.completed/failed`、快照查询。
  - `SandboxProvider.restart(sandbox_id, ready_path)`；Docker 适配器重启容器
    并轮询 `/api/health`，就绪后返回。
- **Java BFF**
  - `POST /api/v1/runs/fullstack` 透传 SSE；`GET /api/v1/runs/{id}/events/stream`
    支持 `afterSeq` 重放。
- **前端**
  - `useArchitect.generateFullstack` + `fullstackStream.js`：独立 controller、
    `reduceFullstack` 状态机、断线重连与持久化。
  - `ArchitectPanel`：确认后展示【🚀 开始生成全栈代码】、五节点进度与生成日志。
  - 成功后把 `sandboxId/previewUrl` 回填 Run 状态，刷新 iframe 并聚焦预览。
  - 沙盒模板 `vite.config.js` 增加 `/api` → `127.0.0.1:3100` 代理。

## 验证

- `scripts/fullstack_generation_e2e_test.py`：BFF 全链路 9s PASS，
  `/api/health`、集合 GET/POST、列表回读全部通过。
- `scripts/fullstack_generation_ui_e2e_test.py`：浏览器点击生成 → 五节点
  SUCCEEDED → 预览 iframe 数据面板 `ready` → UI POST 出现在列表，PASS。
- `services/orchestrator/tests/test_fullstack.py`：12 单测 PASS。
- 截图：`docs/screenshots/fullstack-generation-completed.png`、
  `fullstack-preview-live-api.png`。

## 已知边界

- 生成后端为内存仓储，DDL 仅落 `schema.sql`，未接 MySQL。
- 关闭项目 Tab 会中断 SSE 并销毁沙盒；生成本身不可恢复，需重新点击。
- `test_eval_suite` 在容器 `/app` 挂载下因 `REPO_ROOT` 路径断言失败，
  属测试环境差异，非业务回归。
