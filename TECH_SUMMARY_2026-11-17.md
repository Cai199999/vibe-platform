# vibe-platform · 2026-11-17 技术总结

> 范围：Architect 流式稳定性、双轨制项目模式、VisualDocument 多页面与两阶段后端绑定、
> 方案文档投影、存量 React 只读投影、源码级 Agent 语义工具。
>
> 证据：`docs/memory/2026-11-17_*.md`、`scripts/visual_codegen_e2e.py`、
> `scripts/visual_codegen_playwright.js`、`reports/visual_codegen_e2e.png`、
> `reports/source_projection_sample_e2e.png`。

---

## 一、今日任务清单

| # | 时间 | 任务 | 结果 |
|---|------|------|------|
| 1 | 08:38 | VisualDocument 确定性 CodeGen 与 Babel 编译门禁 | ✅ 单测 + 真实沙盒 E2E |
| 2 | 08:52 | 输出完整 Tailwind/Vite 工程并接入 Playwright 验证 | ✅ 渲染与 0 报错校验通过 |
| 3 | 09:39 | 存量 React 源码只读投影 Phase 1 | ✅ 133 节点投影，0 浏览器错误 |
| 4 | 09:43 | 新增可导入存量示例，修复 `className` 表达式解析 | ✅ 示例导入 E2E 通过 |
| 5 | 09:50 | 修正存量投影文案，设计 AST 直改命令层 | ✅ `docs/design/12` 落地 |
| 6 | 10:04 | Architect 流式断连自动重试与可恢复错误 | ✅ 5 项新增单测 + 真实冒烟 |
| 7 | 11:17 | 双轨制项目模式分流与存量模式门禁 | ✅ 前后端判定 + 浏览器 E2E |
| 8 | 11:48 | 新建项目两阶段：Visual V0 → 后端绑定 | ✅ V0→编辑→冻结→绑定 E2E |
| 9 | 11:54 | V0 后端字段确定性剥离，消除无效修复循环 | ✅ 修复耗尽不再落通用错误 |
| 10 | 12:14 | 多页面 VisualDocument 与高保真预览 | ✅ 页面 Tab + 多页 CodeGen |
| 11 | 13:19 | 网页模拟预览与全量业务审查范围 | ✅ 稀疏 JSON 不再像白纸 |
| 12 | 14:00 | 方案文档投影、质量分级一次、CodeGen 示例降级 | ✅ 多页面沙盒 + Playwright |
| 13 | 17:04 | 恢复画布选择 SDK 与前端 Agent 语义工具 | ✅ 696 项 Python 全通过 |

---

## 二、今日技术栈

### 2.1 前端

- **React 18 + Vite 5**（`apps/platform-web`）
  - `VisualDocument` 多页面 Schema：`pages / activePageId / targetPageId`；
  - 画布页面 Tab、新增页面、高保真预览 `VisualDocumentPreview`；
  - 真实 HTML/CSS + designTokens CSS 变量渲染，不是截图或画布重绘；
  - `visualPreview.js` 模拟层：按 path/title 补统计卡、表格、表单、卡片、页脚；
  - 存量只读投影：`sourceProjection.js`（纯函数）→ `ReadOnlySourceCanvas`；
  - 方案文档投影：`workspace/projectDocs.js` → `ProjectDocsDrawer`；
  - 状态：Zustand（画布）+ localStorage/sessionStorage（V0 基线、导入源文件名）；
  - 测试：Vitest 65 项，`vite build` 通过。

### 2.2 Java BFF

- **Java 21 / Spring Boot 3.3.5**（`apps/platform-bff`）
  - `ArchitectStartRequest` 透传 `projectMode` / `outputPhase`；
  - 新增 `SourceAnalysisController` / `SourceAnalysisService`，只读透传源码分析；
  - 仍只做鉴权、编排与 SSE 代理；BFF 无持久化。

### 2.3 Python AI Core

