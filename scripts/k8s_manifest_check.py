#!/usr/bin/env python3
"""校验生成的 Helm / K8s 清单：YAML 可解析、必需 Kind 齐全、引用存在。"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "orchestrator"))

from ai_core.prototype.generator import generate_workspace  # noqa: E402
from ai_core.prototype.planner import plan_topology  # noqa: E402
from ai_core.prototype.spec import default_ticket_spec  # noqa: E402

REQUIRED_KINDS = {
    "Deployment",
    "Service",
    "Job",
    "ConfigMap",
    "PersistentVolumeClaim",
}


def main() -> int:
    spec = default_ticket_spec()
    with tempfile.TemporaryDirectory(prefix="vibe-k8s-") as tmp:
        workspace = Path(tmp) / "workspace"
        generate_workspace(workspace, spec, plan_topology(spec))
        helm = workspace / "helm" / spec.name
        kinds: dict[str, list[str]] = {}
        names: set[str] = set()
        for path in sorted(helm.rglob("*.yaml")):
            # 结构校验：把 Helm 模板表达式替换为占位符，再按 YAML 解析。
            text = re.sub(
                r"\{\{.*?\}\}",
                "{}",
                path.read_text(encoding="utf-8"),
                flags=re.DOTALL,
            )
            for document in yaml.safe_load_all(text):
                if not document:
                    continue
                kind = str(document.get("kind") or "")
                name = str((document.get("metadata") or {}).get("name") or "")
                kinds.setdefault(kind, []).append(path.name)
                if name:
                    names.add(name)
        missing = REQUIRED_KINDS - set(kinds)
        if missing:
            print(f"[k8s-check] 缺少 Kind: {sorted(missing)}")
            return 1
        # migration Job 引用的 PVC 必须存在
        pvc = f"{spec.name}-data"
        if pvc not in names:
            print(f"[k8s-check] 缺少 PVC: {pvc}")
            return 1
        print(
            "[k8s-check] OK "
            + ", ".join(f"{kind}={len(files)}" for kind, files in sorted(kinds.items()))
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
