"""六边形架构边界测试。

验证三件事：
  1. ``ai_core.domain`` 只依赖标准库 / Pydantic，不泄漏任何框架或厂商 SDK；
  2. ``ai_core.ports`` 只描述抽象，不反向依赖 ``ai_core.adapters``；
  3. 适配器确实实现端口，且旧 import 路径仍指向同一对象（平滑迁移）。

运行：
    docker run --rm -i -v "$PWD:/app" -w /app \
      vibe-platform/python-ai:dev python -m unittest \
      tests.test_hexagonal_architecture -v
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from ai_core.adapters.checkpoint import MemoryCheckpointerProvider
from ai_core.adapters.editor import AnchorCodeEditor, FileCreator, TreeSitterEditor
from ai_core.adapters.model import DeepSeekClient
from ai_core.adapters.rag import ChromaRAGProvider, MarkdownRAGProvider
from ai_core.adapters.sandbox import LocalDockerSandbox
from ai_core.domain.agent import AnnotationEnvelope
from ai_core.domain.architect import ArchitectContract
from ai_core.domain.common import CamelModel
from ai_core.domain.skills import SkillDescriptor
from ai_core.domain.state import AgentState, ArchitectState
from ai_core.ports.checkpoint import CheckpointerProvider
from ai_core.ports.editor import CodeEditor
from ai_core.ports.model import ModelClient
from ai_core.ports.rag import RAGProvider
from ai_core.ports.sandbox import SandboxProvider

AI_CORE = Path(__file__).resolve().parents[1] / "ai_core"

FORBIDDEN_IN_DOMAIN = {
    "docker",
    "fastapi",
    "httpx",
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langgraph",
    "minio",
    "openai",
    "redis",
    "sqlalchemy",
}
# 领域层只能依赖自身与标准库 / Pydantic，不能反向依赖应用层或适配层。
OUTER_LAYERS = {
    "ai_core.adapters",
    "ai_core.agent",
    "ai_core.api",
    "ai_core.architect",
    "ai_core.editor",
    "ai_core.ports",
    "ai_core.rag",
    "ai_core.sandbox",
    "ai_core.supervisor",
}


def _imports(path: Path) -> set[str]:
    """返回模块级与函数级 import 的完整模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _matches(module: str, roots: set[str]) -> bool:
    return any(module == root or module.startswith(root + ".") for root in roots)


class DomainPurityTest(unittest.TestCase):
    """领域层必须是可以脱离框架单独测试的纯模型。"""

    def test_domain_has_no_framework_or_vendor_sdk(self) -> None:
        violations: list[str] = []
        for path in sorted((AI_CORE / "domain").glob("*.py")):
            for module in _imports(path):
                if module.split(".", 1)[0] in FORBIDDEN_IN_DOMAIN:
                    violations.append(f"{path.name}: {module}")
        self.assertEqual(violations, [], f"领域层出现外部依赖: {violations}")

    def test_domain_does_not_import_outer_layers(self) -> None:
        violations: list[str] = []
        for path in sorted((AI_CORE / "domain").glob("*.py")):
            for module in _imports(path):
                if module.startswith("ai_core.") and not module.startswith(
                    "ai_core.domain"
                ):
                    violations.append(f"{path.name}: {module}")
                elif _matches(module, OUTER_LAYERS):
                    violations.append(f"{path.name}: {module}")
        self.assertEqual(violations, [], f"领域层反向依赖外层: {violations}")


class PortBoundaryTest(unittest.TestCase):
    """端口只定义契约，不依赖具体适配器。"""

    def test_ports_do_not_import_adapters(self) -> None:
        violations: list[str] = []
        for path in sorted((AI_CORE / "ports").glob("*.py")):
            for module in _imports(path):
                if module.startswith("ai_core.adapters"):
                    violations.append(f"{path.name}: {module}")
                if module.startswith(("docker", "fastapi", "langchain")):
                    violations.append(f"{path.name}: {module}")
        self.assertEqual(violations, [], f"端口层出现实现依赖: {violations}")

    def test_model_client_requires_generate(self) -> None:
        self.assertEqual(ModelClient.__abstractmethods__, {"generate"})

    def test_adapters_implement_ports(self) -> None:
        self.assertTrue(issubclass(DeepSeekClient, ModelClient))
        self.assertTrue(issubclass(LocalDockerSandbox, SandboxProvider))
        self.assertTrue(issubclass(MarkdownRAGProvider, RAGProvider))
        self.assertTrue(issubclass(ChromaRAGProvider, RAGProvider))
        # Editor 与 Checkpointer 使用 Protocol 结构化校验，不强制继承。
        self.assertIsInstance(AnchorCodeEditor(), CodeEditor)
        self.assertIsInstance(FileCreator(), CodeEditor)
        self.assertIsInstance(TreeSitterEditor(), CodeEditor)
        self.assertIsInstance(MemoryCheckpointerProvider(), CheckpointerProvider)


class CompatibilityShimTest(unittest.TestCase):
    """旧路径必须仍然可用，且与领域层是同一对象。"""

    def test_legacy_schema_paths_point_to_domain(self) -> None:
        from ai_core.agent.schemas import AnnotationEnvelope as LegacyEnvelope
        from ai_core.architect.schemas import ArchitectContract as LegacyContract

        self.assertIs(LegacyEnvelope, AnnotationEnvelope)
        self.assertIs(LegacyContract, ArchitectContract)

    def test_legacy_state_paths_point_to_domain(self) -> None:
        from ai_core.agent.state import AgentState as LegacyAgentState
        from ai_core.architect.state import ArchitectState as LegacyArchitectState

        self.assertIs(LegacyAgentState, AgentState)
        self.assertIs(LegacyArchitectState, ArchitectState)

    def test_legacy_provider_paths_point_to_ports_and_adapters(self) -> None:
        from ai_core.editor.anchor import AnchorCodeEditor as LegacyAnchor
        from ai_core.rag.base import RAGProvider as LegacyRAGProvider
        from ai_core.sandbox.base import SandboxProvider as LegacySandboxProvider
        from ai_core.sandbox.local_docker import LocalDockerSandbox as LegacySandbox

        self.assertIs(LegacyRAGProvider, RAGProvider)
        self.assertIs(LegacySandboxProvider, SandboxProvider)
        self.assertIs(LegacySandbox, LocalDockerSandbox)
        self.assertIs(LegacyAnchor, AnchorCodeEditor)

    def test_domain_objects_are_shared_language(self) -> None:
        self.assertTrue(issubclass(AnnotationEnvelope, CamelModel))
        self.assertTrue(issubclass(ArchitectContract, CamelModel))
        self.assertEqual(
            SkillDescriptor.__dataclass_fields__["skill_id"].name, "skill_id"
        )


if __name__ == "__main__":
    unittest.main()
