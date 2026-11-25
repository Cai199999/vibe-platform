"""上传文档“长期复用 / 一次性”语义判断测试。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_core.architect.document_classifier import classify_document_reuse
from ai_core.ports.model import ModelClient, ModelClientError


class _FakeClient(ModelClient):
    def __init__(self, payload: str | None = None, error: Exception | None = None):
        self.payload = payload
        self.error = error

    def generate(self, task: str, system: str, human: str) -> str:
        if self.error:
            raise self.error
        return self.payload or ""


class DocumentClassifierTests(unittest.TestCase):
    def test_frequent_signal_is_stored(self) -> None:
        decision = classify_document_reuse(
            "gis-glossary.md",
            "# 术语规范\n像控点、空三、DOM、DLG 的长期约定。",
        )
        self.assertEqual(decision.decision, "STORE")
        self.assertEqual(decision.reuse, "FREQUENT")
        self.assertEqual(decision.source, "rules")

    def test_one_off_signal_is_discarded(self) -> None:
        decision = classify_document_reuse(
            "临时草稿.md",
            "仅本次参考，看完就删，不作为需求。",
        )
        self.assertEqual(decision.decision, "DISCARD")
        self.assertEqual(decision.reuse, "ONE_OFF")

    def test_structured_requirements_are_stored(self) -> None:
        content = (
            "# 测绘需求\n\n## 角色\n项目经理、作业员、质检员。\n\n"
            "## 流程\n项目 -> 任务 -> 成果 -> 质检。\n\n"
            "## 数据\n实体、字段、接口、权限、数据库。\n"
        ) * 4
        decision = classify_document_reuse("requirements.md", content)
        self.assertEqual(decision.decision, "STORE")
        self.assertEqual(decision.reuse, "FREQUENT")

    def test_ambiguous_document_asks(self) -> None:
        decision = classify_document_reuse("notes.md", "今天看了下页面。")
        self.assertEqual(decision.decision, "ASK")
        self.assertEqual(decision.reuse, "UNCERTAIN")

    @patch("ai_core.architect.document_classifier.get_settings")
    def test_llm_fallback_can_store(self, get_settings) -> None:
        get_settings.return_value = SimpleNamespace(deepseek_api_key="test-key")
        client = _FakeClient(
            '{"reuse":"FREQUENT","decision":"STORE",'
            '"reason":"看起来会反复使用","confidence":0.82}'
        )
        decision = classify_document_reuse(
            "notes.md",
            "今天看了下页面。",
            llm_provider="deepseek",
            model_client=client,
        )
        self.assertEqual(decision.decision, "STORE")
        self.assertEqual(decision.source, "llm")

    @patch("ai_core.architect.document_classifier.get_settings")
    def test_llm_failure_falls_back_to_ask(self, get_settings) -> None:
        get_settings.return_value = SimpleNamespace(deepseek_api_key="test-key")
        client = _FakeClient(error=ModelClientError("timeout"))
        decision = classify_document_reuse(
            "notes.md",
            "今天看了下页面。",
            llm_provider="deepseek",
            model_client=client,
        )
        self.assertEqual(decision.decision, "ASK")
        self.assertEqual(decision.source, "rules")


if __name__ == "__main__":
    unittest.main()
