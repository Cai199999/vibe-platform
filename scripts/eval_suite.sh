#!/usr/bin/env bash
# ============================================================
# Eval Suite CLI 包装器
#
# 复用已有 python-ai 镜像（不重复构建、不启动 MySQL/Redis/MinIO），
# 以一次性容器执行离线跑批：
#   ./scripts/eval_suite.sh run
#   ./scripts/eval_suite.sh run --filter architect --fail-fast
#   ./scripts/eval_suite.sh list
#   ./scripts/eval_suite.sh validate
#
# 自定义评测集：放到 config/eval_suite/ 下，然后
#   ./scripts/eval_suite.sh run --suite /config/eval_suite/your_suite.yaml
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${EVAL_IMAGE:-vibe-platform/python-ai:dev}"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: 需要 Docker 来复用 python-ai 运行环境" >&2
  exit 2
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "ERROR: 镜像不存在: $IMAGE" >&2
  echo "       先执行: docker compose build python-ai" >&2
  exit 2
fi

mkdir -p "$ROOT/reports/eval"

ARGS=("$@")
if [ ${#ARGS[@]} -eq 0 ]; then
  ARGS=(run)
fi

exec docker run --rm \
  --user "$(id -u):$(id -g)" \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONUNBUFFERED=1 \
  -e EVAL_SUITE_PATH=/config/eval_suite/core_cases.yaml \
  -e EVAL_REPORT_DIR=/reports/eval \
  -v "$ROOT/services/orchestrator:/app:ro" \
  -v "$ROOT/config:/config:ro" \
  -v "$ROOT/reports:/reports" \
  -w /app \
  --entrypoint python \
  "$IMAGE" -m eval_suite "${ARGS[@]}"
