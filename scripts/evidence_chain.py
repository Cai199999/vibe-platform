#!/usr/bin/env python3
"""生成“需求 → 决策 → 契约 → 画布 → 代码”的证据链报告。

只读基线产物（contract / v0-contract / visual / source / metadata /
discovery），不执行生成物代码。默认 report-only；``--strict`` 在存在
critical 缺口时返回退出码 1。

用法：
    python3 scripts/evidence_chain.py \
      --baseline samples/gis-baseline-iter6

    # 写入指定目录，并在有 critical 缺口时失败
    python3 scripts/evidence_chain.py \
      --baseline samples/gis-baseline-iter6 --strict
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "services" / "orchestrator"
if str(ORCH) not in sys.path:
    sys.path.insert(0, str(ORCH))

from ai_core.evidence_chain import analyse_baseline, render_markdown  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--out", default=str(ROOT / "reports" / "traceability"))
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    baseline = Path(args.baseline).resolve()
    if not (baseline / "contract.json").is_file():
        print(f"缺少 contract.json: {baseline}", file=sys.stderr)
        return 2
    report = analyse_baseline(baseline)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = baseline.name
    json_path = out_dir / f"{name}-{stamp}.json"
    md_path = out_dir / f"{name}-{stamp}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")

    summary = report["summary"]
    print(
        f"verdict={summary['verdict']} "
        f"critical={summary['critical']} warnings={summary['warnings']}"
    )
    metrics = report["metrics"]
    print(
        "pages={pages[ok]}/{pages[total]} "
        "components={components[ok]}/{components[total]} "
        "api(real/mock/missing)={apis[ok]}/{apis[mock]}/{apis[missing]} "
        "interactions(real/mock/missing)="
        "{interactions[ok]}/{interactions[mock]}/{interactions[missing]}".format(
            **metrics
        )
    )
    print("sourceTrace={mappedNodes}/{totalNodes} ".format(**metrics["sourceTrace"]))
    print(f"json={json_path}")
    print(f"md={md_path}")
    if args.strict and summary["critical"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