- **Python 3.12 + FastAPI + LangGraph**（`services/orchestrator/ai_core`）
  - Architect Graph：澄清 → 契约 → 校验 → 冻结/绑定；
  - `ProjectModeResolver`：`NEW_PROJECT` / `EXISTING_SOURCE` 确定性判定；
  - `outputPhase`：`VISUAL_V0 | BACKEND_BINDING | FULL`；
  - 确定性 CodeGen：`codegen/visual.py`；
  - 编译门禁：`codegen/visual_gate.py` + `visual_codegen_check.mjs`；
  - 存量分析：`source_analysis/service.py` + Node/Babel 分析脚本；
  - 前端 Agent 工具：`tools/frontend_visual_tools.py`；
  - 模型：DeepSeek 真实调用（按任务路由），鉴权仍是 Mock。

### 2.4 生成代码与沙盒

- **Tailwind JSX + Vite 完整工程**：`package.json`、`index.html`、
  `src/main.jsx`、`src/App.jsx`、`src/visual.css`、`vite.config.js`；
- 每个节点注入 `data-node-id`、`data-visual-type`、`data-source="file:line"`；
- 生成工程自带 `public/inspect_sdk.js` 与 `/api → 127.0.0.1:3000` 代理；
- **Docker 沙盒**：`npm install` / `npm run build` / `npm run dev`；
- **Playwright（真实 Chromium）**：节点数量、可见性、文本、placeholder、
  `data-source`、Tailwind 背景色、多页面导航点击、控制台/页面错误。

### 2.5 分析与门禁

- **Babel Parser**：存量 JSX/TSX 分析、生成代码 AST 校验，禁止正则回退；
- 编译门禁白名单：`react` / `react-dom/client` / `vite` /
  `@vitejs/plugin-react` / 本地模块与样式；
- 拒绝 `dangerouslySetInnerHTML`、`position:absolute/fixed`、绝对定位 Tailwind；
- 每个 `VisualNode` 必须且只能出现一次 `data-node-id`，必须有 `data-source`；
- 门禁不可用时抛 `VisualCodegenGateUnavailable`，不静默放行。

### 2.6 测试

- Python `unittest`：696 项通过；
- 前端 Vitest：65 项通过；
- Python E2E：`scripts/visual_codegen_e2e.py`（真实 Docker 沙盒）；
- Playwright：`scripts/visual_codegen_playwright.js`（真实浏览器）；
- 浏览器交互 E2E：V0 → 画布编辑 → 冻结 → BACKEND_BINDING、存量导入投影。

### 2.7 基础设施

- Docker Compose 管理应用容器；复用 `~/Workspace/infra` 的 MySQL / Redis / MinIO；
- 沙盒未使用 gVisor，未做云端部署。

---

## 三、问题、方案与取舍

### 问题 1：Architect 澄清断连被显示为“系统遇到未预期问题”

**现象**

前端只看到“架构澄清失败 / 系统遇到未预期问题，已记录诊断编号”，
用户不知道是否可以重试。

**根因**

- DeepSeek SSE 在产出部分 token 后断连：
  `peer closed connection without sending complete message body (incomplete chunked read)`；
- `ArchitectLLM._generate_with_repair` 只对 JSON 校验错误重试，传输级
  `ModelClientError` 直接冒泡；
- `ARCHITECT_LLM_ERROR` 未注册 ErrorSpec，错误信封降级为 `INTERNAL_ERROR`。

**方案**

1. 连接级瞬态重试：`ARCHITECT_LLM_RETRY_LIMIT=2`，指数退避 0.8s / 1.6s，
   上限 4s；只匹配流式中断、`peer closed`、502/503、timeout、rate limit；
2. 重试前发送 `architect.token.reset`，前端清空已展示的半截流式 JSON；
3. 注册可恢复错误码 `ARCHITECT_LLM_ERROR`，返回“已保留当前进度，请重新发起或稍后重试”。

