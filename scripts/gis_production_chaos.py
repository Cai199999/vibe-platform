#!/usr/bin/env python3
"""GIS 生成项目的生产基线 + 真实故障注入 + 恢复指标。

场景：
  1. 通过真实 API 写入一批数据，测基线 P50/P95/错误率；
  2. 在沙盒内杀掉 Express 后端进程，测故障期错误率与不可用时间；
  3. 手动拉起后端，测恢复时间与恢复后指标；
  4. 输出可直接用于复盘的报告与 postmortem 模板。

这是“真实故障”而不是 mock：故障发生在真实沙盒容器里的 Node 进程。

用法：
    python3 scripts/gis_production_chaos.py \
      --preview-url http://127.0.0.1:54973 \
      --sandbox-id sbx_79e8d55c82ca4f66bfdf \
      --seed-count 200
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(pct * (len(ordered) - 1)))))
    return ordered[index]


def request(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 10,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        status = exc.code
    except (urllib.error.URLError, TimeoutError) as exc:
        body = str(exc)
        status = 0
    return {
        "status": status,
        "latencyMs": round((time.perf_counter() - started) * 1000, 2),
        "body": body[:500],
    }


def summarize(
    samples: list[dict[str, Any]],
    *,
    success_statuses: set[int] | tuple[int, ...] = (200,),
) -> dict[str, Any]:
    latencies = [item["latencyMs"] for item in samples]
    errors = [
        item for item in samples if item["status"] not in success_statuses
    ]
    return {
        "requests": len(samples),
        "errors": len(errors),
        "errorRate": round(len(errors) / len(samples), 4) if samples else 0.0,
        "p50Ms": round(percentile(latencies, 0.5), 2),
        "p95Ms": round(percentile(latencies, 0.95), 2),
        "avgMs": round(statistics.mean(latencies), 2) if latencies else 0.0,
        "statuses": sorted({item["status"] for item in samples}),
    }


def load_test(url: str, count: int, concurrency: int) -> list[dict[str, Any]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(request, "GET", url) for _ in range(count)]
        return [future.result() for future in futures]


def docker_exec(container: str, command: list[str], *, detach: bool = False) -> subprocess.CompletedProcess:
    args = ["docker", "exec"]
    if detach:
        args.append("-d")
    args.append(container)
    args.extend(command)
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


def wait_health(base: str, timeout: float = 60) -> tuple[bool, float]:
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        result = request("GET", f"{base}/api/health", timeout=3)
        if result["status"] == 200:
            return True, round((time.perf_counter() - started) * 1000, 2)
        time.sleep(0.5)
    return False, round((time.perf_counter() - started) * 1000, 2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview-url", default="http://127.0.0.1:54973")
    parser.add_argument("--sandbox-id", default="sbx_79e8d55c82ca4f66bfdf")
    parser.add_argument("--seed-count", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument(
        "--out", default=str(ROOT / "reports" / "production")
    )
    args = parser.parse_args()

    base = args.preview_url.rstrip("/")
    container = f"vibe-sandbox-{args.sandbox_id}"
    collection = f"{base}/api/v1/facilities"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "previewUrl": base,
        "sandboxId": args.sandbox_id,
        "container": container,
        "seedCount": args.seed_count,
        "concurrency": args.concurrency,
    }

    print(f"== 1. health ==\n{request('GET', f'{base}/api/health')}")
    healthy, health_ms = wait_health(base, timeout=10)
    if not healthy:
        print("FAIL: health not ready")
        return 1
    report["healthMs"] = health_ms

    print(f"== 2. seed {args.seed_count} facilities ==")
    seed_latencies: list[float] = []
    seed_errors = 0
    for index in range(args.seed_count):
        result = request(
            "POST",
            collection,
            {
                "name": f"chaos-seed-{index}",
                "type": "station",
                "status": "active",
                "lng": 116.4 + (index % 100) * 0.0001,
                "lat": 39.9 + (index % 100) * 0.0001,
            },
        )
        seed_latencies.append(result["latencyMs"])
        if result["status"] not in {200, 201}:
            seed_errors += 1
    report["seed"] = {
        "errors": seed_errors,
        "p50Ms": round(percentile(seed_latencies, 0.5), 2),
        "p95Ms": round(percentile(seed_latencies, 0.95), 2),
    }
    print(f"seed done errors={seed_errors} p95={report['seed']['p95Ms']}ms")

    print("== 3. baseline load ==")
    baseline = summarize(load_test(collection, 100, args.concurrency))
    report["baseline"] = baseline
    print(baseline)

    print("== 4. inject backend crash ==")
    kill = docker_exec(container, ["pkill", "-f", "server/index.js"])
    report["faultCommand"] = {
        "exitCode": kill.returncode,
        "stdout": kill.stdout[-200:],
        "stderr": kill.stderr[-200:],
    }
    # 立即打一轮，捕获故障窗口；随后轮询健康，测自动恢复时间。
    started = time.perf_counter()
    fault = summarize(load_test(collection, 20, args.concurrency))
    report["fault"] = fault
    print(fault)
    outage_statuses: list[int] = []
    auto_recovered = False
    while time.perf_counter() - started < 10:
        result = request("GET", f"{base}/api/health", timeout=2)
        outage_statuses.append(result["status"])
        if result["status"] == 200:
            auto_recovered = True
            break
        time.sleep(0.2)
    report["outage"] = {
        "autoRecovered": auto_recovered,
        "outageMs": round((time.perf_counter() - started) * 1000, 2),
        "statuses": outage_statuses[:20],
    }
    print(report["outage"])

    if not auto_recovered:
        print("== 5. manual restart backend ==")
        restart = docker_exec(
            container,
            [
                "sh",
                "-lc",
                "cd /app && nohup node server/index.js >/tmp/api-recovery.log 2>&1 &",
            ],
            detach=True,
        )
        report["restartCommand"] = {
            "exitCode": restart.returncode,
            "stderr": restart.stderr[-200:],
        }
        recovered, recovery_ms = wait_health(base, timeout=60)
        report["recovery"] = {
            "ok": recovered,
            "recoveryMs": recovery_ms,
            "mode": "manual",
        }
    else:
        report["recovery"] = {
            "ok": True,
            "recoveryMs": report["outage"]["outageMs"],
            "mode": "auto",
        }
    print(report["recovery"])
    if report["recovery"]["ok"]:
        report["afterRecovery"] = summarize(
            load_test(collection, 50, args.concurrency)
        )
        print(report["afterRecovery"])

    recovery_mode = report.get("recovery", {}).get("mode", "unknown")
    report["postmortem"] = {
        "phenomenon": "Express 后端进程被杀死后，/api 返回 500/连接失败，前端无法读取设施列表。",
        "impact": (
            f"故障期请求错误率 {report['fault']['errorRate'] * 100:.1f}%；"
            f"故障窗口 {report.get('outage', {}).get('outageMs', -1)}ms；"
            f"恢复模式 {recovery_mode}。"
        ),
        "rootCause": [
            "旧版 dev.mjs 只拉起 API，不在 API 退出后重启；",
            "生成物 docker-compose 没有 restart policy；",
            "前端 DataList 只有一次性 fetch，没有退避重试与手动重试。",
        ],
        "fix": [
            "dev.mjs 对 API 退出做 1s 自动重启，避免预览永久失联；",
            "docker-compose 增加 restart: unless-stopped；",
            "DataList 增加 1s/2s 两次退避重试与手动重试按钮。",
        ],
        "prevention": [
            "把本次 chaos 脚本接入里程碑 CI；",
            "监控错误率、P95、恢复时间，设置阈值告警；",
            "定期做进程崩溃与依赖超时演练。",
        ],
    }

    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"chaos-{stamp}.json"
    md_path = out_dir / f"chaos-{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "baseline.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def row(name: str, item: dict[str, Any]) -> str:
        return (
            f"| {name} | {item['requests']} | {item['errorRate'] * 100:.1f}% | "
            f"{item['p50Ms']} | {item['p95Ms']} |"
        )

    lines = [
        "# GIS 生产基线 + 故障注入报告",
        "",
        f"- 预览：`{base}`",
        f"- 沙盒：`{args.sandbox_id}`",
        f"- 种子数据：{args.seed_count} 条",
        f"- 故障：杀掉 Express 后端进程",
        "",
        "| 阶段 | 请求 | 错误率 | P50(ms) | P95(ms) |",
        "|---|---:|---:|---:|---:|",
        row("baseline", report["baseline"]),
        row("fault", report["fault"]),
    ]
    if "afterRecovery" in report:
        lines.append(row("after-recovery", report["afterRecovery"]))
    lines.extend(
        [
            "",
            f"- 故障窗口：{report['outage']['outageMs']}ms；"
            f"自动恢复：{'是' if report['outage']['autoRecovered'] else '否'}",
            f"- 恢复：{'成功' if report['recovery']['ok'] else '失败'}"
            f"（{report['recovery'].get('mode', 'unknown')}），"
            f"{report['recovery']['recoveryMs']}ms",
            "",
            "## Postmortem 模板",
            "",
            f"**现象**：{report['postmortem']['phenomenon']}",
            "",
            f"**影响**：{report['postmortem']['impact']}",
            "",
            "**根因**：",
            *[f"- {item}" for item in report["postmortem"]["rootCause"]],
            "",
            "**修复**：",
            *[f"- {item}" for item in report["postmortem"]["fix"]],
            "",
            "**防复发**：",
            *[f"- {item}" for item in report["postmortem"]["prevention"]],
            "",
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"json={json_path}")
    print(f"md={md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
