# 2026-11-16 局部 VisualNode Agent 与冻结审查

## 已完成

### 1. 局部 JSON Patch Agent

- 新增 `POST /api/v1/visual-agent/nodes`（BFF 同步透传）。
- 输入：projectId / documentId / nodeId / instruction / 当前容器 VisualNode /
  existingIds / maxNodes。
- Coder Prompt 强制：
  - 只输出 VisualNode JSON 数组；
  - 严禁 JSX / HTML / TSX / Markdown；
  - 禁止 page 根、绝对坐标、position/inset/translate 与定位 Tailwind；
  - 非容器不得有 children；
  - 最多 40 节点、深度 6。
- 服务端确定性收紧：
  - 所有 id 重新生成，避免模型碰撞；
  - 拒绝 page、坐标字段、定位 class、raw HTML/JSX；
  - 临时 VisualDocument 做完整 Schema 校验；
  - 第一次失败自动带错误反馈 repair 一次；
  - 仍失败返回 `VISUAL_NODE_INVALID`，不写入画布。
- 前端：
  - 节点悬浮工具栏新增 ✨ AI 生成；
  - 弹窗输入需求，发送当前节点 JSON；
  - `insertNodes` 一次命令插入整组节点，只增加一次 revision；
  - 不触发沙盒、源码构建或 Direct Ops。

### 2. 冻结与业务审查

- 画布顶部新增“确认视觉，进入业务审查”。
- `freezeVisualDocument`：
  - status=FROZEN；
  - review.frozenAt；
  - revision + 1；
  - Web Crypto SHA-256 稳定 JSON → `metadata.contentHash`；
  - 清空 undo/redo 历史。
- Zustand 所有编辑命令在 FROZEN 时返回 false；拖拽、添加、删除、属性、
  撤销/重做、调色板全部禁用。
- 语义审查包：
  - 节点层级（nodeId/parentId/path/type/name/text/purpose/repeat）；
  - interactions / bindings / unresolved 缺口；
  - contentHash / revision / summary；
  - 不包含像素坐标、DOM 或源码。
- VisualCanvasPage 把审查包写入 sessionStorage 并跳回工作台。
- WorkspacePage 延迟 300ms 消费（避开 StrictMode abort），启动 Architect：
  - 首轮强制停在业务澄清；
  - 不被通用技术栈问题挤掉；
  - test/真实 Mock 均能询问“登录按钮需要什么鉴权能力？”。
- 画布重开后显示“已冻结 · sha256:...”，添加与属性面板均只读。

## 验证

- Python 641 项 unittest 全绿。
- 前端 37 项 Vitest 全绿，生产构建通过。
- BFF 单测通过。
- 真实 DeepSeek 业务审查：读取 packet 后返回 auth.method 问题（JWT / Session / OAuth）。
- 真实 DeepSeek 局部 Agent：
  - 首轮 `importance=high` 枚举漂移，自动 repair；
  - 最终 form + 2 input + button，Schema 通过，无 JSX。
- `scripts/visual_canvas_agent_e2e.js` 真实 E2E PASS：
  - 空白画布 → ✨ 生成登录表单 → 节点增加；
  - contentHash `sha256:...`；
  - Architect 首轮鉴权问题出现；
  - 重开画布只读；
  - 0 个 `/api/v1/runs`、`/sandbox`、`/direct-ops`。

## 尚未完成

- 业务审查答案回填 bindings 后的二次冻结。
- CodeGen 确定性映射器与 LLM 兜底编译门禁。
- 多项目 VisualDocument 存储隔离。
