# 2026-11-09 · 画布 / 预览自适应铺满修复

## 问题

顶栏压缩后，画布高度改变，固定 1280×720 + cover 缩放导致：
- 容器宽高比与 16:9 不一致时，上下内容被裁切；
- `--viewport-h` 固定值留下无意义底部空白；
- 生成页外层可能被 max-width / 默认 margin 影响。

## 修复

### 1. 应用壳 Flex 链

`WorkspacePage` 增加 `app-shell-workspace`：

- `.app-shell-workspace { height:100dvh; display:flex; flex-direction:column; overflow:hidden; }`
- `.workspace { flex:1; min-height:0; }`
- `.workspace-body { display:flex; flex:1; min-height:0; overflow:hidden; }`
- `.tab-window { display:flex; flex-direction:column; flex:1; min-height:0; }`
- `.project-viewport { flex:1; min-height:0; height:auto; }`

只在 ≥721px 生效，窄屏保留整页滚动。登录页 / 管理页不受影响。

### 2. 动态逻辑视口（关键）

`useFillViewport` 从 cover 改为：

```
scale        = min(containerW / 1280, containerH / 720)
designWidth  = containerW / scale   // ≥1280
designHeight = containerH / scale   // ≥720
offsetX/Y    = 0
```

画布与预览使用同一组逻辑宽高 + transform：
- 画面严格铺满容器，没有黑边；
- 逻辑视口只扩不缩，页面不会被上下裁掉；
- 生成页在更宽/更高的逻辑视口中自然重排。

所有归一化坐标改用 `designWidth / designHeight`：
- CanvasStage 圈选、浮层定位、screen→world 换算；
- ScaledPreview 指针 → iframe 坐标（用 `iframe.clientWidth/Height`）；
- Inspect 结果使用 SDK payload 里的 `viewport.width/height`。

`usePanZoom` 的平移边界改为 `containerSize * zoom`，不再依赖固定基准尺寸。

### 3. 沙盒与生成页

- `sandbox_template/index.html`：`html/body/#root` 统一 `width/height:100%; margin:0; padding:0; overflow:hidden`，并加全局 `box-sizing`。
- `sandbox_template/src/App.jsx`：最外层 `width:100%; minHeight:100vh; boxSizing:border-box`。
- `fullstack/coders.py` 生成骨架：`styles.page` 增加 `width:100%; minHeight:100vh`，移除对根容器宽度的隐式依赖。

## 验证

- `conversation_first_e2e_test.py` 新增 1600×1000 / 1280×800 / 1920×1080 三档断言：应用壳高度、无页面滚动、live 页面 bounding box 与 stage 完全重合、transform 后逻辑视口严格等于 stage。
- `ui_e2e_test.py`、`multi_annotation_e2e_test.py`、`drag_placement_e2e_test.py`、`inspect_mode_e2e_test.py`、`annotation_delete_rollback_e2e_test.py`、`snapshot_management_e2e_test.py` 全部 PASS。
- `python -m unittest tests.test_fullstack`：17 tests PASS。
- 前端生产构建 PASS。

## 注意

Supervisor 轨迹出现后画布高度会自然收缩，但宽度不变且仍铺满；`ui_e2e_test.py` 已改为分别断言宽度一致与预览高度合理。
