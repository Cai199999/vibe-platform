# 18 · G2 薄切片：平台 JWT 与审计落库

> 2026-11-21 · 目标：把**平台自己的 Mock 登录**替换为可验证的 JWT
> 签发/校验，并把 ChangeLog 与危险操作 HITL 记录写入 MySQL 审计表。
>
> 结论：**这是 1–1.5 天的垂直切片，不建议在面试前切换默认鉴权。**
> 采用 `AUTH_MODE=mock|jwt`，演示默认 `mock`，`jwt` 在测试/验收环境启用。
>
> **注意**：本文只覆盖“平台登录”。如果你要的是“生成出来的 GIS 应用”
> 自带 JWT 登录与 RBAC，那是另一条线，见 `19_生成应用JWT与RBAC.md`。

## 1. 范围

### 做

- BFF 真实 JWT 签发与校验（HS256）。
- MySQL `users` 表 + 演示用户种子；BCrypt 密码。
- 前端登录改为调用 `/api/v1/auth/login`；保留 Mock 模式开关。
- MySQL `audit_events` 表；BFF 审计写入接口。
- Python Direct Ops 的 ChangeLog / HITL 决策上报审计；失败 fail-open。
- 单元测试 + BFF/前端联调测试 + 一条登录/审计 E2E。

### 不做

- 完整 OIDC / SSO / Keycloak。
- 多租户自助注册、租户管理后台。
- 细粒度 RBAC 管理 UI。
- gVisor / 生产级沙盒。
- 审计查询大屏（先落库 + 基础列表）。

## 2. 鉴权

### 端点

```text
POST /api/v1/auth/login
  { "account": "admin@vibe.local", "password": "demo1234" }
  -> 200 { token, expiresAt, user: { id, name, role, tenantId } }
  -> 401 AUTH_INVALID_CREDENTIALS
```

### JWT

- 算法：HS256；secret 来自 `AUTH_JWT_SECRET`（本地 `.env`）。
- Claims：`sub`（userId）、`role`、`tenantId`、`iat`、`exp`。
- 默认 TTL：2 小时。
- 失败信封沿用统一 `code/message/category/severity/stage/traceId`。

### BFF 过滤器

- 白名单：`/api/v1/auth/login`、`/api/v1/health`、`/actuator/health`。
- 其余 `/api/**` 必须 `Authorization: Bearer <jwt>`。
- 校验通过后写入 request attributes：`userId / role / tenantId`。
- 401 不返回 500；不泄露具体校验原因。

### 用户表

```sql
CREATE TABLE IF NOT EXISTS users (
  id VARCHAR(64) PRIMARY KEY,
  tenant_id VARCHAR(64) NOT NULL,
  account VARCHAR(128) NOT NULL UNIQUE,
  display_name VARCHAR(128) NOT NULL,
  password_hash VARCHAR(100) NOT NULL,
  role VARCHAR(32) NOT NULL,
  enabled TINYINT NOT NULL DEFAULT 1,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

种子用户：`admin@vibe.local / demo1234 / platform_admin / tenant_local`。

### 前端

- `AuthContext.login`：
  - `VITE_AUTH_MODE=mock`：保留现有一键体验；
  - `VITE_AUTH_MODE=jwt`：调用 `/api/v1/auth/login`，存 token/user/expiresAt。
- 所有已有客户端已经带 `Authorization: Bearer ${token}`，不需要逐个改；
  只需确保登录返回的 token 是真实 JWT。
- 401 时清理会话并跳登录页。

## 3. 审计

### 表

```sql
CREATE TABLE IF NOT EXISTS audit_events (
  id VARCHAR(64) PRIMARY KEY,
  tenant_id VARCHAR(64) NOT NULL,
  project_id VARCHAR(64) NOT NULL,
  run_id VARCHAR(64) NOT NULL DEFAULT '',
  actor_id VARCHAR(64) NOT NULL DEFAULT '',
  actor_role VARCHAR(32) NOT NULL DEFAULT '',
  action VARCHAR(64) NOT NULL,
  risk VARCHAR(16) NOT NULL,
  target_json JSON NULL,
  payload_json JSON NULL,
  created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  INDEX idx_audit_project_time (project_id, created_at),
  INDEX idx_audit_run (run_id)
);
```

### 写入路径

```text
Direct Ops ChangeLog.append(entry)
  -> POST /api/v1/audit/events
  -> MySQL audit_events
  -> 失败：记录 warning，保留 JSONL 本地文件（fail-open）

Direct Ops HITL 决策
  -> POST /api/v1/audit/events
  -> action=HITL_DECISION, risk=L2_HITL
```

### 审计字段映射

| 来源 | action | risk |
|---|---|---|
| Direct Ops 删除/移动/样式 | `DIRECT_OP` | `L0_DIRECT` / `L2_HITL` |
| HITL 决策 | `HITL_DECISION` | `L2_HITL` |
| 契约修订 | `CONTRACT_REVISE` | `L2_HITL` |
| 回滚 | `ROLLBACK` | `L2_HITL` |

## 4. Feature flags

```text
AUTH_MODE=mock|jwt        # BFF/前端；默认 mock
AUDIT_PERSIST_ENABLED=true|false  # 默认 false，先观察
AUTH_JWT_SECRET=...       # 仅 jwt 模式
```

**回滚**：

- `AUTH_MODE=mock` 恢复现有演示；
- `AUDIT_PERSIST_ENABLED=false` 停止审计写入；
- 不需要回滚数据库表（表是新增的）。

## 5. 测试与验收

### BFF

- 登录成功返回 JWT；错误密码 401；
- 无 token 访问受保护接口 401；
- 过期/篡改 token 401；
- admin 角色能访问 `/api/v1/admin/**`，member 不能；
- 审计插入成功、字段完整。

### 前端

- mock 模式一键体验不变；
- jwt 模式登录成功、401 跳登录；
- 现有请求头仍带 token。

### Python

- ChangeLog 写入调用审计客户端；
- HITL 决策写入审计；
- BFF 不可达时 fail-open，不阻断 Direct Ops；
- 重复事件按 `id` 幂等。

### E2E

1. `AUTH_MODE=jwt` 登录 → 打开项目 → Direct Ops 删除 → 审计表出现 `DIRECT_OP`；
2. HITL 决策 → 审计表出现 `HITL_DECISION`；
3. 切回 `AUTH_MODE=mock`，原演示流程不变。

## 6. 工作量与风险

| 部分 | 估计 |
|---|---|
| JWT 签发/校验 + 用户表 + 种子 | 0.5 天 |
| 前端登录切换 + 401 处理 | 0.25 天 |
| 审计表 + BFF 接口 | 0.25 天 |
| Python ChangeLog/HITL 上报 | 0.25 天 |
| 测试 + 联调 + E2E | 0.5–1 天 |
| **合计** | **1–1.5 天** |

风险：

- 前端登录/请求头改动会触及所有页面；
- BFF 过滤器可能影响 SSE/长连接/健康检查；
- 审计写入失败不能阻断 Direct Ops（必须 fail-open）；
- MySQL 表结构与 `~/Workspace/infra` 的现有库要隔离，避免影响其他项目。

## 7. 建议

- **面试前**：只保留设计，不切默认鉴权；Demo 继续 Mock。
- **面试后**：在 `feat/g2-jwt-audit` 分支实现，`AUTH_MODE=jwt` 验收通过后再考虑切换。
- 若必须在面试前展示：只演示 `AUTH_MODE=jwt` 的登录 + 一条审计记录，不切换主 Demo。
