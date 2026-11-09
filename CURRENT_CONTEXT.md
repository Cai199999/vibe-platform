# vibe-platform · 当前状态简报

> 唯一上下文入口 · 2026-11-09 · 详细历史见 `docs/design`、`docs/archive`（默认不读）

## 架构底座

React 18 + Vite → Java 21 Spring Boot 3.3.5 BFF → Python 3.12 FastAPI + LangGraph → Docker 沙盒（Node 22 + Vite + Express + SQLite）。中间件复用 `~/Workspace/infra`。AI Core 六边形分层：domain / ports / adapters / agent·architect·fullstack。

## 已闭环能力

- 画布圈选 / ADD / Inspect，多标注、多 Tab、zip/HTML/图片导入与预览
- SSE `seq` 重放、HITL 澄清、Diff 审批、取消、Git 回滚与项目快照
- Supervisor 路由、Architect 形态判别 + 动态澄清、8 Agent Manifest
- Tool（search_web）/ Skill / MCP；记忆隔离、上下文分层压缩、Token 降本
- 契约→全栈生成：`FullstackGraph` 五节点；Vite 5173 + Express 3000，
  `/api` 代理；SQLite 落盘 `data/fullstack.sqlite`，契约 CRUD + `/api/data`
- 生成按钮门控：可见禁用原因 + 自定义 Tooltip + `[fullstack]` 控制台诊断；
  both 模式先 release 再生成；Graph 节点 INFO 日志可查
- 验证：`fullstack_e2e_check.py` 12/12；gate/disabled/API/UI E2E、Python 192、
  Java BFF、前端构建 PASS

## 真 / 假边界

| 能力 | 状态 |
|---|---|
| DeepSeek | 已接 OpenAI 兼容接口；Eval 仍 Mock |
| 鉴权 | Mock Token + Header，无 JWT |
| 持久化 | BFF 无 DB；全栈沙盒用 SQLite |
| MySQL/Redis/MinIO | 仅连接配置，未读写 |
| 沙盒 | Vite + Express + SQLite 双服务，无 gVisor |
| Agent | 3 available + 5 reserved |
| 外网 | 不可达，search_web 返回可读失败 |

## 下一步

- P0 真实 DeepSeek 质量/成本基线
- P0 M3.2 知识中台摄取与租户隔离检索
- P1 高 DPI 圈选精度、Redis checkpoint/Stream
- P2 全栈深化：真实 MySQL、按契约重写用户表单
