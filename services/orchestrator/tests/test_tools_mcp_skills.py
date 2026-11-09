"""M3 扩展能力测试：真实 Tool / Markdown Skill / MCP stdio 往返。

运行（容器内依赖完整）：

    docker exec vibe-python-ai python -m unittest \
        tests.test_tools_mcp_skills -v

覆盖：
  1. ``search_web`` 的 DuckDuckGo 解析、跳转解包与禁用行为；
  2. Markdown Skill（YAML frontmatter + 正文 Prompt）加载与 ui_graph 注入；
  3. 标准 MCP stdio Server 的 initialize / tools/list / tools/call 往返；
  4. Supervisor Tool 网关的列表与真实调用（含 MCP 工具）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from ai_core.agent.graph import _extract_research_query
from ai_core.agent.llm import DeepSeekAgentLLM
from ai_core.config import Settings
from ai_core.mcp.client import McpServerConfig, McpStdioClient
from ai_core.skill_loader import (
    load_skill_directory,
    skill_prompts_to_system,
)
from ai_core.supervisor.tools import (
    SupervisorToolNotFoundError,
    SupervisorToolService,
)
from ai_core.tools import ToolRegistry
from ai_core.tools.web_search_tool import (
    parse_duckduckgo_html,
    register_web_search,
)

TESTS_DIR = Path(__file__).resolve().parent
ORCHESTRATOR_DIR = TESTS_DIR.parent
REPO_ROOT = ORCHESTRATOR_DIR.parent.parent
# 宿主机测试从仓库根目录找；容器内 /app 是服务根，脚本挂载在 /app/scripts。
_MCP_SCRIPT_CANDIDATES = (
    REPO_ROOT / "scripts" / "mcp_server_demo.py",
    ORCHESTRATOR_DIR / "scripts" / "mcp_server_demo.py",
    Path("/app/scripts/mcp_server_demo.py"),
)
MCP_SCRIPT = next(
    (path for path in _MCP_SCRIPT_CANDIDATES if path.is_file()),
    _MCP_SCRIPT_CANDIDATES[0],
)
ACCESSIBILITY_DIR = ORCHESTRATOR_DIR / "config" / "skills"


def _settings(**overrides: object) -> Settings:
    """隔离 .env / 环境变量，保证测试结果可重复。"""
    values = {
        "tool_shell_enabled": False,
        "mcp_servers": [],
        "tool_web_search_enabled": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class WebSearchParserTest(unittest.TestCase):
    """DuckDuckGo HTML 解析必须稳定、可离线验证。"""

    HTML = """
    <div class="result">
      <a class="result__a"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs%3Fv%3D1">
        Example <b>Docs</b>
      </a>
      <a class="result__snippet" href="https://example.com/docs?v=1">
        Official &amp; latest <b>documentation</b>.
      </a>
    </div>
    <div class="result">
      <a class="result-link" href="https://vuejs.org/guide/">Vue.js Guide</a>
      <td class="result-snippet">Vue 3 official guide.</td>
    </div>
    """

    def test_parse_results_and_unwrap_redirect(self) -> None:
        results = parse_duckduckgo_html(self.HTML, max_results=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "Example Docs")
        self.assertEqual(results[0]["url"], "https://example.com/docs?v=1")
        self.assertEqual(
            results[0]["snippet"],
            "Official & latest documentation.",
        )
        self.assertEqual(results[1]["url"], "https://vuejs.org/guide/")

    def test_max_results_is_respected(self) -> None:
        results = parse_duckduckgo_html(self.HTML, max_results=1)
        self.assertEqual(len(results), 1)

    def test_disabled_tool_returns_readable_message(self) -> None:
        settings = _settings(tool_web_search_enabled=False)
        registry = ToolRegistry()
        register_web_search(registry, settings)
        tool = registry.get("search_web")
        self.assertIsNotNone(tool)
        message = tool.invoke({"query": "vite docs"})
        self.assertIn("禁用", message)

    def test_empty_query_is_rejected_before_network(self) -> None:
        registry = ToolRegistry()
        register_web_search(registry, _settings())
        message = registry.get("search_web").invoke({"query": "   "})
        self.assertIn("不能为空", message)


class MarkdownSkillTest(unittest.TestCase):
    """Markdown Skill 的 frontmatter 与正文注入。"""

    def test_temp_markdown_skill_loads_as_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.skill.md"
            path.write_text(
                """---
