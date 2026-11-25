# 登录表单 Backend

由 BackendCoder 根据契约生成：Express + Sequelize ORM。
目标数据库：**MySQL 8（生产部署）**；生产通过环境变量 `DATABASE_URL` 连接。
沙盒未配置 `DATABASE_URL` 时自动回退到 Node 22 内置 SQLite，
保证预览与 E2E 不依赖外部数据库。

- 表：`facilities`
- 集合路由：`/api/v1/facilities`
- 详情路由：`/api/v1/facilities/:facility_id`
- 通用数据别名：`/api/data`
- 健康检查：`/api/health`（真实探测数据库；异常返回 503）
- 本地预览库：`data/fullstack.sqlite`
- 生产迁移：`node server/migrate.mjs`（执行 server/migrations/*.sql，
  需要 DATABASE_URL；应用启动不会调用 sync() 自动建表）

## 契约路由

- `POST /api/v1/users/register` — 注册用户（仅 admin 可创建非 viewer 角色）
- `POST /api/v1/users/login` — 登录签发 JWT
- `GET /api/v1/users/me` — 获取当前用户信息
- `GET /api/v1/facilities` — 设施列表与空间范围查询
- `POST /api/v1/facilities` — 创建设施
- `GET /api/v1/facilities/{facility_id}` — 设施详情
- `PUT /api/v1/facilities/{facility_id}` — 更新设施
- `DELETE /api/v1/facilities/{facility_id}` — 删除设施（仅 admin）
- `GET /api/v1/facilities/{facility_id}/facility-images` — 设施影像列表
- `POST /api/v1/facilities/{facility_id}/facility-images` — 上传设施影像
- `GET /api/v1/patrol-routes` — 巡检路线列表
- `POST /api/v1/patrol-routes` — 创建巡检路线
- `GET /api/v1/health` — 健康检查

## 运行

```bash
node server/index.js
```

## 生产环境变量

```bash
NODE_ENV=production
DATABASE_URL='<mysql 连接串>'
DB_ENGINE=mysql
DB_SSL=true
PORT=3000
CORS_ORIGINS='https://your-frontend.example.com'
```

发布前必须执行迁移：

```bash
docker run --rm --env-file .env <image> node server/migrate.mjs
```

## 鉴权

- 首次登录默认 `admin / admin123`，可用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 覆盖。
- 生产必须设置 `JWT_SECRET`；`viewer` 只读，`admin` / `inspector` 可写。
- 相关环境变量：`JWT_SECRET`、`ADMIN_USERNAME`、`ADMIN_PASSWORD`、`JWT_ACCESS_TTL`。
