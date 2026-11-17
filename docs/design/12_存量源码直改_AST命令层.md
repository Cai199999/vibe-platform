# 12 · 存量项目源码直改（AST 命令层）

> 2026-11-17 · Phase 2 架构设计 · 仅设计，不包含写回实现

## 1. 背景

轨道一（新建项目）走 `VisualDocument → CodeGen → React 源码`。
轨道二（存量项目）**不走虚拟树**，目标是对真实 React 源码做可视化直改：

```
源码 → 只读投影（画布） → AST 命令 → 源码变更 → HMR → 重新解析投影
```

参考 Utopia 的核心思想：源码是唯一事实源，画布是源码的投影，拖拽/编辑
最终都落成结构化 AST 变换。

## 2. 不可突破的红线

1. **源码是唯一事实源**：存量项目不生成、不保存、不回写 `VisualDocument`。
2. **绝不写入 `visualDocumentStore`**：`sourceProjection` 只作为显示模型，
   禁止调用 `replaceDocument` / `addNode` / `moveNode` 等虚拟树命令。
3. **禁止双向同步**：不做“源码 ↔ 虚拟树”自动合并；源码变更后只重新解析
   生成新投影，投影自身永远不反向覆盖源码。
4. **不做整页 LLM 生成**：存量直改只做结构化、可校验的 AST 命令。
5. **命令必须可审计、可回滚**：每条命令带 commandId / baseHash / diff，
   写入前通过 AST 与构建门禁，失败自动回滚。

## 3. 分层架构

```
┌────────────────────────────────────────────────────────────┐
│ Frontend                                                   │
│  ReadOnlySourceCanvas / SourceProjectionCanvas             │
│        │ hover / select / drag                             │
│        ▼                                                   │
│  ASTCommandBuilder（纯函数：交互 → ASTCommand）            │
│        │ 只产生数据，不直接改源码                          │
│        ▼                                                   │
│  sourceEditClient.js                                       │
└────────┼───────────────────────────────────────────────────┘
         │ POST /api/v1/source-edit/commands
┌────────▼───────────────────────────────────────────────────┐
│ BFF                                                        │
│  SourceEditController / SourceEditService                  │
│  - 透传命令、项目写锁、SSE/JSON 结果                       │
└────────┼───────────────────────────────────────────────────┘
         │ /api/v1/internal/source-edit/commands
┌────────▼───────────────────────────────────────────────────┐
│ Python AI Core                                             │
│  SourceEditService                                         │
│  - 命令白名单 / 文件边界 / baseHash / 超时                 │
│  - 调度 Node Babel Worker                                  │
└────────┼───────────────────────────────────────────────────┘
         │ spawn node scripts/apply_ast_command.mjs
┌────────▼───────────────────────────────────────────────────┐
│ ASTCommandLayer（Node + Babel）                            │
│  1. parse 真实 .jsx/.tsx                                   │
│  2. 定位目标节点（uid / sourceRef / 结构路径）             │
│  3. 执行 INSERT_NODE / MOVE_NODE / DELETE_NODE /           │
│     UPDATE_ATTR / UPDATE_TEXT                              │
│  4. Babel AST 门禁 + 生成源码 + 原子写入                   │
│  5. 返回 diff / 新源码 hash                                │
└────────┼───────────────────────────────────────────────────┘
         │ Vite HMR
┌────────▼───────────────────────────────────────────────────┐
│ 重新分析：analyze_existing_react.mjs                       │
│  → 新的 SourceProjectionDocument（只读）                   │
└────────────────────────────────────────────────────────────┘
```

## 4. ASTCommand 契约

所有命令都是**数据**，前端不拼接源码、不执行任意表达式。

```ts
type AstCommandType =
  | 'INSERT_NODE'
  | 'MOVE_NODE'
  | 'DELETE_NODE'
  | 'UPDATE_ATTR'
  | 'UPDATE_TEXT'

interface AstCommandEnvelope {
  commandId: string          // cmd_xxx，幂等键
  projectId: string
  filePath: string           // 只允许当前项目内相对路径
  baseHash: string           // 执行前文件内容 sha256，乐观锁
  expectedProjectionHash: string // 可选：防止投影过期
  command: AstCommand
}

interface InsertNodeCommand {
  type: 'INSERT_NODE'
  parentUid: string
  index: number
  node: {
    tag: string              // 白名单 HTML 标签或同文件组件
    className?: string       // 只允许静态 class 字符串
    text?: string            // 纯文本，不允许 JSX/HTML
    attributes?: Record<string, string | number | boolean>
  }
}

interface MoveNodeCommand {
  type: 'MOVE_NODE'
  nodeUid: string
  targetParentUid: string
  index: number
}

interface DeleteNodeCommand {
  type: 'DELETE_NODE'
  nodeUid: string
}

interface UpdateAttrCommand {
  type: 'UPDATE_ATTR'
  nodeUid: string
  attribute: 'className' | 'placeholder' | 'src' | 'alt' | string
  value: string | number | boolean
}

interface UpdateTextCommand {
  type: 'UPDATE_TEXT'
  nodeUid: string
  text: string               // 只允许替换纯文本字面量
}
```

