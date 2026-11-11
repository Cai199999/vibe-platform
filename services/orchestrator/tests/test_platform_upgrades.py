"""五项平台升级的回归测试：分级决策、澄清轮次、Skill、AI-Native、导入。

运行：

    cd services/orchestrator
    PYTHONPATH=. python -m unittest tests.test_platform_upgrades -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

from ai_core.agent.preferences import (
    CRITICAL,
    MAJOR,
    MINOR,
    normalize_decision_impact,
    should_interrupt,
)
from ai_core.architect.graph import ArchitectGraphRunner
from ai_core.architect.llm import MockArchitectLLM
from ai_core.architect.service import _merge_adopted_answers
from ai_core.config import Settings
from ai_core.domain.architect import ArchitectAnswersRequest, ArchitectContract
from ai_core.domain.state import DecisionImpact
from ai_core.fullstack.coders import (
    generate_backend_bundle,
    generate_frontend_bundle,
    generate_infra_bundle,
)
from ai_core.skill_loader import load_agent_skill_prompts, read_skill_file

ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "config" / "skills"
MANIFEST = ROOT / "ai_core" / "backend_coder" / "manifest.json"

DDL = (
    "CREATE TABLE resumes ("
    "id BIGINT PRIMARY KEY AUTO_INCREMENT, "
    "name VARCHAR(200) NOT NULL, "
    "file_path VARCHAR(500) NULL, "
    "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);"
)


def _contract(*, ai: bool, auth: bool = False) -> ArchitectContract:
    markdown = "# 简历筛选\n上传简历并由 AI 分析。"
    if auth:
        markdown += "\n用户登录校验，密码使用 Bcrypt 加密后入库。"
    return ArchitectContract.model_validate(
        {
            "contractId": "ctr_upgrade",
            "runId": "arc_upgrade",
            "appShape": "full_stack_system",
            "status": "CONFIRMED",
            "confidenceScore": 0.92,
            "contractMarkdown": markdown,
            "includeAiCapability": ai,
            "techStack": {
                "backend": "node_express",
                "database": "mysql",
                "databaseEngine": "mysql",
                "orm": "sequelize",
            },
            "apiRoutes": [
                {
                    "method": "GET",
                    "path": "/api/v1/resumes",
                    "response": "list",
                },
                {
                    "method": "POST",
                    "path": "/api/v1/resumes",
                    "request": "CreateResume",
                    "response": "Resume",
                },
            ],
            "databaseDdl": DDL,
            "componentInteractions": [
                {
                    "component": "Upload",
                    "event": "upload",
                    "apiRoute": "POST /api/v1/resumes",
                    "logic": "上传简历",
                }
            ],
        }
    )


def _existing_frontend() -> dict[str, str | None]:
    return {
        "existing_main": (
            "import React from 'react'\n"
            "import { createRoot } from 'react-dom/client'\n"
            "import App from './App.jsx'\n"
            "createRoot(document.getElementById('root')).render(<App />)\n"
        ),
        "existing_vite": (
            "import { defineConfig } from 'vite'\n"
            "export default defineConfig({ server: { port: 5173 } })\n"
        ),
        "existing_package": json.dumps(
            {"scripts": {"dev": "vite"}, "dependencies": {"react": "^18.0.0"}}
        ),
    }


class DecisionImpactTests(unittest.TestCase):
    def test_enum_and_aliases(self) -> None:
        self.assertEqual(DecisionImpact.CRITICAL.value, "CRITICAL")
        self.assertEqual(normalize_decision_impact("CORE_BUSINESS"), CRITICAL)
        self.assertEqual(normalize_decision_impact("IMPLEMENTATION_DETAIL"), MINOR)
        self.assertEqual(normalize_decision_impact("MAJOR"), MAJOR)
        self.assertIsNone(normalize_decision_impact("UNKNOWN"))

    def test_critical_change_interrupts_but_minor_never_does(self) -> None:
        self.assertTrue(
            should_interrupt(
                decision_impact=CRITICAL,
                needs_clarification=False,
                confidence_score=0.2,
                threshold=0.85,
            )
        )
        self.assertFalse(
            should_interrupt(
                decision_impact=MINOR,
                needs_clarification=True,
                confidence_score=0.0,
                threshold=0.99,
                mode="always",
            )
        )

    def test_major_only_interrupts_when_information_is_missing(self) -> None:
        self.assertTrue(
            should_interrupt(
                decision_impact=MAJOR,
                needs_clarification=True,
                confidence_score=0.1,
                threshold=0.9,
            )
        )
        self.assertFalse(
            should_interrupt(
                decision_impact=MAJOR,
                needs_clarification=False,
                confidence_score=0.1,
                threshold=0.9,
            )
        )


class ArchitectClarificationTests(unittest.TestCase):
    def test_default_and_hard_limit_are_ten_rounds(self) -> None:
        self.assertEqual(
            Settings(deepseek_api_key="test").architect_max_clarification_rounds,
            10,
        )
        self.assertEqual(
            ArchitectGraphRunner(MockArchitectLLM())._route_confidence(
                {
                    "adopt_suggestions": True,
                    "app_shape": "full_stack_system",
                    "missing_slots": ["data.entities"],
                    "confidence_score": 0.2,
                    "threshold": 0.85,
                    "round": 1,
                    "max_rounds": 10,
                }
            ),
            "draft_contract",
        )

    def test_adopt_suggestions_is_user_only_and_merges_missing_answers(self) -> None:
        self.assertFalse(ArchitectAnswersRequest(comment="继续").adopt_suggestions)
        self.assertTrue(
            ArchitectAnswersRequest(adopt_suggestions=True).adopt_suggestions
        )
        with self.assertRaises(ValidationError):
            ArchitectAnswersRequest()

        questions = [
            SimpleNamespace(
                question_id="q1",
                type="single_choice",
                default_value=None,
                options=[
                    {"value": "mysql", "label": "MySQL", "recommended": True},
                    {"value": "sqlite", "label": "SQLite"},
                ],
            ),
            SimpleNamespace(
                question_id="q2",
                type="single_choice",
                default_value="nextjs",
                options=[],
            ),
            SimpleNamespace(
                question_id="q3",
                type="entity_fields",
                default_value=None,
                options=[],
            ),
        ]
        merged = _merge_adopted_answers(
            questions,
            [{"questionId": "q1", "value": "postgres"}],
        )
        by_id = {item["questionId"]: item for item in merged}
        self.assertEqual(by_id["q1"]["value"], "postgres")
        self.assertNotIn("source", by_id["q1"])
        self.assertEqual(by_id["q2"]["value"], "nextjs")
        self.assertEqual(by_id["q2"]["source"], "USER_ADOPTED")
        self.assertIn("实体", by_id["q3"]["value"])
        self.assertEqual(by_id["q3"]["source"], "USER_ADOPTED")


class WebBestPracticesSkillTests(unittest.TestCase):
    def test_skill_file_is_valid_and_backend_mounted(self) -> None:
        descriptor = read_skill_file(SKILLS_DIR / "web_best_practices.skill.md")
        self.assertEqual(descriptor.skill_id, "web.best_practices")
        self.assertEqual(descriptor.agent_id, "backend_coder")
        self.assertIn("Bcrypt", descriptor.system_prompt or "")
        self.assertIn("SQL", descriptor.system_prompt or "")

        backend_prompts = "\n".join(
            load_agent_skill_prompts("backend_coder", SKILLS_DIR)
        )
        self.assertIn("RESTful API 设计", backend_prompts)
        self.assertIn("SQL 注入防护", backend_prompts)
        frontend_prompts = "\n".join(
            load_agent_skill_prompts("frontend_coder", SKILLS_DIR)
        )
        self.assertNotIn("web.best_practices", frontend_prompts)

    def test_manifest_marks_skill_as_mandatory(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        skills = {item["skill_id"]: item for item in manifest["skills"]}
        entry = skills["web.best_practices"]
        self.assertTrue(entry["metadata"]["mandatory"])
        self.assertEqual(entry["prompt_ref"], "web_best_practices.skill.md")

    def test_generated_auth_backend_uses_bcrypt_and_parameterized_fields(self) -> None:
        backend = generate_backend_bundle(_contract(ai=False, auth=True))
        security = backend.files["server/security.js"]
        self.assertIn("bcrypt.hash", security)
        self.assertIn("bcrypt.compare", security)
        self.assertIn("BCRYPT_ROUNDS = 12", security)
        self.assertIn("PASSWORD_LENGTH_INVALID", security)
        package = json.loads(backend.files["server/package.json"])
        self.assertIn("bcryptjs", package["dependencies"])
        self.assertTrue(backend.metadata["webBestPractices"]["bcrypt"])

    def test_non_auth_backend_does_not_add_security_noise(self) -> None:
        backend = generate_backend_bundle(_contract(ai=False, auth=False))
        self.assertNotIn("server/security.js", backend.files)
        package = json.loads(backend.files["server/package.json"])
        self.assertNotIn("bcryptjs", package["dependencies"])


class AiNativeAppTests(unittest.TestCase):
    def test_contract_carries_ai_flag_with_snake_and_camel_input(self) -> None:
        contract = _contract(ai=True)
        self.assertTrue(contract.include_ai_capability)
        payload = contract.model_dump(mode="json", by_alias=True)
        self.assertTrue(payload["includeAiCapability"])

    def test_ai_backend_generates_real_openai_calls_not_mock(self) -> None:
        backend = generate_backend_bundle(_contract(ai=True))
        source = backend.files["server/index.js"]
        self.assertIn("from 'openai'", source)
        self.assertIn("client.chat.completions.create", source)
        self.assertIn("client.responses.create", source)
        self.assertIn("/api/v1/ai/chat", source)
        self.assertIn("/api/v1/ai/analyze", source)
        self.assertIn("AI_NOT_CONFIGURED", source)
        self.assertNotIn("OPENAI_API_KEY = '", source)

        package = json.loads(backend.files["server/package.json"])
        self.assertIn("openai", package["dependencies"])
        manifest = json.loads(backend.files["server/api-routes.json"])
        routes = {(item["method"], item["path"]) for item in manifest["routes"]}
        self.assertIn(("POST", "/api/v1/ai/chat"), routes)
        self.assertIn(("POST", "/api/v1/ai/analyze"), routes)

    def test_ai_frontend_and_env_are_generated_together(self) -> None:
        contract = _contract(ai=True)
        frontend = generate_frontend_bundle(contract, **_existing_frontend())
        self.assertIn("src/ai-workspace.jsx", frontend.files)
        self.assertIn("AIWorkspace", frontend.files["src/main.jsx"])
        panel = frontend.files["src/ai-workspace.jsx"]
        self.assertIn("/api/v1/ai/chat", panel)
        self.assertIn("/api/v1/ai/analyze", panel)
        self.assertIn("data-source", panel)

        infra = generate_infra_bundle(contract)
        self.assertIn("OPENAI_API_KEY", infra.files[".env.example"])
        self.assertIn("OPENAI_MODEL", infra.files[".env.example"])
        self.assertIn("OPENAI_API_KEY", infra.files["DEPLOY.md"])

    def test_non_ai_project_has_no_ai_artifacts(self) -> None:
        contract = _contract(ai=False)
        backend = generate_backend_bundle(contract)
        self.assertNotIn("openai", backend.files["server/index.js"])
        package = json.loads(backend.files["server/package.json"])
        self.assertNotIn("openai", package["dependencies"])

        frontend = generate_frontend_bundle(contract, **_existing_frontend())
        self.assertNotIn("src/ai-workspace.jsx", frontend.files)
        infra = generate_infra_bundle(contract)
        self.assertNotIn("OPENAI_API_KEY", infra.files[".env.example"])


if __name__ == "__main__":
    unittest.main()
