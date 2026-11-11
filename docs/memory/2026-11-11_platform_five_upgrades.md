# 2026-11-11 · 通用 AI 全栈研发平台五项升级

## 目标

在已具备生产级全栈生成能力的基础上，补齐分级提问、澄清体验、开发规范
Skill、AI-Native 产物与全栈导入五块能力。

## 已完成

1. **DecisionImpact 分级提问**
   - 新增 `CRITICAL / MAJOR / MINOR` 枚举；旧值 `CORE_BUSINESS` /
     `IMPLEMENTATION_DETAIL` 读取时归一化。
   - `should_interrupt()` 统一裁决：CRITICAL 按置信度/阈值中断；
     MAJOR 仅在显式缺信息时中断；MINOR 永不中断。
2. **Architect 10 轮澄清 + 一键采用建议**
   - 默认与硬上限均为 10；前端按钮提交 `adoptSuggestions=true`。
   - 该字段只由澄清 API 接收，初始状态固定 false，模型无法写入；
     合成答案标记 `source=USER_ADOPTED` 供审计。
3. **Web 安全 Skill**
   - 新增 `config/skills/web_best_practices.skill.md`，强制挂载 BackendCoder。
   - 契约出现登录/密码/鉴权意图时生成 `server/security.js`（Bcrypt rounds=12、
     输入白名单、排序白名单）并加入 `bcryptjs` 依赖。
4. **AI-Native**
   - 契约新增 `includeAiCapability`；为 true 时后端生成 `openai` SDK 真实调用
     `/api/v1/ai/chat`、`/api/v1/ai/analyze`，缺 Key 返回 503；
     前端生成 AI 对话/上传面板；`.env.example` 与 `DEPLOY.md` 注入
     `OPENAI_API_KEY` / `OPENAI_MODEL`。
5. **全栈导入**
   - `/import` 支持 `.zip / .tar.gz / .tgz`；tar 安全拒绝穿越路径、符号链接
     与非普通文件。
   - 识别 root `package.json` + `server/` + 前端入口后，同时启动 Express（3000）
     与 Vite（5173），自动补 `/api` 代理。
   - 重新注入 `data-source` 并挂载运行时 MutationObserver 兜底，响应返回
     `kind=fullstack`、`services`、`dataSourceCount`。

## 验证

- Python 306 项单测全绿（新增 20 项）。
- 真实导出→导入实测：普通全栈包与 AI 全栈包均返回 `kind=fullstack`；
  Vite 页面、`/api/health`、CRUD 与 AI 503 均通过 Vite 代理验证。
- Playwright：两个导入项目 iframe 内分别有 26 / 39 个 `data-source`
  节点，AI 面板 `[data-source="ai-workspace"]` 可见，无页面 JS 错误。
- 前端 `npm run build` 通过；Java BFF Maven 构建通过。

## 边界

- 全栈导入仍是单容器多进程，未做 web/api/db 多服务编排。
- AI 能力验证到“真实 SDK 调用 + 缺 Key 503”；未使用真实 OpenAI Key
  发起计费请求。
- 导入器保留 `.env.example`，剔除真实 `.env`；`data/fullstack.sqlite`
  仍随导出包导入，后续可考虑改为仅导入源码。
