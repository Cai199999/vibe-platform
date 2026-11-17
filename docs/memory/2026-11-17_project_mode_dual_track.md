# 2026-11-17 双轨制项目模式分流

## 背景

存量项目被误当新建项目，Architect 凭空生成 VisualDocument 与 DDL，
出现“关联 users → resumes 的父键 id 不在父表字段”等无中生有的契约错误。

## 已完成

### 1. ProjectModeResolver

- 前端 `workspace/projectMode.js`：
  - imported / `template=imported-*` / generated / sandbox / 非内置模板非空 files
    → `EXISTING_SOURCE`；
  - 空白新建、仅内置演示模板 → `NEW_PROJECT`。
- 后端 `domain/project_mode.py`：
  - source.sandbox_id 或非空 source.files → `EXISTING_SOURCE`；
  - 前端声明只作回退，source 事实优先。

### 2. 新建项目强制分流

- 新建项目必须先选【从零开始生成】或【导入源码分析】；
- 导入源码：
  - 读取 `.jsx/.tsx/.js/.ts`（≤500KB）；
  - 创建 `imported-react` 项目，源码落到 `src/App.jsx`；
  - 原始文件名写入 sessionStorage 供投影展示；
  - 同步落盘 workspace 后再跳转，避免 WorkspacePage 卸载丢项目；
  - 跳转 `/visual-canvas?project=...&mode=existing`。

### 3. 存量模式 UI

- `VisualCanvasPage` 自动解析项目源码，渲染 `ReadOnlySourceCanvas`；
- 不渲染可编辑虚拟画布，不读写 `visualDocumentStore`；
- 顶栏“返回工作台”，不再提供“返回虚拟画布”；
- Architect 面板显示“存量源码模式”提示。

### 4. Architect / 契约 / CodeGen 门禁

- `ArchitectRunRequest.projectMode` + `ArchitectContract.projectMode`；
- `ArchitectContext.project_mode`；`architect.started` / snapshot 回传；
- Prompt 追加 EXISTING_SOURCE 块：
  - 禁止 visual_document / 整页 JSX / 重新设计页面；
  - 只做业务审查与后端增量绑定。
- `validate_draft` 跳过 relations 自检与 componentInteractions 要求；
- `assemble_contract` 强制 `visual_document=None`，不凭空推导 relations；
- `FullstackService`：存量项目拒绝 frontend/fullstack，只允许 backend；
- `audit_contract`：存量项目跳过 DDL 表 / relations 审计；
- 前端 `handleFullstackGenerate` 自动改走 `handleExistingBackendGenerate`；
- `FullstackGeneration` 在存量模式显示“生成后端增量”，不要求 DDL。

## 验证

- Python 116 项 Architect/Fullstack 测试通过（含新增 project_mode 测试）；
- 前端 52 项 Vitest 通过，生产构建通过；
- 浏览器 E2E：
  - 新建项目弹窗恰好 2 个选项；
  - 导入 `samples/ExistingDashboard.jsx` → 只读投影 133 节点；
  - 无 `.vc-palette`（未渲染可编辑画布）；
  - 返回工作台 → 存量项目 Tab → Architect 面板显示“存量源码模式”；
  - 0 浏览器错误。
- 后端 API 冒烟：`projectMode: EXISTING_SOURCE` 在
  `architect.started` 事件中原样回传。

## 已知限制

- 单文件导入统一落为 `src/App.jsx`，不还原原始多文件工程；
- 存量后端增量仍依赖契约 API/DDL 描述；
- AST 直改命令层尚未实现（docs/design/12）。
