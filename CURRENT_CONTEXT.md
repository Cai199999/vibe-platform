# vibe-platform · 当前状态简报

> 唯一上下文入口 · 2026-11-09 · 详细历史见 `docs/design`、`docs/archive`（默认不读）

## 架构底座

React 18 + Vite → Java 21 + Spring Boot 3.3.5 BFF（WebClient）→ Python 3.12 + FastAPI + LangGraph → Docker 沙盒（Node 20 + React/Vite，单服务）。中间件复用 `~/Workspace/infra`：MySQL / Redis / MinIO。AI Core 已按六边形架构分层：`domain`（纯模型）/ `ports`（抽象）/ `adapters`（实现）/ `agent·architect·supervisor`（应用编排），旧 import 保留兼容 shim。

## 已闭环能力

- 画布圈选 / 空白 ADD / 精准点击 Inspect（React+Vue 源码文件:行:列）
- 多标注（≤3）、多轮对话、世界坐标无漂移
- SSE `seq`/`afterSeq` 重放、HITL 澄清、Diff 审批、取消、Git 回滚
- Supervisor 路由：ui / architect / both / custom / rejected
- Architect 形态判别 + 动态澄清 + BM25 契约
- 多 Tab、zip/HTML/图片导入、预览、`/admin`
- Token 降本：模型分级、历史摘要、错误截断、同错终止
- 团队 8 Agent Manifest（3+5）
- Tool：`search_web`（research 节点）+ Supervisor 工具网关
- Skill：Markdown 挂载 `ui.accessibility`，动态注入 Coder
- MCP：标准 stdio 子集 + `get_current_time` 示例
- 六边形重构：`AnnotationEnvelope/ArchitectContract/State` 入 domain；
  `ModelClient/Sandbox/RAG/Checkpoint/Editor` 入 ports；DeepSeek/Docker/Markdown/Chroma/Editor 入 adapters
- 记忆隔离：`ThreadMemoryStore` 按 tenant+project 作用域键；LangGraph
  checkpoint 按 `ui_run`/`architect_run` + 项目作用域物理隔离；Supervisor
  只向 Architect 投影 `architectHistory`，UI 只收画布标注；State 字段边界测试固化
- 序列化：Annotation / chatHistory 的 `createdAt` 统一 ISO 8601；Prompt 与 SSE
  `json.dumps` 加 `default=str` 兜底，修复 datetime 导致的后端 500
- 开发者工具化：ADD 的 `relativeTarget` 增加 CSS 像素坐标（relativeX/Y、
  widthPx/heightPx、parentW/H），Prompt 按 flex/grid/absolute 精确落位；
  澄清期间主输入栏常驻 + 自定义 textarea + comment 入上下文；预览底部
  状态栏与抽屉展示模型/沙盒/AST 路径/Diff/LangGraph 时间轴
- 项目快照：沙盒 Git checkpoint（💾 保存 / 版本历史抽屉 / 只读快照标签 +
  累积 Diff / 二次确认 `reset --hard`）；恢复后版本列表刷新、iframe 以
  nonce 重建加载回滚版本；不引入额外数据库
- 生成流重构：`CREATE_NEW` 只生成组件主体 + `data-vibe-created`，预览层
  拖拽定位；确认后以 `MODIFY_EXISTING + payload.position` 回写 absolute。
  `run.dirty` 保存门控：【下一步】置灰 + Toast；保存快照刷新
  `global_summary.md`，Architect 请求携带 `projectSummary/latestChanges`
- 记忆落盘：BFF camelCase 请求字段改用 Pydantic alias 解析；python-ai
  entrypoint 启动时修正 bind mount 属主后降权到 app，修复摘要静默失败
- 上下文分层：tiktoken 预算 + FAST 意图分级（高优逐字/低优合并）+ 最近
  10 轮原文；工具结果落 `tool_outputs/<sha256>` 只传预览；每 10 轮写
  `local_summary.md`，快照刷新 `global_summary.md` + `user_profile.md` 并注入
  system；Architect `response_format` + 校验错误回传重试；前端 VirtualList +
  rAF 合帧
- Architect 置信度：必需槽位才算封顶，可选 auth/api 不再扣分；
  assumptions 齐全时封顶 0.90；问题按必需槽位优先；自由文本确定性回填
  框架/数据库/实体/业务流；诊断脚本 `scripts/architect_confidence_debug_test.py`
- Eval 10/10；Python 189 passed（含拖拽定位/上下文 4 + 快照 alias 2），
  Java BFF 快照测试 PASS；拖拽定位→保存门控→Architect 澄清 E2E PASS，
  快照/Architect/空白 ADD/新增组件回归全 PASS

## 真 / 假边界

| 能力 | 真实状态 |
|---|---|
| DeepSeek | 已接 OpenAI 兼容接口，`.env` 有 Key；Eval 仍用 Mock |
| 鉴权 | Mock Token + Header，无 JWT |
| 持久化 | BFF 无 DB；状态进程内，重启丢失 |
| MySQL / Redis / MinIO | 仅连接配置，未读写 |
| 数据中台 | Knowledge/Prompt/Feedback 仅契约 |
| 沙盒 | React/Vue Vite 单服务，无多服务/gVisor |
| Agent | 3 available + 5 reserved；reserved 仅 Manifest |
| 外网 | 当前环境不可达，`search_web` 返回可读失败；解析器已单测 |
| Admin Eval | 契约冻结，未接路由 |

## 下一步

- **P0 真实 DeepSeek**：Eval 接 `deepseek-chat`，建质量/成本基线
- **P0 M3.2 知识中台**：摄取、租户隔离检索
- **P1 前端精度**：高 DPI / 极端缩放的圈选与高亮坐标
- **P1 运行时**：Redis checkpoint + Stream + 分布式门控
- **P2 契约驱动**：Spring Boot/DDL + 多服务沙盒
- **P2 平台化**：真实认证/RBAC、Prompt Registry、Feedback
