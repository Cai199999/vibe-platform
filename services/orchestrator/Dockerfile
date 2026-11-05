FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY ai_core ./ai_core
# Architect RAG 知识库：Markdown + BM25，运行时可换 Chroma 而不改 Graph。
COPY knowledge ./knowledge
# 沙盒构建上下文：镜像不存在时由 LocalDockerSandbox 自动构建。
COPY sandbox_template ./sandbox_template

# 以非 root 运行；通过 compose 的 group_add 加入宿主 docker 组以访问 socket。
USER app

EXPOSE 8100

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100", "--proxy-headers"]
