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

from ai_core.agent.graph import (
    AgentGraphRunner,
    _explicit_move_offset,
    _extract_research_query,
    _is_explicit_move_batch,
)
from ai_core.agent.llm import (
    DeepSeekAgentLLM,
    append_css_rule,
    is_move_only_instruction,
)
from ai_core.config import Settings
from ai_core.domain.agent import AnnotationEnvelope
from ai_core.mcp.client import McpServerConfig, McpStdioClient
from ai_core.skill_loader import (
    load_skill_directory,
    skill_prompts_to_system,
)
from ai_core.supervisor.tools import (
    SupervisorToolNotFoundError,
    SupervisorToolService,
)
from ai_core.tools import ToolRegistry, build_default_toolbox
from ai_core.tools.move_component_tool import (
    MoveComponentError,
    build_move_rule,
    build_move_tool,
)
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


class MoveComponentToolTest(unittest.TestCase):
    """Task 6：移动必须是标准 Tool，且只用 transform 预览/落盘。"""

    def test_rule_uses_transform_and_never_width_or_height(self) -> None:
        rule = build_move_rule("cta-button", 24, -12)
        self.assertIn('[data-source="cta-button"]', rule)
        self.assertIn("translate3d(24.00px,-12.00px,0)", rule)
        self.assertIn("will-change:transform", rule)
        self.assertNotIn("width", rule)
        self.assertNotIn("height", rule)

    def test_tool_accepts_selector_and_returns_json(self) -> None:
        payload = json.loads(
            build_move_tool().invoke(
                {"element_id": '[data-vibe-created="ann_1"]', "x": 10, "y": 20}
            )
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["strategy"], "css-transform")
        self.assertIn("translate3d(10.00px,20.00px,0)", payload["rule"])

    def test_rejects_injection_and_out_of_range_offsets(self) -> None:
        with self.assertRaises(MoveComponentError):
            build_move_rule("</style><script>alert(1)</script>", 0, 0)
        with self.assertRaises(MoveComponentError):
            build_move_rule("cta-button", 99999, 0)


class AgentMoveToolShortcutTest(unittest.TestCase):
    """纯移动必须由 AgentGraph 真正调用 Tool，而不是让模型自由写 CSS。"""

    APP = (
        "export default function App() {\n"
        "  return (\n"
        "    <main>\n"
        "      <style>{`.cta{color:#fff;}`}</style>\n"
        '      <button data-source="cta">开始</button>\n'
        "    </main>\n"
        "  )\n"
        "}\n"
    )

    def _runner(self) -> tuple[AgentGraphRunner, list[tuple]]:
        runner = AgentGraphRunner.__new__(AgentGraphRunner)
        runner._toolbox = build_default_toolbox(_settings())
        events: list[tuple] = []
        runner._emit = lambda event, payload, **kwargs: events.append(  # type: ignore[method-assign]
            (event, payload, kwargs)
        )
        return runner, events

    def test_pure_move_calls_move_component_and_only_writes_transform(self) -> None:
        runner, events = self._runner()
        annotation = {
            "annotationId": "ann_move_1",
            "action": "MODIFY_EXISTING",
            "target": {
                "selector": '[data-source="cta"]',
                "source": {"file": "src/App.jsx", "target": "cta"},
            },
            "payload": {"text": "把按钮向右移动 48px（只改位置，不要改宽高）"},
        }
        result = runner._try_move_tool(
            {"run_id": "run_move", "target_files": ["src/App.jsx"]},
            [annotation],
            {"src/App.jsx": self.APP},
            0,
        )

        self.assertIsNotNone(result)
        assert result is not None
        generated = result["generated_files"]["src/App.jsx"]
        self.assertIn("translate3d(48.00px,0.00px,0)", generated)
        self.assertIn('[data-source="cta"]', generated)
        # Tool 规则只允许 transform：不得出现宽高修改。
        self.assertNotIn("width:", generated)
        self.assertNotIn("height:", generated)
        progress = [
            payload["message"]
            for event, payload, _kwargs in events
            if event == "node.progress"
        ]
        self.assertTrue(any("move_component" in item for item in progress))
        self.assertEqual(result["status"], "CODING")

    def test_css_rule_does_not_break_jsx_expression_style(self) -> None:
        """生成骨架使用 ``<style>{GLOBAL_CSS}</style>``，不能把裸 CSS
        插到 ``</style>`` 前（那会变成 JSX 非法文本）。"""
        code = (
            "export default function App() {\n"
            "  return (\n"
            "    <main>\n"
            "      <style>{GLOBAL_CSS}</style>\n"
            '      <button data-source="cta">开始</button>\n'
            "    </main>\n"
            "  )\n"
            "}\n"
        )
        rule = '[data-source="cta"]{transform:translate3d(8.00px,0.00px,0) !important;}'
        updated = append_css_rule(code, rule, "src/App.jsx")
        self.assertIn("<style>{`" + rule + "`}</style>", updated)
        self.assertNotIn("GLOBAL_CSS} [data-source", updated)

    def test_mixed_move_request_falls_back_to_llm(self) -> None:
        runner, _events = self._runner()
        annotation = {
            "annotationId": "ann_move_2",
            "action": "MODIFY_EXISTING",
            "target": {
                "selector": '[data-source="cta"]',
                "source": {"file": "src/App.jsx", "target": "cta"},
            },
            "payload": {"text": "把按钮向右移动 48px 并改成红色"},
        }
        self.assertFalse(is_move_only_instruction(annotation["payload"]["text"]))
        self.assertIsNone(
            runner._try_move_tool(
                {"run_id": "run_move_2", "target_files": ["src/App.jsx"]},
                [annotation],
                {"src/App.jsx": self.APP},
                0,
            )
        )


