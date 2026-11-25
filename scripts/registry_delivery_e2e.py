#!/usr/bin/env python3
"""镜像发布链路 E2E：构建 → 推送本地 registry → 校验 tag / digest → 回写 manifest。"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "orchestrator"))

from ai_core.prototype.delivery import package_delivery  # noqa: E402
from ai_core.prototype.generator import generate_workspace  # noqa: E402
from ai_core.prototype.planner import plan_topology  # noqa: E402
from ai_core.prototype.spec import default_ticket_spec  # noqa: E402

SERVICES = ("api", "consumer", "agent", "web")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run(cmd: list[str], *, check: bool = True, timeout: int = 1800):
    return subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def wait_registry(base: str, *, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/v2/", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    raise RuntimeError("本地 registry 未就绪")


def registry_json(base: str, path: str, *, headers: dict | None = None):
    request = urllib.request.Request(f"{base}{path}", headers=headers or {})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8")), dict(response.headers)


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:6]
    run_dir = ROOT / "reports" / "registry-e2e" / f"{stamp}-{suffix}"
    workspace = run_dir / "workspace"
    spec = default_ticket_spec()
    generate_workspace(workspace, spec, plan_topology(spec))
    package_delivery(workspace, spec)

    project = f"vibereg-{suffix}"
    registry_name = f"vibe-registry-{suffix}"
    port = free_port()
    registry_base = f"http://127.0.0.1:{port}"
    compose = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        str(workspace / "infra" / "docker-compose.yml"),
        "--project-directory",
        str(workspace),
    ]
    report: dict = {
        "project": project,
        "registry": registry_base,
        "images": {},
        "ok": False,
    }
    try:
        run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                registry_name,
                "-p",
                f"127.0.0.1:{port}:5000",
                "registry:2",
            ]
        )
        wait_registry(registry_base)

        build = run(compose + ["build", *SERVICES], check=False)
        if build.returncode != 0:
            raise RuntimeError(f"镜像构建失败:\n{build.stderr[-2000:]}")
        report["stages"] = [{"name": "build", "ok": True}]

        for service in SERVICES:
            local = f"{project}-{service}:latest"
            repository = f"ticket-copilot-{service}"
            remote = f"127.0.0.1:{port}/{repository}:0.1.0"
            run(["docker", "tag", local, remote])
            push = run(["docker", "push", remote], check=False)
            if push.returncode != 0:
                raise RuntimeError(f"推送失败 {remote}: {push.stderr[-800:]}")
            tags, _ = registry_json(registry_base, f"/v2/{repository}/tags/list")
            digest = ""
            try:
                _, headers = registry_json(
                    registry_base,
                    f"/v2/{repository}/manifests/0.1.0",
                    headers={
                        "Accept": (
                            "application/vnd.docker.distribution.manifest.v2+json,"
                            "application/vnd.oci.image.manifest.v1+json,"
                            "application/vnd.oci.image.index.v1+json"
                        )
                    },
                )
                digest = headers.get("Docker-Content-Digest") or ""
            except Exception:  # noqa: BLE001 - 回退 docker inspect
                digest = ""
            if not digest:
                inspected = run(
                    [
                        "docker",
                        "image",
                        "inspect",
                        "--format",
                        "{{index .RepoDigests 0}}",
                        remote,
                    ],
                    check=False,
                )
                digest = inspected.stdout.strip()
            if "0.1.0" not in (tags.get("tags") or []):
                raise RuntimeError(f"registry 中缺少 tag: {repository}")
            report["images"][service] = {
                "repository": repository,
                "tag": "0.1.0",
                "reference": remote,
                "digest": digest,
            }
        report["stages"].append({"name": "push", "ok": True})

        manifest_path = workspace / "dist" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["registry"] = report["images"]
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        report["manifest"] = str(manifest_path.relative_to(ROOT))
        report["stages"].append({"name": "manifest", "ok": True})
        report["ok"] = True
    finally:
        run(["docker", "rm", "-f", registry_name], check=False)
        run(compose + ["down", "--rmi", "local", "--remove-orphans"], check=False)
        report["finishedAt"] = datetime.now(timezone.utc).isoformat()
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(f"[registry-e2e] ok={report['ok']} registry={registry_base}")
    print(
        "[registry-e2e] images="
        + ", ".join(
            f"{name}:{info['digest'][:19]}..."
            for name, info in report["images"].items()
        )
    )
    print(f"[registry-e2e] report={run_dir / 'report.json'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
