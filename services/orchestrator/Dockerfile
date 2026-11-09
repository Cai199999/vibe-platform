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
# 企业 CustomAgent Manifest：只读 JSON，不执行代码。
COPY config ./config
# 沙盒构建上下文：镜像不存在时由 LocalDockerSandbox 自动构建。
COPY sandbox_template ./sandbox_template

# 上下文记忆目录：镜像内先建好并归属 app，named volume / bind mount
# 首次初始化时才能保留可写权限。
RUN mkdir -p /app/data/project_memory \
    && chown -R app:app /app/data

# 入口脚本以 root 修正 bind mount 属主后立即降权到 app；
# 通过 compose 的 group_add 加入宿主 docker 组以访问 socket。
COPY entrypoint.sh /usr/local/bin/vibe-entrypoint
RUN chmod +x /usr/local/bin/vibe-entrypoint

EXPOSE 8100

ENTRYPOINT ["/usr/local/bin/vibe-entrypoint"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100", "--proxy-headers"]
