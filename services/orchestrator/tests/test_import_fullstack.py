"""导入功能全栈化：tar.gz 安全解包、双服务识别、代理与画布标记测试。

运行：

    cd services/orchestrator
    PYTHONPATH=. python -m unittest tests.test_import_fullstack -v
"""

from __future__ import annotations

import io
import json
import tarfile
import unittest
import zipfile

from ai_core.config import Settings
from ai_core.ports.sandbox import SandboxValidationError
from ai_core.sandbox.importer import import_project_file, import_zip


def _tar_bytes(
    entries: dict[str, bytes | str],
    *,
    prefix: str = "",
    symlink: tuple[str, str] | None = None,
) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, value in entries.items():
            data = value.encode("utf-8") if isinstance(value, str) else value
            info = tarfile.TarInfo(f"{prefix}{path}")
            info.size = len(data)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(data))
        if symlink:
            name, target = symlink
            info = tarfile.TarInfo(f"{prefix}{name}")
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return buffer.getvalue()


def _fullstack_entries(prefix: str = "") -> dict[str, bytes | str]:
    return {
        "package.json": json.dumps(
            {
                "scripts": {
                    "dev": "node scripts/dev.mjs",
                    "dev:vite": "vite",
                },
                "devDependencies": {"vite": "^5.0.0"},
            }
        ),
        "vite.config.js": (
            "import { defineConfig } from 'vite'\n"
            "export default defineConfig({ server: { port: 5173 } })\n"
        ),
        "index.html": '<!doctype html><html><body><div id="root"></div></body></html>',
        "src/main.jsx": (
            "import React from 'react'\n"
            "import { createRoot } from 'react-dom/client'\n"
            "import App from './App.jsx'\n"
            "createRoot(document.getElementById('root')).render(<App />)\n"
        ),
        "src/App.jsx": (
            "export default function App() {\n"
            "  const html = '<section>not jsx</section>'\n"
            "  return (\n"
            '    <main className="app">\n'
            "      <h1>Resume AI</h1>\n"
            "      <button onClick={() => alert('hi')}>Analyze</button>\n"
            "    </main>\n"
            "  )\n"
            "}\n"
        ),
        "server/package.json": json.dumps(
            {"type": "module", "dependencies": {"express": "^4.21.0"}}
        ),
        "server/index.js": (
            "import express from 'express'\n"
            "const app = express()\n"
            "app.listen(3000)\n"
        ),
        "README.md": "# 全栈项目\n",
        ".env.example": "DATABASE_URL=\nOPENAI_API_KEY=\n",
        ".env": "DATABASE_URL=mysql://secret\n",
    }


class ImportArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings()

    def test_exported_fullstack_tar_is_recognized_and_both_services_start(self) -> None:
        data = _tar_bytes(_fullstack_entries())

        imported = import_project_file(data, "project-export.tar.gz", self.settings)

        self.assertEqual(imported.kind, "fullstack")
        self.assertEqual(imported.services, ["vite", "express"])
        self.assertEqual(imported.template, "fullstack-export")
        self.assertEqual(imported.entry_file, "src/App.jsx")
        self.assertGreater(imported.data_source_count, 0)
        self.assertIn("src/vibe-import-marker.js", imported.files)
        # 平台导出包里的 .env.example 是部署说明，必须保留；真实 .env 必须剔除。
        self.assertIn(".env.example", imported.files)
        self.assertNotIn(".env", imported.files)

        start = imported.start_command
        self.assertIn("PORT=3000", start)
        self.assertIn("node server/index.js", start)
        self.assertIn("vite --host 0.0.0.0 --port 5173", start)

        app_source = imported.files["src/App.jsx"].decode("utf-8")
        self.assertIn('data-source="imp-src-app-jsx-', app_source)
        # 字符串里的伪标签不能被改写。
        self.assertIn("'<section>not jsx</section>'", app_source)
        self.assertIn(
            "import './vibe-import-marker.js'", imported.files["src/main.jsx"].decode()
        )

        vite = imported.files["vite.config.js"].decode("utf-8")
        self.assertIn("'/api'", vite)
        self.assertIn("127.0.0.1:3000", vite)

        text_files = imported.text_files
        self.assertIn("src/App.jsx", text_files)
        self.assertIn("server/index.js", text_files)

    def test_common_root_folder_is_stripped(self) -> None:
        data = _tar_bytes(_fullstack_entries(prefix="project-export/"))

        imported = import_project_file(data, "project-export.tgz", self.settings)

        self.assertEqual(imported.kind, "fullstack")
        self.assertIn("package.json", imported.files)
        self.assertIn("server/index.js", imported.files)
        self.assertNotIn("project-export/package.json", imported.files)

    def test_archive_without_server_remains_react(self) -> None:
        entries = _fullstack_entries()
        entries.pop("server/package.json")
        entries.pop("server/index.js")
        entries.pop("vite.config.js")
        data = _tar_bytes(entries)

        imported = import_project_file(data, "frontend.tar.gz", self.settings)

        self.assertEqual(imported.kind, "react")
        self.assertEqual(imported.services, [])
        self.assertEqual(imported.data_source_count, 0)

    def test_zip_import_still_works_through_compat_entrypoint(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("index.html", "<html><body><h1>Hello</h1></body></html>")
        imported = import_zip(buffer.getvalue(), self.settings)
        self.assertEqual(imported.kind, "html")
        self.assertEqual(imported.entry_file, "index.html")

    def test_tar_path_traversal_is_rejected(self) -> None:
        data = _tar_bytes({"../evil.js": "alert(1)"})
        with self.assertRaises(SandboxValidationError):
            import_project_file(data, "evil.tar.gz", self.settings)

    def test_tar_symlink_is_rejected(self) -> None:
        data = _tar_bytes(
            {"package.json": "{}", "src/main.jsx": "export default 1"},
            symlink=("link", "/etc/passwd"),
        )
        with self.assertRaises(SandboxValidationError):
            import_project_file(data, "evil.tgz", self.settings)

    def test_unknown_archive_type_is_rejected(self) -> None:
        with self.assertRaises(SandboxValidationError):
            import_project_file(b"not an archive", "project.rar", self.settings)


if __name__ == "__main__":
    unittest.main()
