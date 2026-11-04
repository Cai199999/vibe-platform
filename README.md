# vibe-platform

以前端可视化为抓手、多轮交互生成完整全栈应用的 Vibe Coding 平台。

当前阶段：**P0 脚手架**。只包含 Java BFF、Python AI Core、基础设施连接与最小 schema，不包含业务逻辑。

## 目录

```text
apps/platform-bff/          Java 21 + Spring Boot 3.3 BFF 骨架
services/orchestrator/      Python 3.12 + FastAPI + LangGraph 依赖骨架
infra/migrations/schema.sql M1 初始 MySQL schema（无物理外键）
scripts/init-db.sh          应用 schema 到已有 Docker MySQL
scripts/smoke-test.sh       验证 Java -> Python 跨语言调用
docker-compose.yml          只编排平台自身服务
```

## 前置条件

- Docker / Docker Compose
- 宿主机已有 `~/Workspace/infra` 提供的 MySQL 3306、Redis 6379、MinIO 9000/9001
- 本文档不启动、不重复部署上述中间件

## 启动

```bash
cp .env.example .env        # 首次执行；按需修改
./scripts/init-db.sh        # 首次执行；创建 users/projects/runs
docker compose up --build -d
docker compose ps
```

服务端口：

| 服务 | 宿主端口 | 容器内地址 |
|---|---|---|
| java-bff | 8080 | `http://java-bff:8080` |
| python-ai | 8100 | `http://python-ai:8100` |

两个服务都加入自定义网络 `vibe-platform-net`，因此 Java 可以直接通过服务名
`http://python-ai:8100` 访问 Python。

## 验证

### 1. Python 自身

```bash
curl -s http://127.0.0.1:8100/api/v1/health | python3 -m json.tool
curl -s http://127.0.0.1:8100/api/v1/internal/ping | python3 -m json.tool
```

### 2. Java 调用 Python

```bash
curl -s http://127.0.0.1:8080/api/v1/health | python3 -m json.tool
```

预期返回：

```json
{
  "status": "UP",
  "service": "java-bff",
  "version": "0.1.0",
  "timestamp": "...",
  "dependencies": {
    "pythonAi": {
      "status": "UP",
      "target": "python-ai",
      "details": {
        "status": "UP",
        "service": "python-ai",
        "version": "0.1.0",
        "langgraph_version": "..."
      }
    }
  }
}
```

`dependencies.pythonAi.status=UP` 即证明 Docker 自定义网络内 Java -> Python 的 DNS 与 HTTP 调用成功。
若 Python 不可达，Java 返回 HTTP 503 且 `status=DEGRADED`，不会抛出 500。

一键验证：

```bash
./scripts/smoke-test.sh
```

## 数据库

```bash
# 查看表
docker exec -it geo-mysql mysql -uroot -proot -e "USE geo_ai_db; SHOW TABLES;"

# 确认没有物理外键
docker exec -it geo-mysql mysql -uroot -proot -N -e \
  "SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA='geo_ai_db';"
```

预期外键数量为 `0`。

## 停止

```bash
docker compose down
```

不删除外部中间件，也不删除 MySQL 中的业务表。
