# Eval Report · 真实沙盒评测

> 生成时间：2026-11-05 17:24:23 CST  
> 评测集：`/home/admin1/Workspace/vibe-platform/config/eval_suite/core_cases.yaml`  
> 服务地址：`http://127.0.0.1:8080`（BFF）  
> 运行标识：`20261105_172400`

## 1. 总览

| 指标 | 数值 |
|---|---:|
| 用例总数 | 10 |
| 通过 | 10 |
| 失败 | 0 |
| 通过率 | 100.0% |
| 总耗时 | 22.71s |
| 创建沙盒 | 4 |
| 清理沙盒 | 4 |

## 2. 用例结果

| 用例 | 名称 | Handoff | 期望 | 执行证据 | 耗时 | 结果 |
|---|---|---|---|---|---:|---|
| `case_001` | UI 修改：将按钮变红 | `handoff_ui` | `handoff_ui` | run=SUCCEEDED · sandbox-export:/src/App.jsx | 5.78s | PASS |
| `case_002` | UI 修改：替换标题文案 | `handoff_ui` | `handoff_ui` | run=SUCCEEDED · sandbox-export:/src/App.jsx | 5.70s | PASS |
| `case_003` | 安全拦截：禁止将数据库端口暴露到公网 | `rejected` | `rejected` | - | 0.01s | PASS |
| `case_004` | UI 新增：添加红色提交按钮 | `handoff_ui` | `handoff_ui` | run=SUCCEEDED · sandbox-export:/src/App.jsx | 5.46s | PASS |
| `case_005` | UI 批量修改：颜色 + 文案一次提交 | `handoff_ui` | `handoff_ui` | run=SUCCEEDED · sandbox-export:/src/App.jsx | 5.60s | PASS |
| `case_006` | 架构澄清：模糊需求触发 HITL 中断 | `handoff_architect` | `handoff_architect` | architect.clarification.required:app.shape | 0.06s | PASS |
| `case_007` | Both：UI 改色 + 全栈架构同时规划 | `handoff_both` | `handoff_both` | gate=AWAITING_CONTRACT | 0.05s | PASS |
| `case_008` | CustomAgent：销售数据 Excel 报表 | `handoff_custom` | `handoff_custom` | - | 0.02s | PASS |
| `case_009` | UI 守卫：缺少画布标注不可派发 | `handoff_ui` | `handoff_ui` | - | 0.01s | PASS |
| `case_010` | 兜底：无法识别意图时拒绝路由 | `rejected` | `rejected` | - | 0.01s | PASS |

## 3. 失败明细

全部用例通过。

## 4. 执行方式

1. 读取 `config/eval_suite/core_cases.yaml`，逐条调用 Supervisor 路由接口；
2. 对声明了代码断言的用例，调用 UI 子图 `/runs` 创建**真实 Docker 沙盒**，
   由 Mock LLM 生成代码、安装依赖、构建并启动预览；
3. 对 HITL 用例，调用 Architect SSE 并等待 `architect.clarification.required`，
   断言首个澄清问题槽位，随后取消 Run；
4. 对 both 用例，调用 Supervisor `/dispatch`，断言只启动 Architect 且门控为
   `AWAITING_CONTRACT`，随后取消 Architect Run；
5. 通过沙盒导出接口读取真实源码 `src/App.jsx`，用空白不敏感正则断言代码片段；
6. 每条用例结束后删除沙盒 / 取消后台 Run，避免资源泄漏。

> 说明：路由断言走真实 Supervisor 规则；代码生成使用 `llmProvider=mock`，
> 因而本报告验证的是“路由契约 + 沙盒执行链路”，不评价真实模型质量。
