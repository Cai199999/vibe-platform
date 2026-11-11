# 2026-11-11 · 真实模型体验专项

## 目标

消除真实 DeepSeek 接入后的随机反问、拖拽 API 风暴、Coder 长阻塞与首字延迟。

## 落地

- **确定性提问**：任务级温度覆盖 `planner=0.1`，`architect_analyze/contract/helper=0.1`；
  Planner / Architect Prompt 与确定性裁剪统一为“每轮最多 2 问 + A/B/C 选项”，
  禁止开放式提问；Architect 问题解析器同步截断到 2 条。
- **拖拽跟手**：SDK `pointermove` 只写元素内联 `translate`，不发 API；父页面在
  `drag-end` 后 300ms 防抖，合并为 `action=MOVE` 的一次静默 Run；等待期间保留
  拖后位置并显示“代码同步中...”，成功等 `submitting=false` 再解除，失败/超时
  立即回滚并强制解除遮罩。
- **Coder 熔断**：`_generate_with_coder_timeout` 用独立线程 + `copy_context()`
  执行模型调用，30s 硬超时；超时或 Pydantic 校验耗尽（`AGENT_JSON_RETRY_LIMIT=1`，
  共 2 次）直接写入 `fatal_error=true`，路由到 `failed`，前端收到
  `CODER_GENERATION_FAILED` 与“由于代码结构复杂，AI 生成超时，请尝试缩小标注范围”。
- **流式输出**：新增 `ai_core.observability.streaming`（ContextVar TokenSink +
  80ms/48 字符聚合），DeepSeek 适配器在 Sink 存在时走 `.stream()`，SSE 新增
  `token` 事件；前端按 100ms 窗口合并 token，Run 时间轴与 Architect 面板打字机渲染。
  客户端开启 `stream_usage=True`，流式调用仍能回传 token 用量。
- Supervisor 维持确定性规则路由，不引入 LLM 分类阻塞。

## 验证

- 328 个 Python 单测通过（新增 Coder 超时/路由、TokenSink 生命周期、温度与 usage 断言）。
- `npm --prefix apps/platform-web run build` 通过。
- Docker `python-ai` 重建并健康检查通过；运行时路由确认 Planner/Architect 温度均为 0.1。
