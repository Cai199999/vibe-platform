"""确定性测试计划生成。

不依赖模型：从契约（apiRoutes / databaseDdl / 认证路由）推导 L1/L2/L3
检查项。这样同一份契约每次生成同一份计划，便于回归对比与问题归因。
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any

from ai_core.testing.models import TestCheck, TestPlan, TestRequest

_ADVISORY = {
    "L1": "L1 仅验证页面可打开与基础结构，仅供参考，不代表业务可用。",
    "L2": "L2 为自动业务验收，仅供参考；人工接受率仍需单独确认。",
    "L3": "L3 需要生产仿真沙盒（构建产物 + 迁移 + 权限 + 故障）；未接入时只输出计划。",
}

_USERS_LOGIN_HINTS = ("/login", "/auth/login", "/users/login")


def _dig(data: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(data, dict):
        return default
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def _stable(prefix: str, seed: str) -> str:
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _routes(contract: dict[str, Any]) -> list[dict[str, Any]]:
    routes = _dig(contract, "apiRoutes", "api_routes", default=[])
    return [item for item in routes or [] if isinstance(item, dict)]


def _path(route: dict[str, Any]) -> str:
    return str(_dig(route, "path", default="") or "").strip()


def _method(route: dict[str, Any]) -> str:
    return str(_dig(route, "method", default="GET") or "GET").upper()


def login_route(contract: dict[str, Any]) -> dict[str, Any] | None:
    for route in _routes(contract):
        path = _path(route).lower()
        if any(hint in path for hint in _USERS_LOGIN_HINTS):
            return route
    return None


def core_resource(contract: dict[str, Any]) -> str:
    """挑选一个同时有 GET/POST 的集合根路径作为 CRUD 目标。"""
    candidates: list[str] = []
    for route in _routes(contract):
        path = _path(route)
        if not path or "{" in path or _method(route) != "GET":
            continue
        lowered = path.lower()
        if any(hint in lowered for hint in ("/health", "/login", "/register", "/me")):
            continue
        has_post = any(
            _method(item) == "POST" and _path(item) == path
            for item in _routes(contract)
        )
        if has_post:
            candidates.append(path)
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (len(item.split("/")), len(item), item))
    return candidates[0]


def _table_for_resource(contract: dict[str, Any], resource: str) -> str:
    segment = resource.rstrip("/").split("/")[-1].replace("-", "_").lower()
    ddl = str(_dig(contract, "databaseDdl", "database_ddl", default="") or "")
    tables = [
        match.group(1).lower()
        for match in re.finditer(
            r"create\s+table\s+(?:if\s+not\s+exists\s+)?`?(\w+)`?", ddl, re.I
        )
    ]
    if not tables:
        return ""
    for table in tables:
        if table == segment or table.rstrip("s") == segment.rstrip("s"):
            return table
    for table in tables:
        if segment in table or table in segment:
            return table
    return tables[0]


def _sample_value(column: str, column_type: str) -> Any:
    lowered = column.lower()
    if any(token in lowered for token in ("email", "mail")):
        return "tester@example.com"
    if any(token in lowered for token in ("phone", "mobile")):
        return "13800000000"
    if any(token in lowered for token in ("status", "state")):
        return "ACTIVE"
    if any(token in lowered for token in ("title", "name", "subject")):
        return f"自动测试 {datetime.now(timezone.utc).strftime('%H%M%S')}"
    if "int" in column_type.lower():
        return 1
    if any(
        token in column_type.lower()
        for token in ("decimal", "numeric", "float", "double")
    ):
        return 1.0
    if "date" in column_type.lower() or "time" in column_type.lower():
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if "bool" in column_type.lower():
        return True
    return f"auto-{column}"


def derive_sample_payload(contract: dict[str, Any], resource: str) -> dict[str, Any]:
    """从 DDL 推导可在集合根 POST 的最小请求体。

    跳过主键 / 时间戳 / 租户 / 外键列；其余 NOT NULL 无默认值的列给示例值。
    推导失败时返回空字典，由测试如实失败并回流 Architect/Coder。
    """
    table = _table_for_resource(contract, resource)
    if not table:
        return {}
    ddl = str(_dig(contract, "databaseDdl", "database_ddl", default="") or "")
    match = re.search(
        rf"create\s+table\s+(?:if\s+not\s+exists\s+)?`?{re.escape(table)}`?\s*\((.*?)\)\s*engine",
        ddl,
        re.I | re.S,
    )
    if not match:
        match = re.search(
            rf"create\s+table\s+(?:if\s+not\s+exists\s+)?`?{re.escape(table)}`?\s*\((.*?)\)\s*;",
            ddl,
            re.I | re.S,
        )
    if not match:
        return {}
    payload: dict[str, Any] = {}
    for raw in match.group(1).splitlines():
        line = raw.strip().rstrip(",")
        if not line or line.startswith(
            ("primary", "key ", "unique", "index", "constraint", "foreign")
        ):
            continue
        column_match = re.match(r"`?(\w+)`?\s+([a-zA-Z]+(?:\([^)]*\))?)", line)
        if not column_match:
            continue
        column = column_match.group(1)
        column_type = column_match.group(2)
        lowered = column.lower()
        if lowered in {"id", "created_at", "updated_at", "deleted_at", "version"}:
            continue
        if lowered == "tenant_id" or lowered.endswith("_id"):
            continue
        if re.search(r"default\s+", line, re.I):
            continue
        payload[column] = _sample_value(column, column_type)
    return payload


def ui_form_path(contract: dict[str, Any]) -> str:
    """挑选最可能存在创建表单的前端路由。"""
    routes = _dig(contract, "frontendRoutes", "frontend_routes", default=[])
    paths = [
        str(item.get("path") or "").strip()
        for item in routes or []
        if isinstance(item, dict)
    ]
    candidates = [
        path
        for path in paths
        if path
        and path != "/"
        and not any(token in path.lower() for token in ("login", "register", "auth"))
    ]
    for path in candidates:
        if any(token in path.lower() for token in ("new", "create", "edit", "submit")):
            return path
    return candidates[0] if candidates else "/"


def _api_base(request: TestRequest) -> str:
    base = request.api_base_url or request.preview_url
    return base.rstrip("/")


def build_plan(request: TestRequest) -> TestPlan:
    """按 tier 生成确定性检查计划。"""
    plan_seed = "|".join(
        [
            request.source_run_id,
            request.project_id,
            request.contract_id,
            request.tier,
            request.preview_url,
            request.api_base_url,
        ]
    )
    plan_id = _stable("tplan", plan_seed)
    checks: list[TestCheck] = []
    login = login_route(request.contract)
    resource = core_resource(request.contract)

    if request.tier in {"L1", "L2", "L3"}:
        checks.append(
            TestCheck(
                check_id=_stable("chk", f"{plan_seed}:preview"),
                tier="L1",
                kind="preview_shell",
                description="预览返回 200，且包含 React 根节点",
                target=request.preview_url,
                metadata={"timeoutSeconds": request.timeout_seconds},
            )
        )

    if request.tier in {"L2", "L3"}:
        if login:
            checks.append(
                TestCheck(
                    check_id=_stable("chk", f"{plan_seed}:login"),
                    tier="L2",
                    kind="login",
                    description="使用测试账号登录并获取 JWT",
                    target=_path(login),
                    metadata={
                        "method": _method(login),
                        "apiBase": _api_base(request),
                    },
                )
            )
        if resource:
            sample = dict(request.sample_payload or {})
            if not sample:
                sample = derive_sample_payload(request.contract, resource)
            checks.append(
                TestCheck(
                    check_id=_stable("chk", f"{plan_seed}:crud"),
                    tier="L2",
                    kind="crud_flow",
                    description=f"对 {resource} 执行 创建→查询→更新→删除 闭环",
                    target=resource,
                    metadata={
                        "apiBase": _api_base(request),
                        "samplePayload": sample,
                    },
                )
            )
        runner_url = (
            request.ui_runner_url or os.environ.get("TEST_UI_RUNNER_URL") or ""
        ).strip()
        if runner_url:
            ui_path = ui_form_path(request.contract)
            checks.append(
                TestCheck(
                    check_id=_stable("chk", f"{plan_seed}:ui"),
                    tier="L2",
                    kind="ui_flow",
                    description=("浏览器打开生成应用，填写表单并提交，刷新后数据仍在"),
                    target=ui_path,
                    metadata={
                        "runnerUrl": runner_url.rstrip("/"),
                        "path": ui_path,
                        "loginPath": _path(login) if login else "",
                    },
                )
            )

    if request.tier == "L3":
        checks.append(
            TestCheck(
                check_id=_stable("chk", f"{plan_seed}:production"),
                tier="L3",
                kind="production_artifact",
                description="构建产物、正式迁移、权限与故障验收",
                target=request.preview_url,
                metadata={"requires": "verify-sandbox"},
            )
        )

    if not checks:
        checks.append(
            TestCheck(
                check_id=_stable("chk", f"{plan_seed}:fallback"),
                tier="L1",
                kind="preview_shell",
                description="预览返回 200，且包含 React 根节点",
                target=request.preview_url,
            )
        )

    return TestPlan(
        plan_id=plan_id,
        source_run_id=request.source_run_id,
        project_id=request.project_id,
        contract_id=request.contract_id,
        tier=request.tier,
        checks=checks,
        advisory=_ADVISORY[request.tier],
    )


__all__ = [
    "build_plan",
    "core_resource",
    "derive_sample_payload",
    "login_route",
]
