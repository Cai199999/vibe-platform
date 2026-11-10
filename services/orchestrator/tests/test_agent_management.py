"""AgentBuilder 与自定义 Agent 知识库测试。

全部使用临时目录，不依赖 Docker、MySQL 或模型密钥；重点验证：
  1. 需求 → Manifest / Prompt / 知识目录 的落盘结果可被 AgentRegistry 加载；
  2. 知识文件只允许 .md/.txt，且写入后 Manifest 清单同步更新；
  3. 删除只作用于受管目录，不会误删内置 Agent 或越界文件。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_core.supervisor.agent_builder import AgentBuilder, AgentBuilderError
from ai_core.supervisor.registry import AgentRegistry


class AgentBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.base = Path(self._temp.name)
        self.builder = AgentBuilder(custom_agents_dir=self.base)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_create_generates_loadable_manifest_and_prompt(self) -> None:
        built = self.builder.create(
            requirement="审查采购合同中的付款条款与违约责任",
            display_name="合同审查助手",
        )
        self.assertTrue(built.manifest_path.is_file())
        self.assertTrue(built.prompt_path.is_file())
        self.assertTrue(built.knowledge_dir.is_dir())
        self.assertRegex(built.manifest.agent_id, r"^[a-z][a-z0-9_-]{2,63}$")

        payload = json.loads(built.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["displayName"], "合同审查助手")
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["taskTypes"], [f"{built.manifest.agent_id}_task"])
        self.assertIn("审查采购合同", built.prompt_path.read_text(encoding="utf-8"))

        # 注册中心重新扫描后应能识别该自定义 Agent。
        registry = AgentRegistry(custom_agents_dir=self.base)
        registry.reload_custom_agents()
        descriptor = registry.get(built.manifest.agent_id)
        self.assertIsNotNone(descriptor)
        assert descriptor is not None
        self.assertEqual(descriptor.source, "manifest")
        self.assertEqual(descriptor.status, "custom")

    def test_create_rejects_duplicate_agent_id(self) -> None:
        self.builder.create(requirement="整理每周运营数据", display_name="运营周报")
        with self.assertRaises(AgentBuilderError) as ctx:
            self.builder.create(
                requirement="再建一个同名 Agent", display_name="运营周报"
            )
        self.assertEqual(ctx.exception.code, "AGENT_ALREADY_EXISTS")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_knowledge_upload_and_manifest_sync(self) -> None:
        built = self.builder.create(
            requirement="根据内部制度回答差旅报销问题",
            display_name="差旅制度助手",
        )
        document = self.builder.save_knowledge(
            built.manifest.agent_id,
            "报销制度.md",
            "# 差旅报销\n\n机票需在 7 天内提交。\n",
        )
        self.assertEqual(document.file_name, "报销制度.md")
        self.assertGreater(document.size_bytes, 0)

        documents = self.builder.list_knowledge(built.manifest.agent_id)
        self.assertEqual([item.file_name for item in documents], ["报销制度.md"])

        payload = json.loads(built.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["metadata"]["knowledgeFiles"], ["报销制度.md"])

        removed = self.builder.delete_knowledge(
            built.manifest.agent_id,
            "报销制度.md",
        )
        self.assertEqual(removed["removed"], "报销制度.md")
        self.assertEqual(self.builder.list_knowledge(built.manifest.agent_id), [])

    def test_knowledge_rejects_unsafe_name_and_extension(self) -> None:
        built = self.builder.create(
            requirement="整理产品 FAQ",
            display_name="Product FAQ Bot",
        )
        for name in ["../secret.md", "notes.exe", ".hidden.md", "a/b.md"]:
            with self.assertRaises(AgentBuilderError):
                self.builder.save_knowledge(
                    built.manifest.agent_id,
                    name,
                    "payload",
                )

    def test_delete_removes_managed_artifacts_only(self) -> None:
        built = self.builder.create(
            requirement="生成竞品分析摘要",
            display_name="Competitor Analyst",
        )
        self.builder.save_knowledge(
            built.manifest.agent_id,
            "竞品清单.txt",
            "A 公司 / B 公司",
        )
        # 受管目录里的无关文件不应被删除。
        unrelated = self.base / "unrelated.md"
        unrelated.write_text("keep me", encoding="utf-8")
        result = self.builder.delete(built.manifest.agent_id)
        self.assertEqual(result["agentId"], built.manifest.agent_id)
        self.assertFalse(built.manifest_path.exists())
        self.assertFalse(built.prompt_path.exists())
        self.assertFalse(built.knowledge_dir.exists())
        self.assertTrue(unrelated.exists())

    def test_delete_supports_relative_managed_dir(self) -> None:
        """生产配置使用相对路径，删除时不能因 relative_to 抛 ValueError。"""
        with tempfile.TemporaryDirectory(dir=".") as temp:
            builder = AgentBuilder(custom_agents_dir=Path(temp).name)
            built = builder.create(
                requirement="整理渠道周报",
                display_name="Channel Reporter",
            )
            result = builder.delete(built.manifest.agent_id)
            self.assertEqual(result["agentId"], built.manifest.agent_id)
            self.assertFalse(built.manifest_path.exists())
            self.assertFalse(built.knowledge_dir.exists())

    def test_delete_unknown_agent_is_not_found(self) -> None:
        with self.assertRaises(AgentBuilderError) as ctx:
            self.builder.delete("custom_missing_agent")
        self.assertEqual(ctx.exception.status_code, 404)


class InteractionStatesSkillTest(unittest.TestCase):
    def test_skill_registers_for_ui_graph(self) -> None:
        from ai_core.skill_loader import load_skill_directory

        root = Path(__file__).resolve().parents[1] / "config" / "skills"
        skills = {item.skill_id: item for item in load_skill_directory(root)}
        self.assertIn("ui.interaction_states", skills)
        skill = skills["ui.interaction_states"]
        self.assertEqual(skill.agent_id, "ui_graph")
        self.assertIn("Loading", skill.system_prompt)
        self.assertIn("Error", skill.system_prompt)


if __name__ == "__main__":
    unittest.main()
