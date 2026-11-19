#!/usr/bin/env bash
# 本地 CI：Python / 前端 / BFF 全量校验。
#
# 只创建临时容器与临时构建目录，不删除、不重启任何共享中间件
# （MySQL / Redis / MinIO）或其他项目的容器。
#
# 用法：
#   ./scripts/ci_local.sh
#   RUN_E2E=1 ./scripts/ci_local.sh   # 额外跑浏览器 E2E（需服务已启动）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_IMAGE="${PY_IMAGE:-vibe-platform/python-ai:dev}"
BFF_BUILD_DIR="${BFF_BUILD_DIR:-/tmp/vibe-bff-ci}"

step() {
  printf '\n== %s ==\n' "$1"
}

step "Python: 全量 unittest"
docker run --rm \
  --entrypoint python \
  -v "${ROOT}:/repo" \
  -w /repo/services/orchestrator \
  "${PY_IMAGE}" \
  -m unittest discover -s tests -q

step "Frontend: Vitest + build"
(
  cd "${ROOT}/apps/platform-web"
  npm test
  npm run build
)

step "BFF: Maven test（临时目录，避免污染工作区 target）"
rm -rf "${BFF_BUILD_DIR}"
mkdir -p "${BFF_BUILD_DIR}"
cp -a "${ROOT}/apps/platform-bff/pom.xml" "${ROOT}/apps/platform-bff/src" "${BFF_BUILD_DIR}/"
(
  cd "${BFF_BUILD_DIR}"
  mvn -q test
)

if [[ "${RUN_E2E:-0}" == "1" ]]; then
  step "E2E: source refresh + call-site props + visual chain"
  if [[ -z "${NODE_PATH:-}" ]]; then
    echo "RUN_E2E=1 需要 NODE_PATH 指向包含 playwright-core 的目录" >&2
    exit 2
  fi
  node "${ROOT}/scripts/project_source_refresh_e2e.mjs"
  node "${ROOT}/scripts/call_site_props_e2e.mjs"
  node "${ROOT}/scripts/architect_visual_chain_e2e.mjs"
fi

step "CI PASS"
