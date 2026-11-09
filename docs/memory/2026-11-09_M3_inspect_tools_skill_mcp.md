# 2026-11-09 M3.1：精准点击选择 + Tool / Skill / MCP 闭环

## 背景

圈选精度受 DOM 嵌套与坐标系偏移影响；同时 Tool / Skill / MCP 在 M3
之前只有声明或占位。本阶段把三条扩展链路推进到真实可验证。

## 1. Inspect Mode（精准点击选择）

- 沙盒 Vite 插件在**构建期**给 JSX / Vue 模板注入：
  `data-source-file / line / column / component / kind`。
  - React：Babel `JSXOpeningElement` visitor；
  - Vue：`@vue/compiler-sfc` + `@vue/compiler-dom`，绝对行号。
- `inspect_sdk.js` 由 Vite `transformIndexHtml` 注入，跨域 iframe 通过
  `postMessage` 协议通信：`enable / move / pick / clear` →
  `ready / hover / picked`。
- SDK 使用 `document.elementFromPoint` + `closest([data-source])`，
  返回 rect、源码、最近容器、祖先链与视口信息。
- 前端工具栏新增「精准选择」；蓝色高亮按 iframe transform 反算屏幕坐标；
  点击后弹出 `InspectDraftCard`，可切 MODIFY / ADD，生成带
  `type: "visual-draw"`、精确 `target.source`、±2 行 snippet、
  `measured.clickedSource` 的 AnnotationEnvelope。
- 圈选模式保留；`/vue-demo.html` 提供 Vue 验证页。

## 2. Tool：search_web

- `ai_core/tools/web_search_tool.py`：DuckDuckGo HTML / Lite 双端点、
  超时、结果数限制、uddg 跳转解包、可读失败信息。
- LangGraph 新增 `research` 节点（`summarize_history → research → planner`）；
  标注意图命中“搜索 / 查一下 / 最新版本 / 官网…”时真实调用，
  结果并入 plan；失败不阻断 Run。
- Supervisor 网关：`GET /internal/supervisor/tools`、
  `POST /internal/supervisor/tools/{name}/invoke`。

## 3. Skill：Markdown 挂载

- `skill_loader.py` 支持 `*.skill.md`（YAML frontmatter + Markdown 正文）。
- `config/skills/accessibility.skill.md`：WCAG 2.1 AA / WAI-ARIA 约束。
- `agent/manifest.json` 声明 `ui.accessibility`；`DeepSeekAgentLLM`
  初始化时调用 `skill_prompts_to_system("ui_graph", skills_dir)`，
  实际注入 Coder System Prompt。

## 4. MCP：标准 stdio Server

- `scripts/mcp_server_demo.py`：JSON-RPC 2.0，按行分隔，暴露
  `get_current_time`（IANA 时区）。
- `ai_core/mcp/`：自研最小客户端（select 超时、进程隔离、最小环境变量）、
  `inputSchema → Pydantic → StructuredTool` 适配、`allowed_tools` 白名单。
- compose 只读挂载 `./scripts:/app/scripts:ro`；`.env` 配置 `MCP_SERVERS`。

## 验证

- Python 单测 **129 passed**（新增 16：搜索解析、Markdown Skill、
  DeepSeek 注入、研究触发词、MCP 往返、Supervisor 网关）。
- `scripts/inspect_mode_e2e_test.py` PASS：
  - React 高亮 / 点击 → `src/App.jsx:9:9`；
  - Vue 高亮 / 点击 → `src/VueDemo.vue:17:14`；
  - Supervisor route 请求体：`type=visual-draw`，snippet 5 行，
    `measured.mode=inspect`。
- Supervisor HTTP 实测：
  `GET /tools` 返回 3 个工具 + `mcpServers: ["vibe_demo"]`；
  `POST .../mcp_vibe_demo_get_current_time/invoke` 返回 Asia/Shanghai 时间。
- `search_web` 真实调用：当前执行环境无外网，两个 DuckDuckGo 端点均
  `Network is unreachable`；Tool 按设计返回可读失败（8.4s），不抛异常。
  解析器与禁用分支由单测覆盖。

## 环境变量

`TOOL_WEB_SEARCH_*`、`MCP_ENABLED`、`MCP_CALL_TIMEOUT_SECONDS`、
`MCP_SERVERS`，见 `.env.example`。
