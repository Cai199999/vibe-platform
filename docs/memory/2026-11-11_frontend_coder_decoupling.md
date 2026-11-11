# FrontendCoder 职责解耦与降级（2026-11-11）

## 问题

画布“加一个按钮”这类纯视觉需求，旧链路把整份源码、后端 API 契约与 DDL
一起塞进 UI Coder 上下文，导致：

- 大模型被迫同时设计业务与视觉，输出膨胀、耗时经常超过 30s；
- 超时直接 `FAILED`，用户草稿与标注被钉死在失败页；
- 简单需求也要整文件重写，Diff 大、回滚风险高。

## 决策

**Architect 负责逻辑与业务，FrontendCoder 只负责视觉与 DOM。**

1. `ui_context.py` 统一组装 UI Coder 上下文：
   - 小文件（≤1800 token）整文件发送；
   - 大文件按 CSS selector / component 定位 JSX，只保留目标节点 +
     最多 2 层父级 + 同层兄弟（3 层结构），前后各留 4 行；
   - 显式剔除 `business` / `apiContract` / `ddl` / 鉴权等业务字段；
   - 总预算 5200 token，超限丢弃非首个目标文件并强制 `mode=diff`
     （3200 token 阈值），禁止 `mode=files` 整文件重写。
2. Coder System Prompt 增加职责边界：缺业务逻辑时只生成视觉占位
   `onClick={() => console.log('待绑定逻辑')}` + `TODO(interaction)`；
   上下文没有的 API / 路由 / DDL 一律视为不存在。
3. 超时两级降级：
   - 普通 30s 超时 → 发出“已精简上下文并切换快速模型重试”进度；
   - 以 `compact=True, prefer_fast=True`、20s 预算重试 `coder_fast`；
   - 仍失败返回 `CODER_TIMEOUT_RECOVERABLE`（`backgroundAvailable=true`），
     不再直接判死。
4. 前端“转后台运行”：
   - 失败页 / 结果 HUD 提供服务端确认可恢复时的按钮；
   - 首次提交快照 `backgroundRequest`，重放只派发 Run，不重复写用户气泡；
   - 后台 Run 使用 240s 超时 + 独立 SSE，`submitting=false` 不阻塞画布；
   - 顶部 HUD 提示“后台生成中”，完成 / 失败后 Toast 通知并保留草稿。

## 关键文件

- `services/orchestrator/ai_core/agent/ui_context.py`（新增）
- `services/orchestrator/ai_core/agent/llm.py`
- `services/orchestrator/ai_core/agent/graph.py`
- `services/orchestrator/ai_core/agent/service.py`
- `apps/platform-web/src/pages/WorkspacePage.jsx`
- `apps/platform-web/src/components/RuntimePanel.jsx`
- `services/orchestrator/tests/test_ui_coder_context.py`（新增）

## 验证

- 340 个 orchestrator 单测通过（含新增 9 例：窗口裁剪、业务字段隔离、
  强制 diff、FAST 降级、双超时可恢复、后台长超时）。
- 前端 Vite build 通过；`vibe-python-ai` 重建后 `/api/v1/health` 200。
- 仍待真实模型观察：简单按钮是否稳定 5s 内完成；长任务转后台后的
  Toast / 预览刷新体验。
