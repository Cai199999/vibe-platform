#!/usr/bin/env python3
"""全流程原型入口：需求 → 契约 → 拓扑 → 生成 → 迁移 → 验收 → 交付 → 飞轮。"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "orchestrator"))

from ai_core.prototype.pipeline import run_full_flow  # noqa: E402


def main() -> int:
    try:
        report = run_full_flow()
    except Exception:
        traceback.print_exc()
        return 1
    verification = report["verification"]
    print(f"[full-flow] run={report['runId']} status={report['status']}")
    print(
        "[full-flow] migrations="
        + ", ".join(verification["migrations"])
        + f" | idempotent={verification['idempotency']}"
    )
    print(
        "[full-flow] categories="
        + ", ".join(item["aiCategory"] for item in verification["tickets"])
    )
    print(
        "[full-flow] delivery image="
        + report["delivery"]["manifest"]["imageDigest"][:19]
        + "..."
    )
    print(f"[full-flow] flywheel={report['flywheel']['decision']}")
    print(f"[full-flow] report={report['reportPath']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
