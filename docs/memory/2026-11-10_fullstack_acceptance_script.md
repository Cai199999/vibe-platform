# 2026-11-10 · 全栈产物验收脚本与 Dockerfile 修复

## 目标

让每次全栈生成后都能用一条命令判断产物是否真的可部署：

    python3 scripts/acceptance_test.py

全部通过时固定输出：

    🎉 全栈产物验收通过，已具备上云条件

失败时逐条打印缺失文件、路由 404 风险、npm/build/容器运行错误，退出码 1。

## 实现

`scripts/acceptance_test.py` 复用 `scripts/fullstack_generation_e2e_test.py`
的生成链路（Architect mock → CONFIRM → 全栈 SSE），然后完成 6 个阶段：

1. **生成**：拿到 `runId`、`sandboxId`、`collectionPath`；
2. **导出**：BFF 走 `/api/v1/runs/{runId}/export?sandboxId=...`，
   直连 AI Core 走 `/api/v1/internal/sandbox/{id}/export`；
   校验 gzip 魔数、大小与 HTTP 状态；
3. **安全解压**：拒绝绝对路径、`..`、符号链接/硬链接/设备文件，
   限制 5000 文件与 200MB，使用 Python 3.12 `filter="data"`；
   检查根目录 `DEPLOY.md`、`Dockerfile`、`render.yaml`；
4. **路由一致性**：
   - 后端以 `server/api-routes.json` 的 `routes` / `registeredRoutes`
     为主，`server/index.js` 的 `app.get/post/...` 为补充；
   - 前端扫描 `src/**/*.{js,jsx,ts,tsx}`：
     `INTERACTIONS` 按 JSON 整体解析（避免 `{task_id}` 文本干扰），
     路径常量 + `fetch(apiUrl(CONST))` 推断 GET/POST，
     直接 `fetch('/api/...')` 与字符串常量兜底；
   - 动态段统一归一化为 `:param`，比对 method + path；
     报“方法不匹配”或“404 风险”，同一路径不重复报；
5. **纯净容器安装/构建**：`node:22-alpine` 挂载导出包，
   `npm ci`（无 lock 时 `npm install`），有 `build` 脚本则继续 `npm run build`；
   随后 `docker build`；
6. **无配置运行**：`docker run` 只映射端口，不注入任何
   `DATABASE_URL` / `DB_*` / MySQL / PG / Redis / MinIO 环境变量；
   检查 `/api/health` 为 `status=ok`、`storage=sqlite`、
   `runtime=express-sqlite`，再对 `collectionPath` 做 GET/POST 落库验证。

清理：finally 中删除临时容器、镜像、临时目录与本次生成的沙盒；
`ACCEPTANCE_KEEP=1` 可保留临时目录，`ACCEPTANCE_VERBOSE=1` 输出命令日志。

## 发现并修复的真实缺陷

首次验收在纯净容器中启动失败：

    {"code":"INTERNAL_ERROR","message":"EACCES: permission denied, mkdir '/data'"}

原因：`server/db.js` 默认把 SQLite 放在 `resolve(__dirname, '..', 'data')`。
沙盒里 `db.js` 位于 `/app/server/`，得到 `/app/data`；但旧 Dockerfile 把
`server/` 内容平铺到 `/app`，`__dirname` 变成 `/app`，于是尝试创建根目录
`/data`，`USER node` 无权限。

修复 `BACKEND_DOCKERFILE_TEMPLATE`，保留 `server/` 层级：

    COPY server/package.json ./server/package.json
    RUN cd server && npm install --omit=dev --no-audit --no-fund \
        && chown -R node:node /app
    COPY server/ ./server
    CMD ["node", "server/index.js"]

这样容器与沙盒目录结构一致，SQLite 自动落到可写的 `/app/data`。
同步更新 `tests/test_fullstack.py` 的 Dockerfile 断言。

## 验证

- `scripts/acceptance_test.py` 实跑通过：
  - 生成 `sandboxId=sbx_...`，导出 56.7KB；
  - 三件套齐全，前端请求与后端 4 条路由文本匹配；
  - `npm ci + vite build` 成功，`docker build` 成功；
  - 无数据库环境变量启动，`storage=sqlite`，`/app/data/fullstack.sqlite`，
    `GET/POST /api/v1/app_records` 落库成功；
  - 输出 `🎉 全栈产物验收通过，已具备上云条件`。
- 负向抽查：删除 `render.yaml` 能被识别；注入 `/api/v1/ghost`
  调用会报 `404 风险`。
- `docker run ... python -m unittest tests.test_fullstack`：27 tests OK。
- `black scripts/acceptance_test.py` 通过。

## 注意

- 验收脚本会新建并清理自己的沙盒，不影响用户已有项目。
- 静态路由检查是“文本层面”的 404 风险预警，不替代真实请求；
  动态拼接且无法静态解析的 URL 不会被误判。
- Docker 阶段需要本机 daemon 与 `node:22-alpine` 镜像（缺失会自动拉取）。
