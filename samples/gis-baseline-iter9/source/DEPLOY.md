# 登录表单 全栈应用 · 部署指引

本目录由 Vibe Platform 的 InfraCoder 自动生成，已包含：

| 文件 | 用途 |
|---|---|
| `Dockerfile` | 后端 API 生产镜像（Express + Sequelize） |
| `.dockerignore` | 隔离本地依赖、运行期数据与密钥 |
| `render.yaml` | Render Blueprint：后端 Docker + 前端静态站 |
| `.env.example` | 数据库地址、端口、CORS 等环境变量模板 |
| `DEPLOY.md` | 本部署指引 |

目标数据库：**MySQL 8（生产部署）**（ORM: Sequelize，连接串由 `DATABASE_URL` 注入）。

## 1. 导出代码包

在工作区点击「导出」，或使用平台提供的归档下载；导出包不包含
`node_modules`、`dist`、`.env` 与本地 SQLite 数据文件。

## 2. 准备云数据库

1. 在云厂商（阿里云 RDS / 腾讯云 / Neon / Supabase / Railway 等）创建
   MySQL 8（生产部署） 实例；
2. 创建业务库与账号，拿到连接串；
3. 生产连接串写入部署平台的 `DATABASE_URL`，不要写进代码或 Git。

## 3. Render 一键部署（推荐）

1. 将代码推到 GitHub / GitLab；
2. Render → **New → Blueprint**，选择仓库与 `render.yaml`；
3. 填写：
   - `DATABASE_URL`：第 2 步的数据库连接串；
   - `CORS_ORIGINS`：前端公网地址，逗号分隔多个域名（生产禁止 `*`）；
   - `DB_SSL`：云数据库通常填 `true`；
   - `VITE_API_BASE_URL`：后端 API 公网地址，例如
     `https://vibe-facilities-api.onrender.com`；
4. 应用 Blueprint：Render 会先执行 `node server/migrate.mjs`，
   迁移成功后才启动 Docker 镜像，最后构建前端静态站；
5. 打开 `https://<后端域名>/api/health`，确认 HTTP 200、`status=ok`、
   `tableReady=true` 且 `storage=mysql`。

## 4. Vercel 部署前端（可选）

1. Vercel → **Add New → Project**，导入同一仓库；
2. Framework 选择 Vite，Build Command `npm run build`，Output `dist`；
3. 环境变量 `VITE_API_BASE_URL` 填写后端公网地址；
4. 部署完成后，把 Vercel 域名加入后端的 `CORS_ORIGINS`。

## 5. Railway / 阿里云 / 其它 Docker 平台

```bash
# 1) 构建生产镜像（多阶段构建，不包含 devDependencies）
docker build -t vibe-facilities-api .

# 2) 先执行 SQL 迁移；失败会以非 0 退出，禁止继续发布
docker run --rm --env-file .env \
  vibe-facilities-api node server/migrate.mjs

# 3) 迁移成功后再启动服务
docker run --rm -p 3000:3000 --env-file .env \
  vibe-facilities-api
```

平台会自动读取根目录 `Dockerfile`；也可以把镜像推送到任意容器 Registry。
生产环境必须把迁移作为独立发布步骤，不能依赖应用启动时自动建表。

## 6. 数据库迁移与验收（生产必读）

- 迁移文件：`server/migrations/001_init.sql`（随契约 DDL 生成）；
- 执行入口：`node server/migrate.mjs` / `npm run db:migrate`；
- 执行顺序：按文件名升序；已执行文件记录在 `_schema_migrations`，
  重复执行不会重复建表；改动历史文件会因 checksum 不一致直接报错；
- 禁止在生产调用 Sequelize `sync()`：应用启动只连接数据库，不建表；
- 新增结构变更：新增 `002_xxx.sql`，不要修改 `001_init.sql`；
- 验收：
  1. `GET /api/health` 返回 HTTP 200、`status=ok`、`tableReady=true`；
  2. `GET /api/v1/facilities` 能读取列表；
  3. `POST /api/v1/facilities` 能写入并再次查询到记录；
- 本地沙盒：未配置 `DATABASE_URL` 时自动使用 SQLite，便于先联调再上云。

> 安全提示：`DATABASE_URL`、账号密码与 `CORS_ORIGINS` 必须通过部署平台的
> 环境变量注入；不要把真实密钥写入仓库、镜像或前端构建产物。
