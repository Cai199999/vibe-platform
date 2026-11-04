# 2026-11-04 · M1 全链路（画布 → BFF → Agent → 沙盒）

## 目标

把已完成的沙盒与 LangGraph 单 Agent 接入真实前端画布，跑通
「圈选组件 → 提交标注 → 同步执行 → 预览变化 + 代码 Diff」的 M1 闭环。

## 已实现

### Java BFF

- `POST /api/v1/runs`：`CreateRunRequest` → `RunService` → Python
  `POST /api/v1/internal/runs/execute`。
- 两个 `RestClient`：
  - `aiCoreRestClient`（`@Primary`）：2s 连接 / 3s 读取，供健康检查；
  - `aiCoreRunRestClient`：2s 连接 / 300s 读取，供 Run 同步执行。
- `annotation` / `options` 用 `JsonNode` 透传，BFF 只校验 `projectId` 与
  `annotation` 非空，避免契约在 Java / Python 两侧漂移。
- AI Core 的 HTTP 状态与 JSON 原样返回：构建重试耗尽仍是
  `200 + status=FAILED`；参数/沙盒错误原样 4xx/5xx。

### platform-web

- 画布：Pointer Events 拖拽圈选，坐标归一化到 0~1；新圈选会清除旧标注。
- `buildAnnotation()`：把选区组装为 `AnnotationEnvelope`（version 1、
  `worldTarget`、`target.source`、`intent.ui`、`payload.text`）。
- 提交：POST `/api/v1/runs`，携带 `files={"src/App.jsx": INITIAL_APP}` 与
  `options={llmProvider:"mock", mockScenario:"fail_first", maxRetries:3,
  keepSandbox:true}`。
- 执行状态面板：
  - Loading 阶段时间轴（规划 → 编码 → 写入 → 验证 → 自我修复 → 再验证）；
  - 成功展示 `previewUrl` iframe、`generatedPatch` 彩色 Diff、构建耗时、
    重试次数与 Agent 节点轨迹；
  - 失败展示 BFF / AI Core 错误体与最终构建日志。
- Vite 代理 `/api` → `VITE_BFF_PROXY_TARGET`（Compose 内为
  `http://java-bff:8080`）。

### Compose

新增 `platform-web` 服务（5173），`depends_on java-bff healthy`，
健康检查 `wget /`。Java 增加 `AI_CORE_RUN_READ_TIMEOUT_MS=300000`。

## 关键问题与修复

### JDK HttpClient 的 h2c Upgrade 导致 POST body 丢失

- 现象：Java BFF 调用 AI Core 返回 `422`，FastAPI 报
  `{"loc":["body"],"msg":"Field required","input":null}`；Python 日志出现
  `Unsupported upgrade request.`。
- 原因：JDK HttpClient 默认 HTTP/2，会先发 h2c Upgrade；Uvicorn(h11) 不支持
  升级，请求体未被正确投递。
- 修复：`AiCoreClientConfig` 强制 `HttpClient.Version.HTTP_1_1`。
  同时 RunService 显式 `ObjectMapper.writeValueAsString()` 后发送 String，
  便于记录请求体字节数并排除 POJO 转换器差异。

## 实测结果

`python3 scripts/platform_e2e_test.py`：

- platform-web / java-bff / python-ai 健康检查 200；
- Vite `/api` 代理健康检查 200；
- 画布同款请求经 Vite 代理 → BFF → AI Core，约 5–6s 完成；
- `status=AWAITING_APPROVAL`，`retryCount=1`，`verifyResult.success=true`；
- `generatedPatch` 含 `backgroundColor` + `red`，符合 unified diff；
- 预览页 200，沙盒 `/src/App.jsx` 已包含红色样式；
- 清理沙盒成功。

前端 `npm run build` 通过：34 modules，CSS 15.82 kB，JS 155.72 kB。

Playwright 浏览器 E2E `scripts/ui_e2e_test.py`：

- 在真实 Chromium 中拖拽圈选按钮并提交；
- 捕获 Loading 面板；
- 等待 `AWAITING_APPROVAL` / 自动修复 1 次；
- iframe 内按钮计算样式为 `rgb(255, 0, 0)`；
- 无浏览器控制台错误；
- 截图：`docs/screenshots/m1-loading.png`、`docs/screenshots/m1-result.png`。

## 开发体验修正

- platform-web 在 Compose 中挂载源码 `./apps/platform-web:/app`，
  `node_modules` 使用匿名卷。此前镜像内是代码快照，前端修改不生效。
- 沙盒初始页面增加 `html,body{margin:0;background:#020617}` 并压缩版式，
  避免 iframe 出现白边，确保窄预览面板能直接看到红色按钮。

## 下一步（M2）

- 同步 execute 改为 `202 + runId`，通过 SSE / WebSocket 推送节点事件，
  前端 loading 使用真实状态而非时间模拟。
- 审批接口：批准后快照合并，拒绝后回滚沙盒。
- DeepSeek 实模联调与 Prompt 收敛。
- 独立沙盒节点 + gVisor，移除业务容器对 Docker Socket 的依赖。
