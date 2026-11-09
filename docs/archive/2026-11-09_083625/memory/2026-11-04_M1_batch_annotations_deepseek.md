# 2026-11-04 · M1 批量标注与真实 DeepSeek 接入

## 1. 本轮目标

- 画布从“单个标注”升级为“最多 3 个标注”，每个标注独立输入、独立删除。
- 一次提交 `AnnotationEnvelope[]`，Agent 综合全部意图，一次编码、一次写入、
  一次构建，预览 / 导出 / Diff 同时包含全部修改。
- 解除 Mock 固定“变红”限制：接入真实 DeepSeek，支持颜色、文案、字号、
  间距、布局等任意 UI 指令。

## 2. 前后端契约

### 请求

```json
{
  "projectId": "proj_01",
  "annotations": [
    { "annotationId": "ann_01", "target": { "selector": "[data-vibe-target='cta-button']" }, "intent": { "ui": { "summary": "改成蓝色" } }, "payload": { "text": "改成蓝色" } }
  ],
  "files": { "src/App.jsx": "..." },
  "options": { "llmProvider": "auto", "maxRetries": 3 }
}
```

- `annotations` 长度 1~3，Pydantic `Field(max_length=3)` 硬校验；第 4 个标注
  返回 **422 `too_long`**。
- 旧版单 `annotation` 仍可用，`ExecuteRunRequest` 的
  `model_validator(mode="after")` 会归一为单元素 `annotations`。
- Java BFF 的 `CreateRunRequest` 用 `@AssertTrue` 校验
  “`annotations` 数组或 `annotation` 至少有一个”，其余字段 `JsonNode`
  透传；契约唯一真源仍是 Python schema。

### 响应

- 成功：`AWAITING_APPROVAL` + `generatedPatch` + `previewUrl` +
  `sandboxId` + agent messages。
- 构建失败自动重试，`retryCount <= 3`；耗尽后 `FAILED`。
- `VIBE_CONTEXT.md` 在 Agent 写入沙盒时生成，列出全部标注、归一化选区、
  目标文件 / 组件 / 选择器、每条指令与完整 JSON；导出包直接携带。

## 3. 前端实现

- `WorkspacePage`：`EMPTY_RUN.annotations = []`；会话快照持久化数组，并兼容
  旧快照的单 `annotation`。
- `CanvasStage`：
  - 最多 3 个圈，每个圈有独立 `AnnotationPopover` 输入框；
  - 圈选中心命中 `data-vibe-target` 元素，写入 selector / role / component；
  - 达到上限后再圈选只闪提示，不新增标注；
  - **隐藏标注输入** 解决浮层遮挡下一笔圈选；
  - **清除标注** 一次清空全部圈和输入框。
- 顶部工具栏显示 `${n}/3`、目标标签、模型选择器
  `auto / deepseek / mock`，持久化在 `localStorage["vibe.llmProvider"]`。
- `buildAnnotation` 按每条标注独立生成 envelope，不再硬编码“红色按钮”。

## 4. LangGraph 批量流程

```text
planner（综合 1~3 条标注，列冲突与验收）
   -> coder（一次生成包含全部修改的 diff / files）
   -> applier（一次写入全部文件，并重读目标文件）
   -> verifier（一次 npm run build）
   -> evaluator（成功 / 携带日志重试，最多 3 次）
```

- State 新增 `target_files: list[str]` 与
  `current_files: dict[path, source]`；保留旧 `target_file/current_code`
  兼容单文件场景。
- `service._resolve_target_files` 对标注中的源文件去重；默认
  `src/App.jsx`。
- Coder 输出校验：`files` 必须覆盖全部 `target_files`；路径禁止绝对路径与
  `..`；`diff` 必须含目标文件。
- Applier 一次性写入，避免三次 HMR / 三次构建的中间态；写入后重读所有
  目标文件及 diff 中出现的路径。

## 5. DeepSeek 接入

- `DeepSeekAgentLLM` 使用 `langchain_openai.ChatOpenAI`，指向
  `https://api.deepseek.com`，模型 `deepseek-chat`，`temperature=0`。
- `llmProvider=auto`：有 `DEEPSEEK_API_KEY` 用 DeepSeek，否则 Mock；
  显式 `deepseek` 缺 Key 时报配置错误，显式 `mock` 永远离线。
- System Prompt 明确：
  - 必须一次处理全部 1~3 个标注，禁止只处理第一个；
  - 支持任意 UI 属性（颜色 / 文案 / 字号 / 间距 / 布局 / 响应式 / 结构）；
  - 多文件或多处修改优先 `mode=files`；
  - 只输出结构化 JSON，不输出 Markdown。
- `.env` 需要：
  `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`；
  修改后必须 `docker compose up -d --force-recreate python-ai`（改了代码则
  `--build`）。
- 健康检查 `dependencies.llm` 显示 `deepseek` / `mock`，不泄露 Key。
- 本机 `.env` 的 Key 为空，因此 **真实 DeepSeek 网络请求未在本轮执行**；
  已验证的是 provider 选择、模型名 / base URL、Mock 批量闭环和全链路。

## 6. 测试结果（2026-11-04）

| 测试 | 结果 |
|---|---|
| `npm run build`（platform-web） | PASS，55 modules |
| `mvn -q -DskipTests compile`（java-bff） | PASS |
| `docker compose up --build -d python-ai java-bff` | PASS |
| `scripts/agent_e2e_test.py` | PASS：3 标注一次修改、fail_first 重试 1 次、always_fail 3 次、FULLSTACK 422、4 标注 422 |
| `scripts/platform_e2e_test.py` | PASS：Vite 代理 → BFF → AI Core → 沙盒，3 处修改同时生效 |
| `scripts/multi_annotation_e2e_test.py` | PASS：UI 画 3 圈、独立输入、上限提示、POST `annotations` 长度 3、预览 / 导出 / VIBE_CONTEXT / Diff、清除 |
| `scripts/ui_e2e_test.py` | PASS：回归双视图、视口状态、导出、沙盒销毁等 |

## 7. 已知边界

- Canvas Mock 只覆盖 `card / kicker / title / copy / cta-button` 五个目标；
  新增元素需同时补 `data-vibe-target`，否则选区中心可能回退到最近的标记。
- Mock LLM 仍只解析常见颜色 / 文案 / 放大缩小；任意布局请用 DeepSeek。
- 真实 DeepSeek 的稳定性（首次是否一次构建通过、重试次数）需用户填入 Key
  后实测；重试与错误回灌机制已就绪。