**为什么这样取舍**

- 鉴权、参数、模型不存在等非瞬态错误重试没有收益，必须立即失败；
- 半截 JSON 不能留给前端拼接，否则重试成功后契约会被污染；
- 传输层错误必须与业务错误分开，不能让用户为网络问题看到“内部错误”。

---

### 问题 2：存量项目被误判为新建，Architect 凭空设计页面

**现象**

存量 React 项目进入 Architect 后，模型生成 VisualDocument、DDL 与 relations，
出现“父键 id 不在父表字段”等无中生有的契约错误。

**根因**

系统没有确定性的项目模式判定，UI 把“已导入源码”与“空白新建”当成同一条链路。

**方案**

- 前端 `workspace/projectMode.js`、后端 `domain/project_mode.py` 双重判定：
  - imported / `template=imported-*` / generated / sandbox / 非内置模板非空 files
    → `EXISTING_SOURCE`；
  - 空白新建、仅内置演示模板 → `NEW_PROJECT`；
  - 后端以 `source.sandbox_id` / 非空 `source.files` 为事实，前端声明只作回退。
- 新建项目强制二选一：【从零开始生成】/【导入源码分析】。
- 存量模式门禁：
  - 不渲染可编辑虚拟画布，只渲染只读投影；
  - Architect Prompt 禁止 VisualDocument / 整页 JSX / 重新设计页面；
  - 契约跳过 `ui.layout` 槽位与 DDL relations 自检；
  - 禁止 frontend/fullstack CodeGen，只运行 backend 增量子图；
  - UI 文案改为“生成后端增量”。

**为什么这样取舍**

项目模式是源码事实，不是用户偏好；让前端声明兜底但以后端 source 为准，
可以避免“同一项目在不同入口被判定成两种模式”。存量项目也不会被转成虚拟树，
源码始终是唯一事实源。

---

### 问题 3：导入源码后跳转丢失项目 / StrictMode 重复副作用

**现象**

新建项目选择“导入源码分析”后跳转画布，工作台里找不到刚导入的项目，
或导入动作被重复执行。

**根因**

导入后立即 `navigate`，workspace 尚未同步落盘；`WorkspacePage` 卸载后
内存项目丢失。React StrictMode 下副作用重复执行放大了问题。

**方案**

- 导入后先同步落盘 workspace，再跳转 `/visual-canvas?project=...&mode=existing`；
- 源码统一落到 `src/App.jsx`，原始文件名写入 sessionStorage 供投影展示；
- 导入流程与空白新建彻底分流，不共用“先创建再填充”的旧路径。

**为什么这样取舍**

单文件导入统一落为 `src/App.jsx`，不尝试还原多文件工程——当前阶段目标是
“看到结构并做后端增量”，还原工程属于后续 AST 命令层与多文件分析范围。

---

### 问题 4：VISUAL_V0 的“禁止后端字段”变成无意义修复循环

**现象**

真机出现“VISUAL_V0 阶段禁止生成 API / DDL / relations / 交互契约”，
修复次数耗尽后落成通用内部错误。

**根因**

V0 把“模型多写后端字段”当成阻塞校验；模型在 repair 时仍可能输出完整契约，
同一输入必然得到同一错误，形成无效循环。

**方案**

- `validate_draft` 与 `assemble_contract` 两处**确定性剥离**后端字段；
- 只有 `visual_document` 本身不合法才进入 repair；
- 修复耗尽错误码改为 `VISUAL_V0_INVALID`，返回可重试的友好文案；
- V0 图停在 `AWAITING_VISUAL_EDIT`，不进入契约确认 interrupt。

**为什么这样取舍**

提示词只能表达意图，不能作为唯一门禁。模型多写字段是可预期的输出偏差，
确定性剥离比反复要求模型“只输出视觉树”更可靠，也不会浪费模型调用与构建时间。

