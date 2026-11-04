# Python AI Core

FastAPI + Pydantic Settings + LangGraph 依赖，M1 提供：

- `GET /api/v1/health`
- `GET /api/v1/internal/ping`
- 沙盒生命周期 API（见下）

## 沙盒 API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/internal/sandbox/create` | 创建 React + Vite 容器 |
| POST | `/api/v1/internal/sandbox/apply-patch` | 全量文件或 unified diff |
| GET | `/api/v1/internal/sandbox/verify` | `check=build` / `check=test` |
| DELETE | `/api/v1/internal/sandbox/{sandbox_id}` | 销毁容器 |

实现结构：

```text
ai_core/
  config.py                 环境变量配置
  schemas.py                请求 / 响应模型
  api/sandbox.py            内部 HTTP API
  sandbox/base.py           SandboxProvider 抽象
  sandbox/local_docker.py   LocalDockerSandbox（Docker SDK）
sandbox_template/           React + Vite 基础镜像
```

沙盒镜像不存在时，`LocalDockerSandbox` 会以 `sandbox_template/` 为上下文
自动构建 `vibe-platform/sandbox-react:dev`。Dockerfile 已配置
`registry.npmmirror.com`，依赖层与源码层分离以利用 Docker 缓存。

## 本地运行

本地直跑需要访问 Docker Socket，并显式指定模板目录：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export SANDBOX_TEMPLATE_DIR="$(pwd)/sandbox_template"
uvicorn main:app --host 0.0.0.0 --port 8100 --reload
```

## 容器运行

由仓库根目录 `docker-compose.yml` 构建启动，容器内监听 `8100`，并挂载
`/var/run/docker.sock` 管理沙盒容器。

## 后续

LangGraph Graph、Supervisor、Planner、FrontendCoder、BackendCoder、DBA、
Reviewer 与远程 SandboxProvider 在 M1/M2 逐步实现。
