# vibe-platform

以前端可视化为抓手、多轮交互生成完整全栈应用的 Vibe Coding 平台。

当前阶段：**P0 脚手架**。只包含 Java BFF、Python AI Core、基础设施连接与最小 schema，不包含业务逻辑。

## 目录

```text
apps/platform-bff/                    Java 21 + Spring Boot 3.3 BFF 骨架
services/orchestrator/                Python 3.12 + FastAPI + LangGraph 依赖骨架
services/orchestrator/ai_core/        SandboxProvider 抽象与 LocalDockerSandbox
services/orchestrator/sandbox_template/ React + Vite 沙盒基础镜像
infra/migrations/schema.sql           M1 初始 MySQL schema（无物理外键）
scripts/init-db.sh                    应用 schema 到已有 Docker MySQL
scripts/smoke-test.sh                 验证 Java -> Python 跨语言调用
scripts/sandbox_e2e_test.py           沙盒创建/补丁/构建校验/销毁 E2E
docker-compose.yml                    只编排平台自身服务
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

## 沙盒（Sandbox）

`python-ai` 通过挂载的 `/var/run/docker.sock` 调用宿主 Docker，为每个项目创建
独立的 React + Vite 容器。容器限制：`cap_drop=ALL`、`no-new-privileges`、
内存 / CPU / PID 限额，预览端口只绑定 `127.0.0.1`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/internal/sandbox/create` | 创建容器，写入初始文件，返回预览 URL |
| POST | `/api/v1/internal/sandbox/apply-patch` | 全量文件 / unified diff，触发 Vite HMR |
| GET | `/api/v1/internal/sandbox/verify?sandbox_id=...&check=build` | 执行 `npm run build` / `npm run test` |
| DELETE | `/api/v1/internal/sandbox/{sandboxId}` | 强制销毁容器 |

沙盒镜像 `vibe-platform/sandbox-react:dev` 首次使用时自动构建（依赖已预热，
单次创建约 1~3 秒）。E2E 验证：

```bash
python3 scripts/sandbox_e2e_test.py
```

测试会依次验证：创建 → 端口映射 → 注入语法错误 → verify 失败 → diff 修复 →
verify 成功 → Vite 返回最新模块 → 销毁 → 资源 404。

> 安全提示：挂载 Docker Socket 等价于授予 python-ai 宿主 root 权限，仅限本地
> 开发与受信任环境。生产环境必须替换为独立沙盒节点 + gVisor，业务容器不得
> 直接挂载 Docker Socket。
>
> 若容器内无权限访问 Socket，执行
> `stat -c '%g' /var/run/docker.sock` 获取 GID，并写入 `.env` 的 `DOCKER_GID`。

## Agent 闭环（M1）

`POST /api/v1/internal/runs/execute` 接收画布 `AnnotationEnvelope`，创建沙盒并
同步执行 LangGraph：

```text
planner -> coder -> applier -> verifier -> evaluator
                        ^                       |
                        +---- retry (<= 3) <-----+
                                                +-- success -> awaiting_approval
                                                +-- exhausted -> failed
```

- `applier` 直接复用 SandboxProvider（与沙盒 `apply-patch` API 同一实现），
  避免内部 HTTP 自调用。
- `verifier` 执行 `npm run build`，失败日志写入状态并由 `coder` 重试；
  `retry_count` 硬上限为 3，超过后返回 `FAILED`。
- 模型选择：`llmProvider=auto` 时，有 `DEEPSEEK_API_KEY` 用 DeepSeek，
  否则用 Mock；也可显式指定 `mock` / `deepseek`。
- Mock 场景：`fail_first`（首次写错，验证自动修复）、`always_fail`
  （验证 3 次上限）、`none`（直接成功）。

最小请求：

```json
{
  "runId": "run_01",
  "projectId": "proj_01",
  "annotation": {
    "version": 1,
    "annotationId": "ann_01",
    "type": "note",
    "target": {
      "mode": "source",
      "source": { "file": "src/App.jsx" }
    },
    "intent": {
      "kind": "UI_ONLY",
      "ui": { "summary": "将按钮改为红色" }
    },
    "payload": { "text": "将按钮改为红色" }
  },
  "files": { "src/App.jsx": "export default function App() { ... }" },
  "options": {
    "llmProvider": "mock",
    "mockScenario": "fail_first",
    "maxRetries": 3,
    "keepSandbox": true
  }
}
```

E2E：

```bash
python3 scripts/agent_e2e_test.py
```

覆盖：首次语法错误 → 自动修复 → `AWAITING_APPROVAL`；持续失败 → 3 次上限
→ `FAILED`；`FULLSTACK` 意图 → `422 FULLSTACK_NOT_SUPPORTED`。

> M1 的 execute 是同步接口，便于后台联调；M2 将改为 202 + checkpoint + 事件流。

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
