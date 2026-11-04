# Python AI Core（骨架）

FastAPI + Pydantic Settings + LangGraph 依赖。M1 只实现：

- `GET /api/v1/health`
- `GET /api/v1/internal/ping`

## 本地运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8100 --reload
```

## 容器运行

由仓库根目录 `docker-compose.yml` 构建启动，容器内监听 `8100`。

## 后续

LangGraph Graph、Supervisor、Planner、FrontendCoder、BackendCoder、DBA、
Reviewer 与 Sandbox 调度在 M1/M2 逐步实现。
