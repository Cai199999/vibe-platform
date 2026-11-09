#!/bin/sh
# ============================================================
# python-ai 容器启动入口
#
# compose 把宿主目录 bind mount 到 /app/data。宿主机上的目录可能
# 由 Docker 以 root 创建（755），而应用以 app(999) 运行，导致
# project_memory 无法创建，global_summary.md 静默写入失败。
#
# 启动时以 root 修正挂载目录属主，然后立即降权到 app 运行；
# setpriv 保留 group_add 注入的 docker 组（访问 /var/run/docker.sock）。
# ============================================================
set -eu

if [ "$(id -u)" = "0" ]; then
  mkdir -p /app/data/project_memory
  chown -R app:app /app/data

  # 保留非 root 附加组（尤其是 docker 组），同时避免 app 继续拥有 root 组。
  GROUPS="$(id -G | tr ' ' '\n' | grep -v '^0$' | paste -sd, - || true)"
  if [ -n "${GROUPS}" ]; then
    exec setpriv --reuid=app --regid=app --groups "${GROUPS}" -- "$@"
  fi
  exec setpriv --reuid=app --regid=app --clear-groups -- "$@"
fi

exec "$@"
