# 2026-11-11 · 全栈真实业务验收（multipart / MySQL）

> 承接 P0：把“静态断言”升级为“真实请求 + 真实数据库”。

## 1. 验收链路

`scripts/acceptance_test.py` 扩展为 8 个阶段：

1. Architect（mock）→ 契约确认 → 全栈生成；
2. 通过 BFF / AI Core 下载 `project-export.tar.gz`；
3. 安全解压 + 部署三件套检查；
4. 前后端路由一致性；
5. 业务语义断言（契约路由 / DDL / 上传中间件 / 成功态）；
6. 纯净 `node:22-alpine` 内 `npm install` / `npm run build`；
7. `docker build` 后无 DB 配置启动：SQLite 自动建表、CRUD，
   若契约要求上传则发送真实 `multipart/form-data`，校验文件落盘、
   静态 URL 回读且内容逐字节一致；
8. 注入宿主机 MySQL（`~/Workspace/infra`，自动 `CREATE/DROP DATABASE`），
   验证 Sequelize 建表、CRUD，并用宿主机 `pymysql` 直连按 id 查行，
   证明数据真的写入 MySQL 而非仅回显。

新增参数：`ACCEPTANCE_MYSQL=0` 跳过第 8 步；
`ACCEPTANCE_MYSQL_HOST/PORT/USER/PASSWORD` 与
`ACCEPTANCE_MYSQL_CONTAINER_HOST`（默认 `host.docker.internal`，
容器通过 `--add-host ...:host-gateway` 访问宿主机）。

## 2. 业务 Eval 接入真实运行

- `business_cases.yaml` 新增 `semantic.runtime: true`；
- `biz_003 简历上传与列表` 开启；
- `business_eval.py` 复用验收脚本的 `docker_build` /
  `docker_run_and_verify_sqlite` / `docker_run_and_verify_mysql`，
  静态语义通过后继续跑真实容器。

实测（DeepSeek，2026-11-11）：

| 指标 | 结果 |
|---|---|
| 业务通过率 | 1/1 = 100% |
| 契约理解 | 1/1 = 100% |
| 产物语义 | 1/1 = 100% |
| 耗时 / 成本 | 101.9s / 10,220 tokens / ¥0.028918 |

关键日志：

```
✓ multipart 上传真实落盘：POST /api/v1/resumes
  -> /uploads/1794357767431-v2cab8ns.pdf，回读一致
✓ MySQL 真实持久化：直连 vibe_acc_...resumes 查到 id=1
```

## 3. 真实验收抓出的生成缺陷（已修）

1. **Dockerfile 权限**：`COPY server/ ./server` 发生在 `chown` 之后，
   文件归 root，`node` 用户写 `server/uploads` 直接 `EACCES`。
   修复：二次 COPY 后 `mkdir -p /app/server/uploads /app/data
   && chown -R node:node /app`，再 `USER node`。
2. **上传字段漏映射**：只识别 `file_name` 等精确名，
   `resume_file_name` / `resume_file_path` / `resume_file_size`
   全部漏掉。修复：字段正则支持业务前缀，匹配方式改为 `search`。
3. **上传响应缺元数据**：即使文件已落盘，响应也不含 `/uploads/` 地址。
   修复：集合新增与独立上传动作统一返回
   `upload: { name, path, size }`。
4. **验收脚本自身**：集合路由写作 `app.post(COLLECTION_PATH, ...)`，
   只匹配字符串字面量会漏判。修复：解析 `const` 字符串常量。
5. **表名语义过严**：模型把 `resumes` 命名为 `resume_submissions`
   被误判失败。修复：单数化后按词元前缀匹配。

## 4. 回归

- `services/orchestrator`：29 个 fullstack 单测通过；
- 新增 `scripts/test_acceptance_runtime.py`（6 例）与
  `scripts/test_business_eval_utils.py`（3 例），共 9 个离线单测；
- 全栈产物验收（mock 待办）：SQLite CRUD、MySQL CRUD + 直连查行全部通过。

## 5. 仍为边界

- 生成产物仍是“单应用容器 + 外部 MySQL”，未生成 web/api/db 多服务编排；
- 上传验收在 SQLite 运行实例上验证文件链路，MySQL 实例验证 CRUD，
  尚未在同一请求里同时验证“multipart + MySQL 文件元数据落库”；
- Redis checkpoint、MinIO、真实鉴权、BFF 持久化未覆盖。
