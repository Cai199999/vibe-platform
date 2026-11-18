"""Direct Ops 提交后的源码快照同步。

Direct Ops 的唯一事实源仍是沙盒源码；BFF 的 MySQL / MinIO 快照只是
跨刷新事实缓存。每次 Git commit 成功后，按快照已有文件树从沙盒读取
最新正文，再 PUT 回 BFF，避免“画布改了、刷新后又回到导入版本”。

同步是 best-effort：BFF / MinIO 暂时不可用时不回滚用户修改，只在
DirectActionResponse.sourceSync 中返回 FAILED 诊断。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

from ai_core.ports.sandbox import SandboxProvider

logger = logging.getLogger(__name__)


class SourceSnapshotSync:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 3.0,
        enabled: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = str(base_url or "").rstrip("/")
        self._timeout = max(0.5, float(timeout_seconds))
        self._enabled = bool(enabled)
        self._client = client

    def sync(
        self,
        *,
        project_id: str,
        sandbox_id: str,
        tenant_id: str | None,
        touched_file: str,
        sandbox: SandboxProvider,
    ) -> dict[str, Any]:
        if not self._enabled:
            return {"status": "DISABLED"}
        if not self._base_url:
            return {"status": "FAILED", "message": "BFF_BASE_URL 未配置"}

        tenant = (tenant_id or "tenant_local").strip() or "tenant_local"
        url = (
            f"{self._base_url}/api/v1/project-sources/" f"{quote(project_id, safe='')}"
        )
        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            response = client.get(url, params={"tenantId": tenant})
            if response.status_code == 404:
                return {
                    "status": "SKIPPED",
                    "reason": "项目没有持久化源码快照",
                }
            response.raise_for_status()
            snapshot = response.json()
            paths = sorted(
                path
                for path, content in (snapshot.get("files") or {}).items()
                if isinstance(path, str) and isinstance(content, str)
            )
            if not paths:
                return {"status": "SKIPPED", "reason": "快照文件树为空"}
            if touched_file and touched_file not in paths:
                return {
                    "status": "SKIPPED",
                    "reason": "被修改文件不在持久化快照中",
                }

            files: dict[str, str] = {}
            for path in paths:
                files[path] = sandbox.read_file(sandbox_id, path)

            payload = {
                "tenantId": tenant,
                "projectName": snapshot.get("projectName"),
                "entryFile": snapshot.get("entryFile"),
                "sourceName": snapshot.get("sourceName"),
                "files": files,
            }
            written = client.put(url, json=payload)
            written.raise_for_status()
            body = written.json()
            return {
                "status": "SYNCED",
                "revision": body.get("revision"),
                "contentHash": body.get("contentHash"),
                "fileCount": body.get("fileCount", len(files)),
                "fingerprint": body.get("contentHash"),
            }
        except Exception as exc:  # noqa: BLE001 - 同步失败不能回滚已提交的代码
            logger.warning(
                "source snapshot sync failed project=%s sandbox=%s file=%s: %s",
                project_id,
                sandbox_id,
                touched_file,
                exc,
            )
            return {
                "status": "FAILED",
                "message": str(exc)[:300],
                "retryable": True,
            }
        finally:
            if owns_client:
                client.close()


__all__ = ["SourceSnapshotSync"]
