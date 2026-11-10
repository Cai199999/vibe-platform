# Phase B：画布滚动、双屏同步与影子 LLM

日期：2026-11-10

## 目标

1. 画布 / 预览放弃 `transform: scale` 强行适配，固定 1440×900 逻辑视口 + 容器原生滚动；
2. 画布 iframe 的滚动 / 点击通过 `postMessage` 驱动预览同步；
3. Architect 引入只读影子助手（Helper Agent）与关键问题 Banner / 生成门禁。

## 实现

### 画布滚动

- `useFillViewport` 恒返回 1440×900、scale=1、offset=0；`canvas-stage` / `runtime-preview-screen` 使用 `overflow:auto`。
- 滚轮分工：普通滚轮 → iframe 页面；Ctrl/Meta+滚轮 → `usePanZoom` 缩放；中键 / 空格+左键 → 平移。
- 页面滚到边界后不再 `preventDefault`，剩余滚轮自然移交外层容器。

### postMessage 协议（channel = `vibe-inspect`）

- 父 → iframe：`enable | move | pick | clear | measure | scroll-by | scroll-to`。
- iframe → 父：`ready | hover | picked | measured | scroll | click`。
- **关键坑**：父页面把 `scroll-by/scroll-to` 参数放在 `payload` 内，SDK 旧代码却读顶层 `data.dx`，导致指令静默失效；`inspect_sdk.js` 现同时兼容 `data.payload.x` 与顶层字段。
- **关键坑**：SDK 在 `<head>` 执行，首次 `ready` 的 `scrollHeight` 恒为 900；渲染完成后父页面仍认为不可滚动。现用 `ResizeObserver + MutationObserver`（120ms 防抖 + 指纹去重）补发真实快照。

### 双屏同步

- `CanvasStage` 的 `scroll` 事件统一写入 `canvasScrollRef`；即使单屏模式也记录，切入双屏时立即补一次 `scrollSync`。
- `ScaledPreview` 收到 `scroll-to` 后滚动 iframe；点击 `click` 事件带 `clientX/clientY`，预览按点击点绘制 44px（世界坐标）涟漪，不再用元素 rect（点击 `<main>` 会导致覆盖整页的巨型光斑）。

### 影子 LLM

- 后端：`POST /api/v1/internal/architect/runs/{runId}/helper`，只读快照 + 可选 RAG，返回 `readOnly=true`；BFF 透传同路径。
- 前端：Architect 面板双 Tab（架构确认必须回答 / 随意提问只读）；助手消息独立存储，不写入主 `messages`、不推进状态机。
- `countPendingKeyQuestions` 统计 required 澄清问题；顶部不可关闭 Banner 显示剩余数量；`handleFullstackGenerate` / 手动 bootstrap 均先经过 `blockGenerateUntilAnswered`。

## 验证

- 真实沙盒 E2E（`/tmp/scroll_sync_helper_e2e.py`）：
  - 普通滚轮 iframe `scrollY=700`，画布 transform 不变；
  - 连续滚动到底 `scrollY=3579`，外层 `stage.scrollTop=193` 接力；
  - 切双屏预览自动对齐 `3579`；再滚动画布 `canvas=3079 / preview=3079`；
  - 点击涟漪 49×49 且位于预览视口内；
  - Banner 关闭面板后仍在；影子助手回答只读，主状态仍 `AWAITING_CLARIFICATION`。
- `vite build` 通过；`black --check` 146 文件通过。
- 沙盒镜像 `vibe-platform/sandbox-react:dev` 已重建（改 `inspect_sdk.js` 必须重建，插件有 `sdkCache`）。

## 注意

- 修改 `sandbox_template/inspect_sdk.js` 后必须重建镜像并重新创建沙盒，旧容器不会自动更新。
- 影子助手绝不写主 `architect.messages`，避免污染澄清轮次与契约生成。
