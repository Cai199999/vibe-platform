# 登录表单 全栈应用

由 Vibe Platform 生成的全栈项目。

## 技术栈

- 前端：React 18 + Vite + Tailwind CSS
- 后端：Node.js + Express
- 数据库：MySQL 8（生产部署）
- ORM：Sequelize

## 本地运行

```bash
cp .env.example .env
npm install
cd server && npm install && cd ..
npm run dev
```

- 前端：http://localhost:5173
- 后端：http://localhost:3000
- 健康检查：`GET /api/health`
- 集合：`/api/v1/facilities`

## 生产部署

```bash
docker compose up -d --build
node server/migrate.mjs
```

前端静态构建：`npm run build`，产物在 `dist/`；完整部署方式见 `DEPLOY.md`。