---

### 问题 5：单页、稀疏的视觉树让画布“不像网页”

**现象**

V0 只生成一页、内容单薄；高保真预览接近白纸，无法判断产品形态。

**根因**

Schema 只有单页 `root`；V0 JSON 只描述结构，没有业务数据与页面级布局；
预览直接渲染稀疏节点，缺少真实网页的统计卡、表格、表单等密度。

**方案**

1. **多页面 Schema**：`VisualPage`（`pageId/path/title/root`）、
   `pages/activePageId`；节点 ID 全局唯一，页面路径唯一；
   `VisualInteraction.targetPageId` 支持 navigate；
   旧单页文档自动归一为一页。
2. **画布页面 Tab**：新增页面、切换页面、删除页面根节点后的合法回退。
3. **高保真预览**：真实 HTML/CSS 渲染，共用 designTokens，页面 Tab + navigate 切换。
4. **网页模拟预览**：稀疏 JSON 自动补统计卡、工具栏、表格、卡片、表单、页脚；
   页面已有 ≥7 个有效业务节点时不叠加模板；模拟层只读，不写回。
5. **多页 CodeGen**：每页生成 `PageX` 函数，`VISUAL_PAGES` 列表 + `useState`
   当前页，navigate 交互生成 `onNavigate(targetPageId)`。

**为什么这样取舍**

- 导航先用客户端 state，不引入 React Router：当前是静态原型生成，
  路由属于后续工程化阶段；
- 线框编辑与高保真预览是两套渲染，但共用同一份 `VisualDocument`，
  避免出现两个事实源；
- 模拟数据只存在于预览层，不写入文档，不会污染 CodeGen 与契约。

---

### 问题 6：未绑定 repeat 在冻结、审查、CodeGen 三处语义冲突

**现象**

`repeat.source` 未绑定时，有时阻止进入业务审查，有时被 CodeGen 硬门禁拦截，
最终被包装成“前端工程师失败 / 通用内部错误”。

**根因**

冻结、业务审查、代码生成三个阶段对“未绑定”的要求不同，但早期共用一条硬门禁。

**方案**

- FROZEN 阶段：未绑定 repeat 只给 warning，不阻止进入业务审查；
- 业务审查：第二次 Architect 审查**全部**交互与 repeat，不只处理 delta；
  semantic packet 新增 `interactiveComponents` / `repeatsWithoutBinding`；
- CodeGen：先用 `sampleCount` 示例数据渲染，未绑定写入 compile warnings，
  不再阻断静态原型生成；
- 一度新增 `VISUAL_UNBOUND_REPEAT` 硬门禁，真实 E2E 后按产品语义降级为 warning。

**为什么这样取舍**

静态原型阶段不需要生产数据绑定；真正需要闭环的是进入真实数据/COMPILED 之前。
warning 保留了可观测性，又不会让用户因为一个未绑定列表而拿不到可运行页面。

---

### 问题 7：质量分级被反复询问

**现象**

契约修订、重新确认时再次弹出质量分级；默认 L2 被误判为“必须询问”。

**根因**

质量分级是否已选择没有持久状态，每次都按当前契约重新判断。

**方案**

- 新增 `quality_tier_selected`：首次契约确认前为 `false`；
- 用户选择并 CONFIRM / REVISE 后置为 `true`；
- 契约修订、重新确认沿用该选择；
- 模型生成的 quality-tier 澄清问题被确定性过滤。

**为什么这样取舍**

质量分级是用户的一次性决策，不是每轮契约的派生属性；
把它建模成状态，比在每轮 Prompt 里要求模型“不要重复问”更可靠。

---

### 问题 8：存量分析 `className` 表达式取值错误

**现象**

`className={...}`、模板字符串等写法在只读投影中样式丢失或取值不正确。

**根因**

