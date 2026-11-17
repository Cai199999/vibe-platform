# 2026-11-17 存量 React 只读投影 Phase 1

## 目标

双轨制轨道二的 MVP：已有 React 源码 → Babel 分析 → 只读虚拟画布。
只允许“看到结构”，绝不写回源码、绝不生成/覆盖 VisualDocument。

## 分层架构

```
React 源码
  → scripts/analyze_existing_react.mjs        (Node + Babel，纯分析，CLI)
  → Python ai_core/source_analysis/service.py (进程边界 + 安全校验)
  → FastAPI /api/v1/internal/source-analysis/analyze
  → BFF /api/v1/source-analysis/analyze
  → 前端 sourceAnalysisClient.js              (只读 API 客户端)
  → sourceProjection.js                       (分析报告 → VisualNode 形状投影)
  → ReadOnlySourceCanvas.jsx                  (只读树 + hover tooltip)
```

关键隔离：

- 分析器只输出 JSON 报告，`analysisOnly=true`、`writable=false`；
- Python / BFF 只做透传与校验，不引入 AST 写回；
- 前端适配器是纯函数，不读取、不写入 `visualDocumentStore`；
- 只读画布不依赖编辑画布的 Zustand store、拖拽命令或 CodeGen；
- 后续 AST 写回必须作为独立命令层接入，不能污染投影层。

## 分析脚本能力

- 支持 `.js/.jsx/.ts/.tsx`，Babel 解析 JSX；
- 提取函数组件、JSX 树层级、tag、文本、className/classes、attributes、
  `file:line:column`；
- 输出 `entryComponent`、`root`、`components`、`componentHierarchies`；
- 同文件组件可递归展开（Card → Header → Text），循环引用停止；
- `--file`、`--stdin-name`、stdin、`--self-test` 四种入口；
- 自测断言：`Card -> Header -> Text`、Card root className、Header 嵌套。

## 前端只读投影

- 离线画布顶栏新增“导入源码分析”，接受 `.jsx/.tsx/.js/.ts`；
- 文件上限 500KB（前后端一致）；
- 分析成功后切换到 `ReadOnlySourceCanvas`，可“返回虚拟画布”；
- 树节点 hover：显示组件层级、className、源码位置；
- 固定提示：`已识别为存量源码，需转为虚拟树才能自由编辑`；
- 点击节点右侧 inspector 展示只读元数据；
- 同文件组件展开，外部组件/循环引用有明确 badge；
- 不提供任何写回、拖拽或生成入口。

## 验证

- Python `tests/test_source_analysis.py` 4 项通过；
- 全量 Python 653 项仅 7 项原有环境问题；
- 前端 47 项 Vitest 通过，生产构建通过；
- 真实浏览器 E2E：
  - 登录态 → `/visual-canvas`；
  - 上传 `NestedCard.jsx`；
  - 只读投影渲染；
  - hover Header 节点 tooltip 显示
    `Card › section › Header › div`、`flex items-center`、
    `NestedCard.jsx:5:10` 与只读提示；
  - 0 console / page error。

## 限制与扩展点

- 仅单文件、函数组件；不处理跨文件组件、HOC、render props、
  `React.createElement`、动态导入；
- 条件/循环只做有限展开，不保证完整运行期结构；
- 投影状态只保存在页面内存，刷新丢失；
- 未接入工作台“导入项目”流程，仅画布内单文件导入；
- 下一阶段 Phase 2：独立 AST 命令层（同文件拖拽/插入/删除），
  继续不做双向同步。
