# 2026-11-10 · 前端卡顿修复 + 真实 DeepSeek 基线

## 1. 前端卡顿根因与修复

根因不是 SSE token 洪泛（后端仅节点级事件），而是：

1. `WorkspacePage` 每次 SSE 批次都全量替换 `runStates`，所有 Tab 一起重渲染；
2. 内联回调让 `ProjectWorkspace` 的 memo 完全失效；
3. 每次状态变化同步 `JSON.stringify + sessionStorage`（runStates 与 architect 双份）；
4. 页面级 400ms 计时器无条件刷新；
5. 每个打开过的 Tab 都保留 canvas + runtime 两个 iframe；
6. `backdrop-filter` 叠在 iframe 上，合成开销高。

修复：

- `runStream.js`：固定 100ms 批量提交；`node.progress` 按节点合并；终态事件立即 flush。
- `WorkspacePage`：一批 SSE 事件折叠为一次 `patchRun`；`workspaceActionsRef` + 稳定 `projectActions` Map；`ProjectWorkspace` / `CanvasStage` / `RuntimePanel` 全部 `memo`。
- 非激活 Tab：CanvasStage 卸载 live iframe；RuntimePanel 卸载 ScaledPreview；切回自动恢复。
- `runStates` 与 Architect 持久化改为 800ms 防抖，`pagehide` 强制 flush。
- 计时器下沉到 RuntimePanel 的 `useElapsed`，仅运行中每秒 tick。

## 2. 真实 DeepSeek 基线

运行：

```bash
docker compose up -d python-ai java-bff
python3 scripts/run_eval.py --llm-provider deepseek
```

结果（2026-11-10）：

- 10 例 / 7 过 / 3 失败 = **70%**
- 耗时 57.36s；26,223 tokens；预估 ¥0.050088
- 失败分析：
  - `case_001` / `case_005`：断言只认 `background-color:red/blue`，真实模型输出 React `backgroundColor: '#B91C1C'/'#2563eb'`，语义正确但断言格式过窄（假阴性）。
  - `case_004`：模型使用 `bg-red-600` 等 Tailwind 类，但沙盒模板未安装 Tailwind，构建虽通过、视觉未变红（真实失败）。

## 3. 稳定性与成本

- `DeepSeekAgentLLM.plan/generate` 增加结构化输出自修复：校验失败把原始输出 + 错误回传模型，最多 `AGENT_JSON_RETRY_LIMIT=2` 次；Planner 最终降级纯文本计划，Coder 仍失败才抛错。
- `DeepSeekClient` 采集 `usage_metadata` / `token_usage`，兼容 DeepSeek `prompt_cache_hit_tokens`。
- Agent / Architect LangGraph 每个节点用 ContextVar 采集器归因，`token_usage` reducer 跨节点、跨澄清恢复累加；随 `node.finished`、Run 响应与终态事件下发。
- 价格默认 DeepSeek 目录价（元/百万 tokens），`LLM_PRICING` 可按账单覆盖。
