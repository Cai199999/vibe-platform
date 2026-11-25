#!/usr/bin/env bash
# 本地 CI：Python / 前端 / BFF 全量校验。
#
# 只创建临时容器与临时构建目录，不删除、不重启任何共享中间件
# （MySQL / Redis / MinIO）或其他项目的容器。
#
# 用法：
#   ./scripts/ci_local.sh
#   RUN_E2E=1 ./scripts/ci_local.sh          # 旧浏览器 E2E（需服务已启动）
#   RUN_CANVAS_E2E=1 ./scripts/ci_local.sh   # 画布评测套件（需服务 + Playwright）
#   CANVAS_PROVIDER=auto RUN_CANVAS_E2E=1 ./scripts/ci_local.sh  # 真实 GIS 画布
#   RUN_CHAOS_E2E=1 CHAOS_PREVIEW_URL=... CHAOS_SANDBOX_ID=... ./scripts/ci_local.sh
#                                            # 生产故障注入（会杀掉沙盒后端，慎用）
#                                            # 含浏览器 DataList 恢复与 DB 锁竞争
#   RUN_PLATFORM_FAULTS=1 ./scripts/ci_local.sh
#                                            # 平台侧故障矩阵（Redis 宕 / LLM 不可达，
#                                            # 一次性容器隔离，不动共享中间件）
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

if [[ "${RUN_CANVAS_E2E:-0}" == "1" ]]; then
  step "Canvas E2E: foundation + call-site + repeat + add"
  "${ROOT}/scripts/canvas_eval_suite.sh"
fi

if [[ "${RUN_CHAOS_E2E:-0}" == "1" ]]; then
  step "Production chaos: backend crash + recovery"
  if [[ -z "${CHAOS_PREVIEW_URL:-}" || -z "${CHAOS_SANDBOX_ID:-}" ]]; then
    echo "RUN_CHAOS_E2E=1 需要 CHAOS_PREVIEW_URL 与 CHAOS_SANDBOX_ID" >&2
    exit 2
  fi
  python3 "${ROOT}/scripts/gis_production_chaos.py" \
    --preview-url "${CHAOS_PREVIEW_URL}" \
    --sandbox-id "${CHAOS_SANDBOX_ID}"
  if [[ -n "${NODE_PATH:-}" ]]; then
    step "Browser resilience: backend crash -> DataList auto recovery"
    PREVIEW_URL="${CHAOS_PREVIEW_URL}" \
      SANDBOX_ID="${CHAOS_SANDBOX_ID}" \
      node "${ROOT}/scripts/gis_resilience_browser_e2e.mjs"
  else
    echo "SKIP browser resilience: NODE_PATH 未设置" >&2
  fi
  step "DB lock: write contention"
  python3 "${ROOT}/scripts/gis_db_lock_chaos.py" \
    --preview-url "${CHAOS_PREVIEW_URL}" \
    --sandbox-id "${CHAOS_SANDBOX_ID}"
fi

if [[ "${RUN_PLATFORM_FAULTS:-0}" == "1" ]]; then
  step "Platform faults: redis-down + llm-unreachable"
  python3 "${ROOT}/scripts/platform_fault_matrix.py"
fi

step "CI PASS"
