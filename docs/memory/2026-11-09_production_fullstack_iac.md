# 2026-11-09 · 生产级全栈：多引擎 ORM + IaC + 部署指引

## 目标

把全栈生成产物从「沙盒玩具」升级为可直接上线的项目包：契约可选生产数据库，
DBACoder 产出云数据库 ORM 代码，生成部署配置，平台预览区提供部署指引入口。

## 契约与生成链路

- `ArchitectContract.database_engine`（JSON `databaseEngine`）取值
  `mysql | postgresql | sqlite`；`normalize_database_engine` 兼容
  postgres/pgsql、mysql/mariadb、sqlite 前缀。
- 全栈契约未显式选择时默认 `mysql`；澄清问题明确「SQLite 仅供本地测试」。
- `FullstackGraph` 扩展为六节点：
  `DBACoder → BackendCoder → FrontendCoder → InfraCoder → BuildVerifier → SandboxRestart`。
- `InfraCoder` 生成 `Dockerfile`、`.dockerignore`、`render.yaml`、
  `.env.example`、`DEPLOY.md`；`fullstack.started/completed` 增加
  `databaseEngine`、`orm`、`infraFiles`。

## 双路径数据库策略

`server/db.js` 统一暴露仓储函数：

- 配置 `DATABASE_URL`：动态 `import('sequelize')`，dialect 由
  `DB_ENGINE`/URL 决定，支持 MySQL / PostgreSQL；`DB_AUTO_MIGRATE=true`
  可首次建表，`DB_SEED=true` 灌种子数据。
- 未配置：动态 `import('node:sqlite')` 回退本地文件库，预览与 E2E 零外部依赖。
- 动态 import 让生产路径不再触发 `node:sqlite` ExperimentalWarning。

## 平台端

预览工具栏新增【🚀 部署上线】按钮，打开 `DeployGuideDialog` Mock 弹窗，
展示已生成文件与 Render / Railway / Vercel 部署步骤。

## 验证

- Python 全量 203 tests OK（容器内按仓库根布局运行）。
- 生成物 `node --check` 通过；SQLite 与 MySQL（临时库）双路径 health/POST/GET
  冒烟通过；生产路径无 sqlite 警告。
- API E2E、浏览器 UI E2E、gate UI E2E、disabled hint UI E2E 全部 PASS；
  前端 `vite build` PASS。