class DirectDragMoveTest(unittest.TestCase):
    """直接拖拽：payload.move 是事实值，必须跳过 LLM 并生成累计位移。"""

    APP = AgentMoveToolShortcutTest.APP

    def _runner(self) -> tuple[AgentGraphRunner, list[tuple]]:
        """复用生产 Toolbox，但跳过完整 Graph 初始化。"""
        runner = AgentGraphRunner.__new__(AgentGraphRunner)
        runner._toolbox = build_default_toolbox(_settings())
        events: list[tuple] = []
        runner._emit = lambda event, payload, **kwargs: events.append(  # type: ignore[method-assign]
            (event, payload, kwargs)
        )
        return runner, events

    def test_move_envelope_is_accepted_and_keeps_raw_action(self) -> None:
        envelope = AnnotationEnvelope.model_validate(
            {
                "annotationId": "ann_drag_1",
                "type": "drag",
                "action": "MOVE",
                "target": {"selector": '[data-source="cta"]'},
                "payload": {"move": {"dx": 12, "dy": -6}},
            }
        )
        self.assertEqual(envelope.action, "MOVE")
        # 业务分支仍按“修改已有元素”处理，避免被当成新增。
        self.assertTrue(envelope.is_modify_existing)
        dumped = envelope.model_dump(mode="json")
        self.assertEqual(dumped["action"], "MOVE")
        self.assertEqual(dumped["payload"]["move"]["dx"], 12)

    def test_explicit_offset_requires_finite_bounded_numbers(self) -> None:
        base = {
            "action": "MOVE",
            "target": {"selector": '[data-source="cta"]'},
        }
        self.assertEqual(
            _explicit_move_offset({**base, "payload": {"move": {"dx": 3, "dy": -4}}}),
            (3.0, -4.0),
        )
        self.assertIsNone(
            _explicit_move_offset({**base, "payload": {"move": {"dx": "x", "dy": 0}}})
        )
        self.assertIsNone(
            _explicit_move_offset({**base, "payload": {"move": {"dx": 2001, "dy": 0}}})
        )
        self.assertIsNone(
            _explicit_move_offset(
                {
                    "action": "MODIFY_EXISTING",
                    "payload": {"move": {"dx": 3, "dy": 0}},
                }
            )
        )

    def test_explicit_move_batch_bypasses_planner(self) -> None:
        move = {
            "action": "MOVE",
            "target": {"selector": '[data-source="cta"]'},
            "payload": {"move": {"dx": 8, "dy": 4}},
        }
        self.assertTrue(_is_explicit_move_batch([move]))
        self.assertFalse(
            _is_explicit_move_batch(
                [
                    move,
                    {
                        "action": "MODIFY_EXISTING",
                        "target": {"selector": '[data-source="cta"]'},
                        "payload": {"text": "把按钮向右移动 8px"},
                    },
                ]
            )
        )
        self.assertFalse(
            _is_explicit_move_batch(
                [
                    {
                        "action": "MOVE",
                        "target": {},
                        "payload": {"move": {"dx": 8, "dy": 4}},
                    }
                ]
            )
        )

    def test_move_tool_uses_payload_offset_without_text(self) -> None:
        runner, events = self._runner()
        annotation = {
            "annotationId": "ann_drag_2",
            "action": "MOVE",
            "target": {
                "selector": '[data-source="cta"]',
                "source": {"file": "src/App.jsx", "target": "cta"},
            },
            # 刻意不给 payload.text：直接拖拽不依赖自然语言。
            "payload": {"move": {"dx": -32, "dy": 18}},
        }
        result = runner._try_move_tool(
            {"run_id": "run_drag", "target_files": ["src/App.jsx"]},
            [annotation],
            {"src/App.jsx": self.APP},
            0,
        )
        self.assertIsNotNone(result)
        assert result is not None
        generated = result["generated_files"]["src/App.jsx"]
        self.assertIn("translate3d(-32.00px,18.00px,0)", generated)
        self.assertNotIn("width:", generated)
        self.assertNotIn("height:", generated)
        progress = [
            payload["message"]
            for event, payload, _kwargs in events
            if event == "node.progress"
        ]
        self.assertTrue(any("move_component" in item for item in progress))

    def test_source_marker_selector_with_file_line_is_safe(self) -> None:
        """SDK 兜底选择器形如 ``[data-source="src/App.jsx:12:5"]``。"""
        rule = build_move_rule('[data-source="src/App.jsx:12:5"]', 10, 20)
        self.assertIn('[data-source="src/App.jsx:12:5"]', rule)
        self.assertIn("translate3d(10.00px,20.00px,0)", rule)


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

    def test_production_ready_skill_is_mounted_to_backend_and_infra(self) -> None:
        """同一份生产规范必须同时进入 BackendCoder 与 InfraCoder。"""
        skills = load_skill_directory(ACCESSIBILITY_DIR)
        by_id = {item.skill_id: item for item in skills}
        backend = by_id["production.ready"]
        infra = by_id["production.ready.devops_agent"]
        self.assertEqual(backend.agent_id, "backend_coder")
        self.assertEqual(infra.agent_id, "devops_agent")
        self.assertIn("SQL 迁移", backend.system_prompt)
        self.assertIn("多阶段构建", infra.system_prompt)
        self.assertIn("CORS", infra.system_prompt)
        self.assertIn(
            "生产级后端与部署基线",
            skill_prompts_to_system("backend_coder", ACCESSIBILITY_DIR),
        )
        self.assertIn(
            "生产级后端与部署基线",
            skill_prompts_to_system("devops_agent", ACCESSIBILITY_DIR),
        )

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
