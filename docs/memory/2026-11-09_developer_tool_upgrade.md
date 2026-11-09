# 2026-11-09 · 开发者工具形态升级

## 目标调整

产品定位改为「面向开发者的可视化 Vibe Coding 辅助工具」：UI 从隐藏复杂性
转为提供精确控制与反馈。本次优先修复 ADD 圈选落点偏移，再升级 HITL 与
开发者详情面板。

## 相对坐标（P0 Bug）

旧 `relativeTarget` 只有归一化 x/y/width/height（0~1），模型无法知道圈选
区域在父容器中的真实像素位置，只能猜测插入顺序。新契约同时保留：

- 归一化字段：`x/y/width/height`（兼容旧客户端）。
- 像素字段：`relativeX/relativeY`、`widthPx/heightPx`、
  `parentWidth/parentHeight`；`coordinateSpace=parent-relative`。
- CanvasStage 从 `getBoundingClientRect()` 得到的屏幕像素除以
  `viewport.scale` 还原 CSS 像素；Inspect SDK 的 rect 本身已是 iframe
  CSS 像素，直接相减。
- Planner / Coder Prompt 增加落位优先级：复用 flex/grid → absolute +
  left/top → margin 偏移；`relativeX≈0` 靠左，`relativeX+widthPx≈
  parentWidth` 靠右；仅像素缺失时退回归一化 y 比例。
- `context_doc.py` / `VIBE_CONTEXT.md` 同步渲染像素字段。

## HITL 多轮对话

- 澄清挂起时主输入栏不再禁用；发送时把当前问答 + 输入栏文本作为
  `comment` 一起提交，不新起 Run。
- 预设选项保留，新增「自定义」按钮；选项题点击后平滑展开 textarea，
  自由文本题默认展开。多选自定义文本会并入答案数组。
- 后端 `_normalize_answers` 把 `comment` 追加为
  `__developer_comment__` 伪回答：不参与必填校验，但随 answers 进入
  Planner，并被 `_augment_context_with_answers` 写回标注指令。

## 开发者详情面板

- 预览顶部状态栏展示模型（DeepSeek V3 / Mock）与沙盒实时状态。
- 底部常驻状态栏展示模型、沙盒、预览探活、标注数量、sandboxId，并可
  拉起底部抽屉。
- 抽屉常驻展示：AST 路径（file:line:column，可点击复制）、LangGraph
  节点流转时间轴、原始代码 Diff、构建日志、Agent 轨迹。执行中也可查看。
- `handleAnnotationAdd` 写入 `astPath`：Inspect 用 SDK 精确出处，圈选用
  入口文件 + data-source 标记反查行号。

## 验证

- 新增 `tests/test_relative_coordinates.py`（4 项）与
  `tests/test_clarification_comment.py`（3 项）。
- 新增 E2E：`blank_area_add_e2e_test.py` 断言像素坐标与归一化比例一致；
  `clarification_ui_e2e_test.py` 验证输入栏常驻 / 自定义 textarea / 提交；
  `developer_panel_e2e_test.py` 验证状态栏、AST、时间轴、Diff。
- 全量 `159 tests` 通过；`npm run build` 通过；三条 E2E 通过；Black 已格式化。
