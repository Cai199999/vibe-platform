# 面试演示资产 · GIS 工作流

> 目标：用「日常开发工作流」演示一个测绘任务与成果管理平台。
> 主脚本：`docs/DEMO_GIS_5MIN.md`。

## 快速打开

```bash
cd ~/Workspace/vibe-platform
docker compose up -d

# 1) 从 V0 画布开始（推荐：现场冻结 → 契约 → 生成）
PROJECT_ID=$(python3 -c "import json;print(json.load(open('docs/demo/demo-v0-workspace.json'))['activeTabId'])")
DEMO_STATE=demo-v0-storage.json \
DEMO_PATH="/visual-canvas?project=$PROJECT_ID" \
node docs/demo/open-demo.mjs

# 2) 直接打开预生成 live 项目（兜底；workspace 可能只恢复项目 Tab）
DEMO_STATE=demo-storage.json node docs/demo/open-demo.mjs

# 3) 直接打开沙盒 live 预览（最稳的兜底）
DEMO_PREVIEW_URL=$(head -1 docs/demo/live-preview-url.txt) node docs/demo/open-demo.mjs
```

前置：宿主机有 `playwright-core`：

```bash
npm install --prefix /tmp/vibe-e2e playwright-core
```

## 文件说明

| 文件 | 用途 |
|---|---|
| `vibe-architecture.html` | 系统架构图（Archify，离线自包含） |
| `vibe-request-flow.html` | 一次生成/回写的时序图（Archify） |
| `gis-visual-document.json` | 260 节点的 GIS VisualDocument（4 页） |
| `demo-v0-storage.json` | 浏览器状态：DRAFT 画布，现场冻结用 |
| `demo-storage.json` | 浏览器状态：已生成 live 项目 |
| `live-preview-url.txt` | 当前沙盒 live 预览地址（重启后端口会变，用 `docker ps` 确认） |
| `gis-requirements-sample.md` | 可选上传的需求摘要，用来演示 Architect 的文档 RAG |
| `assets/survey-map.svg` | 内联 SVG 示意底图 |
| `assets/survey-map.data-uri.txt` | 可直接粘贴到「图片地址」的 data URI |
| `open-demo.mjs` | 用保存的浏览器状态打开演示 |

## GeoAI 复用目录

- 目录：`services/orchestrator/knowledge/geoai_reuse/catalog.md`
- 刷新：`python3 scripts/fetch_geoai_reuse_catalog.py`
- 覆盖：TorchGeo、Raster Vision、eo-learn、opengeos/geoai、geoai.js、
  golem、QGIS-SamGeo、geospatial-ml 资源清单。
- 已接入 Architect RAG：GIS 查询会命中该目录；只做 REUSE/ADAPT/BUILD 建议，
  不自动安装或执行仓库代码。

## 文档中心 / 语义判断

- 入口：Architect 面板右上角 **📚 文档中心**；也出现在 **追加修改需求** 区。
- 存储：浏览器 `localStorage`，按项目隔离；最多 10 份、单份 200KB、
  最多 3 份启用进入 Architect RAG。
- 判断：**规则优先 + DeepSeek 兜底**；模型失败回退为“询问用户”。
- 结果：
  - **长期复用**（规范 / 需求 / 术语 / 流程）→ 自动加入文档中心；
  - **一次性 / 反向语义**（仅本次 / 临时 / 看完就删）→ 本次使用、不保存；
  - **没有明确语义** → 询问“加入文档中心 / 仅本次使用”。

## 重新生成

```bash
# 重新生成丰富 V0 文档
node docs/demo/build-visual-document.mjs

# 只准备 V0 浏览器状态
node docs/demo/prepare-v0-state.mjs

# 冻结 → 契约 → CodeGen（真实模型）
DEMO_PROVIDER=auto node docs/demo/build-live-demo.mjs
# 网络不稳时用确定性契约
DEMO_PROVIDER=mock node docs/demo/build-live-demo.mjs
```

## Archify

已安装到 `~/.claude/skills/archify`（Apache-2.0，Node ≥18）。重新渲染：

```bash
ARCHIFY_CHROME=$HOME/.cache/ms-playwright/chromium-1254/chrome-linux64/chrome \
ARCHIFY_CHROME_NO_SANDBOX=1 \
node ~/.claude/skills/archify/bin/archify.mjs finalize architecture \
  docs/demo/vibe-architecture.candidate.json docs/demo/vibe-architecture.html \
  --quality showcase --repo-root "$PWD" --json

ARCHIFY_CHROME=$HOME/.cache/ms-playwright/chromium-1254/chrome-linux64/chrome \
ARCHIFY_CHROME_NO_SANDBOX=1 \
node ~/.claude/skills/archify/bin/archify.mjs finalize sequence \
  docs/demo/vibe-request-flow.candidate.json docs/demo/vibe-request-flow.html \
  --quality showcase --json
```

## 边界

- 地图是内联 SVG 示意底图，不是 GIS 引擎；真实项目替换为 Cesium / OpenLayers 组件。
- 存量模式当前是只读投影 + 后端增量；前端 AST 直改是下一阶段。
- 鉴权是 Mock；未上云；沙盒无 gVisor。
