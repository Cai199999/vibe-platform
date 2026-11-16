# 2026-11-16 Phase 1：统一错误信封与边界拦截

## 目标

所有技术异常在到达前端前统一转换为
`ErrorEnvelope(code / userMessage / errorOptions / traceId)`；
SSE 建连后出错必须发送 terminal 事件，绝不让前端连接悬挂。

## Python AI Core

新增 `ai_core/error_envelope.py`：

- `ErrorEnvelope` / `ErrorOption`：标准字段，同时保留 `detail` 与
  `message` 兼容旧前端；
- `ErrorRegistry`：错误码 → 分类、severity、业务文案、retryable；
  已覆盖契约、架构、沙盒、传输、输入校验等现有错误码；
- `translate_exception()`：只把业务文案放进信封，技术消息与堆栈仅进
  服务端日志；
- `install_error_handlers(app)`：
  - `X-Trace-Id` / W3C `traceparent` 中间件；
  - `RequestValidationError` → 422 `REQUEST_VALIDATION_FAILED` + 字段级
    diagnostic；
  - `StarletteHTTPException` → 保留原 code/状态码，替换为业务文案；
  - 未捕获异常 → 500 `INTERNAL_ERROR`，响应头带 traceId。
- `stream_event_frames()`：
  - 普通事件原样输出；
  - 生成器异常 / 缓冲区异常时，尝试写入 `run.failed` /
    `fullstack.failed` / `architect.failed`；
  - 缓冲区不可用时合成 terminal 帧，确保浏览器一定收到终态；
  - 不推进 seq，避免前端用不存在的 seq 重连。
- `attach_error_envelope()`：给已有 `run.failed` / `fullstack.failed` /
  `architect.failed` payload 附加统一字段，保留旧字段兼容前端。

失败事件已接入：Agent Run（3 处）、Fullstack、Architect（graph +
service）。Fullstack 终态改为“先发事件再改快照状态”，消除测试/前端
观察到 FAILED 但事件尚未入队的竞态。

## Java BFF

- 新增 `common/GlobalExceptionHandler`：
  - `@Order(LOWEST_PRECEDENCE)` 全局兜底；
  - 校验失败 / 请求体不可解析 / 未捕获异常均返回同构信封；
  - 响应头 `X-Trace-Id`，不向响应写入堆栈或上游技术消息；
  - 日志只记录 traceId、异常类型与截断消息，不打印堆栈。
- 现有 4 个局部 Advice 提升为 `HIGHEST_PRECEDENCE`，确保 SSE 建流前
  的 `ArchitectStreamException` / `AiCoreStreamException` 仍由原处理器
  返回上游状态码，不被全局兜底覆盖。
- 新增 `common/SseErrorFrames`：建流前错误也输出同构信封；
  SSE 已建连后 `onErrorResume` 发送 terminal 事件。
- `RunStreamService` / `ArchitectStreamService` 用 `AtomicBoolean`
  区分“建流前错误”（交 HTTP Advice）与“流中断”（发 terminal）。
  全栈重放通过事件命名空间自动选择 `fullstack.failed`，避免全栈前端
  收到 `run.failed` 后重连。

## 验证

- 新增 `tests/test_error_envelope.py`（6 项）：
  - 未捕获异常不泄漏路径/密码；
  - 已知错误码使用业务文案；
  - Pydantic 校验返回字段级 diagnostic；
  - SSE 生成器与缓冲区同时失败时仍输出 terminal envelope；
  - 信封构造器保持 code 且隐藏技术消息。
- 全量 Python unittest：558 项通过。
- 新增 BFF 测试：`GlobalExceptionHandlerTest`（真实 Controller 抛错 +
  MockMvc 校验信封与 trace 头）、`SseErrorFramesTest`（terminal 帧
  结构与 envelope）；BFF 全部单测通过。
- 真实服务验证：
  - `POST /api/v1/internal/architect/runs/stream` 空 body → 422 信封；
  - 缺失 Run → 404 `ARCHITECT_RUN_NOT_FOUND` 业务文案；
  - 经 BFF 代理返回完全一致的信封。

## 边界

Phase 1 只打通“分类 + 信封 + terminal”，`errorOptions` 默认空数组；
自愈重试（Phase 2）、降级（Phase 3）、HITL 选项（Phase 4）尚未接入。
