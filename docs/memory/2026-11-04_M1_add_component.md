# 2026-11-04 · M1 ADD 组件（从“只改已有”到“新增节点”）

## 目标

打破 M1 只能修改已有 DOM 的限制：用户可以圈选大卡片内部或空白区域，输入
“添加一个红色的提交按钮”，Agent 在正确的父级 / 兄弟级位置插入完整 JSX，
一次写入沙盒并通过 `npm run build`，预览实时出现新增组件。

## 协议：`AnnotationEnvelope.action`

```ts
action: "MODIFY" | "ADD" = "MODIFY"
```

- `MODIFY`：沿用原有行为，修改已有元素；`target` 缺省时前端回退到 CTA。
- `ADD`：新增组件；`target` 允许为空，此时模型按 `worldTarget` 归一化
  选区与源码结构自行选择插入容器。
- Python `schemas.py` 是单一真源；Java BFF 仍以 `JsonNode` 透传，无需改
  DTO。`service.py` 的逐标注上下文额外提供 `action` 副本。

## 前端

- `WorkspacePage.buildAnnotation()` 输出 `action`，ADD 且无 target 时不再
  回退到 `cta-button`。
- 自动推断 `inferAnnotationAction(target, intent)`：
  - 圈中没有命中 `data-vibe-target` → `ADD`；
  - 指令包含“添加 / 新增 / 新建 / 插入 / 加一个 / add” → `ADD`；
  - 否则命中元素 → `MODIFY`。
- 创建标注时 `actionSource='auto'`；输入框内容变化会实时重算。浮层头部
  新增极简 **修改 / 新增** 分段控件，手动点击后 `actionSource='manual'`，
  不再被自动推断覆盖。
- 浮层表单带 `data-action`，E2E 直接断言当前操作类型。

## Mock LLM

- `mock_scenario` 新增 `add_button`：无条件走新增分支。
- `none` / `fail_first` 也会识别“添加 / 新建 / 插入”语义。
- 插入策略（保证 JSX 结构合法）：
  1. `card` 或无 target → 最后一个 `</section>` 之前（容器内部末尾）；
  2. 命中 `button` / `h1` / `p` 等叶子元素 → 该元素闭合标签之后（兄弟节点）；
  3. 兜底 → `</section>` / `</main>` 之前。
- 生成 `<button className="vibe-added-button" data-vibe-target="added-button(-N)"
  style={{ backgroundColor: 'red', ... }}>提交</button>`；按钮文案从
  “添加一个红色的提交按钮”中剥离颜色词得到，颜色复用 `_detect_color`。
- 返回 `mode=files` 的全量源码；Graph 已有逻辑自动合成为展示用 unified
  diff，Applier 一次写回，无需额外 AST 处理。新增不会覆盖已有 DOM。

## DeepSeek Prompt

Planner 与 Coder 的 System Prompt 新增约束：

- `action=ADD` 时先规划插入位置（父级 / 兄弟级），禁止把新增伪造成修改；
- 必须保留原有 DOM 结构，结合 `target.selector`、`target.source.component`
  与 `worldTarget` 选区中心定位；优先插入容器内部末尾，叶子元素之后作为
  兄弟；
- `target` 为空时按几何位置选择容器；新增元素需有可辨识文本与样式；
  同一 thread 多轮 ADD 不重复插入完全相同的组件。

## 测试

新增 `scripts/add_component_e2e_test.py`：

```text
圈选大卡片内部 → 输入“添加一个红色的提交按钮”
action: 自动 ADD；手动 修改/MODIFY ↔ 新增/ADD 正常
状态: 已完成 / 沙盒: sbx_...
新增按钮: 提交 / 背景 rgb(255, 0, 0); 原按钮仍为“开始体验”
ADD COMPONENT E2E: PASS
```

截图：`docs/screenshots/m1-add-component-preview.png`。

回归全绿：

- `ui_copy_clean_e2e_test.py`（浮层标题断言更新为“圆点 + 修改/新增切换”）
- `agent_e2e_test.py` / `platform_e2e_test.py`
- `multi_annotation_e2e_test.py` / `multi_turn_chat_e2e_test.py`
- `rollback_e2e_test.py` / `annotation_delete_rollback_e2e_test.py`
- `popover_equal_scale_e2e_test.py` / `ui_e2e_test.py`
- `docker exec vibe-platform-web npm run build`