约束：

- `tag` 白名单：基础 HTML 标签 + 当前文件已声明的组件；
- `className` 只接受静态字符串；动态表达式返回 `HANDOFF_TO_AI`；
- 文本节点如果包含表达式（`{t('x')}` / `{user.name}`），MVP 不直接改；
- 跨文件移动、共享组件、循环中的节点，MVP 返回 HITL / 不支持；
- 不允许 `dangerouslySetInnerHTML`、`style`、任意事件处理器写入。

## 5. 元素身份（UID）

编辑必须能稳定定位 AST 节点。Phase 2 采用两级策略：

1. **生成时注入**：开发/构建期 Babel 插件为 JSX 元素注入
   `data-source-uid`，由 `filePath + 组件名 + 结构路径` 派生；
2. **分析时回填**：分析器优先读取 `data-source-uid`；没有时用
   `(componentName, jsxPath, tag, loc)` 计算临时 UID，并在报告中标记
   `uidStable=false`。

写回定位顺序：

```
uid → sourceRef(file:line:column) + 结构路径 → 失败则拒绝，不模糊匹配
```

## 6. 后端执行流程

1. **输入校验**：projectId / filePath 必须在当前项目根目录内，禁止 `..`；
2. **乐观锁**：读取文件并校验 `baseHash`；不一致返回 `SOURCE_EDIT_CONFLICT`；
3. **项目写锁**：复用现有 `project_write_lock`，与 Direct Ops / Fullstack
   串行；
4. **AST 定位**：按 UID / sourceRef 找 `JSXElement`；找不到返回
   `SOURCE_NODE_NOT_FOUND`；
5. **命令校验**：父节点必须是容器；移动不能形成环；索引范围合法；
6. **变换**：只修改目标 AST 子树，保留其余节点；
7. **门禁**：
   - Babel parse 成功；
   - 禁止绝对定位 / 原始 HTML / 动态代码注入；
   - import 白名单与 nodeId/uid 完整性；
   - `npm run build`（必要时）；
8. **原子写入**：临时文件 + rename，或 Git apply；失败不留下半写状态；
9. **HMR**：Vite 监听文件变更，预览自动刷新；
10. **重新分析**：调用 `analyze_existing_react.mjs` 生成新投影；
11. **审计**：写 ChangeLog（command / file / diff / commitHash），支持撤销。

## 7. API 契约（Phase 2 提案）

```
POST /api/v1/source-edit/commands
{
  "commandId": "cmd_...",
  "projectId": "proj_...",
  "filePath": "src/pages/Users.jsx",
  "baseHash": "sha256:...",
  "command": { "type": "MOVE_NODE", ... }
}

200 {
  "ok": true,
  "filePath": "src/pages/Users.jsx",
  "newHash": "sha256:...",
  "diff": "unified diff",
  "projection": { ...新的只读分析报告... },
  "hmr": { "triggered": true }
}
```

错误码：

- `SOURCE_EDIT_INVALID_COMMAND`
- `SOURCE_EDIT_FILE_OUT_OF_SCOPE`
- `SOURCE_EDIT_CONFLICT`
- `SOURCE_NODE_NOT_FOUND`
- `SOURCE_EDIT_UNSUPPORTED`（跨文件 / 动态节点 / 循环）
- `SOURCE_EDIT_BUILD_FAILED`
- `SOURCE_EDIT_UNAVAILABLE`

## 8. 前端交互

- 投影树在 Phase 2 增加 `editable` 模式，但**仍然不接 VirtualDocument Store**；
- 拖拽 / 属性编辑 → `ASTCommandBuilder` → `sourceEditClient`；
- 成功后用返回的 `projection` 整体替换当前只读投影；
- 失败展示错误码与 diff，不本地乐观写入；
- 未实现的命令显示“暂不支持，交给 AI / 人工处理”；
- 画布上始终显示：源码是唯一事实源，当前投影只读。

## 9. Phase 2 里程碑

| 里程碑 | 内容 | 估算 |
|---|---|---|
| M1 | UID 注入 + 同文件 MOVE_NODE / INSERT_NODE / DELETE_NODE + HMR + 重解析 | 1.5–2 周 |
| M2 | UPDATE_ATTR / UPDATE_TEXT + AST/构建门禁 + undo / ChangeLog | 1–1.5 周 |
| M3 | 跨文件移动、共享组件、循环节点 HITL、组件抽取 | 3–5 周 |

## 10. 明确非目标

- 不把存量源码转换为 `VisualDocument`；
- 不写入 `visualDocumentStore`；
- 不做源码与虚拟树的双向实时同步；
- 不对存量项目走 CodeGen；
- 不允许前端直接拼接 / 写入源码文本。
