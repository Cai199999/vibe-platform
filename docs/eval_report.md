# Eval Report · 真实沙盒评测

> 生成时间：2026-11-05 16:58:40 CST  
> 评测集：`/home/admin1/Workspace/vibe-platform/config/eval_suite/core_cases.yaml`  
> 服务地址：`http://127.0.0.1:8080`（BFF）  
> 运行标识：`20261105_165804`

## 1. 总览

| 指标 | 数值 |
|---|---:|
| 用例总数 | 10 |
| 通过 | 10 |
| 失败 | 0 |
| 通过率 | 100.0% |
| 总耗时 | 35.72s |
| 创建沙盒 | 6 |
| 清理沙盒 | 6 |

## 2. 用例结果

| 用例 | 名称 | Handoff | 期望 | Run | 耗时 | 结果 |
|---|---|---|---|---|---:|---|
| `case_001` | UI 修改：将按钮变红 | `handoff_ui` | `handoff_ui` | `SUCCEEDED` | 5.58s | PASS |
| `case_002` | UI 修改：替换标题文案 | `handoff_ui` | `handoff_ui` | `SUCCEEDED` | 6.09s | PASS |
| `case_003` | UI 修改：卡片放大一倍 | `handoff_ui` | `handoff_ui` | `SUCCEEDED` | 6.19s | PASS |
| `case_004` | UI 新增：添加红色提交按钮 | `handoff_ui` | `handoff_ui` | `SUCCEEDED` | 5.65s | PASS |
| `case_005` | UI 批量修改：颜色 + 文案一次提交 | `handoff_ui` | `handoff_ui` | `SUCCEEDED` | 6.19s | PASS |
| `case_006` | Architect：全栈电商系统设计 | `handoff_architect` | `handoff_architect` | `-` | 0.01s | PASS |
| `case_007` | Both：UI 改色 + 全栈架构同时规划 | `handoff_both` | `handoff_both` | `SUCCEEDED` | 5.97s | PASS |
| `case_008` | CustomAgent：销售数据 Excel 报表 | `handoff_custom` | `handoff_custom` | `-` | 0.02s | PASS |
| `case_009` | UI 守卫：缺少画布标注不可派发 | `handoff_ui` | `handoff_ui` | `-` | 0.01s | PASS |
| `case_010` | 兜底：无法识别意图时拒绝路由 | `rejected` | `rejected` | `-` | 0.01s | PASS |

## 3. 失败明细

全部用例通过。

## 4. 执行方式

1. 读取 `config/eval_suite/core_cases.yaml`，逐条调用 Supervisor 路由接口；
2. 对声明了代码断言的用例，调用 UI 子图 `/runs` 创建**真实 Docker 沙盒**，
   由 Mock LLM 生成代码、安装依赖、构建并启动预览；
3. 通过沙盒导出接口读取真实源码 `src/App.jsx`，用空白不敏感正则断言代码片段；
4. 每条用例结束后删除沙盒，避免资源泄漏。

> 说明：路由断言走真实 Supervisor 规则；代码生成使用 `llmProvider=mock`，
> 因而本报告验证的是“路由契约 + 沙盒执行链路”，不评价真实模型质量。
