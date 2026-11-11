#!/usr/bin/env python3
"""验收脚本运行时能力的离线单测：上传路由解析与 multipart 编码。

这些用例不依赖 Docker / MySQL / 真实模型，可在 CI 中快速回归。

运行::

    python3 -m unittest scripts.test_acceptance_runtime -v
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import acceptance_test as acceptance  # noqa: E402


class UploadRouteTest(unittest.TestCase):
    def _package(self, index_code: str) -> Path:
        """在临时目录构造最小 package_root/server/index.js。"""
        directory = Path(tempfile.mkdtemp(prefix="acceptance-route-"))
        server = directory / "server"
        server.mkdir()
        (server / "index.js").write_text(index_code, encoding="utf-8")
        return directory

    def test_prefers_collection_route(self) -> None:
        package = self._package(
            "app.post('/api/v1/resumes', upload.single('file'), createHandler)\n"
            "app.post('/api/v1/resumes/upload', upload.single('resume'), handler)\n"
        )
        route = acceptance.find_upload_route(package, "/api/v1/resumes")
        self.assertEqual(route, ("POST", "/api/v1/resumes", "file"))

    def test_resolves_string_constant_path(self) -> None:
        package = self._package(
            "const COLLECTION_PATH = '/api/v1/resumes'\n"
            "app.post(COLLECTION_PATH, upload.single('file'), createHandler)\n"
        )
        route = acceptance.find_upload_route(package)
        self.assertEqual(route, ("POST", "/api/v1/resumes", "file"))

    def test_returns_none_without_multer(self) -> None:
        package = self._package("app.post('/api/v1/resumes', createHandler)\n")
        self.assertIsNone(acceptance.find_upload_route(package))

    def test_detects_business_upload_route(self) -> None:
        package = self._package(
            'app.put("/api/v1/files/:id/upload", upload.single("file"), handler)\n'
        )
        route = acceptance.find_upload_route(package)
        self.assertEqual(route, ("PUT", "/api/v1/files/:id/upload", "file"))


class MultipartTest(unittest.TestCase):
    def test_multipart_body_is_parseable(self) -> None:
        received: dict[str, object] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server 约定
                length = int(self.headers.get("Content-Length", "0"))
                received["content_type"] = self.headers.get("Content-Type", "")
                received["body"] = self.rfile.read(length)
                payload = json.dumps({"success": True, "file_path": "/uploads/x"})
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload.encode("utf-8"))

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/upload"
            status, payload = acceptance.http_multipart(
                url,
                field="file",
                filename="resume.pdf",
                content=b"%PDF-1.4\nbody\n",
                fields={"name": "张三", "email": "a@example.com"},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(status, 201)
        self.assertEqual(payload["file_path"], "/uploads/x")
        content_type = str(received["content_type"])
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        body = received["body"]
        assert isinstance(body, bytes)
        self.assertIn(b'name="file"; filename="resume.pdf"', body)
        self.assertIn(b"%PDF-1.4", body)
        self.assertIn("张三".encode("utf-8"), body)


class OrmAstConsistencyTest(unittest.TestCase):
    """验收阶段的 ORM 门禁必须走 Babel AST，而不是正则。"""

    def _package(self, db_code: str) -> Path:
        root = Path(tempfile.mkdtemp(prefix="acceptance-orm-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        server = root / "server"
        (server / "migrations").mkdir(parents=True)
        (server / "db.js").write_text(db_code, encoding="utf-8")
        (server / "index.js").write_text(
            "const TABLE = 'resumes'\napp.get('/api/v1/resumes', handler)\n",
            encoding="utf-8",
        )
        (server / "api-routes.json").write_text(
            json.dumps({"table": "resumes", "collection": "/api/v1/resumes"}),
            encoding="utf-8",
        )
        (server / "migrations" / "001_init.sql").write_text(
            "CREATE TABLE resumes (\n"
            "  id BIGINT PRIMARY KEY,\n"
            "  file_name VARCHAR(255),\n"
            "  content TEXT NOT NULL\n"
            ");\n",
            encoding="utf-8",
        )
        return root

    def test_static_orm_passes(self) -> None:
        package = self._package(
            "const TABLE = 'resumes'\n"
            "const COLUMN_DEFS = [['id', 'INTEGER'], "
            "['file_name', 'TEXT'], ['content', 'TEXT']]\n"
        )
        self.assertEqual(acceptance.check_orm_ddl_consistency(package), [])

    def test_runtime_mutation_is_blocked(self) -> None:
        package = self._package(
            "const TABLE = 'resumes'\n"
            "const COLUMN_DEFS = [['id', 'INTEGER'], "
            "['file_name', 'TEXT'], ['content', 'TEXT']]\n"
            "COLUMN_DEFS.push(['name', 'TEXT'])\n"
        )
        issues = acceptance.check_orm_ddl_consistency(package)
        combined = " ".join(issues)
        self.assertIn("运行期", combined)
        self.assertIn("COLUMN_DEFS", combined)

    def test_dynamic_column_defs_are_blocked(self) -> None:
        package = self._package(
            "const TABLE = 'resumes'\n"
            "const COLUMN_DEFS = JSON.parse(process.env.COLUMNS || '[]')\n"
        )
        issues = acceptance.check_orm_ddl_consistency(package)
        self.assertTrue(any("静态" in issue for issue in issues))


class ContractUploadIntentTest(unittest.TestCase):
    def test_upload_intent(self) -> None:
        self.assertTrue(
            acceptance.contract_requires_upload(
                {"contractMarkdown": "支持上传 PDF 简历到服务器"}
            )
        )
        self.assertFalse(
            acceptance.contract_requires_upload(
                {"contractMarkdown": "客户管理 CRUD，无文件操作"}
            )
        )


if __name__ == "__main__":
    unittest.main()
