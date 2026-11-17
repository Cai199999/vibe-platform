# 2026-11-17 VisualDocument CodeGen 阶段三

## 已完成

### 1. 确定性 Compiler（Python）

- 新增 `ai_core/codegen/visual.py`：
  - 基础节点（page/section/container/grid/stack/text/button/input/image/
    list/table/form/modal/component）全部硬编码映射为 Tailwind JSX；
  - 每个节点注入唯一 `data-node-id`、`data-visual-type` 与
    `data-source="src/App.jsx:{line}"`；行号在生成后按真实源码回填；
  - 输出完整 Vite 工程：`package.json`（react/vite）、`index.html`
    （Tailwind CDN）、`src/main.jsx`、`src/App.jsx`、`src/visual.css`、
    `vite.config.js`；
  - 设计令牌生成 CSS 变量；布局字段转 Tailwind 任意值类；
  - 遇到 `absolute` / `fixed` / `top-*` / `translate-*` 直接拒绝；
  - list/table 的 repeat 只输出一次子节点模板，运行时按 sampleCount 渲染，
    保证源码中 nodeId 不重复；
  - 当前静态阶段默认不生成 `onClick` / API 请求；交互生成保留
    `include_interactions=True` 开关；
  - `codegen.mode=custom` 目前生成安全占位组件，不调用 LLM。

### 2. 编译门禁

- 新增 `ai_core/codegen/visual_gate.py` + `visual_codegen_check.mjs`：
  - Babel AST 解析 JSX，禁止正则回退；
  - import 白名单：react / react-dom/client / vite / @vitejs/plugin-react
    / 本地模块 / 本地样式；
  - 每个 VisualNode 必须且只能出现一次 `data-node-id`，且必须有
    格式为 `{filePath}:{line}` 的 `data-source`；
  - 拒绝 `dangerouslySetInnerHTML`、position:absolute/fixed、
    绝对定位 Tailwind；
  - node / Babel 缺失时抛 `VisualCodegenGateUnavailable`，不静默放行。
- 编译门禁失败 → 扁平安全占位页（仍保留全部 data-node-id）；
- 沙盒 `npm run build` 失败 → BuildVerifier 重写安全占位页并只重试一次；
  仍失败才终止。构建失败不会留下不可编译源码。

### 3. 接入 FullstackGraph

- `FullstackRunRequest.visualDocument` 支持前端回传冻结树；
- `mode=frontend` 有 VisualDocument 时不再要求 frontendRoutes / components；
- `_ensure_sandbox` 在创建容器前先编译完整 Vite 工程并写入初始文件，
  避免 package.json / index.html 缺失导致首轮 dev 失败；
- FrontendCoder 有冻结树时走确定性 Compiler，全栈模式以 base bundle 的
  package.json / main.jsx / vite.config.js 为准，只覆盖 App.jsx / CSS /
  index.html；
- Visual CodeGen 即使 `mode=frontend` 也执行 `npm install`；
- 完成事件回传 `visualDocumentId`、`visualNodeCount`、`frontendEntry`；
- 成功后写 ChangeLog：`成功从冻结版本 {contentHash} 编译生成真实前端工程`；
- 事件新增 `fullstack.visual.codegen` 与
  `fullstack.visual.codegen.fallback`。

### 4. 前端闭环

- `loadFrozenVisualDocument(projectId)`：内存 store + localStorage 双读，
  按 projectId 严格隔离，只接受 FROZEN + contentHash；
- 工作台“生成全栈代码”入口在契约含 visualDocument 时自动改走 CodeGen；
- 无沙盒时携带当前入口源码，全栈模式也能创建模板沙盒；
- ArchitectPanel 新增“🚀 生成应用（CodeGen）”状态卡与节点进度；
- 成功后把真实 App.jsx 写回项目、建立 v0 快照、画布切换 live；
- 失败恢复 canvasMode=visual，用户可以重新冻结后重试。

## 验证

- Python 新增 16 项 `tests.test_visual_codegen` 全绿；
- 全量 Python 649 项中仅 7 项与本改动无关的环境问题（MCP demo /
  /app 写权限 / eval_suite 缺依赖）；
- 前端 41 项 Vitest 全绿，生产构建通过；
- 真实 Docker 沙盒 E2E `scripts/visual_codegen_e2e.py` PASS：
  FROZEN 6 节点 → data-node-id + data-source 完整 → 完整 Vite 工程 →
  npm install → npm run build → npm run dev → ChangeLog 写入；
- Playwright `scripts/visual_codegen_playwright.js` PASS：
  节点数量 / 可见性 / 文本 / placeholder / data-source / Tailwind
  背景色全部通过，0 页面错误、0 控制台错误；截图
  `reports/visual_codegen_e2e.png`。

## 尚未完成

- custom 节点的真实 LLM 局部 TSX 兜底（当前为安全占位）；
- 业务审查答案回填 bindings 后的二次冻结；
- 多项目 VisualDocument 存储隔离（当前仍单 localStorage key）；
- 生成后 Direct Ops 基于 data-node-id 的微调链路（阶段四）。
