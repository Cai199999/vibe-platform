# Eval Suite 评测集

`core_cases.yaml` 是 vibe-platform 的核心回归评测集。同一份 YAML 支持两种
执行方式：

- **离线引擎**：不启动前端、不启动 FastAPI、不创建沙盒，用确定性 Mock
  完成“Supervisor 路由 + UI 代码生成”断言，秒级完成，适合每次提交与 CI；
- **真实沙盒引擎**：经 BFF / AI Core 启动 Docker 沙盒，验证构建、预览与
  真实源码落盘，适合合并前或夜间跑批。

评测集是“考题”，RAG 知识库是“教材”：考题不写入知识库，也不作为检索语料。

## 目录

```text
config/eval_suite/
├── core_cases.yaml              # 10 条核心用例
├── fixtures/
│   └── react_dashboard/         # UI 用例的初始源码
│       └── src/App.jsx
└── README.md
```

## 运行

```bash
# 执行全部用例，生成 JSON / Markdown / JUnit 报告
./scripts/eval_suite.sh run

# 只跑架构相关用例，首条失败即停止
./scripts/eval_suite.sh run --filter architect --fail-fast

# 查看 / 校验用例，不执行
./scripts/eval_suite.sh list
./scripts/eval_suite.sh validate

# 只跑 smoke 标签
./scripts/eval_suite.sh run --tag smoke

# 真实沙盒引擎（需 BFF :8080 / AI Core :8100 / Docker 已启动）
python scripts/run_eval.py
python scripts/run_eval.py --filter case_001 --verbose
```

离线报告写入 `reports/eval/`；真实沙盒报告写入 `docs/eval_report.md`，
终端同时输出成功数、失败数、通过率与总耗时，结束后自动清理沙盒。

报告默认写入 `reports/eval/`（已加入 `.gitignore`）：

- `eval-report.json`：机器可读，适合看板 / 趋势分析；
- `eval-report.md`：人读摘要与失败明细；
- `eval-junit.xml`：CI（GitLab / Jenkins / GitHub Actions）测试报告。

退出码：`0` 全部通过；`1` 断言失败或低于 `--min-pass-rate`；`2` 配置错误。

## 用例格式

```yaml
- id: "case_001"                     # 必填，全局唯一
  name: "UI 修改：将按钮变红"          # 必填
  tags: ["ui", "smoke"]              # 可选，用于 --tag / --filter
  input:
    target_agent: "auto"             # auto 走 Supervisor；也可写 ui_graph 等
    fixture: "react_dashboard"       # 可选；代码断言必填
    text: "改成红色"                  # 用户自然语言
    annotation:                      # 单标注；或用 annotations: [...]
      annotationId: "ann_eval_001"
      type: "note"
      action: "MODIFY"               # MODIFY / ADD
      target:
        mode: "source"
        selector: "[data-source='primary-action']"
        source:
          file: "src/App.jsx"
      payload:
        text: "改成红色"
  assertions:
    expected_handoff: "handoff_ui"           # 必填
    expected_agent: "ui_graph"
    expected_task_type: "ui_local_edit"
    expected_dispatchable: true
    expected_handoffs: ["ui_graph"]          # 必须完全一致（含顺序）
    expected_missing_inputs: []              # 只要求包含；空表示必须为空
    expected_code_contains: ["background-color:red"]
    expected_code_not_contains: ["background-color:blue"]
    max_duration_ms: 2000
```

### 字段语义

| 字段 | 说明 |
|---|---|
| `target_agent` | `auto` 时由 Supervisor 规则路由；否则写入 `options.targetAgent` |
| `annotation` / `annotations` | 二选一；最多 3 个。单标注未写 `payload.text` 时自动继承 `input.text` |
| `fixture` | 相对 `config/eval_suite/fixtures/` 的目录；加载其中全部 UTF-8 文本文件 |
| `expected_handoff` | `handoff_ui` / `handoff_architect` / `handoff_both` / `handoff_custom` / `rejected` |
| `expected_handoffs` | 子图交接顺序；`both` 必须是 `["architect_graph", "ui_graph"]` |
| `expected_missing_inputs` | 实际值必须包含这些项；声明为空列表时要求实际为空 |
| `expected_code_contains` | Mock 生成代码必须包含的稳定片段；每条独立报告 |
| `max_duration_ms` | 单用例耗时预算；超出即失败 |

未知字段、重复 ID、fixture 路径穿越都会在加载阶段直接报错，避免“配置写错
却静默通过”。

## 新增用例

1. 在 `core_cases.yaml` 追加用例，ID 保持 `case_xxx` 递增；
2. UI 用例优先复用 `react_dashboard` fixture；需要新页面时在 `fixtures/`
   下新建目录并用 `data-source` 标记可圈选元素；
3. 断言只写稳定片段，不要把整段 JSX 快照写进 YAML；
4. 运行 `./scripts/eval_suite.sh validate`，再运行 `run`；
5. 如果这是新发现的线上缺陷，先让用例失败，再修代码，最后全绿。

## 边界

- 当前引擎验证的是 **路由契约 + Mock 代码生成**，不证明 Docker 沙盒/真实
  DeepSeek 可用；后者由 `scripts/*_e2e_test.py` 覆盖。
- 新增 HTTP / 真模型 / 沙盒引擎时，应复用同一套 YAML 与报告，不另造格式。
