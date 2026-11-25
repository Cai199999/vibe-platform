#!/usr/bin/env python3
"""用已冻结的 V0 画布真实跑一次 BACKEND_BINDING，验证契约不丢页面/组件。

与 ``evidence_chain.py`` 的区别：后者只读静态产物；本脚本会调用真实
Architect（LLM + 契约装配），因此需要 BFF / python-ai 已启动。

用法：
    python3 scripts/evidence_chain_live_check.py \
      --baseline samples/gis-baseline-iter6

退出码：0 无漂移；1 存在漂移或调用失败；2 输入缺失。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gis_baseline_e2e as base  # noqa: E402


def _route_paths(contract: dict[str, Any]) -> list[str]:
    return [
        str(item.get("path") or "/")
        for item in contract.get("frontendRoutes") or []
        if isinstance(item, dict)
    ]


def _component_names(contract: dict[str, Any]) -> list[str]:
    return [
        str(item.get("name") or "").strip()
        for item in contract.get("components") or []
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]


def _visual_pages(visual: dict[str, Any]) -> list[str]:
    return [
        str(item.get("path") or "/")
        for item in visual.get("pages") or []
        if isinstance(item, dict)
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--out", default=str(ROOT / "reports" / "traceability"))
    args = parser.parse_args()

    baseline = Path(args.baseline).resolve()
    required = ["v0-contract.json", "v0-visual-frozen.json", "metadata.json"]
    missing = [name for name in required if not (baseline / name).is_file()]
    if missing:
        print(f"缺少输入: {', '.join(missing)}", file=sys.stderr)
        return 2

    v0_contract = json.loads(
        (baseline / "v0-contract.json").read_text(encoding="utf-8")
    )
    visual = json.loads(
        (baseline / "v0-visual-frozen.json").read_text(encoding="utf-8")
    )
    metadata = json.loads((baseline / "metadata.json").read_text(encoding="utf-8"))
    choice = metadata.get("solutionChoice") or {}
    if not choice:
        print("基线缺少 solutionChoice，无法复现方案选择", file=sys.stderr)
        return 2

    run_id = f"arc_drift_{uuid.uuid4().hex[:10]}"
    project_id = f"prj_drift_{uuid.uuid4().hex[:10]}"
    started = time.perf_counter()
    try:
        contract, _, _ = base.drive_to_contract(
            run_id,
            phase="BACKEND_BINDING",
            project_id=project_id,
            choice=choice,
            visual_document=visual,
            visual_delta={},
        )
    except base.E2EFailure as exc:
        print(f"BACKEND_BINDING 失败: {exc}", file=sys.stderr)
        return 1
    elapsed = round(time.perf_counter() - started, 2)

    final_routes = _route_paths(contract)
    v0_routes = _route_paths(v0_contract)
    final_components = _component_names(contract)
    v0_components = _component_names(v0_contract)
    visual_pages = _visual_pages(visual)
    lost_routes = [path for path in v0_routes if path not in final_routes]
    lost_components = [name for name in v0_components if name not in final_components]
    missing_pages = [path for path in visual_pages if path not in final_routes]

    result = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "baseline": str(baseline),
        "runId": run_id,
        "elapsedSeconds": elapsed,
        "contractId": contract.get("contractId"),
        "routes": final_routes,
        "components": final_components,
        "lostRoutes": lost_routes,
        "lostComponents": lost_components,
        "visualPagesMissingFromContract": missing_pages,
        "ok": not (lost_routes or lost_components or missing_pages),
        "contract": contract,
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"{baseline.name}-backend-replay-{stamp}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"replay ok={result['ok']} elapsed={elapsed}s "
        f"routes={len(final_routes)} components={len(final_components)}"
    )
    print(f"lostRoutes={lost_routes}")
    print(f"lostComponents={lost_components}")
    print(f"visualPagesMissingFromContract={missing_pages}")
    print(f"json={path}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
