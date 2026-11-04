#!/usr/bin/env bash
# 把 M1 初始 schema 应用到已运行的 Docker MySQL。
# 默认复用 ~/Workspace/infra 的 geo-mysql 容器。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MYSQL_CONTAINER="${MYSQL_CONTAINER:-geo-mysql}"
MYSQL_USER="${MYSQL_USER:-root}"
MYSQL_PASSWORD="${MYSQL_PASSWORD:-root}"

echo "[init-db] applying ${ROOT_DIR}/infra/migrations/schema.sql -> ${MYSQL_CONTAINER}"
docker exec -i "${MYSQL_CONTAINER}" \
  mysql -u"${MYSQL_USER}" -p"${MYSQL_PASSWORD}" < "${ROOT_DIR}/infra/migrations/schema.sql"

echo "[init-db] tables:"
docker exec -i "${MYSQL_CONTAINER}" \
  mysql -u"${MYSQL_USER}" -p"${MYSQL_PASSWORD}" -N -e \
  "USE geo_ai_db; SHOW TABLES;"
