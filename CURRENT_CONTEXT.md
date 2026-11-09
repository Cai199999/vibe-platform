# vibe-platform · 当前状态简报

> 2026-11-09 · 历史见 `docs/design`、`docs/archive`

## 架构底座

React+Vite → Java BFF → Python FastAPI/LangGraph → Docker 沙盒（Node 22+Vite+Express）；中间件复用 `~/Workspace/infra`；AI Core 六边形分层。

## 已闭环

- 画布圈选/ADD/Inspect、多标注多 Tab、导入预览
- SSE 重放、HITL 澄清、Diff 审批、取消、Git 回滚/快照
- Supervisor 路由、Architect 动态澄清、8 Agent Manifest、Tool/Skill/MCP
- 记忆隔离、上下文压缩
- **对话前置建项（新）**：新建项目=空白画布+【✨ 开始创建】；纯自然语言驱动 Architect 多轮敲定契约（前后端路由/布局/组件/API/实体）；形态由 `infer_app_shape` 识别，已移除“全栈/轻量/脚本”三选一
- **自动前端接力（新）**：契约 CONFIRMED → 自动跑 FrontendCoder→BuildVerifier→SandboxRestart（`mode=frontend`）；`frontendEntry` 回写项目源码，画布从 blank→live 实时预览；失败可重试/重开架构
- 项目态：pending → architecting → confirmed → bootstrapping → ready/failed
- **画布双态（新）**：live 页面默认 `annotate`（覆盖层吃指针，可圈选）；单击页面切 `interact`（iframe 吃指针，可直接点按钮）；点灰色空白/Esc/开关回 `annotate`。中键平移在双态均生效：标注态父页处理，交互态由 `inspect_sdk.js` postMessage 桥接。
- **紧凑工具栏（新）**：单行 TopBar；视图模式合为单图标切换；清空/快照/导出/Agent 目录收进「⋯ 更多」；模型选择保留极简下拉。
- **自适应铺满**：工作台 `100dvh` flex 链；动态逻辑视口（≥1280×720）严格铺满，不 cover 裁切。
- **分级决策（新）**：Planner 输出 `decision_impact`；仅 `CORE_BUSINESS` 触发 HITL，`IMPLEMENTATION_DETAIL` 自主生成。核心澄清下发 `clarificationOptions` 一键按钮，点击推荐项自动恢复。
- **Reflection 偏好（新）**：成功路径提取主色/圆角/密度证据写入 `preferences.json`，累计 ≥2 次后注入 Planner/Coder 默认采用。
- 生产级全栈：六节点 FullstackGraph；Sequelize+`DATABASE_URL`，沙盒回退 node:sqlite；IaC（Dockerfile/render.yaml/DEPLOY.md）
- 验证：Python 16 分级决策单测 + 全栈单测；分级决策/澄清 UI/画布双态/多标注/快照全流程 E2E；前端构建 PASS

## 真假边界

| 能力 | 状态 |
|---|---|
| DeepSeek | 接口已接，Eval 仍 Mock |
| 鉴权 | Mock Token |
| 持久化 | BFF 无 DB；生产路径需自备云库 |
| 中间件 | 仅配置未读写 |
| 沙盒 | 双服务，无 gVisor |
| 部署按钮 | 仅指引 |
| Agent | 3 available + 5 reserved |
| 外网 | 不可达，search_web 失败 |

## 下一步

- P0 DeepSeek 基线
- P0 M3.2 知识中台
- P1 圈选精度、Redis checkpoint
- P2 部署按钮接真实 API
