# 2026-11-16 契约资源/动作分离与命名冲突决策

## 背景

真实 Run 在多轮澄清后失败：

```
API 集合 /api/v1/auth 对应的业务表 auth 不在 DDL 中
errorCode=CONTRACT_VALIDATION_FAILED
```

根因是模型没有区分“实体资源（Resource）”与“动作端点（Action）”：
注册 / 登录属于 users 表上的动作，模型却把 `/api/v1/auth` 声明为集合根；
审计器又从路由推导集合资源并要求 DDL 存在同名表，自动修复一次仍未收敛，
最终直接 FAILED。

## 方案

### 1. Prompt 强约束（`architect/llm.py`）

起草与修订 Prompt 同时增加：

- 认证 / 会话 / 账号属于动作，不得作为静态集合根，也不得单独建 auth 表；
- 认证动作应挂在 users 资源下（如 `/api/v1/users/login`）；
- 每条集合根必须在 DDL 中有同名（允许单复数差异）真实表。

### 2. 审计器识别动作端点（`fullstack/coders.py` + `contract_audit.py`）

- `ACTION_RESOURCE_ROOTS`：auth / session / account / login / token 等；
- `is_action_resource()`：集合候选与 `collection_path()` 双重排除，
  防止动作端点被选为前端 CRUD 集合根；
- `action_resource_table()`：认证类资源优先映射 users / accounts / members；
- 审计时动作端点即使没有用户表也跳过 `collection_table_missing`；
- 只有存在真实业务集合路由时才执行“集合根必须有表”的检查，避免
  auth-only 契约拿兜底 `/records` 误报。

### 3. 修复死锁转用户决策（LangGraph interrupt）

新增节点 `conflict_gate`：

1. `validate_contract` 自动修复耗尽后，若剩余问题为
   `collection_table_missing`，返回 `AWAITING_DECISION` 并写入
   `contract_conflict`；
2. `conflict_gate` 先发 `architect.contract.conflict` 事件再 `interrupt()`；
3. 用户通过新端点
   `POST /api/v1/architect/runs/{runId}/contract-conflicts/{conflictId}/decision`
   提交服务端下发的 `optionId`；
4. 决策确定性应用：
   - `MAP_TO_TABLE`：把集合根、子路由与 component_interactions 一起改写
     到已有真实表（保留 HTTP 方法前缀与查询串），去重冲突路由；
   - `CREATE_TABLE`：按数据库方言追加最小表 DDL（mysql/postgresql/sqlite）；
   - `CANCEL`：进入 `cancelled` 终态；
5. 应用后重新走 `validate_contract` 复验，并设置
   `conflict_resolution_applied=True` + `repair_count=repair_limit`，
   禁止死循环或再次交给模型盲修。

服务层新增 `ActiveArchitectRun.conflict`、`decide_conflict()`、
快照 `conflict` 字段；BFF 与前端新增对应代理 / API / 决策弹窗。

## 关键取舍

- **只对 `collection_table_missing` 提供三选一**：它有确定性出路；
  其它契约矛盾（关联字段不存在、嵌套表缺失）仍按失败处理，避免把
  复杂问题伪装成简单选项。
- **前端只回传 optionId**：目标表与动作都由服务端在冲突载荷中给出，
  客户端不能借决策接口任意改写契约。
- **auth-only 契约跳过集合校验**：没有业务集合时不强行要求 DDL 表，
  由生成阶段的业务逻辑自行承接认证动作。
- **改写 component_interactions**：只改 api_routes 会让交互引用失配，
  审计会继续报 `undeclared_interaction_route`，必须同步改写并保留
  `GET ` 等方法前缀。

## 验证

- Python 552 项 unittest 通过，覆盖：
  - auth 路由 + users 表审计通过；
  - auth-only 契约不误报 records 缺表；
  - 修复耗尽后进入 `AWAITING_DECISION`；
  - MAP_TO_TABLE / CREATE_TABLE / CANCEL 三条恢复路径；
  - 服务层拒绝非法 optionId；
  - Prompt 包含资源/动作分离与集合根落地约束。
- 前端 16 项 Vitest 通过（新增 3 项冲突 reducer 测试），生产构建通过。
- Java BFF 镜像构建通过（新增冲突决策代理端点）。
