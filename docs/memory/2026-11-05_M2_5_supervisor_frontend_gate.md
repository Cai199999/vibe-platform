# 2026-11-05 M2.5：前端统一调度与 both 强门控

## 1. 目标

1. 前端“发送 / 提交 / 下一步”统一接入 Supervisor，不再硬编码
   `/api/v1/runs/stream` 或 `/architect/runs/stream`。
2. 按后端 `decision.handoff` 动态切换局部时间轴 / Architect 面板 / Toast。
3. `handoff_both` 实现真正的“先契约、后 UI”强门控，防止乱序执行造成
   沙盒状态冲突。
4. 跑通浏览器级“UI 修改 + 架构澄清”混合 E2E。

## 2. 后端实现

### 2.1 Handoff 语义

`SupervisorDecision` 新增必填 `handoff`：

| target | handoff |
|---|---|
| `ui_graph` | `handoff_ui` |
| `architect_graph` | `handoff_architect` |
| `both` | `handoff_both` |
| `none` | `rejected` |
| 自定义 | `handoff_custom` |

前端只消费该字段，不再自行推断目标。

### 2.2 门控注册表

`ai_core/supervisor/gates.py`：

- `GateRecord` 保存 `projectId / architectRunId / 原始 SupervisorRequest /
  status / uiDispatch`；
- 状态机：`AWAITING_CONTRACT → RELEASING → RELEASED / FAILED`；
- `reserve_project`：dispatch 前先占项目锁，避免并发启动；
- `create`：Architect 启动成功后才落 gate；
- `begin_release`：并发抢占，只有一次能启动 UI；
- `complete_release`：写入 UI dispatch 并释放项目锁；
- `fail`：Architect 取消 / 失败 / 丢失时自动中止并解锁。

错误码：

| code | 场景 |
|---|---|
| `SUPERVISOR_CONTRACT_NOT_CONFIRMED` | 契约未确认 release |
| `SUPERVISOR_PROJECT_GATE_ACTIVE` | 同项目已有 active gate |
| `SUPERVISOR_GATE_RELEASING` | 另一个 release 正在执行 |
| `SUPERVISOR_GATE_ABORTED` | Architect 已取消 / 失败 |
| `SUPERVISOR_GATE_NOT_FOUND` | gateId 不存在 |

### 2.3 服务逻辑

- `dispatch`：先 `_reconcile_project`，`both` 走 `_dispatch_both`，其余走
  `ensure_project_available`；
- `_dispatch_both`：预留项目锁 → 只启动 Architect → 创建 gate；
  Architect 或 gate 创建失败时释放预留 / 取消孤儿 Run；
- `release_gate`：幂等返回 `RELEASED`；校验契约确认；`begin_release` 后
  使用 gate 保存的原始请求启动 UI；失败置 `FAILED`；
- `_assert_contract_confirmed`：对 `CONFIRMING_CONTRACT` 做最多 2 秒轮询，
  消除 `architect.contract.confirmed` 事件先于服务端状态落定的竞态。

### 2.4 API

Python 内部 / Java BFF 同步新增：

- `GET /gates/{gateId}`
- `POST /gates/{gateId}/release`

`SupervisorDispatchResponse` 新增 `gate`；`SupervisorGateResponse` 返回
`gate + dispatches`。

## 3. 前端实现

### 3.1 统一入口

`WorkspacePage.jsx`：

- `submitViaSupervisor`：`route → rejected/dispatchable 检查 → dispatch`；
- `handoff_ui`：设置 Run 状态并 `attach` 到已启动 UI Run；
- `handoff_architect`：`architect.attach` 打开面板；
- `handoff_both`：打开 Architect + 保存 gate + Toast；
- `rejected`：Toast “请先在画布上圈选组件，再发送修改指令”。

`supervisorClient.js` 提供 route / dispatch / gate release / gate query，
并保留后端错误码 `error.code`。

### 3.2 attach 模式

`runStream.js` / `architectStream.js` 支持 `attach + streamPath`：
首连使用 dispatch 返回路径，重连时按 Run 默认路径带 `afterSeq` 重放。
UI / Architect 均不再重复 POST 启动接口。

### 3.3 门控 UI

- 工具栏 gate chip：`正在进行全局架构设计，请先确认契约`；
- 提交按钮、画布操作在 gate active 时冻结；
- Architect 面板 gate banner 展示等待 / 放行 / 中止；
- `architect.contract.confirmed` → effect 自动 release，每个 gate 只自动尝试一次；
- 手动“继续执行界面修改”可重试失败释放；
- release 成功后 attach UI Run、聚焦预览、Toast 成功。

## 4. 验证

- Python 单元测试：66 个通过；
- `scripts/supervisor_e2e_test.py`：
  - 直连 AI Core / 经 Java BFF 均通过；
  - both 只启动 Architect；早释放 409；项目锁 409；取消 Architect → gate FAILED；
- `scripts/supervisor_both_e2e_test.py`：
  - 直连 / BFF 均通过；
  - 完整链路：both dispatch → 早释放 409 → 全栈契约 → CONFIRM → release →
    UI SSE `run.succeeded` → 重复 release 幂等 → 沙盒清理；
- `scripts/supervisor_mixed_ui_e2e_test.py`（浏览器级）：
  - 圈选 CTA + 填写 UI 指令 → 点击“下一步”；
  - 只调用 `/supervisor/route` + `/dispatch`，无直连启动请求；
  - gate `AWAITING_CONTRACT` 时提交冻结、沙盒为空；
  - 3 轮全栈澄清 → 契约 → CONFIRM；
  - 自动 release，gate `RELEASED`，UI `run.succeeded`，
    `dispatch=1 / release=1`，无重复 Run / 沙盒；
- 前端生产构建通过（临时 outDir 规避容器内 `dist` 权限问题）。

## 5. 已知边界

1. 门控为单进程内存实现，重启后 gate 丢失；多副本部署需替换为
   Redis / MySQL 分布式锁与持久化状态，API 契约不变。
2. 前端 `sessionStorage` 持久化 gate 视图，刷新后可恢复提示；但服务端
   重启后释放会返回 gate 不存在，需要重新 dispatch。
3. CustomAgent 运行时仍未接入；设计已明确必须使用独立受限沙盒 / 临时 venv，
   用户 Prompt 不得污染主系统（见 `docs/design/05.Supervisor_Multi_Agent.md` §9）。

## 6. 复现命令

```bash
# Python 单测（镜像内运行，仓库挂载到 /repo）
docker run --rm -v "$PWD":/repo -w /repo/services/orchestrator \
  -e PYTHONPATH=/repo/services/orchestrator \
  vibe-platform/python-ai:dev python -m unittest discover -s tests -v

# API 级 mixed E2E
python3 scripts/supervisor_both_e2e_test.py
AI_BASE_URL=http://127.0.0.1:8080 python3 scripts/supervisor_both_e2e_test.py

# 浏览器级 mixed E2E
/tmp/vibe-browser/bin/python scripts/supervisor_mixed_ui_e2e_test.py

# 前端构建
docker exec vibe-platform-web npx vite build --outDir /tmp/vibe-web-dist --emptyOutDir
```
