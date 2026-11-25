# 19 · 生成应用的 JWT 与 RBAC

> 2026-11-21 · 目标：让**平台生成出来的 GIS 应用**自带真实登录、JWT 校验、
> 角色权限与租户字段；平台自己的 Mock 登录不动。
>
> 现状：契约里写了 JWT，但 Fullstack CodeGen 只生成了 `server/security.js`
> 的 bcrypt / 输入清洗，没有 `/auth/login`、没有 JWT 签发/校验、没有
> `users` 表、没有 RBAC 中间件。
>
> 结论：这是 Fullstack Contract / BackendCoder / FrontendCoder 的增强，
> 不是平台 BFF 的鉴权改造。后端薄切片约 0.5–1 天；带登录页约 1–2 天。

## 1. 现状核查

对最近生成的 GIS 应用沙箱检查：

```text
server/security.js   -> bcrypt hash/verify + sanitize，无 JWT
server/index.js      -> 无 /auth/login，无 Authorization 校验，无角色守卫
server/schema.sql    -> 只有业务表 + tenant_id，没有 users 表
server/api-routes.json -> 只有 CRUD + /api/health，没有 auth 路由
```

所以：

- **平台登录**（`AuthContext.jsx`）是 Mock；
- **生成应用登录**目前不存在；
- 契约中的 `auth: JWT ...` 只是声明，CodeGen 没有落地。

## 2. 目标

生成的应用具备：

1. `POST /api/v1/auth/login`：账号密码 → JWT；
2. `GET /api/v1/auth/me`：返回当前用户与角色；
3. `users` 表：`tenant_id / account / password_hash / role / enabled`；
4. 受保护路由：无 token 401，无权限 403；
5. 种子管理员：`admin@demo.local / demo1234`；
6. （可选 Phase 2）前端登录页、token 存储、401 跳转。

## 3. 契约增强

### 3.1 契约字段

`ArchitectContract` 已有 `auth` 相关声明；需要明确成结构化字段：

```json
{
  "auth": {
    "mode": "jwt",
    "algorithm": "HS256",
    "tokenTtlMinutes": 120,
    "roles": ["admin", "manager", "member"],
    "tenantScoped": true,
    "seedAdmin": {
      "account": "admin@demo.local",
      "password": "demo1234",
      "role": "admin"
    }
  }
}
```

### 3.2 DDL

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

## 4. BackendCoder 增强

生成：

```text
server/auth.js
  - signToken(user) / verifyToken(token)
  - requireAuth middleware
  - requireRole(...roles) middleware

server/index.js
  - POST /api/v1/auth/login
  - GET  /api/v1/auth/me
  - 所有 /api/v1/<resource> 路由挂 requireAuth
  - /api/health 保持公开

server/schema.sql / migrations
  - users 表 + 种子管理员（bcrypt）

package.json
  - 增加 jsonwebtoken 或 jose
```

规则：

- JWT secret 来自 `JWT_SECRET`，启动时缺失直接 fail-fast；
- 密码只存 bcrypt；
- 登录失败统一 401，不泄露账号是否存在；
- 角色守卫：写操作至少 `admin/manager`，读操作 `member+`（按契约角色）；
- `tenant_id` 从 token 注入，查询自动带租户条件。

## 5. FrontendCoder 增强（Phase 2）

- 生成 `/login` 页面：账号、密码、错误提示；
- 登录成功存 token（内存 + sessionStorage）；
- 所有 `/api` 请求带 `Authorization: Bearer <token>`；
- 401 → 清理 token → 跳登录；
- 顶部显示当前用户/角色，退出登录；
- 页面路由守卫：未登录只允许 `/login`。

## 6. 审计（可选）

生成应用如果需要自己的审计：

```sql
CREATE TABLE audit_events (
  id VARCHAR(64) PRIMARY KEY,
  tenant_id VARCHAR(64) NOT NULL,
  actor_id VARCHAR(64) NOT NULL,
  action VARCHAR(64) NOT NULL,
  target_json JSON NULL,
  created_at TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
);
```

用于登录、写操作、角色变更。平台的 ChangeLog / HITL 审计仍走 `18` 的平台侧方案，两者不要混。

## 7. 测试与验收

### 后端薄切片（先做）

```text
[ ] 登录成功 200 + JWT
[ ] 错误密码 401
[ ] 无 token 访问业务接口 401
[ ] 过期/篡改 token 401
[ ] member 访问 admin 接口 403
[ ] tenant_id 注入查询
[ ] seed admin 可登录
```

演示方式：`curl` / Postman：

```bash
curl -s -X POST http://<sandbox>:3000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"account":"admin@demo.local","password":"demo1234"}'

curl -s http://<sandbox>:3000/api/v1/task
# 401

curl -s http://<sandbox>:3000/api/v1/task \
  -H "Authorization: Bearer <token>"
# 200
```

### 前端登录页（Phase 2）

- 登录成功进入业务页；
- 刷新保持登录；
- 401 自动跳登录；
- 角色不足显示 403 页面。

## 8. 工作量与风险

| 部分 | 估计 |
|---|---|
| 契约 auth 字段 + DDL users 表 | 0.25 天 |
| BackendCoder auth.js + login/me + middleware + seed | 0.5 天 |
| 测试（401/403/租户/过期） | 0.25 天 |
| 前端登录页 + token + 401 | 0.5–1 天 |
| **合计** | **1–2 天** |

风险：

- 生成应用的前端是 Visual CodeGen，新增登录页需要改 VisualDocument/CodeGen；
- JWT 依赖需要在沙盒 `npm install` 中可用；
- 种子密码/secret 不能硬编码进生产；
- 角色策略要跟契约一致，避免“生成了 RBAC 但路由没挂”。

## 9. 建议

1. **先做后端薄切片**：users 表 + `/auth/login` + `/auth/me` + middleware + 401/403 E2E；
2. 平台自己的 Mock 登录**不动**，Demo 不受影响；
3. 前端登录页作为 Phase 2，面试后做；
4. 面试时可以展示：
   - 生成应用有真实 `users` 表和登录接口；
   - 无 token 401、无权限 403；
   - 平台登录仍是 Mock，但生成应用鉴权是真的。
