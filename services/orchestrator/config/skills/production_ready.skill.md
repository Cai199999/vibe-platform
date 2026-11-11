---
skillId: production.ready
name: 生产级后端与部署基线
agentId: backend_coder
agentIds:
  - devops_agent
description: 生成可上云的后端与部署产物：禁止硬编码连接串、强制 SQL 迁移、严格 CORS、参数化查询、多阶段镜像与真实健康检查。
version: 1.0.0
enabled: true
priority: 10
tools:
  - read_file
  - create_file
  - apply_code_edit
  - run_docker_build
keywords:
  - 生产
  - 部署
  - 上云
  - 环境变量
  - 迁移
  - migration
  - dockerfile
  - cors
  - sql注入
  - 健康检查
ragRefs: []
---

# 生产级后端与部署基线（强制）

本 Skill 同时挂载给 BackendCoder 与 InfraCoder。生成任何后端代码、SQL、
Dockerfile 或部署清单时，以下规则是**硬性验收条件**，不是建议。

## 1. 配置与密钥隔离

- `DATABASE_URL`、`PORT`、CORS 白名单、第三方密钥只能通过
  `process.env.xxx` 读取；禁止把连接串、账号、密码、域名写进源码。
- 导出包必须包含完整的 `.env.example`，列出每个变量的用途、示例格式
  与是否必填；真实 `.env` 必须被 `.gitignore` 和 `.dockerignore` 排除。
- 生产环境默认拒绝跨域：CORS 只允许 `CORS_ORIGINS` 中逗号分隔的完整
  Origin；禁止 `Access-Control-Allow-Origin: *` 与凭据同时出现。
- 端口必须支持平台注入（Render / 阿里云 / Railway 会覆盖 `PORT`），
  不允许硬编码监听端口后忽略环境变量。

## 2. 数据库与迁移

- 生产环境**禁止** Sequelize `sync()` / Hibernate `ddl-auto=update`
  自动建表；结构变更必须落成可审计的 SQL 迁移文件
  （`server/migrations/001_init.sql`、`002_xxx.sql`）。
- 迁移必须：按文件名升序执行、记录 `_schema_migrations`
  （文件名 + checksum + 时间）、重复执行幂等、历史文件不可修改。
- 迁移失败必须以非 0 退出码终止发布；应用启动不得偷偷补建表。
- 所有 SQL 必须参数化：ORM 使用绑定参数，原生查询使用
  `?` / `$1` 占位符；禁止字符串拼接用户输入。
- 表名 / 列名来自契约时必须先经过标识符白名单校验；动态排序字段
  必须映射到固定白名单，不能直接透传。

## 3. 健康检查与错误处理

- `GET /api/health` 必须真实探测数据库：连接失败或迁移未执行返回
  HTTP 503，并在响应中给出 `status`、`storage`、`tableReady`；
  连接正常且表可查询时才返回 200。
- 全局错误中间件必须统一输出 `{code, message}`，不得把堆栈、SQL、
  连接串返回给客户端；日志中也不得打印完整 `DATABASE_URL`。
- 前端所有网络请求必须有 Loading / Success / Error 三态；网络失败
  与 5xx 通过全局 fetch 拦截器统一提示，禁止静默吞掉异常。
- 优雅停机：监听 `SIGTERM`，停止接收新请求并关闭数据库连接池。

## 4. Docker 与部署

- 使用多阶段构建：`deps` 阶段只安装生产依赖，`runtime` 阶段只复制
  `node_modules` 与源码，禁止把 npm 缓存、devDependencies、`.git`
  打进最终镜像。
- 镜像必须以非 root 用户运行；`COPY --chown` 或显式 `chown` 必须在
  `USER` 之前完成，确保上传目录与数据目录可写。
- `.dockerignore` 必须排除 `node_modules`、`dist`、`data`、`.env*`、
  `*.sqlite`、`.git`。
- 部署清单必须声明：健康检查路径、迁移命令（发布前执行）、
  环境变量占位、数据库 TLS 开关；禁止把 `DB_AUTO_MIGRATE=true`
  作为生产默认值。
- 输出 `DEPLOY.md` 时必须写明：迁移步骤、回滚策略、健康检查验收、
  以及数据库备份/恢复注意事项。

## 5. 交付前自检

1. 搜索生成代码，确认没有 `DATABASE_URL = "..."`、`PORT = 3000`、
   `cors({ origin: '*' })` 之类的硬编码（本地回退除外）。
2. 确认 `server/migrations/001_init.sql` 存在且与 ORM 字段一致。
3. 确认 `/api/health` 在数据库不可达时返回 503。
4. 确认 Dockerfile 为多阶段构建、非 root、`.dockerignore` 完整。
5. 在总结中输出 `Production checks:` 清单，逐项说明通过情况。