`extractClassName` 的参数语义不一致：调用方传 `JSXAttribute.value`，
函数却按 `JSXAttribute` 判断类型，导致字符串、表达式容器与表达式节点混用。

**方案**

- 统一由 `JSXAttribute` 解包 `value`；
- 区分三类：
  1. `StringLiteral`：直接取字符串；
  2. 无插值 `TemplateLiteral`：拼接 quasis；
  3. `JSXExpressionContainer`：先受限静态求值，取不到时收集字符串字面量，
     并保留 `expression` 原文。
- 只做静态可确定的求值，不猜测运行期表达式。

**为什么这样取舍**

存量源码是只读投影，不能为了“看起来完整”而伪造 className；
保留表达式原文比猜测更诚实，也为后续 AST 命令层留下真实源码位置。

---

### 问题 9：CodeGen 产物不足以启动沙盒

**现象**

沙盒首轮 `npm run dev` 因缺少 `package.json` / `index.html` 失败；
CodeGen 只生成 `App.jsx`，无法构成可运行工程。

**根因**

早期把 CodeGen 当成“生成一个组件文件”，但沙盒需要完整 Vite 工程；
容器创建发生在编译之前，初始文件不完整。

**方案**

- 确定性 Compiler 输出完整 Vite 工程（`package.json`、`index.html`、
  `src/main.jsx`、`src/App.jsx`、`src/visual.css`、`vite.config.js`）；
- `_ensure_sandbox` 在创建容器前先编译并写入初始文件；
- `mode=frontend` 也执行 `npm install`；
- 全栈模式以 base bundle 的 `package.json` / `main.jsx` / `vite.config.js` 为准，
  只覆盖 `App.jsx` / CSS / `index.html`；
- 编译门禁失败 → 扁平安全占位页（仍保留全部 `data-node-id`）；
- 构建失败 → BuildVerifier 重写安全占位页并只重试一次；
- 成功后写 ChangeLog：`成功从冻结版本 {contentHash} 编译生成真实前端工程`。

**为什么这样取舍**

基础节点用确定性编译器，不用 LLM：结构、`data-node-id`、`data-source`
必须可验证、可复现；custom 节点暂时生成安全占位，避免把不可编译代码写进沙盒。
门禁放在落盘前，构建失败也绝不留下不可编译源码。

---

### 问题 10：生成的 Vite 工程缺少画布选择 SDK 与 `/api` 代理

**现象**

iframe 内“选择元素”按钮无反应；生成应用的 `/api/v1/users/me` 返回 404。

**根因**

- 生成的 `vite.config.js` 只有 `react()`，没有注入 `inspect_sdk.js`；
- 没有 `/api` 代理，请求落到 Vite fallback 或平台 BFF；
- SDK 选择器只按标签/类名匹配，AI 新增节点无法命中。

**方案**

- `compile_visual_document` 将 `sandbox_template/inspect_sdk.js` 写入
  `public/inspect_sdk.js`，并在 `index.html` 注入脚本；
- 生成的 `vite.config.js` 内置 `/api → 127.0.0.1:3000` 代理；
- SDK 的选择、拖拽、直接编辑选择器增加 `[data-node-id]` 兜底；
- CodeGen 节点补充 `data-source-kind`，容器识别不依赖标签名猜测；
- E2E 增加 `/inspect_sdk.js` HTTP 200 与代理配置断言。

**为什么这样取舍**

生成工程必须是自包含的运行时契约：SDK、代理、入口文件都应在产物内，
不能依赖平台侧事后注入，否则“本地能跑、换个环境就不能跑”。

---

### 问题 11：源码级 Agent 工具不能另起写路径

**风险**

前端 Agent 需要“改文案 / 移动 / 增删 / 撤回”，很容易 fork Utopia 或新增一套
AST 执行器，形成第二条写路径，绕开既有的锁、门禁与 ChangeLog。

**方案**

- 不 fork Utopia，只学习其不变量：源码唯一事实源、稳定节点身份、
  语义命令层、命令可回放；
