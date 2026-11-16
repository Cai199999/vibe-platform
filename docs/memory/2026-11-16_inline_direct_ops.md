# 2026-11-16 内联 Direct Ops 与骨架布局约束

## 内联 Direct Ops（去悬浮卡）

沙盒 `inspect_sdk.js` 新增 `direct-mode`：

- **内联 ➕**：hover 容器时在右下角显示；点击弹出 Button / Input /
  Text / Card 白名单菜单，直接 postMessage 给平台。
- **悬浮 ❌**：hover 任意源码节点时右上角显示；点击先请求
  `analyze-delete`，safe 后在原位置弹出确认面板，确认才执行删除。
- **双击文本**：仅纯文本叶子节点可进入 `contentEditable`；失焦 / Enter
  提交，Esc 取消；旁边预留 B / A- / A+ 字体工具槽位（暂禁用）。
- **↕ 移动按钮已移除**，保留物理拖拽。
- 跨容器拖拽虚线高亮沿用既有 `stageTarget` + `stage-hit` 覆盖层，
  代码路径已确认存在。

平台侧：

- `WorkspacePage` 全局监听 `vibe-inspect` 消息，直接把
  `direct-add / direct-delete-request / direct-delete-confirmed /
  direct-text` 映射到已有 Direct Ops，不进入 Agent / LLM。
- 画布与预览两侧 iframe 在 `ready` 后都会收到 `direct-mode=true`；
  因此预览模式同样可用，不再依赖 InspectDraftCard。
- 旧的 InspectDraftCard Direct Ops 工具栏已移除，只保留“向 Agent
  描述修改意图”的标注入口。

## 新增组件继承样式

- SDK 读取同级兄弟的 `className` 与 inline `style`，随 `direct-add` 发送。
- 后端 `apply_style_context()` 将 className 合并到模板，
  inline style 经过字符白名单过滤后写入根节点。
- 真实沙盒验证：新增 Button 得到
  `className="vibe-direct-button toolbar primary"`，耗时 1559ms。

## 骨架布局约束（任务四）

- `ui.layout` 成为 lightweight_web / full_stack_system 的必需槽位。
- 新增澄清问题：侧边栏+主区域 / 顶部导航+主区域 / 顶部导航+三列卡片。
- 契约起草与修订 Prompt 新增布局铁律：必须使用 CSS Grid / Flexbox，
  列表 / 卡片响应式多列，禁止全量 flex-col 垂直堆叠。

## 验证

- Python 599 项 unittest 全绿；前端 16 项 Vitest 全绿，构建通过。
- 新 SDK 已打入 sandbox 镜像，预览页 `/__vibe_inspect_sdk.js` 包含
  `direct-mode` / `direct-delete-confirmed`。
- 真实 API：ADD 继承样式 1559ms；Direct Ops 后端链路未变。

## 未完成

- 任务三“前端 Agent 新建失败安全路径（重试 / 视觉占位 / 取消）”尚未
  落地；当前安全路径决策仍只有候选 / 调用点 / 取消。
- 浏览器点击级 E2E 尚未自动化；本轮验证为 SDK 下发 + API + 前端构建。
