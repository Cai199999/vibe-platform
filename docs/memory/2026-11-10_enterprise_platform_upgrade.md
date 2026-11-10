# 2026-11-10 · 企业级平台升级（五问题）

## 目标

修复演示阻断的画布截断问题，并完成 Agent 生态、交互引导与跨端一致性校验。

## 已完成

1. **画布显示不全**
   - `styles.css` 重建桌面端 flex 链：`app-shell-workspace → workspace →
     workspace-body → tab-window → project-viewport → viewport-pane →
     canvas-frame → canvas-stage`，逐层 `flex: 1 1 0%` + `min-*: 0`，
     外层保留 `overflow: auto` 兜底，不再用 `overflow: hidden` 硬切。
   - `useFillViewport` 改为容器实测 + `scale = min(w/1280, h/720)` +
     `designWidth = w/scale`，ResizeObserver / orientationchange /
     visualViewport 多源触发，双 rAF 防抖，Tab 激活后强制重算。

2. **多 Tab 预览同步**
   - `WorkspacePage` 维护 `tabActivation` nonce；切换项目 Tab 时递增。
   - `ProjectWorkspace` 合成 `frameSyncNonce = previewRefreshNonce:tabActivationNonce`，
     iframe key 变化触发沙盒重载，确保画布 / 预览属于当前项目。

3. **管理 Agent**
   - 新增 `AgentBuilder`：需求 → `config/custom_agents/<id>.json` +
     `prompts/<id>.md` + `knowledge/<id>/`，确定性生成，不依赖 LLM。
   - 新端点：`POST/DELETE /agents`、`GET/POST/DELETE /agents/{id}/knowledge`，
     BFF 同步代理；前端 `supervisorClient` 接入。
   - `AgentCatalogMenu` 改名“管理 Agent”：内置 Agent 加锁不可删，
     自定义 Agent 可新建 / 删除，知识库支持 `.md` / `.txt` 上传、列表、删除。
   - 新增 Skill `ui.interaction_states`，要求前端必须实现 Loading / Error / Success。

4. **交互引导**
   - Architect `analyze` / `draft_contract` / `revise_contract` 增加强制规则：
     必须澄清提交 / 保存 / 删除后是写库、跳转还是提示；无交互时输出静态占位，
     禁止伪造 onClick。
   - FrontendCoder 按 `component_interactions` 生成真实事件与 fetch，
     带输入校验、15s 超时、`role="status"/"alert"`、失败重试。
   - 修复方法推断顺序：提交 / 保存优先于“刷新列表”等 incidental 读提示。

5. **ConsistencyVerifier**
   - 新增 `ai_core/fullstack/consistency.py`：解析前端 `INTERACTIONS` 与
     `COLLECTION_PATH`，对比后端 `server/api-routes.json` 的 Method / Path /
     Payload；参数名归一为 `:param`，比较忽略参数名差异。
   - 图拓扑：`InfraCoder → ConsistencyVerifier → BuildVerifier → SandboxRestart`，
     总节点数 7；失败不进入构建 / 重启，发送
     `fullstack.consistency.report` + `fullstack.node.failed`。
   - 报告包含问题类型、组件、方法、路径、中文修复建议与后端已注册路由。

## 运行态修复（镜像重建后暴露）

- `entrypoint.sh` 启动时将 `/app/config/custom_agents` 交给 `app` 用户：
  镜像内该目录由 root 拷贝，运行时新建 Agent 会 `PermissionError`。
- `AgentBuilder.delete` 统一用 `base.resolve()` 计算相对路径：
  生产配置为相对路径 `config/custom_agents`，旧的 `relative_to(self._base)`
  在删除时会抛 `ValueError`；已补充相对目录回归测试。
- 已重建三个镜像并 `docker compose up -d`；通过 BFF 实测
  “创建 → 上传知识 → 列表 → 删除”全链路成功。

## 验证

- `tests.test_consistency` + `tests.test_fullstack`：32/32 通过。
- `tests.test_agent_management`：7/7 通过。
- 全量 Python：260 tests，仅 6 个已知环境失败（`test_eval_suite` 挂载路径；
  MCP demo 进程在 `/app` 挂载上下文不可用），与本次改动无关。
- `npm run build`：78 modules，构建通过。
- `black` 已格式化全部改动 Python 文件。

## 后续

- 重新构建三个 Docker 镜像并重启容器，使运行中服务加载本次代码。
- E2E 验证管理 Agent、交互生成与一致性阻断场景。
