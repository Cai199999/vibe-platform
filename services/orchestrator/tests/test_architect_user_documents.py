"""用户上传文档 RAG 的确定性测试。"""

from __future__ import annotations

import unittest

from ai_core.architect.user_documents import retrieve_user_document_chunks
from ai_core.domain.architect import ArchitectRunRequest

DOC = """# 成果质检

## 返工规则

返工后必须追加一条 QCRecord，保留原记录，不得覆盖历史质检结论。

## 抽检规则

每个项目至少抽检 20% 的成果；重点成果全检。

# 影像上传

## 文件格式

支持 TIFF / JPEG / ZIP，单文件不超过 2GB。
"""


class UserDocumentRagTests(unittest.TestCase):
    def test_empty_documents_return_empty(self) -> None:
        self.assertEqual(retrieve_user_document_chunks([], "成果质检"), [])

    def test_markdown_sections_are_ranked(self) -> None:
        chunks = retrieve_user_document_chunks(
            [{"name": "gis.md", "content": DOC}],
            "成果质检返工规则",
            top_k=2,
        )
        self.assertTrue(chunks)
        self.assertIn("返工", chunks[0].content)
        self.assertEqual(chunks[0].metadata["source"], "user_document")
        self.assertEqual(chunks[0].title, "gis.md")

    def test_upload_is_optional(self) -> None:
        request = ArchitectRunRequest(projectId="proj_demo")
        self.assertEqual(request.knowledge_documents, [])

    def test_null_upload_is_treated_as_empty(self) -> None:
        request = ArchitectRunRequest(
            projectId="proj_demo",
            knowledgeDocuments=None,
        )
        self.assertEqual(request.knowledge_documents, [])

    def test_upload_payload_maps_camel_case(self) -> None:
        request = ArchitectRunRequest(
            projectId="proj_demo",
            knowledgeDocuments=[
                {"name": "requirements.md", "content": "# 需求\n需要成果质检。"}
            ],
        )
        self.assertEqual(len(request.knowledge_documents), 1)
        self.assertEqual(request.knowledge_documents[0].name, "requirements.md")


if __name__ == "__main__":
    unittest.main()
