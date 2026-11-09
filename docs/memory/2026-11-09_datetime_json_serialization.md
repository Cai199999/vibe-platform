# 2026-11-09 · datetime JSON 序列化 500 修复

## 现象

画布圈选生成按钮时后端返回 `执行失败: Object of type datetime is not JSON serializable`。

## 根因

`AnnotationEnvelope.createdAt` 与 `AnnotationChatMessage.createdAt` 是
`datetime`。`_build_annotation_context` 使用默认 `model_dump()`（python
mode），datetime 对象原样进入 `annotation_context`；DeepSeek Planner 在
`json.dumps(annotations)` 拼 Prompt 时抛 `TypeError`，被 Service 统一包装成
`run.failed`。Mock LLM 不调用 `json.dumps`，因此测试未暴露。

## 修复

- `domain/common.py` 新增 `iso_datetime()`，统一 UTC ISO 8601（毫秒 + Z）。
- `AnnotationEnvelope` / `AnnotationChatMessage` 增加 `field_serializer`。
- `_build_annotation_context`、`context_doc`、`ThreadMemoryStore._normalize_message`
  显式 `mode="json"`；字典形式的 datetime 也走 `iso_datetime`。
- Prompt 侧 `json.dumps(..., default=str)`（agent / architect / memory）。
- SSE `RunEvent.to_sse` 增加 `default=str`，任何未来字段都不会压垮整条连接。

## 验证

新增 `tests/test_json_serialization.py` 4 项：上下文 JSON 可序列化、
ThreadMemory ISO 归一化、Planner Prompt 容纳 datetime、SSE 帧容纳 datetime。

全量 152 passed；真实 Mock API 调用返回 `SUCCEEDED` 且 `generatedFiles`
非空。