- 工程模型采用 Onlook：iframe 运行应用 + 构建期注入节点 ID + DOM ↔ AST 映射；
- 新增 `frontend_visual_tools.py`，6 个工具全部是 `DirectOperationService` 的门面：
  `update_text`、`move_node`、`insert_component`、`delete_node`、
  `get_frontend_revision`、`undo_last_frontend_change`；
- `node_id` 自动转换为 `[data-node-id="..."]` 选择器；
- 未绑定项目上下文时 fail-closed；
- 写工具支持 `dry_run` 与显式 `base_revision`，缺省读取当前 HEAD；
- `AgentGraphRunner.run` 按 Run 的 `project_id/sandbox_id` 绑定上下文，
  `finally` 恢复，避免串 Run；
- 目标解析、ScopePolicy、revision 锁、AST Gate、ChangeLog、Git checkpoint 全部复用。

**边界**

暂不支持 `UPDATE_CLASS/STYLE/PROPS`、wrap/unwrap、跨文件移动；
这些能力先扩展 Direct Ops，再由工具层暴露。前端 Agent 不接触源码文本、
anchorRange 与文件写入。

**为什么这样取舍**

工具层只做“语义门面”，不拥有写权限。所有写操作仍经过唯一 AST 门禁，
这样新增 Agent 能力不会降低安全边界，也不会产生两套状态。

---

### 问题 12：方案文档既要可查看，又不能成为第二事实源

**现象**

用户需要 01–05 方案文档，但如果让 LLM 另生成一套文档，就会与契约、
VisualDocument 产生事实冲突。

**方案**

- `workspace/projectDocs.js` 从 contract / VisualDocument **确定性投影** 5 份文档：
  01 需求与功能模块清单、02 技术选型与架构设计、03 数据库设计文档、
  04 API 接口设计清单、05 最终设计方案与开发规范；
- `simulated=true`，不写回、不调用 LLM、不作为第二事实源；
- 文档带 `docId / revision / contentHash / status / sections`；
- `ProjectDocsDrawer` 支持 01–05 Tab、元信息、单份/全部 Markdown 导出；
- 文档名称与章节作为后续 Agent 的统一引用规范。

**为什么这样取舍**

文档是契约的只读视图，不是新的输入。先固定命名与章节，未来替换为正式
文档生成器时，引用关系不需要重写。

---

## 四、验证结果

```text
Python unittest      696 passed
前端 Vitest           65 passed
前端生产构建          vite build passed
真实沙盒 E2E          visual_codegen_e2e.py PASS
真实浏览器 E2E        visual_codegen_playwright.js PASS
```

关键证据：

- **CodeGen**：冻结 6 节点 → `data-node-id` + `data-source` 完整 →
  完整 Vite 工程 → `npm install` → `npm run build` → `npm run dev` →
  ChangeLog 写入；
- **Playwright**：节点数量 / 可见性 / 文本 / placeholder / `data-source` /
  Tailwind 背景色全部通过；0 页面错误、0 控制台错误；
- **多页面**：冻结文档含未绑定 `ghost_items`，构建仍成功；
  实际点击“用户页”切换成功，0 报错；
- **SDK / 代理**：`/inspect_sdk.js` HTTP 200，`/api` 代理配置断言通过；
- **V0 → 后端绑定**：新建 → V0 自动打开画布 → 添加节点 → 冻结 →
  第二次 Architect 请求 `outputPhase=BACKEND_BINDING`、
  `visualDelta.added=1`、`visualDocument.status=FROZEN`；
- **双轨制**：导入 `samples/ExistingDashboard.jsx` → 只读投影 133 节点；
  未渲染可编辑画布；存量项目 Tab 显示“存量源码模式”；0 浏览器错误；
- **Architect 重试**：真实 DeepSeek 澄清返回 `architect.clarification.required`，
  包含 `ui.layout` 与 `ui.interactions`。

