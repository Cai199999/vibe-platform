# Eval Report · 真实沙盒评测

> 生成时间：2026-11-10 08:43:49 CST  
> 评测集：`/home/admin1/Workspace/vibe-platform/config/eval_suite/core_cases.yaml`  
> 服务地址：`http://127.0.0.1:8080`（BFF）  
> 模型提供方：`deepseek`  
> 运行标识：`20261110_084251`

## 1. 总览

| 指标 | 数值 |
|---|---:|
| 用例总数 | 10 |
| 通过 | 7 |
| 失败 | 3 |
| 通过率 | 70.0% |
| 总耗时 | 57.36s |
| Input tokens | 22552 |
| Output tokens | 3671 |
| 总 tokens | 26223 |
| 模型调用次数 | 9 |
| 预估费用 | 0.050088 CNY |
| 创建沙盒 | 4 |
| 清理沙盒 | 4 |

## 2. 用例结果

| 用例 | 名称 | Handoff | 期望 | 执行证据 | 耗时 | Tokens | 结果 |
|---|---|---|---|---|---:|---:|---|
| `case_001` | UI 修改：将按钮变红 | `handoff_ui` | `handoff_ui` | run=SUCCEEDED · sandbox-export:/src/App.jsx | 13.66s | 5743 | FAIL |
| `case_002` | UI 修改：替换标题文案 | `handoff_ui` | `handoff_ui` | run=SUCCEEDED · sandbox-export:/src/App.jsx | 10.98s | 5265 | PASS |
| `case_003` | 安全拦截：禁止将数据库端口暴露到公网 | `rejected` | `rejected` | - | 0.01s | - | PASS |
| `case_004` | UI 新增：添加红色提交按钮 | `handoff_ui` | `handoff_ui` | run=SUCCEEDED · sandbox-export:/src/App.jsx | 15.14s | 6167 | FAIL |
| `case_005` | UI 批量修改：颜色 + 文案一次提交 | `handoff_ui` | `handoff_ui` | run=SUCCEEDED · sandbox-export:/src/App.jsx | 15.43s | 6617 | FAIL |
| `case_006` | 架构澄清：模糊需求触发 HITL 中断 | `handoff_architect` | `handoff_architect` | architect.clarification.required:app.shape | 1.98s | 2431 | PASS |
| `case_007` | Both：UI 改色 + 全栈架构同时规划 | `handoff_both` | `handoff_both` | gate=AWAITING_CONTRACT | 0.11s | - | PASS |
| `case_008` | CustomAgent：销售数据 Excel 报表 | `handoff_custom` | `handoff_custom` | - | 0.02s | - | PASS |
| `case_009` | UI 守卫：缺少画布标注不可派发 | `handoff_ui` | `handoff_ui` | - | 0.02s | - | PASS |
| `case_010` | 兜底：无法识别意图时拒绝路由 | `rejected` | `rejected` | - | 0.02s | - | PASS |

## 3. 失败明细

### case_001 · UI 修改：将按钮变红

- Run：`run_655106d6abff4f9085fd`
- Sandbox：`sbx_aea00d14934b4a23be6e`
- 代码来源：`sandbox-export:/src/App.jsx`

| 断言 | 期望 | 实际 | 说明 |
|---|---|---|---|
| `code_contains[0]` | background-color:red | not found | 生成代码缺少必需片段 |

### case_004 · UI 新增：添加红色提交按钮

- Run：`run_84fc8f51af204e5da26d`
- Sandbox：`sbx_0802668f657d4a189fa1`
- 代码来源：`sandbox-export:/src/App.jsx`

| 断言 | 期望 | 实际 | 说明 |
|---|---|---|---|
| `code_contains[0]` | backgroundColor: 'red' | not found | 生成代码缺少必需片段 |
| `code_contains[1]` | >提交</button> | not found | 生成代码缺少必需片段 |

### case_005 · UI 批量修改：颜色 + 文案一次提交

- Run：`run_ee2ad0e949034798ba1e`
- Sandbox：`sbx_78674a03cf7e44a39d20`
- 代码来源：`sandbox-export:/src/App.jsx`

| 断言 | 期望 | 实际 | 说明 |
|---|---|---|---|
| `code_contains[0]` | background-color:blue | not found | 生成代码缺少必需片段 |

## 4. 执行方式

1. 读取 `config/eval_suite/core_cases.yaml`，逐条调用 Supervisor 路由接口；
2. 对声明了代码断言的用例，调用 UI 子图 `/runs` 创建**真实 Docker 沙盒**，
   由 `deepseek` LLM 生成代码、安装依赖、构建并启动预览；
3. 对 HITL 用例，调用 Architect SSE 并等待 `architect.clarification.required`，
   断言首个澄清问题槽位，随后取消 Run；
4. 对 both 用例，调用 Supervisor `/dispatch`，断言只启动 Architect 且门控为
   `AWAITING_CONTRACT`，随后取消 Architect Run；
5. 通过沙盒导出接口读取真实源码 `src/App.jsx`，用空白不敏感正则断言代码片段；
6. 每条用例结束后删除沙盒 / 取消后台 Run，避免资源泄漏。

> 说明：路由断言走真实 Supervisor 规则；代码生成使用 `llmProvider=deepseek`。
> 当提供方为 `deepseek` 时，本报告反映真实模型的端到端成功率与 Token 成本；
> 当提供方为 `mock` 时，仅验证“路由契约 + 沙盒执行链路”，不评价模型质量。
