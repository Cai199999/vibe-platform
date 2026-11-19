#!/usr/bin/env bash
# 开发会话上下文风险审计：只定位“容易被整份带进上下文”的文件与日志，
# 不做业务排查。
#
# 用法：
#   ./scripts/context_audit.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== 大文件（>300 行，优先用 rg + offset/limit 读取）=="
find "$ROOT/services" "$ROOT/apps" "$ROOT/scripts" \
  -type f \( -name '*.js' -o -name '*.jsx' -o -name '*.py' -o -name '*.java' \) \
  -not -path '*/node_modules/*' -not -path '*/dist/*' -print0 \
  | xargs -0 wc -l 2>/dev/null \
  | sort -nr \
  | awk '$1 > 300 { print }' \
  | head -25

echo
echo "== E2E 潜在大输出（完整 JSON / DOM / response 直接打印）=="
grep -rnE \
  "JSON\\.stringify\\(|innerText\\(\\)|outerHTML|DOM tree|log\\('.*(response|payload)'" \
  "$ROOT/scripts"/*.mjs 2>/dev/null \
  | head -40 || true

echo
echo "== 建议 =="
echo "- 默认只打印 httpStatus/action/result/errorCode/parentOid/siblingIndex"
echo "- 完整响应体 / DOM tree 统一用 LIVE_DEBUG=1 或 LIVE_DIAGNOSE=1 开启"
echo "- 读大文件先 rg/grep -n 定位，再 Read offset/limit"
