# 2026-11-04 · 项目导出（tar.gz）

## 目标

把沙盒中的当前项目下载为 tar.gz，解压后是可独立运行的 Vite 项目；
工作区工具栏与项目下拉菜单各提供一个入口。

## 实现

### Python AI Core

- `SandboxProvider.export_archive(sandbox_id) -> bytes` 新增抽象方法。
- `LocalDockerSandbox` 在容器工作目录执行：
  `tar -czf - --exclude=node_modules --exclude=.git --exclude=dist .`
  - `-czf -` 直接写 stdout，不在沙盒里留下临时压缩包；
  - 保留 `package.json` / `package-lock.json` / `vite.config.js` / `src/`；
  - 输出为 bytes，超过 `SANDBOX_MAX_EXPORT_BYTES`（默认 32 MiB）直接拒绝。
- `GET /api/v1/internal/sandbox/{sandbox_id}/export` 返回
  `application/gzip` + `Content-Disposition: attachment`。
- Agent 执行时（读完目标文件后）把本次 AnnotationEnvelope 渲染为
  `VIBE_CONTEXT.md` 写入沙盒根目录：
  - 模块：`ai_core/agent/context_doc.py`；
  - 内容：项目 / Run / 沙盒 / 目标文件 / 组件 / 选择器 / 意图类型、
    用户自然语言意图、UI 摘要、验收标准、归一化选区、源码定位、
    完整 AnnotationEnvelope JSON（`~~~json` 围栏）；
  - 导出时随源码一起打包，Cursor / Claude Code 打开项目即可读到背景；
  - 写入失败只记日志，不影响 Run 与构建校验。

### Java BFF

- `RunSandboxRegistry`：进程内 `runId -> sandboxId` 映射，容量 1000，
  按访问顺序淘汰；RunService 在收到 AI Core 响应后记录。
- `SandboxExportService`：
  - `GET /api/v1/runs/{runId}/export?sandboxId=...`：优先用显式
    sandboxId，缺省时查注册表；两者都没有返回 404 `RUN_NOT_FOUND`。
  - `GET /api/v1/sandboxes/{sandboxId}/export`：直接按沙盒导出。
  - 通过 RestClient 的 `exchange` 在回调内把 AI Core 的 InputStream
    复制到 HttpServletResponse，BFF 不缓冲整包、不落盘。
  - 错误统一为 `{code, message}` JSON；若已经开始写二进制流则只记日志。
- 响应头固定：`application/gzip`、
  `attachment; filename="project-export.tar.gz"`、`Cache-Control: no-store`。

### 前端

- `WorkspacePage.handleExport(projectId)`：
  - 从 `runStates[projectId].result` 取 runId / sandboxId；
  - 有 runId 走 `/api/v1/runs/{runId}/export?sandboxId=...`，否则走
    `/api/v1/sandboxes/{sandboxId}/export`；
  - `fetch` → Blob → `<a download>` 原生下载，1 秒后回收 ObjectURL；
  - 成功/失败都通过右下角 `.workspace-toast` 提示，3s / 6s 自动消失。
- 工具栏最右侧 `.export-button`（📥 导出代码）；项目下拉菜单在
  「导入项目」下方新增「导出当前项目」。
- 没有沙盒时按钮禁用，title 提示先执行一次 Agent。

## 验证

- Python 直连、BFF（runId + sandboxId）、Vite 代理三条链路下载的字节流
  完全一致（`cmp` 通过）。
- 只传 runId（不传 sandboxId）时 BFF 注册表命中，返回 200。
- 未知 runId 返回 404 `RUN_NOT_FOUND`。
- Playwright：工具栏与下拉菜单各触发一次下载，`tarfile` 校验包含
  `package.json` / `package-lock.json` / `vite.config.js` / `index.html` /
  `src/App.jsx` / `src/main.jsx` / `VIBE_CONTEXT.md`，不含
  `node_modules` / `.git` / `dist`，且 `src/App.jsx` 是 Agent 修改后的
  红色按钮版本；`VIBE_CONTEXT.md` 必须包含用户指令「改颜色」、`UI_ONLY`
  与完整 `AnnotationEnvelope`。
- 解压到干净目录，在 `node:20-alpine` 容器执行 `npm ci && npm run build`
  成功（31 modules，991ms）。

## 踩坑 / 决策

- Docker exec 的输出是字节流，tar 的 gzip 数据可以直接经 `bytes` 透传；
  不要 `decode` 再编码。
- BFF 侧若先把压缩包读成 `byte[]` 会多一次内存复制，也可能受默认响应
  缓冲限制；改用 `exchange` 内 `InputStream.transferTo(servletOutput)`
  才是真流式。
- `exchange` 方法的签名不声明 `IOException`（内部会包装），外层只
  `catch RestClientException`；lambda 内仍可抛 `IOException`。
- runId 映射只在内存中，BFF 重启会丢失；前端始终带 sandboxId，因此
  用户侧不受影响。
