"""证据链报告：阶段漂移 / Mock / 缺失必须被确定性识别。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_core.evidence_chain import (
    analyse_baseline,
    infer_interaction_method,
    render_markdown,
)


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _visual() -> dict:
    return {
        "pages": [
            {
                "pageId": "pg_home",
                "path": "/",
                "title": "首页",
                "root": {
                    "id": "nd_root",
                    "type": "page",
                    "name": "首页",
                    "children": [
                        {
                            "id": "nd_form",
                            "type": "form",
                            "name": "设施表单",
                            "children": [],
                        },
                        {
                            "id": "nd_list",
                            "type": "list",
                            "name": "工单列表",
                            "children": [],
                        },
                    ],
                },
            },
            {
                "pageId": "pg_tasks",
                "path": "/tasks",
                "title": "工单",
                "root": {"id": "nd_tasks", "type": "page", "name": "工单"},
            },
        ]
    }


class EvidenceChainTests(unittest.TestCase):
    def _baseline(self, root: Path) -> Path:
        _write(
            root / "contract.json",
            {
                "projectName": "证据链样例",
                "frontendRoutes": [
                    {"path": "/", "name": "首页"},
                    {"path": "/tasks", "name": "工单"},
                ],
                "components": [
                    {"name": "设施表单", "role": "form"},
                    {"name": "工单列表", "role": "list"},
                ],
                "apiRoutes": [
                    {"method": "GET", "path": "/api/v1/items"},
                    {"method": "POST", "path": "/api/v1/items"},
                ],
                "componentInteractions": [
                    {
                        "component": "设施表单",
                        "event": "submit",
                        "apiRoute": "/api/v1/items",
                    },
                    {
                        "component": "工单列表",
                        "event": "click",
                        "apiRoute": "/api/v1/tasks",
                    },
                ],
                "contractMarkdown": "采用 opengeos/geoai 方案。",
                "assumptions": [],
            },
        )
        _write(
            root / "v0-contract.json",
            {
                "frontendRoutes": [
                    {"path": "/", "name": "首页"},
                    {"path": "/tasks", "name": "工单"},
                    {"path": "/login", "name": "登录"},
                ],
                "components": [
                    {"name": "设施表单"},
                    {"name": "工单列表"},
                    {"name": "工单状态按钮"},
                ],
            },
        )
        _write(root / "v0-visual-frozen.json", _visual())
        _write(
            root / "discovery.json",
            {"options": [{"optionId": "opt_1", "title": "opengeos/geoai"}]},
        )
        _write(
            root / "metadata.json",
            {
                "projectName": "证据链样例",
                "solutionChoice": {
                    "optionId": "opt_1",
                    "title": "opengeos/geoai",
                },
            },
        )
        source = root / "source"
        (source / "server").mkdir(parents=True)
        (source / "src").mkdir(parents=True)
        (source / "public").mkdir(parents=True)
        (source / "server" / "index.js").write_text(
            "app.get('/api/v1/items', asyncHandler(async (req, res) => {"
            " res.json({ success: true }) }))\n"
            "app.post('/api/v1/items', asyncHandler(async (req, res) => {"
            " res.status(201).json({ success: true,"
            " message: '沙盒 Mock 响应' }) }))\n",
            encoding="utf-8",
        )
        (source / "src" / "App.jsx").write_text("fetch('/items')\n", encoding="utf-8")
        _write(
            source / "public" / "vibe-source-map.json",
            {
                "nodes": {
                    "oid_root": {"oid": "oid_root", "nodeId": "nd_root"},
                    "oid_form": {"oid": "oid_form", "nodeId": "nd_form"},
                    "oid_list": {"oid": "oid_list", "nodeId": "nd_list"},
                    "oid_tasks": {"oid": "oid_tasks", "nodeId": "nd_tasks"},
                }
            },
        )
        return root

    def test_detects_stage_drift_mock_and_missing_interaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = analyse_baseline(self._baseline(Path(tmp)))
        drift = report["metrics"]["stageDrift"]
        self.assertEqual(drift["lostRoutes"], ["/login"])
        self.assertEqual(drift["lostComponents"], ["工单状态按钮"])
        self.assertEqual(report["metrics"]["apis"]["ok"], 1)
        self.assertEqual(report["metrics"]["apis"]["mock"], 1)
        self.assertEqual(report["metrics"]["interactions"]["missing"], 1)
        self.assertEqual(report["metrics"]["decision"]["status"], "ok")
        self.assertEqual(report["metrics"]["sourceTrace"]["coverage"], 1.0)
        self.assertGreater(report["summary"]["critical"], 0)

    def test_upload_change_prefers_post_over_get(self) -> None:
        """同一路径有 GET/POST 时，上传类 change 必须核对 POST，不能被 GET 冒充。"""
        self.assertEqual(
            infer_interaction_method(
                "change",
                "影像上传",
                "/api/v1/facilities/{facility_id}/facility-images",
                ["GET", "POST"],
            ),
            "POST",
        )
        self.assertEqual(
            infer_interaction_method(
                "change",
                "设施筛选面板",
                "/api/v1/facilities",
                ["GET", "POST"],
            ),
            "GET",
        )

    def test_markdown_lists_drift_and_mock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = analyse_baseline(self._baseline(Path(tmp)))
        markdown = render_markdown(report)
        self.assertIn("丢失路由", markdown)
        self.assertIn("/login", markdown)
        self.assertIn("Mock", markdown)
        self.assertIn("方案决策", markdown)


if __name__ == "__main__":
    unittest.main()
