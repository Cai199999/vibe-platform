#!/usr/bin/env bash
# 画布评测套件：基础 Direct Ops + 跨文件调用点 + 循环/网格/尺寸 + 新增组件。
#
# 默认用 mock provider（确定性、快），适合里程碑 CI；
# 真实 GIS 画布用 CANVAS_PROVIDER=auto 单独跑。
#
# 前置：
#   - web :5173 / BFF :8080 / python-ai :8100 已启动；
#   - playwright-core 与 Chromium 可用（NODE_PATH / CHROME_BIN）。
#
# 运行：
#   ./scripts/canvas_eval_suite.sh
#   CANVAS_PROVIDER=auto ./scripts/canvas_eval_suite.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export NODE_PATH="${NODE_PATH:-/tmp/vibe-e2e/node_modules}"
export CHROME_BIN="${CHROME_BIN:-$HOME/.cache/ms-playwright/chromium-1255/chrome-linux64/chrome}"

provider="${CANVAS_PROVIDER:-mock}"
if [[ -z "${CANVAS_MIN_OIDS:-}" ]]; then
  if [[ "$provider" == "auto" ]]; then
    min_oids=20
  else
    min_oids=10
  fi
else
  min_oids="$CANVAS_MIN_OIDS"
fi

status=0
run_step() {
  local name="$1"
  shift
  printf '\n== %s ==\n' "$name"
  if "$@"; then
    printf 'PASS: %s\n' "$name"
  else
    printf 'FAIL: %s\n' "$name"
    status=1
  fi
}

run_step "canvas-foundation($provider)" \
  env CANVAS_PROVIDER="$provider" CANVAS_MIN_OIDS="$min_oids" \
  node "$ROOT/scripts/canvas_foundation_e2e.mjs"

run_step "cross-file-call-site" \
  node "$ROOT/scripts/call_site_props_e2e.mjs"

run_step "repeat-grid-size-free" \
  env MOVE_E2E_GRAB_RATIO="${MOVE_E2E_GRAB_RATIO:-0.25}" \
  node "$ROOT/scripts/direct_move_e2e.mjs"

if python3 -c "import playwright" >/dev/null 2>&1; then
  run_step "add-component" python3 "$ROOT/scripts/add_component_e2e_test.py"
else
  printf '\nSKIP: add-component（未安装 Python Playwright）\n'
fi

printf '\n== canvas suite result ==\n'
if [[ "$status" -eq 0 ]]; then
  printf 'CANVAS E2E SUITE PASS\n'
else
  printf 'CANVAS E2E SUITE FAIL\n'
fi
exit "$status"
