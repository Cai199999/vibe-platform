# 2026-11-11 · 生产级武装与云端部署模拟验收

> 目标：消除“沙盒玩具”与真实生产环境的 GAP，导出产物可直接交付
> 阿里云 / Render。三维度全部落地，验收脚本全绿。

## 1. 维度一：生产级契约与代码生成

### 环境变量隔离

- `server/db.js` 只通过 `process.env.DATABASE_URL` 连接 MySQL / PostgreSQL；
  支持 `DB_ENGINE`、`DB_SSL`、`DB_POOL_MAX`，不硬编码连接串。
- `server/index.js` 的 `PORT`、`CORS_ORIGINS` 全部来自环境变量；
  生产环境 CORS 为空时默认拒绝跨域，不再隐式放开 `*`；
  未命中白名单的预检请求返回 403。
- 导出包提供完整 `.env.example`（数据库 / 连接池 / CORS / 迁移目录 /
  前端 API 地址），真实 `.env` 继续被 `.gitignore` 屏蔽。

### 显式数据库迁移

- `DBACoder` 生成：
  - `server/migrations/001_init.sql`：按目标引擎规范化 DDL；
  - `server/migrations.mjs`：按文件名升序执行，写入
    `_schema_migrations(filename, checksum, applied_at)`；
  - `server/migrate.mjs`：生产入口，缺少 `DATABASE_URL` 或迁移失败
    立即 `exit(1)`，绝不让旧表结构上线。
- 应用启动不再调用 `model.sync()`，删除 `DB_AUTO_MIGRATE`；
  历史迁移 checksum 被改写时直接失败，保证迁移不可变。
- Render `preDeployCommand: node server/migrate.mjs`；
  `DEPLOY.md` 写明手工迁移、回放与健康检查步骤。

### 健康检查与前端容错

- `/api/health` 真实 `authenticate()` + `model.count()`：
  连接失败或表未迁移返回 HTTP 503，`status=degraded`、
  `tableReady=false`；正常时返回 200、`tableReady=true`。
- 前端新增 `src/api-client.jsx`：全局 fetch 拦截器、统一
  `apiFetch`、`ApiError`、`vibe:api-error` 事件与 `ApiErrorBanner`；
  面板与骨架页均接入，网络错误不再静默。

### 容器与部署

- 多阶段 Dockerfile：`deps` 阶段安装生产依赖，`runtime` 仅复制产物；
  `COPY --chown=node:node`、`USER node`，非 root 运行。
- `.env.example` / `render.yaml` / `DEPLOY.md` 与代码保持同一契约。

## 2. 维度二：Agent 武装（Skill + MCP）

### 生产 Skill

- 新增 `config/skills/production_ready.skill.md`：
  环境变量、迁移、参数化 SQL、严格 CORS、503 健康检查、
  多阶段非 root 镜像、`.dockerignore` 与发布检查清单。
- `skill_loader` 支持 `agentIds` 多 Agent 挂载；
  同时挂载到 `backend_coder` 与 `devops_agent`，
  技能 ID 分别为 `production.ready` / `production.ready.devops_agent`。

### SQLite MCP 真实验证

- 新增 `ai_core/mcp_servers/sqlite_mcp.py`（stdio JSON-RPC 2.0）：
  - `validate_schema`：在临时 SQLite 真实执行 DDL，
    查询 `sqlite_master` + `PRAGMA table_info`；
  - `describe_table` / `list_tables`：只读 URI 打开数据库；
  - 仅允许 `CREATE TABLE / CREATE INDEX / ALTER TABLE ADD COLUMN`，
    拒绝 `DROP` 等破坏性语句。
- 新增 `fullstack/schema_mcp.py`，`ConsistencyVerifier` 在静态路由检查后
  调用 MCP 验证契约表与字段；失败时以 `FIX_SCHEMA` 阻断构建。
- 配置项：`SCHEMA_MCP_ENABLED` / `SCHEMA_MCP_PYTHON` /
  `SCHEMA_MCP_SCRIPT` / `SCHEMA_MCP_TIMEOUT_SECONDS` /
  `SCHEMA_MCP_STRICT`。

## 3. 维度三：云端部署模拟验收

`scripts/acceptance_test.py` 第 8 步重写为真实发布链路：

1. 创建一次性 MySQL 库；
2. 生成生产 `.env`（真实连接串 + `CORS_ORIGINS`），
   通过 `docker run --env-file` 注入，连接串不出现在命令行；
3. 启动业务容器，确认未迁移时 `/api/health` 返回 **503**、
   `tableReady=false`，杜绝 ORM 偷偷建表；
4. 独立容器执行 `node server/migrate.mjs`，要求 `exit=0`；
5. 直连 MySQL 校验 `_schema_migrations` 记录与 64 位 checksum、
   真实表字段；
6. 业务容器健康检查恢复 **200**（`tableReady=true`），
   完成 CRUD 并用宿主机 `pymysql` 直连查行；
7. 再次执行迁移，确认 `alreadyApplied`、记录不重复、checksum 不变。

成功输出：

```
🎉 生产级产物验收通过，可直接交付云端部署
```

## 4. 验证结果

| 项目 | 结果 |
|---|---|
| Python 单测（fullstack / consistency / MCP+Skill） | 69/69 通过 |
| 验收脚本离线单测 | 9/9 通过 |
| 端到端验收（mock 待办，真实 Docker + MySQL） | 全绿 |
| SQLite 回退 + CRUD | 通过 |
| 未迁移 503 → migrate → 200 | 通过 |
| MySQL 直连持久化 | 通过 |
| 迁移幂等 | 通过 |

关键日志：

```
✓ 未迁移数据库健康检查正确返回 503：status=degraded tableReady=false
✓ 真实 MySQL 建表完成：vibe_acc_xxx.app_records columns=id,tenant_id,name,...
✓ 迁移后健康检查恢复 200：status=ok storage=mysql tableReady=true
✓ MySQL 真实持久化：直连 vibe_acc_xxx.app_records 查到 id=1
✓ 迁移幂等验证通过：重复发布未重复建表、checksum 未变化
```

## 5. 仍为边界

- 产物仍是“单应用容器 + 外部 MySQL”，未生成 web/api/db 多服务编排；
- 云端真实平台（Render / 阿里云）未实际部署，仅本地镜像模拟发布顺序；
- 生产鉴权、密钥托管（KMS / Secret Manager）、TLS 终止未在产物内实现；
- 迁移只支持顺序 SQL，尚未生成回滚脚本（down migration）。
