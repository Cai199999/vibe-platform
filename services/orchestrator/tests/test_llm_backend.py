"""LLM BackendCoder 修复器：诊断入 Prompt、输出强校验、权限边界。"""

from __future__ import annotations

import json
import unittest

from ai_core.config import Settings
from ai_core.fullstack.coders import generate_backend_bundle, generate_dba_bundle
from ai_core.fullstack.llm_backend import (
    LLMBackendCoder,
    LLMBackendRepairError,
    LLMBackendRepairUnavailable,
)
from ai_core.ports.model import ModelClient
from tests.test_fullstack import _master_detail_contract


class _FakeModelClient(ModelClient):
    """记录 Prompt，并返回预设 JSON。"""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str, str, dict | None]] = []

    def generate(self, task: str, system: str, human: str) -> str:
        self.calls.append((task, system, human, None))
        return self.response

    def generate_json(
        self,
        task: str,
        system: str,
        human: str,
        *,
        json_schema: dict | None = None,
    ) -> str:
        self.calls.append((task, system, human, json_schema))
        return self.response


def _bundle():
    contract = _master_detail_contract()
    dba = generate_dba_bundle(contract)
    schema = dba.files["server/schema.core.json"]
    return contract, schema, generate_backend_bundle(contract, dba_schema=schema)


class LLMBackendCoderTest(unittest.TestCase):
    def test_prompt_contains_full_file_and_applier_diagnostics(self) -> None:
        contract, schema, bundle = _bundle()
        response = json.dumps(
            {
                "files": {"server/index.js": bundle.files["server/index.js"]},
                "reasoning": "修复 JOIN 别名",
            }
        )
        client = _FakeModelClient(response)
        coder = LLMBackendCoder(
            Settings(
                deepseek_api_key="test-key",
                fullstack_llm_repair_enabled=True,
            ),
            client=client,
        )

        repaired = coder.repair(
            contract=contract,
            bundle=bundle,
            dba_schema=schema,
            diagnostics=[
                "server/index.js 嵌套路由 /:id/resumez 未在契约 relations 中定义"
            ],
            attempt=1,
            max_attempts=2,
        )

        self.assertEqual(len(client.calls), 1)
        task, system, human, schema_payload = client.calls[0]
        self.assertEqual(task, "coder")
        self.assertIn("完整文件内容", system)
        self.assertIn("server/db.js", system)
        self.assertIn("/:id/resumez", human)
        self.assertIn('"user_id"', human)
        self.assertIn("server/index.js", human)
        self.assertIsNotNone(schema_payload)
        self.assertEqual(repaired.metadata["llmRepair"]["files"], ["server/index.js"])
        self.assertEqual(
            repaired.files["server/index.js"], bundle.files["server/index.js"]
        )
        # 除 server/index.js 外的产物永远不被 LLM 覆盖。
        untouched = {
            path: content
            for path, content in bundle.files.items()
            if path != "server/index.js"
        }
        self.assertTrue(untouched)
        for path, content in untouched.items():
            self.assertEqual(repaired.files[path], content)

    def test_prompt_includes_read_only_db_js_authority(self) -> None:
        """db.js 是 Applier 判断 JOIN 别名的真实事实源，必须只读入 Prompt。"""
        contract, schema, bundle = _bundle()
        dba = generate_dba_bundle(contract)
        response = json.dumps(
            {"files": {"server/index.js": bundle.files["server/index.js"]}}
        )
        client = _FakeModelClient(response)
        coder = LLMBackendCoder(
            Settings(
                deepseek_api_key="test-key",
                fullstack_llm_repair_enabled=True,
            ),
            client=client,
        )

        coder.repair(
            contract=contract,
            bundle=bundle,
            dba_schema=schema,
            diagnostics=["[server/index.js] 嵌套路由别名错误"],
            authoritative_files={"server/db.js": dba.files["server/db.js"]},
        )

        human = client.calls[0][2]
        self.assertIn("只读权威 ORM", human)
        self.assertIn("RELATIONS", human)
        self.assertIn("server/db.js", human)

    def test_accepts_markdown_fenced_json(self) -> None:
        contract, schema, bundle = _bundle()
        response = (
            "```json\n"
            + json.dumps(
                {"files": {"server/index.js": bundle.files["server/index.js"]}}
            )
            + "\n```"
        )
        coder = LLMBackendCoder(
            Settings(
                deepseek_api_key="test-key",
                fullstack_llm_repair_enabled=True,
            ),
            client=_FakeModelClient(response),
        )
        repaired = coder.repair(
            contract=contract,
            bundle=bundle,
            dba_schema=schema,
            diagnostics=["JOIN 别名错误"],
        )
        self.assertIn("server/index.js", repaired.files)

    def test_rejects_unapproved_file_paths(self) -> None:
        contract, schema, bundle = _bundle()
        response = json.dumps(
            {
                "files": {
                    "server/index.js": bundle.files["server/index.js"],
                    "server/db.js": "export const hacked = true",
                }
            }
        )
        coder = LLMBackendCoder(
            Settings(
                deepseek_api_key="test-key",
                fullstack_llm_repair_enabled=True,
            ),
            client=_FakeModelClient(response),
        )
        with self.assertRaises(LLMBackendRepairError) as ctx:
            coder.repair(
                contract=contract,
                bundle=bundle,
                dba_schema=schema,
                diagnostics=["字段错误"],
            )
        self.assertIn("未授权文件", str(ctx.exception))

    def test_rejects_empty_file_content(self) -> None:
        contract, schema, bundle = _bundle()
        response = json.dumps({"files": {"server/index.js": "   "}})
        coder = LLMBackendCoder(
            Settings(
                deepseek_api_key="test-key",
                fullstack_llm_repair_enabled=True,
            ),
            client=_FakeModelClient(response),
        )
        with self.assertRaises(LLMBackendRepairError):
            coder.repair(
                contract=contract,
                bundle=bundle,
                dba_schema=schema,
                diagnostics=["字段错误"],
            )

    def test_disabled_or_missing_key_is_unavailable(self) -> None:
        contract, schema, bundle = _bundle()
        disabled = LLMBackendCoder(
            Settings(
                deepseek_api_key="test-key",
                fullstack_llm_repair_enabled=False,
            ),
            client=_FakeModelClient("{}"),
        )
        self.assertFalse(disabled.available)
        with self.assertRaises(LLMBackendRepairUnavailable):
            disabled.repair(
                contract=contract,
                bundle=bundle,
                dba_schema=schema,
                diagnostics=["错误"],
            )

        missing_key = LLMBackendCoder(
            Settings(deepseek_api_key="", fullstack_llm_repair_enabled=True)
        )
        self.assertFalse(missing_key.available)

    def test_requires_non_empty_diagnostics(self) -> None:
        contract, schema, bundle = _bundle()
        coder = LLMBackendCoder(
            Settings(
                deepseek_api_key="test-key",
                fullstack_llm_repair_enabled=True,
            ),
            client=_FakeModelClient("{}"),
        )
        with self.assertRaises(LLMBackendRepairError):
            coder.repair(
                contract=contract,
                bundle=bundle,
                dba_schema=schema,
                diagnostics=[],
            )


if __name__ == "__main__":
    unittest.main()
