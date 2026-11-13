# 2026-11-13 · 圈选精度评测（Canvas Precision Eval）

## 目标

把前端圈选当成模型一样评测：固定夹具 + 可复现用例 + Playwright 真实
鼠标操作，量化 `AnnotationEnvelope` 的源码命中率与几何 IoU。

## 资产

- 夹具：`services/orchestrator/sandbox_template/canvas-precision.html`
  （1440×900、深层嵌套卡片、列表、表单、FAB、透明遮罩；内层
  span/svg 故意也带 `data-source-*`）。
- 评测集：`config/eval_suite/canvas_precision_cases.yaml`（3 个
  HIT_TEST、1 个 LAYER_PRIORITY、2 个 ELEMENTS_IN_BOX）。
- 脚本：`scripts/canvas_precision_eval.py`，真实登录 → 创建沙盒 →
  强制 live 画布 → iframe 导航到夹具 → 悬停/点击/拖拽 → 截获
  `POST /api/v1/supervisor/route`。
- 报告：`reports/eval/canvas-precision-{label,report}.{json,md}`；
  截图：`docs/screenshots/canvas-precision-{label}.png`。
- 批次提交时把 route 短路为 `rejected`，只采集请求体，避免长任务
  覆盖画布导致后续批次无法继续。

## 基线（修复前）

| 指标 | 值 |
| --- | --- |
| Hit Rate | 44.45% |
| 用例全中率 | 33.33% |
| Mean IoU | 0.643 |

失败点：
1. 点击 FAB 内部 svg 命中 `FloatingAction.tsx:30`（图标），而非
   `:22`（button）；IoU 0.165。
2. 点击按钮内文字命中 `Button.tsx:14`（span），而非 `:12`。
3. 表单圈选只召回 2/3，漏 `SaveButton.tsx:21`。
4. 列表圈选召回 0/3，完全为空。

## 根因与修复

### 1. DOM 节点去重键冲突（严重）

`analyzeSelection` 用普通对象 `var seen = {}` 去重，写入 `seen[node]`
时节点被转成字符串 `"[object HTMLButtonElement]"`。同标签元素只有
第一个参与测量，导致所有后续按钮被静默丢弃。

修复：改为 `new Set()` + `seen.has/add(node)`。修复后表单 3/3、
列表 3/3。

### 2. 缺少可交互父级提升

插件逐元素注入 `data-source-file`，`elementFromPoint` 先命中内层
`span` / `svg`，`closest('[data-source]')` 直接停在那里。

修复：新增 `INTERACTIVE_TAGS` / `PROMOTE_TAGS` 与
`promoteInteractiveNode()`，在 `inspectAt` 和 `analyzeSelection`
中把纯展示型节点提升到最近的、能解析出源码的可交互祖先
（button / a[href] / input / select / textarea / [role=button] /
`[data-interactive=true]`）。祖先没有源码时保留原节点，避免丢失
`file:line`。

### 3. 坐标并非 scale/translate 误差

点击命中框与真实 DOM rect 只差 1px，来源是夹具卡片 `border:1px`
使绝对定位子元素整体偏移 1px，不是画布坐标矩阵错误。已把 YAML
`expected_box` 修正为真实视觉框（145/173、791/171 等）。

### 4. IoU 口径修正

`ELEMENTS_IN_BOX` 改为“召回元素并集框 vs 期望并集框”，不再拿
用户预留 padding 的拖拽框直接比组件并集；点击类仍用
`worldTarget`，因此同时覆盖了屏幕→iframe→归一化坐标链路。

## 修复后

| 指标 | 值 |
| --- | --- |
| Hit Rate | 100.00% |
| 用例全中率 | 100.00% |
| Mean IoU | 1.000 |
| 阈值 | Hit ≥ 0.90，IoU ≥ 0.85（PASS） |

## 回归

- `scripts/inspect_mode_e2e_test.py`：PASS（React/Vue 精准选择）。
- 重建 `vibe-platform/sandbox-react:dev` 后运行评测，6/6 通过。
- 复跑命令：
  `/tmp/vibe-e2e/bin/python scripts/canvas_precision_eval.py --label post-fix`
