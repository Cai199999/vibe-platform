# 2026-11-09 · Architect JSON 加固与多 Agent 上下文分层压缩

## 背景

真实 DeepSeek 在 Architect 分析阶段偶尔把 `business_flows` 输出为字符串或
混合数组，Pydantic 校验失败导致接口 500；同时多轮对话把完整 chatHistory /
previousRuns / 工具结果塞进 Prompt，Token 成本随轮次线性上涨。

## 改动

### 1. Architect JSON 三层防护

- `ArchitectAnalysis.business_flows` / `entities` 改为
  `Union[dict, str, list[Union[dict, str]]]`，`field_validator(mode="before")`
  把字符串包装成 `{"step": value}` / `{"name": value}`。
- `ModelClient.generate_json()` 端口 + DeepSeek 适配器绑定
  `response_format={"type":"json_object"}`，网关不支持时自动回退普通调用。
- `DeepSeekArchitectLLM._generate_with_repair()`：捕获 `ValidationError`，
  把截断后的原始输出与错误信息回传模型重试，默认 2 次；仍失败抛
  `ArchitectLLMError`（可读错误，不再是裸 500）。

### 2. 上下文分层压缩

| 层级 | 位置 | 行为 |
|---|---|---|
| 工具结果 | `context_memory.ProjectMemory.save_tool_output` | 完整结果写 `tool_outputs/<sha256>.txt`，Prompt 只留工具名/字符数/哈希/预览（默认 2000 字符） |
| 历史对话 | `agent/memory.py` | `tiktoken` 计 Token；超阈值时最近 `HISTORY_RECENT_TURNS`（10）轮原文保留，旧消息摘要 |
| 意图分级 | `IntentClassifier` | FAST 模型一次批量打 high/low；high 用户消息逐字保留，low 合并摘要；无 Key 回退关键词规则 |
| 局部摘要 | `agent/service.py` | 成功 Run 且 `turn % HISTORY_LOCAL_SUMMARY_INTERVAL == 0` 时写 `local_summary.md`（按 turn 幂等） |
| 全局摘要 / 画像 | `api/sandbox.py` + `refresh_global_memory` | 保存快照后基于全部局部摘要刷新 `global_summary.md` 与 `user_profile.md` |
| Prompt 注入 | `AgentLLM.set_memory_context` | 画像 + 全局摘要作为「只读参考」注入 system，声明当前用户指令优先 |

持久化目录：`CONTEXT_MEMORY_DIR=data/project_memory/<tenant>/<project>/`，
compose 将 `services/orchestrator/data` 挂载到容器 `/app/data`，容器重建不丢。
路径分段白名单清洗，写入使用临时文件 + `os.replace` 原子替换，任何记忆失败
都不阻断 Run / 快照。

### 3. 前端流畅度

- `VirtualList`：变高虚拟滚动，快照历史（>12）与 Agent 轨迹（>30）按需渲染。
- `createFrameBatcher`：SSE 事件在 `requestAnimationFrame` 内合帧处理，
  终态事件强制 flush；避免逐条 setState 引发的大量 DOM 操作。
- 列表 / 事件均使用不可变更新，保持 React 引用相等判断有效。

### 4. 配置（`.env.example` 已补）

`HISTORY_RECENT_TURNS`、`HISTORY_LOCAL_SUMMARY_INTERVAL`、
`HISTORY_LOCAL_SUMMARY_MAX_CHARS`、`CONTEXT_TOKEN_ENCODING`、
`CONTEXT_MEMORY_DIR`、`CONTEXT_GLOBAL_SUMMARY_MAX_CHARS`、
`CONTEXT_USER_PROFILE_MAX_CHARS`、`TOOL_OUTPUT_PREVIEW_CHARS`、
`MODEL_INTENT_NAME`、`ARCHITECT_STRUCTURED_OUTPUT`、
`ARCHITECT_JSON_RETRY_LIMIT`。

## 验证

- Python：`178 tests OK`（新增分层压缩 / 项目记忆 / Architect 修复共 14）。
- Java BFF：`6 tests OK`，新增快照作用域透传与路径穿越拦截用例。
- 前端：`vite build` 通过。
- E2E：快照管理、Architect M2.5、空白 ADD、澄清 UI、开发者面板全部 PASS。
- 真实 DeepSeek：`analyze()` / `draft_contract()` 均返回合法
  `ArchitectAnalysis` / `ContractDraft`，无 JSON 校验异常。
- 30 轮长上下文压缩实测：`15969 -> 8733 tokens`（下降 45.3%），保留
  最近 10 轮原文、最近 2 条 Run，旧内容合并为结构化摘要。

## 注意

- 全局摘要优先使用局部摘要；不足 10 轮时使用快照请求的
  `latestChanges` 生成第一份摘要（见 `2026-11-09_drag_placement_save_gate.md`）。
- `tiktoken` 已加入 `requirements.txt`，离线环境自动回退字符密度估算。
