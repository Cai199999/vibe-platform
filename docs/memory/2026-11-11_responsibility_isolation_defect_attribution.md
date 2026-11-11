# 责任隔离、缺陷归因与全栈核心指标（2026-11-11）

## 目标

把多表业务数据流的失败处置固定为确定性链路，避免 Coder 既生成代码又自我裁决：

```
DBACoder ──schema.core.json──► BackendCoder ──► Applier（AST 绝对门禁）
                                                    │
                    ┌───────────────────────────────┴──────────────┐
                    │ 通过                                         │ 拦截
                    ▼                                              ▼
             FrontendCoder / InfraCoder                  audit_contract 归因
                    │                                      │            │
                    ▼                                      ▼            ▼
            ConsistencyVerifier（只诊断）          CODE_DEFECT   CONTRACT_DEFECT
                    │                                      │            │
                    └──────────────► BuildVerifier         重试 ≤2     CONTRACT_REVISION_REQUIRED
```

## 节点职责

- **BackendCoder**：唯一输入是 `server/schema.core.json`（tables + relations）。缺失直接失败，不再回退解析 DDL / 猜测关系；移除生成器内部 AST 自检。
- **ConsistencyVerifier**：只调用 `check_orm_syntax` 生成 `OrmSyntaxReport`，按 `fix_action` 分流：
  - `REGENERATE_ORM` → DBACoder（≤2）；
  - `REPAIR_BACKEND_JOIN` → BackendCoder（≤2）；
  - 缺失路由 → BackendCoder（≤2）。
- **Applier**：所有 `server/` 产物唯一写入口。合并沙盒现有 ORM/迁移与本次产物后无条件跑 Babel AST；失败抛 `ApplierSafetyBlock`，沙盒保持原样。前端/Infra 产物仍走 Applier，但不重复触发 AST 检查。
- **Architect**：契约确认前调用 `validate_contract`（与运行期 `audit_contract` 同一实现），嵌套路由缺子表/缺关系在澄清阶段即进入修复流程。

## 缺陷归因

- `CODE_DEFECT`：契约自洽，JOIN 别名、include 别名、字段拼写或语法错误；BackendCoder 重生成，最多 2 次。
- `CONTRACT_DEFECT`：DDL 缺表、嵌套路由无 JOIN 关系、交互调用未声明 API 等；立即抛 `CONTRACT_REVISION_REQUIRED`，事件携带 `failureKind=CONTRACT`、`defectKind=CONTRACT_DEFECT`、`needsClarification=true` 与 `clarification.payload()`。
- `/users/:id/rewrite` 等动作端点通过动作词表与“复数子资源”双重启发式排除，不误判为缺失子表。

## 指标

`ai_core/observability/fullstack_metrics.py` 进程内单例，SSE `fullstack.completed` / `fullstack.failed` 与 `GET /api/v1/internal/fullstack/metrics` 输出：

| 指标 | 定义 |
| --- | --- |
| `Applier_Block_Rate` | 门禁拦截次数 / 门禁检查次数 |
| `Contract_Misretry_Count` | 已发生自愈重试后，`audit_contract` 仍判契约缺陷的次数 |
| `MultiTable_AST_Pass_Rate` | 多表契约 AST 检查通过次数 / 总次数 |
| `Contract_Revision_Rate` | Architect `REVISE` 次数 / `CONFIRM` 次数 |

## 验证

- 新增 E2E 级图测试：故意把 `/users/:id/resumes` 写成 `resumez` → Applier 拦截 → `CODE_DEFECT` → 重试后成功构建/重启；指标 1/3 拦截率。
- 新增服务测试：嵌套路由引用 DDL 中不存在的 `resumes` 表 → `CONTRACT_REVISION_REQUIRED`，不启动任何生成节点、不触发构建。
- 全量 Python：428 项通过（排除历史 `test_eval_suite`）；Java BFF E2E：全栈生成 / CRUD 联调通过；指标端点在线可读。
