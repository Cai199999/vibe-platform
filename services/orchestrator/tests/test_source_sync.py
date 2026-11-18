"""Direct Ops 源码快照同步测试。"""

from __future__ import annotations

import json
import unittest

import httpx

from ai_core.direct_ops.source_sync import SourceSnapshotSync


class _Sandbox:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files

    def read_file(self, sandbox_id: str, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


def _handler(requests: list[httpx.Request], *, status: int = 200):
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            if status == 404:
                return httpx.Response(404, json={"code": "PROJECT_SOURCE_NOT_FOUND"})
            return httpx.Response(
                200,
                json={
                    "projectId": "proj_1",
                    "tenantId": "tenant_local",
                    "projectName": "存量项目",
                    "entryFile": "src/App.jsx",
                    "sourceName": "App.jsx",
                    "revision": 1,
                    "files": {"src/App.jsx": "export default 1"},
                },
            )
        if status >= 400:
            return httpx.Response(status, json={"code": "STORAGE_UNAVAILABLE"})
        body = json.loads(request.content)
        return httpx.Response(200, json={"revision": 2, **body, "fileCount": 1})

    return handle


class SourceSnapshotSyncTests(unittest.TestCase):
    def test_sync_reads_sandbox_and_puts_full_snapshot(self) -> None:
        requests: list[httpx.Request] = []
        client = httpx.Client(transport=httpx.MockTransport(_handler(requests)))
        sandbox = _Sandbox({"src/App.jsx": "export default 2"})
        sync = SourceSnapshotSync(
            base_url="http://bff:8080",
            enabled=True,
            client=client,
        )

        result = sync.sync(
            project_id="proj_1",
            sandbox_id="sbx_1",
            tenant_id="tenant_local",
            touched_file="src/App.jsx",
            sandbox=sandbox,  # type: ignore[arg-type]
        )

        self.assertEqual(result["status"], "SYNCED")
        self.assertEqual(result["revision"], 2)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(requests[0].url.params["tenantId"], "tenant_local")
        put_body = json.loads(requests[1].content)
        self.assertEqual(put_body["files"]["src/App.jsx"], "export default 2")
        self.assertEqual(put_body["tenantId"], "tenant_local")

    def test_missing_snapshot_is_skipped(self) -> None:
        client = httpx.Client(transport=httpx.MockTransport(_handler([], status=404)))
        sync = SourceSnapshotSync(
            base_url="http://bff:8080",
            enabled=True,
            client=client,
        )

        result = sync.sync(
            project_id="proj_1",
            sandbox_id="sbx_1",
            tenant_id="tenant_local",
            touched_file="src/App.jsx",
            sandbox=_Sandbox({}),  # type: ignore[arg-type]
        )

        self.assertEqual(result["status"], "SKIPPED")

    def test_file_outside_snapshot_is_skipped(self) -> None:
        requests: list[httpx.Request] = []
        client = httpx.Client(transport=httpx.MockTransport(_handler(requests)))
        sync = SourceSnapshotSync(
            base_url="http://bff:8080",
            enabled=True,
            client=client,
        )

        result = sync.sync(
            project_id="proj_1",
            sandbox_id="sbx_1",
            tenant_id="tenant_local",
            touched_file="src/Other.jsx",
            sandbox=_Sandbox({}),  # type: ignore[arg-type]
        )

        self.assertEqual(result["status"], "SKIPPED")
        self.assertEqual(len(requests), 1)

    def test_storage_failure_returns_failed_without_raising(self) -> None:
        client = httpx.Client(transport=httpx.MockTransport(_handler([], status=503)))
        sync = SourceSnapshotSync(
            base_url="http://bff:8080",
            enabled=True,
            client=client,
        )

        result = sync.sync(
            project_id="proj_1",
            sandbox_id="sbx_1",
            tenant_id="tenant_local",
            touched_file="src/App.jsx",
            sandbox=_Sandbox({"src/App.jsx": "x"}),  # type: ignore[arg-type]
        )

        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(result["retryable"])

    def test_disabled_sync_does_not_call_bff(self) -> None:
        sync = SourceSnapshotSync(
            base_url="http://bff:8080",
            enabled=False,
        )

        result = sync.sync(
            project_id="proj_1",
            sandbox_id="sbx_1",
            tenant_id="tenant_local",
            touched_file="src/App.jsx",
            sandbox=_Sandbox({}),  # type: ignore[arg-type]
        )

        self.assertEqual(result["status"], "DISABLED")


if __name__ == "__main__":
    unittest.main()
