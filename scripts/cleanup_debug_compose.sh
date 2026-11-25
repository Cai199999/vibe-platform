#!/usr/bin/env bash
# 只清理本轮调试产生的 compose 项目，不触碰平台服务与共享中间件。
set -u

PROJECTS=(debugotel debug2 debug3 debug4 debug5)

for project in "${PROJECTS[@]}"; do
  containers=$(docker ps -aq --filter "label=com.docker.compose.project=${project}" || true)
  if [ -n "${containers}" ]; then
    # shellcheck disable=SC2086
    docker rm -f ${containers} >/dev/null 2>&1 || true
  fi

  volumes=$(docker volume ls -q --filter "label=com.docker.compose.project=${project}" || true)
  if [ -n "${volumes}" ]; then
    # shellcheck disable=SC2086
    docker volume rm ${volumes} >/dev/null 2>&1 || true
  fi

  networks=$(docker network ls -q --filter "label=com.docker.compose.project=${project}" || true)
  if [ -n "${networks}" ]; then
    # shellcheck disable=SC2086
    docker network rm ${networks} >/dev/null 2>&1 || true
  fi

  echo "cleaned ${project}"
done

echo "remaining:"
docker ps -a --format '{{.Names}}' | grep -E '^(debugotel|debug2|debug3)' || echo "none"
