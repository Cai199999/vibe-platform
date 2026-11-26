"""测试层：分级计划、HTTP/沙盒执行、结构化报告与失败回流。

作为平台小 Agent 注册（``test_agent``），但 ``dispatchable=false``，
不会被 Supervisor 自动派发去抢占 UI/Architect 请求；由编排层或 API
显式调用。
"""

from ai_core.testing.agent import TEST_AGENT, TestAgent
from ai_core.testing.models import (
    CheckResult,
    TestCheck,
    TestPlan,
    TestReport,
    TestRequest,
    TestStatus,
    TestTier,
)
from ai_core.testing.planner import build_plan
from ai_core.testing.runner import HttpTestRunner, TestRunner

__all__ = [
    "TEST_AGENT",
    "CheckResult",
    "HttpTestRunner",
    "TestAgent",
    "TestCheck",
    "TestPlan",
    "TestReport",
    "TestRequest",
    "TestRunner",
    "TestStatus",
    "TestTier",
    "build_plan",
]
