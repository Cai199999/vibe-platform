#!/usr/bin/env python3
"""DB 独占锁 / 写竞争故障矩阵。

场景：
  1. 基线写 5 条、读 20 次；
  2. 在沙盒 SQLite 上持有一个 EXCLUSIVE 锁 6s；
  3. 锁期间并发 6 个 POST + 10 个 GET，测错误率与 P95；
  4. 锁释放后复测写/读，确认恢复。

说明：生成物沙盒使用 SQLite 回退（busy_timeout=5000）；生产 MySQL 不会
出现单写者锁，但仍需要应用层重试/幂等。本脚本用于验证沙盒回退路径。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from gis_production_chaos import ROOT, load_test, request, summarize

LOCK_CODE = r"""
const { DatabaseSync } = require('node:sqlite');
const db = new DatabaseSync('/app/data/fullstack.sqlite');
db.exec('BEGIN EXCLUSIVE');
console.log('locked');
setTimeout(() => {
  db.exec('COMMIT');
  console.log('released');
}, 6000);
"""


def write_batch(
    collection: str, prefix: str, count: int, concurrency: int
) -> list[dict[str, Any]]:
    def one(index: int) -> dict[str, Any]:
        return request(
            "POST",
            collection,
            {
                "name": f"{prefix}-{index}",
                "type": "station",
                "status": "active",
                "lng": 116.4 + index * 0.0001,
                "lat": 39.9 + index * 0.0001,
            },
            timeout=12,
        )

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=concurrency
    ) as pool:
        futures = [pool.submit(one, index) for index in range(count)]
        return [future.result() for future in futures]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview-url", default="http://127.0.0.1:39165")
    parser.add_argument("--sandbox-id", default="sbx_11fe3f3061e941cb9020")
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
    }

    print("== 1. baseline ==")
    report["baselineWrites"] = summarize(
        write_batch(collection, "db-baseline", 5, 2),
        success_statuses={200, 201},
    )
    report["baselineReads"] = summarize(load_test(collection, 20, 5))
    print(report["baselineWrites"], report["baselineReads"])

    print("== 2. acquire SQLite EXCLUSIVE lock (6s) ==")
    lock = subprocess.Popen(
        ["docker", "exec", container, "node", "-e", LOCK_CODE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.5)
    started = time.perf_counter()

    def locked_write(index: int) -> dict[str, Any]:
        return request(
            "POST",
            collection,
            {
                "name": f"db-locked-{index}",
                "type": "station",
                "status": "active",
                "lng": 116.5,
                "lat": 39.95,
            },
            timeout=12,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        write_futures = [pool.submit(locked_write, index) for index in range(6)]
        read_futures = [pool.submit(request, "GET", collection) for _ in range(10)]
        locked_writes = [future.result() for future in write_futures]
        locked_reads = [future.result() for future in read_futures]
    report["lockWindowMs"] = round((time.perf_counter() - started) * 1000, 2)
    report["lockedWrites"] = summarize(
        locked_writes, success_statuses={200, 201}
    )
    report["lockedReads"] = summarize(locked_reads)
    print(report["lockWindowMs"], report["lockedWrites"], report["lockedReads"])

    try:
        stdout, stderr = lock.communicate(timeout=15)
        report["lockCommand"] = {
            "exitCode": lock.returncode,
            "stdout": stdout[-200:],
            "stderr": stderr[-200:],
        }
    except subprocess.TimeoutExpired:
        lock.kill()
        report["lockCommand"] = {"exitCode": -1, "stderr": "lock timeout"}
    print(report["lockCommand"])

    time.sleep(0.5)
    print("== 3. after recovery ==")
    report["afterWrites"] = summarize(
        write_batch(collection, "db-after", 5, 2),
        success_statuses={200, 201},
    )
    report["afterReads"] = summarize(load_test(collection, 20, 5))
    print(report["afterWrites"], report["afterReads"])

    report["postmortem"] = {
        "phenomenon": "SQLite 被 EXCLUSIVE 锁住时，写请求在 busy_timeout 后返回 500；读请求视 journal 模式可能仍可用。",
        "impact": (
            f"锁窗口 {report['lockWindowMs']}ms；写错误率 "
            f"{report['lockedWrites']['errorRate'] * 100:.1f}%；"
            f"写 P95 {report['lockedWrites']['p95Ms']}ms。"
        ),
        "rootCause": [
            "沙盒回退 SQLite 是单写者模型；",
            "db.js 设了 busy_timeout=5000，但应用层没有 SQLITE_BUSY 重试/排队；",
            "生产 MySQL 不会出现单写者锁，但写竞争仍需幂等与重试。",
        ],
        "fix": [
            "db.js 对 SQLITE_BUSY 做指数退避重试（如 3 次）；",
            "写请求进入轻量队列/串行化，避免并发写直接打满；",
            "生产环境使用 MySQL/PostgreSQL，并保留应用层重试与幂等键。",
        ],
        "prevention": [
            "把 DB 锁场景纳入故障矩阵；",
            "监控写错误率与 P95，设置阈值；",
            "对关键写操作加幂等键与重试预算。",
        ],
    }

    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"db-lock-{stamp}.json"
    md_path = out_dir / f"db-lock-{stamp}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# DB 独占锁 / 写竞争故障报告",
        "",
        f"- 预览：`{base}`",
        f"- 锁窗口：{report['lockWindowMs']}ms",
        "",
        "| 阶段 | 请求 | 错误率 | P50(ms) | P95(ms) |",
        "|---|---:|---:|---:|---:|",
        f"| baseline writes | {report['baselineWrites']['requests']} | "
        f"{report['baselineWrites']['errorRate'] * 100:.1f}% | "
        f"{report['baselineWrites']['p50Ms']} | {report['baselineWrites']['p95Ms']} |",
        f"| locked writes | {report['lockedWrites']['requests']} | "
        f"{report['lockedWrites']['errorRate'] * 100:.1f}% | "
        f"{report['lockedWrites']['p50Ms']} | {report['lockedWrites']['p95Ms']} |",
        f"| after writes | {report['afterWrites']['requests']} | "
        f"{report['afterWrites']['errorRate'] * 100:.1f}% | "
        f"{report['afterWrites']['p50Ms']} | {report['afterWrites']['p95Ms']} |",
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
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"json={json_path}")
    print(f"md={md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
