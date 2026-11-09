# 2026-11-09 · 全栈生成按钮门控诊断与 both 释放顺序

## 现象

用户反馈契约确认后【开始生成全栈代码】无法点击或点击无反应。按四个排查点
（按钮状态 / 点击请求 / 后端图 / SSE）逐项定位。

## 根因与修复

- **静默返回**：`useArchitect.generateFullstack` 在 `statesRef` 尚未同步、
  契约或 contractId 缺失时只 `return false`，调用方只显示笼统 Toast。
  现在点击入口显式传入本次渲染的 `contract/contractId`，所有跳过分支输出
  `console.warn('[fullstack] generate skipped', ...)`。
- **禁用按钮无提示**：`disabled` 按钮在多数浏览器不展示原生 `title`。
  现在按钮旁渲染可见中文 `[data-fullstack-disabled-reason]`，包裹层提供
  自定义 Tooltip；`data-fullstack-ready` 暴露就绪状态。
- **门控顺序**：`both` 门控下点击生成先 `await releaseGateFor(...,
  { awaitUi: false })`，只等 release 接口返回，不阻塞 UI Run 事件流，然后才
  POST `/api/v1/runs/fullstack`。`releaseGateFor` 新增 `awaitUi` 选项。
- **防双击**：门控 release 的 await 窗口内 React 状态尚未变 RUNNING，新增
  `fullstackStartingRef` 同步防重入。
- **后端可观测性**：AI Core 未配置 root logging，节点 INFO 日志不可见。
  `main.py` 增加 `logging.basicConfig(LOG_LEVEL)`；`FullstackGraph` 启动、
  五节点开始/完成/失败、结束均输出 run_id / sandbox_id / 文件列表。

## 验证

- `scripts/fullstack_gate_ui_e2e_test.py`：真实拖拽主链路 → 新增未发送标注
  触发 both 门控 → 确认契约 → 门控自动 RELEASED → 点击生成 → 五节点成功。
  请求轨迹断言 `gates/{id}/release` 先于 `runs/fullstack`；`docker exec`
  断言 `/app/server/{index.js,db.js,schema.sql}` 与 `data/fullstack.sqlite`
  （文件头 `SQLite format 3`）。
- `scripts/fullstack_disabled_hint_ui_e2e_test.py`：轻量网页契约下按钮禁用，
  可见提示解释原因，控制台输出 `[fullstack] gate state`。
- 回归：API E2E 13.25s PASS，默认 UI E2E PASS，Python `test_fullstack`
  12/12 PASS，前端 `npm run build` PASS。

## 已知边界

- 生成前端仍是附加数据面板，未按契约重写用户原始表单。
- 页面刷新会中断全栈 SSE；持久化状态把 RUNNING 收敛为 FAILED，可重试。
