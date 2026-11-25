#!/usr/bin/env python3
"""平台侧故障矩阵：Redis 不可用回退 + LLM 依赖不可达。

用一次性 python-ai 容器做隔离故障，不停止/重启共享 Redis、MySQL、MinIO。

场景：
  1. redis-down：REDIS_HOST 指向容器内关闭端口，RUN_STORE_BACKEND=redis；
     期望服务仍能启动、/api/v1/health 返回 200，并回退 memory / degraded。
  2. llm-unreachable：DEEPSEEK_BASE_URL 指向容器内关闭端口，
     ARCHITECT_LLM_RETRY_LIMIT=0；启动 Architect，期望在有限时间内收到
     architectural failed / ARCHITECT_LLM_ERROR，而不是无限挂起。

运行：
    python3 scripts/platform_fault_matrix.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "vibe-platform/python-ai:dev"
NETWORK = "vibe-platform-net"

HEALTH_LOOP = r"""
for i in $(seq 1 25); do
  code=$(curl -s -o /tmp/health.json -w "%{http_code}" http://127.0.0.1:8101/api/v1/health || true)
  [ "$code" = "200" ] && break
  sleep 1
done
"""

REDIS_SCRIPT = (
    "uvicorn main:app --host 127.0.0.1 --port 8101 >/tmp/ai.log 2>&1 &\n"
    "pid=$!\n"
    + HEALTH_LOOP
    + 'echo "HTTP $code"\n'
    "cat /tmp/health.json 2>/dev/null || true\n"
    "kill $pid 2>/dev/null || true\n"
)

LLM_SCRIPT = (
    "uvicorn main:app --host 127.0.0.1 --port 8101 >/tmp/ai.log 2>&1 &\n"
    "pid=$!\n"
    + HEALTH_LOOP
    + 'echo "HEALTH $code"\n'
    "cat > /tmp/payload.json <<'JSON'\n"
    '{"runId":"chaos_llm_1","projectId":"chaos_llm_proj",'
    '"projectName":"chaos","source":{"files":{}},'
    '"uiHistory":[{"role":"user","content":"做一个待办任务应用"}],'
    '"options":{"llmProvider":"auto","ragProvider":"markdown"}}\n'
    "JSON\n"
    "curl -N -s --max-time 20 -X POST "
    "http://127.0.0.1:8101/api/v1/internal/architect/runs/stream "
    "-H 'Content-Type: application/json' "
    "--data-binary @/tmp/payload.json > /tmp/sse.txt || true\n"
    "grep -Eo 'architect\\.[a-z.]+|ARCHITECT_LLM_ERROR|ARCHITECT_LLM_NOT_CONFIGURED' "
    "/tmp/sse.txt | tail -8 || true\n"
    "kill $pid 2>/dev/null || true\n"
)


def run_container(
    script: str,
    env: dict[str, str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess:
    args = [
        "docker",
        "run",
        "--rm",
        "--network",
        NETWORK,
        "--entrypoint",
        "sh",
    ]
    for key, value in env.items():
        args.extend(["-e", f"{key}={value}"])
    args.extend([IMAGE, "-c", script])
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out", default=str(ROOT / "reports" / "production")
    )
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "scenarios": {},
    }

    print("== 1. redis-down ==")
    started = time.perf_counter()
    redis = run_container(
        REDIS_SCRIPT,
        {
            "REDIS_HOST": "127.0.0.1",
            "REDIS_PORT": "6399",
            "RUN_STORE_BACKEND": "redis",
            "DISTRIBUTED_LOCK_ENABLED": "true",
            "MAX_CONCURRENT_RUNS": "1",
        },
        timeout=60,
    )
    redis_output = (redis.stdout + redis.stderr)[-2000:]
    redis_ok = "HTTP 200" in redis.stdout
    report["scenarios"]["redis-down"] = {
        "ok": redis_ok,
        "durationMs": round((time.perf_counter() - started) * 1000, 2),
        "exitCode": redis.returncode,
        "output": redis_output,
    }
    print(f"redis-down ok={redis_ok}")
    print(redis_output[-600:])

    print("== 2. llm-unreachable ==")
    started = time.perf_counter()
    llm = run_container(
        LLM_SCRIPT,
        {
            "DEEPSEEK_API_KEY": "chaos-dummy",
            "DEEPSEEK_BASE_URL": "http://127.0.0.1:9",
            "ARCHITECT_LLM_RETRY_LIMIT": "0",
            "RAG_PROVIDER": "markdown",
        },
        timeout=60,
    )
    llm_output = (llm.stdout + llm.stderr)[-3000:]
    llm_failed_envelope = (
        "ARCHITECT_LLM_ERROR" in llm_output
        or "architect.failed" in llm_output
    )
    report["scenarios"]["llm-unreachable"] = {
        "ok": llm_failed_envelope,
        "durationMs": round((time.perf_counter() - started) * 1000, 2),
        "exitCode": llm.returncode,
        "output": llm_output,
    }
    print(f"llm-unreachable ok={llm_failed_envelope}")
    print(llm_output[-1000:])

    report["ok"] = all(
        item["ok"] for item in report["scenarios"].values()
    )
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"platform-faults-{stamp}.json"
    md_path = out_dir / f"platform-faults-{stamp}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 平台侧故障矩阵",
        "",
        f"- 结果：**{'PASS' if report['ok'] else 'FAIL'}**",
        "",
        "| 场景 | 结果 | 耗时 | 证据 |",
        "|---|---|---:|---|",
    ]
    for name, item in report["scenarios"].items():
        lines.append(
            f"| {name} | {'✅' if item['ok'] else '❌'} | "
            f"{item['durationMs']}ms | `{item['output'][-120:].replace(chr(10), ' ')}` |"
        )
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"json={json_path}")
    print(f"md={md_path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
