"""方案侦察（澄清前的复用 / 自研选择）确定性测试。

不访问网络、不调用真实模型：GitHub 检索与 ModelClient 都用注入替身。
"""

from __future__ import annotations

import json
import unittest

from ai_core.architect.discovery import (
    ReuseCandidate,
    ReuseSearchError,
    analyze_requirement,
    build_options,
    discover_solutions,
    search_local,
)
from ai_core.architect.llm import ArchitectContext, _analysis_human, _contract_human
from ai_core.config import Settings
from ai_core.domain.architect import (
    ArchitectRunRequest,
    SolutionDiscoveryRequest,
)


def _settings(**overrides) -> Settings:
    values = {"deepseek_api_key": "", "reuse_search_provider": "auto"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeModel:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def generate(self, task: str, system: str, human: str) -> str:
        self.calls.append(task)
        return json.dumps(self.payload, ensure_ascii=False)

    def generate_json(self, task: str, system: str, human: str, *, json_schema=None) -> str:
        return self.generate(task, system, human)


class SolutionDiscoveryTests(unittest.TestCase):
    def test_rule_analysis_detects_gis_capabilities(self) -> None:
        brief = analyze_requirement(
            "做一个可上线的 GIS 任务管理平台，支持地图图层、空间查询、"
            "要素编辑、影像上传和任务工单",
            "GIS 平台",
        )
        self.assertEqual(brief.domain, "GIS / 地理信息")
        self.assertIn("地图渲染与图层管理", brief.capabilities)
        self.assertIn("空间查询与分析", brief.capabilities)
        self.assertIn("要素绘制与编辑", brief.capabilities)
        self.assertIn("需要可上线部署", brief.constraints)
        self.assertTrue(any("gis" in query for query in brief.search_queries))
        self.assertEqual(brief.source, "rules")

    def test_generic_profile_fallback_has_no_domain_hardcoding(self) -> None:
        brief = analyze_requirement(
            "做一个团队任务管理后台，成员登录后创建任务并指派负责人",
            "任务后台",
        )
        self.assertEqual(brief.profile_id, "generic")
        self.assertEqual(brief.domain, "通用 Web 应用")
        self.assertTrue(brief.profile is not None)
        self.assertTrue(brief.profile.is_generic)
        self.assertNotIn("地图渲染与图层管理", brief.capabilities)

    def test_geoai_profile_is_loaded_from_config(self) -> None:
        brief = analyze_requirement("做一个 GIS 地图平台，支持图层和空间查询", "GIS 平台")
        self.assertEqual(brief.profile_id, "geoai")
        self.assertEqual(brief.domain, "GIS / 地理信息")
        self.assertTrue(brief.profile.local_catalog.endswith("repos.json"))

    def test_local_search_ranks_geoai_candidates(self) -> None:
        brief = analyze_requirement(
            "做一个 GIS 地图平台，支持图层和空间查询", "GIS 平台"
        )
        candidates = search_local(
            brief.search_queries, path=brief.profile.local_catalog
        )
        names = [candidate.full_name for candidate in candidates]
        self.assertIn("opengeos/geoai", names)
        self.assertIn("decision-labs/geoai.js", names)

    def test_build_options_always_includes_build_and_respects_limit(self) -> None:
        brief = analyze_requirement("做一个 GIS 地图平台", "GIS 平台")
        candidates = search_local(
            brief.search_queries, path=brief.profile.local_catalog
        )
        options = build_options(brief, candidates, max_options=3)
        self.assertLessEqual(len(options), 3)
        self.assertEqual(options[-1].kind, "BUILD")
        self.assertEqual(options[-1].option_id, "opt_build")
        self.assertTrue(all(option.fit_score >= 0 for option in options))

    def test_discover_local_provider_returns_selection(self) -> None:
        request = SolutionDiscoveryRequest(
            message="做一个可上线的 GIS 任务管理平台，支持地图和空间查询",
            projectName="GIS 平台",
            provider="local",
        )
        response = discover_solutions(request, settings=_settings())
        self.assertFalse(response.degraded)
        self.assertTrue(response.options)
        self.assertEqual(response.options[-1].kind, "BUILD")
        self.assertTrue(response.recommended_option_id)
        self.assertIn("local", response.provider)

    def test_github_failure_falls_back_to_local(self) -> None:
        local_candidate = ReuseCandidate(
            full_name="opengeos/geoai",
            url="https://github.com/opengeos/geoai",
            description="GIS library",
            stars=100,
            language="Python",
            license="MIT",
            topics=("gis",),
            source="local",
        )

        def failing_github(_queries):
            raise ReuseSearchError("HTTP 403")

        request = SolutionDiscoveryRequest(
            message="做一个 GIS 地图平台", provider="auto"
        )
        response = discover_solutions(
            request,
            settings=_settings(),
            github_search=failing_github,
            local_search=lambda _queries: [local_candidate],
        )
        self.assertTrue(response.degraded)
        self.assertTrue(any("回退本地" in note for note in response.notes))
        self.assertTrue(any(option.kind == "REUSE" for option in response.options))

    def test_mock_provider_is_deterministic(self) -> None:
        request = SolutionDiscoveryRequest(
            message="做一个 GIS 地图平台", provider="mock"
        )
        first = discover_solutions(request, settings=_settings())
        second = discover_solutions(request, settings=_settings())
        self.assertEqual(
            [(option.option_id, option.kind) for option in first.options],
            [(option.option_id, option.kind) for option in second.options],
        )
        self.assertIn(
            first.recommended_option_id,
            {option.option_id for option in first.options if option.kind != "BUILD"},
        )

    def test_llm_analysis_merges_and_sanitizes_queries(self) -> None:
        model = FakeModel(
            {
                "domain": "地理信息平台",
                "capabilities": ["地图渲染", "空间查询"],
                "constraints": ["可上线"],
                "search_queries": ["leaflet map!!", "postgis spatial query"],
                "build_summary": "React + PostGIS 自研",
                "assumptions": ["默认 MySQL/PostGIS"],
            }
        )
        brief = analyze_requirement(
            "做一个 GIS 地图平台", "GIS 平台", model=model
        )
        self.assertEqual(brief.source, "llm")
        # 模型把领域写成泛化中文时，保留规则识别出的 GIS 领域，确保
        # 后续本地目录排序仍走 GIS 分支。
        self.assertIn("GIS", brief.domain)
        self.assertIn("leaflet map", brief.search_queries)
        # 规则检索词必须保留，不能被模型的泛化英文覆盖。
        self.assertTrue(any("geoai" in query for query in brief.search_queries))
        self.assertTrue(all("!" not in query for query in brief.search_queries))
        self.assertEqual(model.calls, ["solution_discovery"])

    def test_solution_choice_is_injected_into_prompts(self) -> None:
        context = ArchitectContext(
            project_id="prj_test",
            project_name="GIS 平台",
            source_files={},
            ui_history=[],
            slots={},
            app_shape="full_stack_system",
            round=1,
            threshold=0.85,
            solution_choice={
                "optionId": "opt_reuse_opengeos_geoai",
                "kind": "REUSE",
                "title": "opengeos/geoai",
                "sourceUrl": "https://github.com/opengeos/geoai",
                "license": "MIT",
                "summary": "GIS 库",
                "integrationPlan": "作为后端依赖",
            },
        )
        analysis_prompt = _analysis_human(context)
        contract_prompt = _contract_human(context)
        for prompt in (analysis_prompt, contract_prompt):
            self.assertIn("已确认方案", prompt)
            self.assertIn("opengeos/geoai", prompt)
            self.assertIn("不得静默替换", prompt)
        empty = ArchitectContext(
            project_id="prj_test",
            project_name="GIS 平台",
            source_files={},
            ui_history=[],
            slots={},
            app_shape="full_stack_system",
            round=1,
            threshold=0.85,
        )
        self.assertNotIn("已确认方案", _analysis_human(empty))

    def test_run_request_accepts_camel_case_solution_choice(self) -> None:
        request = ArchitectRunRequest.model_validate(
            {
                "projectId": "prj_test",
                "solutionChoice": {
                    "optionId": "opt_build",
                    "kind": "BUILD",
                    "title": "自研方案",
                    "integrationPlan": "按契约实现",
                },
            }
        )
        self.assertIsNotNone(request.solution_choice)
        self.assertEqual(request.solution_choice.kind, "BUILD")


if __name__ == "__main__":
    unittest.main()
