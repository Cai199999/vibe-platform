# 业务 Eval 报告

- 时间：2026-11-10T09:01:15.610816+00:00
- 评测集：`/home/admin1/Workspace/vibe-platform/config/eval_suite/business_cases.yaml`
- 模型：`deepseek`
- 服务：`http://127.0.0.1:8080`
- 耗时：123.5s
- Tokens：36586（¥0.102416）

## 汇总

| 指标 | 结果 |
|---|---|
| 业务通过率 | 4/4 = 100.0% |
| 契约理解准确率 | 4/4 = 100.0% |
| 产物语义通过率 | 3/3 = 100.0% |

## 用例明细

| 用例 | 形态 | 契约 | 产物 | 总耗时 | Tokens |
|---|---|---|---|---|---|
| biz_001 纯前端作品集展示 | lightweight_web | 通过 | 不适用 | 11.0s | 6068 |
| biz_002 客户管理 CRUD | full_stack_system | 通过 | 通过 | 37.7s | 10348 |
| biz_003 简历上传与列表 | full_stack_system | 通过 | 通过 | 37.8s | 9982 |
| biz_004 待办事项 CRUD | full_stack_system | 通过 | 通过 | 37.0s | 10188 |

## 失败明细

本套用例全部通过。
