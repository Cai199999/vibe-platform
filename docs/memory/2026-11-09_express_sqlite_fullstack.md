# 2026-11-09 · Express + SQLite 全栈沙盒与真实持久化

## 目标

验证“前端表单 → 后端 API → 数据持久化”是否真实可用，并把原先的
`node:http` + 内存仓储升级为 Express + SQLite。

## 体检基线（升级前）

`scripts/fullstack_e2e_check.py` 12 项中 6 项失败：

- 后端是 `node:http`，无 Express；
- 无 `server/db.js`，无 SQLite 文件；
- 数据只存在进程内存，重启即丢；
- 前端没有显式成功提示。

## 关键改动

- **沙盒模板**：`node:20-alpine` → `node:22-alpine`；预装 `express`；
  Vite `/api` 代理默认 `127.0.0.1:3000`；镜像 `EXPOSE 5173 3000`。
- **SQLite 选型**：原生 `sqlite3` npm 包在 Alpine 上依赖 node-gyp 编译，
  构建期预编译二进制下载不稳定；改用 Node 22 内置 `node:sqlite`
  （同一个 SQLite 引擎，零原生依赖、镜像秒级构建）。
- **DBACoder**：`server/schema.sql` 生成可直接执行的 SQLite DDL；
  `server/schema.mysql.sql` 保留契约原始 MySQL DDL。
- **BackendCoder**：生成 `server/index.js`（Express 路由、CORS、错误中间件）
  与 `server/db.js`（建表、种子数据、CRUD、`/api/health` 上报
  `runtime=express-sqlite`）；额外提供 `/api/data` 别名。
- **FrontendCoder**：数据面板增加 `data-fullstack-success` 成功提示；
  POST 后合并真实记录。
- **启动脚本**：`scripts/dev.mjs` 同时拉起 Express 与 Vite。
- **沙盒 Git**：`.gitignore` 排除 `data/`、`*.sqlite`、`*.sqlite3`、`*.db`。

## 验证

`/tmp/vibe-browser/bin/python scripts/fullstack_e2e_check.py`：**12/12 PASS**。

- 容器内 `server/index.js` / `db.js` / `schema.sql` 齐全；
- 后端监听 3000，`/api/health` 返回 `storage=sqlite`；
- Vite `/api` 代理 GET/POST 正常；
- Playwright 填表提交 → 出现“提交成功，数据已写入数据库”；
- `/api/data` 能查回浏览器刚提交的数据；
- `docker restart` 后数据仍在（`/app/data/fullstack.sqlite`）。

回归：`fullstack_generation_e2e_test.py` 8.46s PASS，
`fullstack_generation_ui_e2e_test.py` PASS，Python 全套 192 PASS。

## 已知边界

- SQLite 文件存容器文件系统，删除沙盒即销毁数据；未挂载宿主卷。
- 生成前端仍是“附加数据面板”，尚未按契约重写用户原始表单。
- 目标 MySQL DDL 仅生成文件，未在沙盒内连接 MySQL。
