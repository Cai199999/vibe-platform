# 2026-11-05 · Eval Suite v0.1（CLI 离线自动化评测）

## 为什么要做

多 Agent 系统（UI / Architect / both / CustomAgent / rejected）靠人工点击回归
成本高、覆盖不稳定。引入 YAML 驱动的评测集，把“路由契约 + 生成结果”变成
每次提交都能跑的确定性检查，为后续 Prompt 灰度、模型切换与 CI 门禁提供基线。

## 交付内容

1. **评测集**
   - `config/eval_suite/core_cases.yaml`：10 条用例，覆盖 UI 改色 / 文案 /
     缩放 / 新增、双标注批量、Architect、both、CustomAgent、缺标注守卫、
     未知意图兜底。
   - `config/eval_suite/fixtures/react_dashboard/src/App.jsx`：带 `data-source`
     的确定性 React fixture。
   - `config/eval_suite/README.md`：字段语义、新增用例流程、报告与边界。

2. **执行器**
   - `services/orchestrator/eval_suite/`：
     - `loader.py`：严格校验 YAML，拒绝未知字段、重复 ID、fixture 路径穿越；
       单标注自动继承 `input.text`。
     - `engine.py`：使用真实 `SupervisorService` / `AgentRegistry` 路由；
       有代码断言时用 `MockAgentLLM(scenario="none")` 对 fixture 生成代码。
     - `report.py`：console / JSON / Markdown / JUnit XML。
     - `cli.py`：`run` / `list` / `validate`，支持 tag / 关键字过滤、
       `--fail-fast`、`--min-pass-rate`。
   - 不启动 FastAPI、不创建沙盒、不读取模型密钥。

3. **CLI 包装**
   - `scripts/eval_suite.sh`：复用 `vibe-platform/python-ai:dev` 镜像，以
     一次性容器运行；只读挂载源码与 config，可写挂载 `reports/`。
   - `reports/` 已加入 `.gitignore`。

4. **测试**
   - `services/orchestrator/tests/test_eval_suite.py`：9 项，覆盖加载器错误、
     路由 / 代码断言失败、报告写出、核心集全绿。
   - 全量 Python 测试 81 passed。

5. **文档**
   - `docs/design/07.Eval_Suite.md`：架构、契约、执行流程、报告、CI、
     演进路线。
   - `PROJECT_STATUS.md` 增加 Eval Suite 里程碑、验证入口与已知边界。

## 核心设计决策

- **路由走真实实现，生成走 Mock**：路由是本次评测最核心的契约，必须与生产
  同源；代码生成用确定性 Mock，保证零密钥、秒级、可重复。
- **断言只做稳定片段**：不写整段 JSX 快照，避免格式/无关变更造成误报。
- **配置错误在加载期失败**：未知字段、重复 ID、fixture 穿越直接退出码 2，
  不允许“悄悄跑 0 条用例也算绿”。
- **报告是产品界面**：每次失败输出用例、断言名、期望值、实际值；JUnit 可直接
  接入 CI。
- **未来引擎可替换**：YAML / 断言 / 报告与执行器解耦，后续可加 HTTP、
  DeepSeek、真实沙盒引擎。

## 验证

```bash
./scripts/eval_suite.sh validate   # 10 条用例，6 条代码生成
./scripts/eval_suite.sh run        # 10/10 PASS，约 40 ms
docker run --rm ... python -m unittest tests.test_eval_suite -v   # 9 passed
```

## 已知边界

- 只验证路由契约与 Mock 生成，不证明 Docker 沙盒 / 真实 DeepSeek 可用；
  后者仍由 `scripts/*_e2e_test.py` 覆盖。
- 未做历史趋势、基线对比、Prompt 版本 A/B；规划与 M3.3 Prompt Registry 联动。