skillId: ui.demo
name: Demo Skill
agentId: ui_graph
enabled: true
priority: 10
---
# 规则
请确保生成的代码符合 WCAG 2.1 AA。
""",
                encoding="utf-8",
            )
            skills = load_skill_directory(tmp)
            self.assertEqual([item.skill_id for item in skills], ["ui.demo"])
            descriptor = skills[0]
            self.assertEqual(descriptor.agent_id, "ui_graph")
            self.assertIn("WCAG", descriptor.system_prompt or "")
            self.assertEqual(
                descriptor.metadata["source"],
                "skill_markdown",
            )
            self.assertEqual(descriptor.prompt_ref, "demo.skill.md")
            merged = skill_prompts_to_system("ui_graph", tmp)
            self.assertIn("WCAG 2.1 AA", merged)

    def test_markdown_without_frontmatter_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.skill.md"
            path.write_text("# 没有 frontmatter\n", encoding="utf-8")
            self.assertEqual(load_skill_directory(tmp), [])

    def test_accessibility_skill_is_mounted_to_ui_graph(self) -> None:
        skills = load_skill_directory(ACCESSIBILITY_DIR)
        by_id = {item.skill_id: item for item in skills}
        self.assertIn("ui.accessibility", by_id)
        descriptor = by_id["ui.accessibility"]
        self.assertEqual(descriptor.agent_id, "ui_graph")
        self.assertIn("WCAG", descriptor.system_prompt or "")
        merged = skill_prompts_to_system("ui_graph", ACCESSIBILITY_DIR)
        self.assertIn("WCAG", merged)
        # 必须实际挂到 ui_graph，而不是只被目录扫描到。
        self.assertNotIn(merged, ("", None))


class RuntimeInjectionTest(unittest.TestCase):
    """Skill 与 Tool 必须进入运行时，而不只是单元层可读。"""

    def test_deepseek_llm_injects_accessibility_skill(self) -> None:
        llm = DeepSeekAgentLLM(
            _settings(
                deepseek_api_key="test-key",
                skills_dir=str(ACCESSIBILITY_DIR),
            )
        )
        self.assertIn("WCAG", llm._skill_system)

    def test_research_query_is_extracted_from_annotation(self) -> None:
        context = {
            "annotations": [
                {
                    "annotation": {
                        "intent": {
                            "ui": {
                                "summary": "查一下 Vite 最新版本并升级依赖",
                            }
                        }
                    }
                }
            ]
        }
        self.assertEqual(
            _extract_research_query(context),
            "查一下 Vite 最新版本并升级依赖",
        )

    def test_research_not_triggered_for_plain_ui_edit(self) -> None:
        context = {
            "annotations": [
                {"annotation": {"intent": {"ui": {"summary": "把按钮改成蓝色"}}}}
            ]
        }
        self.assertIsNone(_extract_research_query(context))


class McpStdioRoundTripTest(unittest.TestCase):
    """真实子进程 + JSON-RPC，不 mock subprocess。"""

    def _config(self, **overrides: object) -> McpServerConfig:
        values = {
            "name": "vibe_demo",
            "command": sys.executable,
            "args": (str(MCP_SCRIPT),),
            "timeout_seconds": 10.0,
        }
        values.update(overrides)
        return McpServerConfig(**values)

    def test_list_and_call_get_current_time(self) -> None:
        with McpStdioClient(self._config()) as client:
            tools = client.list_tools()
            names = {item["name"] for item in tools}
            self.assertIn("get_current_time", names)
            result = client.call_tool(
                "get_current_time",
                {"timezone": "Asia/Shanghai"},
            )
            self.assertFalse(result["isError"])
            payload = json.loads(result["text"])
            self.assertEqual(payload["timezone"], "Asia/Shanghai")
            self.assertRegex(payload["date"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertIn("iso", payload)

    def test_invalid_timezone_returns_tool_error(self) -> None:
        with McpStdioClient(self._config()) as client:
            result = client.call_tool(
                "get_current_time",
                {"timezone": "Mars/Olympus"},
            )
            self.assertTrue(result["isError"])
            self.assertIn("未知时区", result["text"])

    def test_unknown_tool_is_isolated(self) -> None:
        with McpStdioClient(self._config()) as client:
            result = client.call_tool("missing_tool", {})
            self.assertTrue(result["isError"])


class SupervisorToolGatewayTest(unittest.TestCase):
    """Supervisor 网关必须能看见并调用真实 MCP Tool。"""

    def setUp(self) -> None:
        self.settings = _settings(
            mcp_servers=[
                {
                    "name": "vibe_demo",
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(MCP_SCRIPT)],
                    "cwd": str(REPO_ROOT),
                    "enabled": True,
                    "allowed_tools": ["get_current_time"],
                }
            ]
        )
        self.service = SupervisorToolService(self.settings)

    def test_list_contains_local_and_mcp_tools(self) -> None:
        views, servers = self.service.list_tools()
        names = {item["name"] for item in views}
        self.assertIn("search_web", names)
        self.assertIn("mcp_vibe_demo_get_current_time", names)
        self.assertEqual(servers, ["vibe_demo"])
        mcp_view = next(
            item for item in views if item["name"] == "mcp_vibe_demo_get_current_time"
        )
        self.assertEqual(mcp_view["dangerLevel"], "external")
        self.assertTrue(mcp_view["metadata"]["mcp"])

    def test_invoke_mcp_tool_returns_time(self) -> None:
        payload = self.service.invoke_tool(
            "mcp_vibe_demo_get_current_time",
            {"timezone": "UTC"},
        )
        self.assertEqual(payload["tool"], "mcp_vibe_demo_get_current_time")
        self.assertGreaterEqual(payload["duration_ms"], 0)
        body = json.loads(payload["result"])
        self.assertEqual(body["timezone"], "UTC")

    def test_unknown_tool_raises_not_found(self) -> None:
        with self.assertRaises(SupervisorToolNotFoundError):
            self.service.invoke_tool("does_not_exist", {})


if __name__ == "__main__":
    unittest.main()
