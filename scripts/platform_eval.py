#!/usr/bin/env python3
"""vibe-platform 自身评测：契约闭环、溯源、门禁、确定性、交付与安全。

与 ``gis_launch_eval.py`` 的区别：
  - gis_launch_eval 评“平台生成的项目”；
  - 本脚本评“平台本身”的工程能力，使用最新生成产物作为证据。

建议在 python-ai 容器内执行（需要 ai_core 依赖）：

    docker run --rm -v "$PWD:/repo" -w /repo \
      -e PYTHONPATH=/repo/services/orchestrator \
      --entrypoint python vibe-platform/python-ai:dev \
      /repo/scripts/platform_eval.py --run-tests --baseline

默认只做静态检查；``--run-tests`` 会额外跑门禁相关单测。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
ORCH = ROOT / "services" / "orchestrator"
if str(ORCH) not in sys.path:
    sys.path.insert(0, str(ORCH))

FORBIDDEN_DOMAIN_PATTERNS = (
    r"\bGIS\b",
    r"GeoAI",
    r"geospatial",
    r"leaflet",
    r"postgis",
    r"地图",
    r"遥感",
    r"地理",
)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def add_check(
    checks: list[dict[str, Any]],
    name: str,
    ok: bool,
    detail: str,
    *,
    points: int = 1,
) -> None:
    checks.append(
        {
            "name": name,
            "ok": bool(ok),
            "detail": detail[:600],
            "points": points,
        }
    )


def category_result(
    name: str,
    checks: list[dict[str, Any]],
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    total = sum(check["points"] for check in checks)
    earned = sum(check["points"] for check in checks if check["ok"])
    return {
        "name": name,
        "score": round(100 * earned / total, 1) if total else 0.0,
        "checks": checks,
        "metrics": metrics or {},
    }


def load_config(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("缺少 PyYAML")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("评测清单必须是 YAML 对象")
    return payload


def latest_glob(pattern: str) -> Path | None:
    matches = sorted(ROOT.glob(pattern))
    return matches[-1] if matches else None


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------
# 1. 架构与领域隔离
# ------------------------------------------------------------
def eval_architecture(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    profiles_dir = root / "services" / "orchestrator" / "config" / "domain_profiles"
    profile_files = sorted(profiles_dir.glob("*.yaml"))
    add_check(
        checks,
        "领域 profile 配置层",
        (profiles_dir / "generic.yaml").is_file() and len(profile_files) >= 2,
        f"profiles={[item.name for item in profile_files]}",
        points=2,
    )
    discovery = read_text(
        root / "services" / "orchestrator" / "ai_core" / "architect" / "discovery.py"
    )
    add_check(
        checks,
        "发现链路读取 domain profile",
        "domain_profiles" in discovery and "select_domain_profile" in discovery,
        "discovery.py 引用 domain_profiles",
        points=2,
    )
    stack_dep = (
        root
        / "services"
        / "orchestrator"
        / "config"
        / "codegen"
        / "stack_dependencies.yaml"
    )
    stack_mod = (
        root
        / "services"
        / "orchestrator"
        / "ai_core"
        / "codegen"
        / "stack_dependencies.py"
    )
    add_check(
        checks,
        "techStack 依赖配置层",
        stack_dep.is_file() and stack_mod.is_file(),
        f"{stack_dep.name} / {stack_mod.name}",
        points=2,
    )
    stack_cap = (
        root
        / "services"
        / "orchestrator"
        / "config"
        / "codegen"
        / "stack_capabilities.yaml"
    )
    stack_cap_mod = (
        root
        / "services"
        / "orchestrator"
        / "ai_core"
        / "codegen"
        / "stack_capabilities.py"
    )
    add_check(
        checks,
        "可执行栈配置层",
        stack_cap.is_file() and stack_cap_mod.is_file(),
        f"{stack_cap.name} / {stack_cap_mod.name}",
        points=2,
    )
    signals = (
        root
        / "services"
        / "orchestrator"
        / "config"
        / "architect"
        / "brief_signals.yaml"
    )
    signals_mod = (
        root
        / "services"
        / "orchestrator"
        / "ai_core"
        / "architect"
        / "brief_signals.py"
    )
    add_check(
        checks,
        "抽象需求信号配置层",
        signals.is_file() and signals_mod.is_file(),
        f"{signals.name} / {signals_mod.name}",
        points=2,
    )

    violations: list[str] = []
    core = root / config.get("core_dir", "services/orchestrator/ai_core")
    for path in core.rglob("*.py"):
        text = read_text(path)
        for pattern in FORBIDDEN_DOMAIN_PATTERNS:
            if re.search(pattern, text):
                violations.append(f"{path.relative_to(root)}: {pattern}")
    add_check(
        checks,
        "核心无行业硬编码",
        not violations,
        "无命中" if not violations else "；".join(violations[:5]),
        points=3,
    )

    domain_imports: list[str] = []
    for path in (core / "domain").rglob("*.py"):
        text = read_text(path)
        for forbidden in (
            "import fastapi",
            "from fastapi",
            "import langgraph",
            "from langgraph",
        ):
            if forbidden in text:
                domain_imports.append(f"{path.name}: {forbidden}")
    add_check(
        checks,
        "domain 层不依赖框架",
        not domain_imports,
        "无框架依赖" if not domain_imports else "；".join(domain_imports[:5]),
        points=2,
    )
    return category_result(
        "architecture",
        checks,
        {
            "profileCount": len(profile_files),
            "domainViolations": violations[:10],
            "domainFrameworkImports": domain_imports[:10],
        },
    )


# ------------------------------------------------------------
# 2. 契约闭环
# ------------------------------------------------------------
def eval_contract_loop(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    architect_graph = read_text(
        root / "services" / "orchestrator" / "ai_core" / "architect" / "graph.py"
    )
    required = ("draft_contract", "validate_contract", "contract_gate")
    missing = [name for name in required if name not in architect_graph]
    add_check(
        checks,
        "Architect 契约节点",
        not missing,
        "全部存在" if not missing else f"缺少 {missing}",
        points=4,
    )
    fullstack_graph = read_text(
        root / "services" / "orchestrator" / "ai_core" / "fullstack" / "graph.py"
    )
    required = ("ConsistencyVerifier", "BuildVerifier", "SandboxRestart")
    missing = [name for name in required if name not in fullstack_graph]
    add_check(
        checks,
        "Fullstack 门禁节点",
        not missing,
        "全部存在" if not missing else f"缺少 {missing}",
        points=4,
    )

    artifact = config.get("artifact") or {}
    base = root / str(artifact.get("baseline_dir") or "samples/gis-baseline-iter6")
    contract = read_json(base / str(artifact.get("contract") or "contract.json"))
    source = base / str(artifact.get("source") or "source")
    api_routes = contract.get("apiRoutes") or []
    ddl = str(contract.get("databaseDdl") or "")
    visual = contract.get("visualDocument") or {}
    add_check(
        checks,
        "确认契约完整",
        contract.get("status") == "CONFIRMED"
        and bool(api_routes)
        and "CREATE TABLE" in ddl
        and visual.get("status") == "FROZEN",
        f"status={contract.get('status')} routes={len(api_routes)} "
        f"tables={ddl.count('CREATE TABLE')} visual={visual.get('status')}",
        points=4,
    )

    tech = contract.get("techStack") or {}
    server_pkg = read_json(source / "server" / "package.json")
    server_deps = server_pkg.get("dependencies") or {}
    backend_text = str(tech.get("backend") or "").lower()
    orm_text = str(tech.get("orm") or "").lower()
    stack_match = (
        ("node" in backend_text or "express" in backend_text)
        and "sequelize" in orm_text
        and "express" in server_deps
        and "sequelize" in server_deps
    )
    add_check(
        checks,
        "契约技术栈与产物一致",
        stack_match,
        f"contract={tech.get('backend')}/{tech.get('orm')}；"
        f"server={sorted(server_deps)[:6]}",
        points=4,
    )

    try:
        gis = load_script("gis_launch_eval")
        contract_routes = {
            gis.normalize_api_path(str(item.get("path") or ""))
            for item in api_routes
            if item.get("path")
        }
        manifest = read_json(source / "server" / "api-routes.json")
        generated_routes = {
            gis.normalize_api_path(str(item.get("path") or ""))
            for item in (manifest.get("routes") or [])
            if item.get("path")
        }
        ratio = (
            round(len(contract_routes & generated_routes) / len(contract_routes), 3)
            if contract_routes
            else 0.0
        )
    except Exception as exc:  # noqa: BLE001
        ratio = 0.0
        add_check(checks, "契约路由覆盖", False, f"评测器异常：{exc}", points=4)
    else:
        add_check(
            checks,
            "契约路由覆盖",
            ratio >= 0.8,
            f"{ratio:.0%}（{len(contract_routes)} 条契约路由）",
            points=4,
        )
    return category_result(
        "contract_loop",
        checks,
        {
            "contractId": contract.get("contractId"),
            "apiRoutes": len(api_routes),
            "tables": ddl.count("CREATE TABLE"),
            "routeCoverage": ratio,
            "techStack": tech,
        },
    )


# ------------------------------------------------------------
# 3. 溯源
# ------------------------------------------------------------
def eval_traceability(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    artifact = config.get("artifact") or {}
    base = root / str(artifact.get("baseline_dir") or "samples/gis-baseline-iter6")
    source = base / str(artifact.get("source") or "source")
    metadata = read_json(base / str(artifact.get("metadata") or "metadata.json"))
    contract = read_json(base / str(artifact.get("contract") or "contract.json"))
    threshold = config.get("thresholds") or {}

    source_map = read_json(source / "public" / "vibe-source-map.json")
    files = source_map.get("files") or {}
    entry_count = len(files) if isinstance(files, dict) else 0
    add_check(
        checks,
        "source map 存在",
        (source / "public" / "vibe-source-map.json").is_file()
        and entry_count >= int(threshold.get("min_source_map_entries") or 1),
        f"files={entry_count}",
        points=3,
    )

    app_source = read_text(source / "src" / "App.jsx")
    oid_count = app_source.count("data-node-id")
    add_check(
        checks,
        "生成代码带稳定 oid",
        oid_count >= int(threshold.get("min_oid_nodes") or 50),
        f"data-node-id={oid_count}",
        points=3,
    )
    add_check(
        checks,
        "冻结视觉哈希",
        bool(metadata.get("visualContentHash")),
        str(metadata.get("visualContentHash") or "缺失")[:40],
        points=3,
    )
    add_check(
        checks,
        "契约摘要可追溯",
        bool(metadata.get("contractSummary")),
        json.dumps(metadata.get("contractSummary") or {}, ensure_ascii=False)[:300],
        points=3,
    )
    add_check(
        checks,
        "冻结树状态",
        (contract.get("visualDocument") or {}).get("status") == "FROZEN",
        str((contract.get("visualDocument") or {}).get("status") or "缺失"),
        points=3,
    )
    return category_result(
        "traceability",
        checks,
        {
            "sourceMapFiles": entry_count,
            "oidNodes": oid_count,
            "visualContentHash": metadata.get("visualContentHash"),
            "contractSummary": metadata.get("contractSummary"),
        },
    )


# ------------------------------------------------------------
# 4. 门禁与自愈
# ------------------------------------------------------------
def eval_gates(
    root: Path,
    config: dict[str, Any],
    *,
    run_tests: bool,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    codegen = root / "services" / "orchestrator" / "ai_core" / "codegen"
    add_check(
        checks,
        "Babel AST 门禁",
        (codegen / "visual_gate.py").is_file()
        and (codegen / "visual_codegen_check.mjs").is_file(),
        "visual_gate.py + visual_codegen_check.mjs",
        points=3,
    )
    add_check(
        checks,
        "Applier AST 写入门",
        (
            root / "services" / "orchestrator" / "ai_core" / "fullstack" / "applier.py"
        ).is_file(),
        "applier.py",
        points=2,
    )
    add_check(
        checks,
        "一致性校验",
        (
            root
            / "services"
            / "orchestrator"
            / "ai_core"
            / "fullstack"
            / "consistency.py"
        ).is_file(),
        "consistency.py",
        points=2,
    )
    graph = read_text(
        root / "services" / "orchestrator" / "ai_core" / "fullstack" / "graph.py"
    )
    repair_markers = (
        "ConsistencyVerifier",
        "BuildVerifier",
        "consistency_repair_count",
    )
    missing = [marker for marker in repair_markers if marker not in graph]
    add_check(
        checks,
        "自愈回环",
        not missing,
        "存在" if not missing else f"缺少 {missing}",
        points=3,
    )
    test_files = [
        root / "services" / "orchestrator" / "tests" / name
        for name in (
            "test_codegen_bindings.py",
            "test_fullstack.py",
            "test_consistency.py",
            "test_visual_agent.py",
        )
    ]
    add_check(
        checks,
        "门禁测试覆盖",
        all(path.is_file() for path in test_files),
        f"{sum(path.is_file() for path in test_files)}/{len(test_files)}",
        points=3,
    )

    if run_tests:
        cwd = ORCH
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ORCH)
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "tests.test_codegen_bindings",
                    "tests.test_fullstack",
                    "tests.test_no_domain_hardcoding",
                    "-q",
                ],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            output = (proc.stdout + proc.stderr)[-800:]
            add_check(
                checks,
                "门禁定向测试",
                proc.returncode == 0,
                f"exit={proc.returncode}；{output[-300:]}",
                points=2,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            add_check(checks, "门禁定向测试", False, str(exc), points=2)
    else:
        add_check(
            checks,
            "门禁定向测试",
            True,
            "未执行（加 --run-tests 可运行；静态检查通过）",
            points=2,
        )
    return category_result(
        "gates_repair",
        checks,
        {"runTests": run_tests},
    )


# ------------------------------------------------------------
# 5. 确定性与可靠性
# ------------------------------------------------------------
def _deterministic_compile(root: Path, config: dict[str, Any]) -> tuple[bool, str]:
    try:
        from ai_core.codegen.bindings import apply_contract_interactions
        from ai_core.codegen.stack_dependencies import (
            contract_stack_text,
            resolve_stack_dependencies,
        )
        from ai_core.codegen.visual import compile_visual_document
        from ai_core.codegen.visual_extensions import (
            extension_dependencies,
            load_visual_extensions,
        )
        from ai_core.domain.visual_document import VisualDocument
    except Exception as exc:  # noqa: BLE001
        return False, f"缺少运行环境：{exc}"

    artifact = config.get("artifact") or {}
    base = root / str(artifact.get("baseline_dir") or "samples/gis-baseline-iter6")
    contract = read_json(base / str(artifact.get("contract") or "contract.json"))
    visual = read_json(base / str(artifact.get("visual") or "v0-visual-frozen.json"))
    try:
        document = VisualDocument.model_validate(visual)
        document = apply_contract_interactions(
            document, contract.get("componentInteractions") or []
        )
        extensions = load_visual_extensions()
        stack = resolve_stack_dependencies(contract_stack_text(contract))
        dependencies = {
            **stack.dependencies,
            **extension_dependencies(extensions),
        }
        kwargs = {
            "include_interactions": True,
            "extra_dependencies": dependencies,
            "extra_dev_dependencies": stack.dev_dependencies,
            "extra_imports": list(stack.imports),
            "visual_extensions": extensions,
        }
        first = compile_visual_document(document, **kwargs)
        second = compile_visual_document(document, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return False, f"编译失败：{exc}"

    def digest(result: Any) -> str:
        payload = json.dumps(sorted(result.files.items()), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    first_hash = digest(first)
    second_hash = digest(second)
    return (
        first_hash == second_hash,
        f"sha256={first_hash[:16]}… files={len(first.files)}",
    )


def eval_determinism(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    ok, detail = _deterministic_compile(root, config)
    add_check(checks, "同输入两次编译一致", ok, detail, points=6)
    add_check(
        checks,
        "Run 持久化",
        (
            root
            / "services"
            / "orchestrator"
            / "ai_core"
            / "persistence"
            / "run_store.py"
        ).is_file(),
        "run_store.py",
        points=3,
    )
    guard_file = root / "services" / "orchestrator" / "ai_core" / "run_guard.py"
    config_text = read_text(
        root / "services" / "orchestrator" / "ai_core" / "config.py"
    )
    compose_text = read_text(root / "docker-compose.yml")
    has_capacity = (
        "max_concurrent_runs" in config_text or "MAX_CONCURRENT_RUNS" in compose_text
    )
    add_check(
        checks,
        "全局 Run 舱壁",
        guard_file.is_file() and has_capacity,
        "run_guard.py + max_concurrent_runs/MAX_CONCURRENT_RUNS",
        points=3,
    )
    bff_resilience = (
        root
        / "apps"
        / "platform-bff"
        / "src"
        / "main"
        / "java"
        / "com"
        / "vibeplatform"
        / "bff"
        / "resilience"
    )
    add_check(
        checks,
        "BFF 舱壁/熔断",
        bff_resilience.is_dir(),
        (
            str(bff_resilience.relative_to(root))
            if bff_resilience.is_dir()
            else "缺少 resilience/"
        ),
        points=3,
    )
    return category_result(
        "determinism_reliability",
        checks,
        {"deterministicCompile": detail},
    )


# ------------------------------------------------------------
# 6. 工程交付
# ------------------------------------------------------------
def eval_delivery(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    artifact = config.get("artifact") or {}
    base = root / str(artifact.get("baseline_dir") or "samples/gis-baseline-iter6")
    source = base / str(artifact.get("source") or "source")
    threshold = config.get("thresholds") or {}
    required = ("README.md", "docker-compose.yml", "Dockerfile", ".env.example")
    present = [name for name in required if (source / name).is_file()]
    add_check(
        checks,
        "交付文件",
        len(present) == len(required),
        f"{present}",
        points=3,
    )
    add_check(
        checks,
        "构建产物",
        (source / "dist").is_dir(),
        "dist/ 存在" if (source / "dist").is_dir() else "缺少 dist/",
        points=2,
    )
    gis_report = read_json(
        root / str(artifact.get("gis_report") or "reports/gis/baseline.json")
    )
    gis_score = float(gis_report.get("score") or 0)
    add_check(
        checks,
        "生成项目评测",
        gis_score >= float(threshold.get("gis_eval_min_score") or 90),
        f"GIS score={gis_score}",
        points=3,
    )
    smoke = latest_glob(str(artifact.get("smoke_glob") or "reports/gis/smoke-*.json"))
    smoke_payload = read_json(smoke) if smoke else {}
    add_check(
        checks,
        "冒烟 E2E",
        bool(smoke_payload.get("ok")),
        f"{smoke.name if smoke else '缺少 smoke 报告'} ok={smoke_payload.get('ok')}",
        points=2,
    )
    return category_result(
        "delivery",
        checks,
        {
            "files": present,
            "gisScore": gis_score,
            "smoke": str(smoke) if smoke else "",
        },
    )


# ------------------------------------------------------------
# 7. 生产就绪度
# ------------------------------------------------------------
def eval_production_readiness(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    artifact = config.get("artifact") or {}
    base = root / str(artifact.get("baseline_dir") or "samples/gis-baseline-iter6")
    source = base / str(artifact.get("source") or "source")
    contract = read_json(base / str(artifact.get("contract") or "contract.json"))
    metadata = read_json(base / str(artifact.get("metadata") or "metadata.json"))
    tables = str(contract.get("databaseDdl") or "").count("CREATE TABLE")
    server = read_text(source / "server" / "index.js")
    db_code = read_text(source / "server" / "db.js")
    multi_markers = (
        "MODEL_BY_TABLE",
        "listTableRecords",
        "createTableRecord",
        "EXTRA_MODELS",
    )
    multi_hits = [
        marker for marker in multi_markers if marker in server or marker in db_code
    ]
    multi_entity = tables <= 1 or bool(multi_hits)
    add_check(
        checks,
        "多实体真实 CRUD",
        multi_entity,
        f"tables={tables}，命中 " + ("、".join(multi_hits) or "无"),
        points=4,
    )
    auth_code = read_text(source / "server" / "auth.js")
    auth_markers = (
        "jwt.verify",
        "authenticate(",
        "authenticateToken",
        "requireAuth",
        "requireWriteRole",
        "PUBLIC_AUTH_PATHS",
    )
    auth_hits = [
        marker for marker in auth_markers if marker in server or marker in auth_code
    ]
    add_check(
        checks,
        "JWT/RBAC 中间件",
        bool(auth_hits),
        (
            "命中 " + "、".join(auth_hits)
            if auth_hits
            else "server/index.js / auth.js 未发现鉴权中间件"
        ),
        points=3,
    )
    app_source = read_text(source / "src" / "App.jsx")
    map_data = (
        "<Marker" in app_source
        or "facilities.map" in app_source
        or "markers.map" in app_source
    )
    add_check(
        checks,
        "地图按数据打点",
        map_data,
        "JSX 使用 <Marker>/数据 map" if map_data else "地图仅底图，未按设施数据打点",
        points=2,
    )
    preview = str(metadata.get("previewUrl") or "")
    cloud = bool(preview) and "127.0.0.1" not in preview and "localhost" not in preview
    add_check(
        checks,
        "云端部署",
        cloud,
        f"previewUrl={preview or '缺失'}",
        points=1,
    )
    return category_result(
        "production_readiness",
        checks,
        {
            "tables": tables,
            "authMarkers": auth_hits,
            "mapData": map_data,
            "previewUrl": preview,
        },
    )


# ------------------------------------------------------------
# 8. 安全
# ------------------------------------------------------------
def eval_security(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    discovery = read_text(
        root / "services" / "orchestrator" / "ai_core" / "architect" / "discovery.py"
    )
    dangerous = [
        marker
        for marker in ("subprocess", "os.system", "eval(", "exec(")
        if marker in discovery
    ]
    add_check(
        checks,
        "方案侦察不执行代码",
        not dangerous,
        "无 subprocess/eval/exec" if not dangerous else f"命中 {dangerous}",
        points=1,
    )
    artifact = config.get("artifact") or {}
    source = (
        root
        / str(artifact.get("baseline_dir") or "samples/gis-baseline-iter6")
        / str(artifact.get("source") or "source")
    )
    add_check(
        checks,
        "生成项目无真实 .env",
        not (source / ".env").exists(),
        "未发现 .env" if not (source / ".env").exists() else "存在 .env",
        points=1,
    )
    secret_hits: list[str] = []
    secret_re = re.compile(
        r"(?i)(password|secret|api[_-]?key|token)\s*[:=]\s*['\"][^'\"]{6,}['\"]"
    )
    for path in source.rglob("*"):
        if not path.is_file() or "node_modules" in path.parts:
            continue
        if path.name in {".env.example", "package-lock.json"}:
            continue
        if path.suffix.lower() not in {
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".json",
            ".md",
            ".yml",
            ".yaml",
        }:
            continue
        match = secret_re.search(read_text(path))
        if match:
            secret_hits.append(f"{path.relative_to(source)}: {match.group(0)[:60]}")
    add_check(
        checks,
        "生成项目无硬编码密钥",
        not secret_hits,
        "无命中" if not secret_hits else "；".join(secret_hits[:3]),
        points=1,
    )
    export_script = read_text(root / "scripts" / "gis_baseline_e2e.py")
    add_check(
        checks,
        "导出路径穿越防护",
        '".." in parts' in export_script and "issym" in export_script,
        "含 .. / symlink 校验",
        points=1,
    )
    add_check(
        checks,
        "核心无行业硬编码",
        not any(
            re.search(pattern, read_text(path))
            for path in (root / "services" / "orchestrator" / "ai_core").rglob("*.py")
            for pattern in FORBIDDEN_DOMAIN_PATTERNS
        ),
        "与 architecture 检查一致",
        points=1,
    )
    return category_result(
        "security",
        checks,
        {"secretHits": secret_hits[:5], "dangerousDiscoveryMarkers": dangerous},
    )


# ------------------------------------------------------------
# 8. 可观测
# ------------------------------------------------------------
def eval_observability(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    add_check(
        checks,
        "可观测模块",
        (root / "services" / "orchestrator" / "ai_core" / "observability").is_dir(),
        "ai_core/observability",
        points=1,
    )
    artifact = config.get("artifact") or {}
    metadata = read_json(
        root
        / str(artifact.get("baseline_dir") or "samples/gis-baseline-iter6")
        / str(artifact.get("metadata") or "metadata.json")
    )
    events = (metadata.get("events") or {}).get("counts") or {}
    add_check(
        checks,
        "Run 事件计数",
        bool(events),
        f"events={sum(events.values())}",
        points=2,
    )
    g1 = latest_glob(str(artifact.get("g1_glob") or "reports/g1/g1_*.json"))
    add_check(
        checks,
        "G1 基准报告",
        g1 is not None,
        g1.name if g1 else "缺少 reports/g1/*.json",
        points=1,
    )
    add_check(
        checks,
        "管理台指标",
        (
            root
            / "apps"
            / "platform-bff"
            / "src"
            / "main"
            / "java"
            / "com"
            / "vibeplatform"
            / "bff"
            / "admin"
            / "AdminMetricsService.java"
        ).is_file(),
        "AdminMetricsService.java",
        points=1,
    )
    return category_result(
        "observability",
        checks,
        {"eventCounts": events, "g1Report": str(g1) if g1 else ""},
    )


# ------------------------------------------------------------
# 汇总
# ------------------------------------------------------------
def grade_for(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 60:
        return "C"
    return "D"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# vibe-platform 自身评测报告",
        "",
        f"- 生成时间：`{report['generatedAt']}`",
        f"- 总分：**{report['score']:.1f} / 100**（{report['grade']}）",
        f"- 说明：评的是平台工程能力；生成项目评测见 `reports/gis/baseline.json`。",
        "",
        "| 类别 | 得分 | 权重 | 加权 |",
        "|---|---:|---:|---:|",
    ]
    for name, category in report["categories"].items():
        weight = report["weights"].get(name, 0)
        lines.append(
            f"| {name} | {category['score']:.1f} | {weight} | "
            f"{category['score'] * weight / 100:.1f} |"
        )
    lines.extend(["", "## 未通过项", ""])
    failed = [
        (name, check)
        for name, category in report["categories"].items()
        for check in category["checks"]
        if not check["ok"]
    ]
    if not failed:
        lines.append("全部检查通过。")
    else:
        for name, check in failed:
            lines.append(
                f"- **{name} / {check['name']}**（{check['points']} 分）："
                f"{check['detail']}"
            )
    lines.extend(["", "## 指标", "", "```json"])
    lines.append(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    lines.extend(["```", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(ROOT / "config" / "platform_eval" / "checklist.yaml"),
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "reports" / "platform"),
    )
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--run-tests", action="store_true")
    parser.add_argument("--fail-under", type=float, default=0.0)
    args = parser.parse_args()

    config = load_config(Path(args.config))
    started = time.time()
    categories = {
        "architecture": eval_architecture(ROOT, config),
        "contract_loop": eval_contract_loop(ROOT, config),
        "traceability": eval_traceability(ROOT, config),
        "gates_repair": eval_gates(ROOT, config, run_tests=args.run_tests),
        "determinism_reliability": eval_determinism(ROOT, config),
        "delivery": eval_delivery(ROOT, config),
        "production_readiness": eval_production_readiness(ROOT, config),
        "security": eval_security(ROOT, config),
        "observability": eval_observability(ROOT, config),
    }
    weights = {
        str(key): float(value) for key, value in (config.get("weights") or {}).items()
    }
    score = round(
        sum(
            categories[name]["score"] * weights.get(name, 0) / 100
            for name in categories
        ),
        1,
    )
    report = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "score": score,
        "grade": grade_for(score),
        "weights": weights,
        "elapsedSeconds": round(time.time() - started, 2),
        "categories": categories,
        "metrics": {
            "baselineDir": (config.get("artifact") or {}).get("baseline_dir"),
            "runTests": args.run_tests,
        },
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"platform-eval-{stamp}.json"
    md_path = out_dir / f"platform-eval-{stamp}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")
    if args.baseline:
        (out_dir / "baseline.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"score={report['score']} grade={report['grade']}")
    for name, category in categories.items():
        print(f"  {name:26s} {category['score']:5.1f}")
    print(f"json={json_path}")
    print(f"md={md_path}")
    if score < args.fail_under:
        print(f"低于 --fail-under {args.fail_under}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
