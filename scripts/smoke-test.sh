#!/usr/bin/env bash
# 验证 Python AI Core、Java BFF，以及 Java -> Python 的跨语言调用。
set -euo pipefail

BFF_BASE_URL="${BFF_BASE_URL:-http://127.0.0.1:8080}"
AI_BASE_URL="${AI_BASE_URL:-http://127.0.0.1:8100}"

echo "[1/3] Python AI Core health: ${AI_BASE_URL}/api/v1/health"
AI_HEALTH="$(curl -fsS "${AI_BASE_URL}/api/v1/health")"
printf '%s\n' "${AI_HEALTH}"

echo "[2/3] Python AI Core internal ping: ${AI_BASE_URL}/api/v1/internal/ping"
AI_PING="$(curl -fsS "${AI_BASE_URL}/api/v1/internal/ping")"
printf '%s\n' "${AI_PING}"

echo "[3/3] Java BFF health (calls python-ai/internal/ping): ${BFF_BASE_URL}/api/v1/health"
BFF_HEALTH="$(curl -fsS "${BFF_BASE_URL}/api/v1/health")"
printf '%s\n' "${BFF_HEALTH}"

AI_PING="${AI_PING}" BFF_HEALTH="${BFF_HEALTH}" python3 - <<'PY'
import json
import os

ai_ping = json.loads(os.environ["AI_PING"])
bff_health = json.loads(os.environ["BFF_HEALTH"])

assert ai_ping["status"] == "UP", ai_ping
assert bff_health["status"] == "UP", bff_health
assert bff_health["dependencies"]["pythonAi"]["status"] == "UP", bff_health
assert ai_ping["service"] == bff_health["dependencies"]["pythonAi"]["details"]["service"], bff_health
print("\nOK: Java BFF -> Python AI Core cross-language call succeeded.")
PY
