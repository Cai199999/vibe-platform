FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# nodejs 供静态门禁使用：ConsistencyVerifier / 生成期断言通过
# @babel/parser 做语法树级字段校验，禁止回退到正则。
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app app

# 允许构建时切换 PyPI 镜像（默认官方源）；本地/国内网络可通过
# `PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker compose build` 加速。
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Run 事件 / 快照 / 契约持久化与多副本项目锁（RUN_STORE_BACKEND=redis）。
# 单独成层：requirements.txt 变化时仍可复用上面的重型依赖缓存。
RUN pip install --no-cache-dir redis==5.2.1

# 混合 RAG：构建阶段预热本地 ONNX Embedding，运行期无需联网下载模型。
# 模型体积约 80MB，缓存在 /app/.cache/chroma，运行时 app 用户可读。
# 离线 / 镜像源不可达时可传 CHROMA_WARMUP_ENABLED=false：构建继续，
# 运行期首次向量检索会尝试下载；Hybrid RAG 在向量路径失败时回退 BM25。
ENV HOME=/app
ARG CHROMA_WARMUP_ENABLED=true
RUN mkdir -p /app/.cache/chroma \
    && if [ "$CHROMA_WARMUP_ENABLED" = "true" ]; then \
         python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction; DefaultEmbeddingFunction()(['warmup'])" ; \
       else \
         echo "chroma warmup skipped (CHROMA_WARMUP_ENABLED=false)" ; \
       fi \
    && chown -R app:app /app/.cache

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