截图：

- `reports/visual_codegen_e2e.png`
- `reports/source_projection_sample_e2e.png`

---

## 五、今日提交

| 提交 | 内容 |
|------|------|
| `e342644` | VisualDocument 确定性 CodeGen 与编译门禁 |
| `3641a8b` | 完整 Tailwind 工程 + Playwright 验证 |
| `2ca2562` | 存量 React 源码只读投影 Phase 1 |
| `300e744` | 可导入存量示例 + `className` 表达式修复 |
| `578edc1` | 存量投影文案修正 + AST 直改命令层设计 |
| `0c2f6af` | Architect 流式断连自动重试 |
| `db73bf4` | 双轨制项目模式分流与存量门禁 |
| `bf31625` | 两阶段 Visual V0 与增量后端绑定 |
| `2b68161` | V0 后端字段确定性剥离 |
| `4ab1e06` | 多页面 VisualDocument 与高保真预览 |
| `408fa4a` | 网页模拟预览与全量业务审查范围 |
| `1cb7483` | 方案文档投影、质量分级一次、CodeGen 示例降级 |
| `b86160f` | 画布选择 SDK 与前端 Agent 语义工具 |

---

## 六、经验总结

1. **传输层错误与业务错误必须分开**：断连要可恢复、可重试，
   不能降级成“系统未预期问题”。
2. **项目模式是事实判定，不是 UI 选择**：后端 source 优先，
   前端声明只作回退；存量项目绝不转虚拟树。
3. **确定性剥离优于反复提示**：模型多写的字段用代码剥离，
   不要用 repair 循环赌模型下一次会听话。
4. **冻结、审查、生成三阶段门禁语义不同**：静态原型可以 warning，
   真实数据/编译前才需要硬闭环。
5. **预览是只读视图**：模拟数据、方案文档都不得写回事实源；
   一份 `VisualDocument` 同时服务线框、预览与 CodeGen。
6. **生成工程必须自包含**：SDK、`/api` 代理、入口文件随产物输出，
   不依赖平台侧事后注入。
7. **Agent 工具只做门面**：复用 Direct Ops 的唯一写路径、锁、门禁与 ChangeLog，
   不新增 AST 执行器。
8. **真实沙盒与真实浏览器验收**：Playwright 验证的是可运行产物，
   不是 mock 协议形状；今天的 SDK、代理、多页切换问题都只在真实环境暴露。
9. **一次性用户决策要建模成状态**：质量分级选择持久化，
   比每轮 Prompt 提醒更可靠。
10. **文档是契约的视图**：01–05 方案文档先固定命名与章节，
    但 `simulated=true`，不成为第二事实源。

---

## 七、当前边界与下一步

### 当前边界

- DeepSeek 真实调用；鉴权仍为 Mock Header，未接入真实 JWT / OIDC；
- BFF 无持久化，指标为进程内聚合；
- 沙盒未使用 gVisor 等强隔离方案；未云端部署；
- 存量分析仅单文件、函数组件；不处理跨文件组件、HOC、render props、
  `React.createElement`、动态导入；
- 存量导入统一落为 `src/App.jsx`，不还原原始多文件工程；
- 多页导航是客户端 state 切换，不是 React Router；
- custom 节点 CodeGen 仍是安全占位，不调用 LLM 做局部 TSX；
- AST 直改命令层尚未实现（`docs/design/12`）。

### 下一步

- **P0**：源码持久化、`node-id → AST` 索引、实例路径；
- **P1**：`class/style/props`、wrap/unwrap、预览/构建验证；存量 Phase 2；
- **P2**：Redis / 云端部署、真实鉴权与租户隔离；
- 扩展前端 Agent 语义工具前，先扩展 Direct Ops 的命令能力；
- 将编译门禁与真实沙盒 E2E 接入 CI，避免回归只靠本地脚本。
