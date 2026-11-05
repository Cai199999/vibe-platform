# 2026-11-05 M3：多格式导入 / 空白区域 ADD / Tools-Skills-MCP

## 背景

在 M2.5 Supervisor 与 M3 审批能力之上，补齐“从外部进入项目”和“在空白处新增”
两条主链路，并把 Agent 扩展点从声明式占位推进到可运行示例。

## 已完成

### 1. 多格式导入

- `POST /api/v1/internal/sandbox/import`：multipart，支持 zip 与图片。
- React zip：安全解压 → 识别入口 → `npm install` → `npm run build`。
- HTML zip：内置 `static_server.mjs`，支持目录索引与 SPA fallback。
- 图片：OpenAI 兼容多模态模型生成 React；无 `VISION_API_KEY` 时走确定性
  占位页，保证离线可编译。
- zip 防护：Zip Slip、符号链接、加密包、解压炸弹、条目数 / 解压体积限制、
  `.env` 与密钥文件过滤。
- 前端“导入项目”入口 + 自动新建 Tab + 预览；BFF 负责 multipart 透传。

### 2. 空白区域自动判定 ADD

- 几何指标：交集面积、目标覆盖率、选区占圈比例。
- 阈值：`MODIFY_MIN_COVERAGE=0.35`、`ADD_AREA_RATIO=4`、
  `MODIFY_MIN_SELECTION_SHARE=0.12`。
- 未命中或圈远大于节点 → `action: "ADD"`；向上找最近 `data-source` 父容器，
  输出 `relativeTarget`（`parent-normalized`）。
- `ui_graph` Prompt 说明 ADD：在父容器内部按坐标插入 JSX，并保留 `data-source`。
- E2E `scripts/blank_area_add_e2e_test.py`：空白处画圈 → 请求体 ADD +
  `[data-source="card"]` + 相对坐标 → 构建成功 → 预览出现新按钮。

### 3. Tools / Skills / MCP

- `ai_core/tools/`：`ToolRegistry`、`shell_tool`；默认关闭，只读白名单、
  固定 workdir、超时、输出截断、拒绝 shell 元字符与路径逃逸。
- `ai_core/agent/graph.py`：真实 `ToolNode`；保留官方 `mcp` SDK 挂载注释。
- `config/skills/tailwind_magic.skill.json`：Tailwind 设计规范，挂载到
  `ui_graph` manifest。
- `ai_core/config.py`：`MCP_SERVERS: list[dict[str, Any]]` 配置项。

## 验证

- `scripts/import_project_e2e_test.py`：React zip / HTML zip / 图片三条路径 PASS。
- `scripts/blank_area_add_e2e_test.py`：空白区域 ADD PASS。
- `scripts/settings_manual_approval_e2e_test.py`：审批开关 E2E PASS。
- 既有 UI / 多轮 / 批量 / 回滚 E2E 已适配 Supervisor 与自动切换预览。

## 环境变量

`VISION_*`、`IMPORT_*`、`SANDBOX_IMPORT_TIMEOUT_SECONDS`、`SKILLS_DIR`、
`TOOL_SHELL_*`、`MCP_SERVERS`，见 `.env.example`。
