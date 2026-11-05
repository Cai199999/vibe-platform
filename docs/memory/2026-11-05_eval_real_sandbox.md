# 2026-11-05 · Eval Suite v0.2（真实沙盒引擎 + Admin 评测契约）

## 交付

1. **`scripts/run_eval.py`（宿主机可运行）**
   - 只依赖标准库 + PyYAML，不导入 `ai_core`，不要求宿主机安装 LangGraph。
   - 读取 `config/eval_suite/core_cases.yaml`，逐条调用：
     - BFF：`POST /api/v1/supervisor/route`、`POST /api/v1/runs`、
       `GET /api/v1/sandboxes/{id}/export`、`DELETE /api/v1/sandboxes/{id}`；
     - 直连 AI Core：自动切换 `/api/v1/internal/...`（端口 8100）。
   - 路由断言：`handoff`、`target`、`taskType`、`dispatchable`、
     `handoffs` 顺序、`missingInputs` 覆盖。
   - 代码断言：真实 UI 子图 Run → 构建 / 预览 → 沙盒导出包读取
     `src/App.jsx` → 空白不敏感正则匹配 `expected_code_contains`。
   - 每条用例独立 `projectId + threadId`；`finally` 删除沙盒。
   - 终端报告：逐条 PASS / FAIL、失败断言、成功数、失败数、通过率、总耗时、
     沙盒创建 / 清理数；Markdown 写入 `docs/eval_report.md`。
   - `--filter` / `--fail-fast` / `--keep-sandboxes` / `--verbose` /
     `--base-url` / `--timeout`。

2. **文档**
   - `docs/design/07.Eval_Suite.md`：v0.2 双引擎架构、真实引擎流程、
     “考题 vs 教材”解耦、Admin 一键评测预留。
   - `docs/design/03.API接口设计.md` §14.4.2：
     `POST /api/v1/admin/eval/run`（202 + evalRunId）、评测详情、
     历史得分曲线、错误码与并发约束。
   - `docs/design/06` §5.5：管理台评测中心与 Eval / Knowledge 边界；
     `/admin` 页面树与审计动作补充。
   - `docs/design/02` §6.6：管理中台增加 Eval 契约与解耦约束。
   - `PROJECT_STATUS.md`、`README.md`、`00_project_brief.md` 同步。

## 调试中发现的两个关键问题

1. **固定 annotationId 会污染评测**
   - 服务端以 `threadId || annotationId` 作为会话标识；YAML 固定
     `ann_eval_004` 会命中上一轮 Redis 快照，ADD 按钮从 1 个累积到 4 个，
     或重复注入 `<style>` 导致构建失败。
   - 修复：评测器为每条用例注入 `eval_<runTag>_<caseId>_<index>`，长度 ≤ 64。
   - 结论：评测必须隔离会话，不能只隔离 `projectId`。

2. **Vite `/src/App.jsx` 返回的是转换后模块**
   - 预览地址读取到 `jsxDEV(...)`，JSX 字面量断言会误报失败。
   - 修复：改为 `GET /sandboxes/{id}/export` 下载 tar.gz，在内存中读取
     `src/App.jsx` 真实源码；失败才回退 `generatedFiles` / `generatedPatch`。

## 验证

```bash
python scripts/run_eval.py
# 10/10 PASS，35.72s，创建 / 清理沙盒各 6 个
# 报告：docs/eval_report.md
```

沙盒用例：case_001–005、case_007；纯路由用例：case_006、008、009、010。
真实沙盒仍使用 `llmProvider=mock`，验证的是“路由 + 沙盒执行 + 代码落盘”，
不是真实模型质量。

## 边界

- DeepSeek 非确定性评测、多次采样、历史基线 / 得分曲线尚未实现。
- Admin Eval 只冻结 API 契约，BFF 尚未注册路由。
- 评测集与知识库解耦是架构约束；Knowledge Service 落地后再补
  `knowledgeSnapshotId` 的真实取值。
