"""交付打包：镜像元数据、Helm、SBOM 与完整性清单。"""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

from ai_core.prototype.spec import ApplicationSpec


def package_delivery(workspace: Path, spec: ApplicationSpec) -> dict:
    dist = workspace / "dist"
    dist.mkdir(parents=True, exist_ok=True)

    helm_source = workspace / "helm" / spec.name
    helm_tar = dist / f"{spec.name}-0.1.0.tgz"
    with tarfile.open(helm_tar, "w:gz") as archive:
        archive.add(helm_source, arcname=spec.name)

    files = sorted(
        path
        for path in workspace.rglob("*")
        if path.is_file() and "dist" not in path.parts and "data" not in path.parts
    )
    components = []
    for path in files:
        relative = path.relative_to(workspace).as_posix()
        components.append(
            {
                "path": relative,
                "sha256": _sha256(path),
                "license": "internal",
            }
        )
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "metadata": {"component": {"name": spec.name, "version": "0.1.0"}},
        "components": components,
    }
    sbom_path = dist / "sbom.json"
    sbom_path.write_text(
        json.dumps(sbom, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    image_digest = "sha256:" + _sha256_text(
        json.dumps([item["sha256"] for item in components], sort_keys=True)
    )
    manifest = {
        "name": spec.name,
        "version": "0.1.0",
        "imageDigest": image_digest,
        "helmChart": helm_tar.name,
        "helmSha256": _sha256(helm_tar),
        "sbom": sbom_path.name,
        "sbomSha256": _sha256(sbom_path),
        "components": len(components),
    }
    manifest_path = dist / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "dist": str(dist),
        "manifest": manifest,
        "verify": verify_manifest(manifest_path),
    }


def verify_manifest(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dist = manifest_path.parent
    checks = {
        "helm": _sha256(dist / manifest["helmChart"]) == manifest["helmSha256"],
        "sbom": _sha256(dist / manifest["sbom"]) == manifest["sbomSha256"],
    }
    return {"ok": all(checks.values()), "checks": checks}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["package_delivery", "verify_manifest"]
