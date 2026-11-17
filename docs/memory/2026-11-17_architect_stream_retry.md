# 2026-11-17 Architect 流式断连重试与错误文案

## 现象

架构澄清时前端显示“架构澄清失败 / 系统遇到未预期问题，已记录诊断编号”。

## 根因

- DeepSeek SSE 在产出部分 token 后断连：
  `peer closed connection without sending complete message body
  (incomplete chunked read)`。
- `DeepSeekClient._stream` 在已产出 token 后把 `ModelClientError` 上抛；
- `ArchitectLLM._generate_with_repair` 只对 JSON 校验错误重试，
  传输级 `ArchitectLLMError` 直接冒泡；
- `ARCHITECT_LLM_ERROR` 未注册 ErrorSpec，错误信封降级为
  `INTERNAL_ERROR`，前端只能看到通用“未预期问题”。

## 修复

- `ArchitectLLM._generate_with_repair` 增加连接级瞬态重试：
  - `ARCHITECT_LLM_RETRY_LIMIT`，默认 2 次；
  - 仅重试 `流式调用中断 / peer closed / 502 / 503 / timeout /
    rate limit` 等 marker；
  - 指数退避 0.8s、1.6s，最多 4s；
  - 鉴权 / 参数 / 模型不存在等非瞬态错误不重试。
- 新增 `TokenReset` 上下文：
  - 重试前清空前端已展示的半截流式 JSON；
  - 新事件 `architect.token.reset`，前端 reducer 清空 `streamingText`。
- 新增错误码：
  - `ARCHITECT_LLM_ERROR`：ARCHITECT/RECOVERABLE，
    “架构师模型连接中断，已保留当前进度，请重新发起或稍后重试。”
  - `ARCHITECT_INTERNAL_ERROR`：明确的内部错误文案。

## 验证

- 新增 `tests/test_architect_llm_retry.py` 5 项：
  - 流式中断自动重试并成功；
  - 非瞬态 401 不重试；
  - 重试预算上限；
  - token reset 上下文作用域；
  - 错误信封文案不再出现“未预期问题”。
- 真实 DeepSeek Architect 澄清冒烟：成功返回
  `architect.clarification.required`，包含 `ui.layout` 与
  `ui.interactions` 两个问题。
