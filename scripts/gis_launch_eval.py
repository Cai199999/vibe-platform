#!/usr/bin/env python3
"""按“可上线标准”评测一个由平台生成的 GIS 项目。

评测不依赖模型主观判断，只检查可复现事实：

    结构 / 构建证据 / 前端调用与后端路由一致性 / 溯源覆盖 /
    安全基线 / GIS 领域信号 / 可部署性

用法：
    python scripts/gis_launch_eval.py \
        --source samples/gis-baseline/source \
        --metadata samples/gis-baseline/metadata.json \
        --out reports/gis --baseline

    # 额外执行一次真实构建（需要 node_modules 或允许 npm ci）
    python scripts/gis_launch_eval.py --source ... --run-build

退出码：0 评测完成；1 低于 --fail-under；2 配置/输入错误。
"""

from __future__ import annotations

import argparse
import hashlib
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
except ImportError:  # pragma: no cover - 容器内始终有 PyYAML
    yaml = None

SKIP_DIRS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    ".cache",
    "__pycache__",
    ".next",
    "coverage",
}
TEXT_SUFFIXES = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".json",
    ".md",
    ".yml",
    ".yaml",
    ".sql",
    ".css",
    ".html",
    ".env",
    ".example",
    ".txt",
    ".sh",
    ".dockerfile",
}
MAX_FILE_BYTES = 512 * 1024


class EvalError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def iter_text_files(
    root: Path,
    globs: Iterable[str] | None = None,
) -> Iterable[tuple[str, str]]:
    patterns = [glob for glob in (globs or ["**/*"])]
    seen: set[Path] = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {
                "Dockerfile",
                ".env.example",
                "DEPLOY.md",
            }:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                yield str(path.relative_to(root)), path.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue


def rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


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
            "detail": detail,
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
    score = round(100 * earned / total, 1) if total else 0.0
    return {
        "name": name,
        "score": score,
        "checks": checks,
        "metrics": metrics or {},
    }


def load_checklist(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise EvalError("缺少 PyYAML；请在 python-ai 容器内运行或安装 PyYAML")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EvalError(f"读取评测清单失败: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvalError("评测清单必须是 YAML 对象")
    return payload


# ------------------------------------------------------------
# 结构
# ------------------------------------------------------------
def eval_structure(
    root: Path,
    config: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for name in config.get("required_files") or []:
        path = root / name
        add_check(checks, f"文件 {name}", path.is_file(), rel(root, path), points=2)
    for name in config.get("required_dirs") or []:
        path = root / name
        add_check(checks, f"目录 {name}", path.is_dir(), rel(root, path), points=2)
    for name in config.get("optional_files") or []:
        path = root / name
        add_check(
            checks,
            f"可选 {name}",
            path.is_file(),
            "存在" if path.is_file() else "缺失（不影响基础分）",
            points=1,
        )
    package = read_json(root / "package.json")
    add_check(
        checks,
        "package.json 可解析",
        bool(package),
        "name=" + str(package.get("name") or "?"),
        points=2,
    )
    add_check(
        checks,
        "package.json 含 scripts",
        isinstance(package.get("scripts"), dict),
        f"scripts={list((package.get('scripts') or {}).keys())[:8]}",
        points=1,
    )
    return category_result(
        "structure",
        checks,
        {
            "files": sorted(
                str(path.relative_to(root))
                for path in root.rglob("*")
                if path.is_file() and not any(part in SKIP_DIRS for part in path.parts)
            )[:200],
            "packageName": package.get("name"),
        },
    )


# ------------------------------------------------------------
# 构建
# ------------------------------------------------------------
def eval_build(
    root: Path,
    config: dict[str, Any],
    metadata: dict[str, Any],
    *,
    run_build: bool,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    package = read_json(root / "package.json")
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    build_script = str(config.get("package_script") or "build")
    add_check(
        checks,
        f"npm script {build_script}",
        bool(scripts.get(build_script)),
        f"scripts={list(scripts.keys())[:8]}",
        points=30,
    )

    status_key = str(config.get("metadata_status_key") or "fullstackStatus")
    status = str(metadata.get(status_key) or metadata.get("status") or "")
    add_check(
        checks,
        "平台构建/生成状态",
        status.upper() == "SUCCEEDED" or not metadata,
        f"{status_key}={status or '未提供'}",
        points=35,
    )

    dist_found = ""
    for candidate in config.get("dist_candidates") or []:
        if (root / candidate).exists():
            dist_found = candidate
            break
    add_check(
        checks,
        "构建产物目录",
        bool(dist_found),
        dist_found or "未找到 dist/build（平台导出可能不包含构建产物）",
        points=15,
    )

    build_log = ""
    if run_build:
        timeout = int(config.get("timeout_seconds") or 240)
        try:
            if not (root / "node_modules").exists():
                add_check(
                    checks,
                    "安装依赖",
                    False,
                    "未找到 node_modules；跳过自动安装（避免不可控网络）",
                    points=0,
                )
            else:
                proc = subprocess.run(
                    ["npm", "run", build_script],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                build_log = (proc.stdout + proc.stderr)[-2000:]
                add_check(
                    checks,
                    "真实构建",
                    proc.returncode == 0,
                    f"exit={proc.returncode}；{build_log[-400:]}",
                    points=20,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            add_check(checks, "真实构建", False, str(exc), points=20)

    return category_result(
        "build",
        checks,
        {
            "scripts": list(scripts.keys()),
            "metadataStatus": status,
            "dist": dist_found,
            "buildLogTail": build_log[-600:],
        },
    )


# ------------------------------------------------------------
# API 一致性
# ------------------------------------------------------------
_FRONTEND_FETCH = re.compile(
    r"(?:fetch|axios\.(?:get|post|put|delete|patch))\s*\(\s*[`'\"]([^`'\"]+)[`'\"]"
)
# CodeGen 的 interaction runtime 把端点集中在 VISUAL_ACTIONS JSON 里，
# fetch 参数是变量而不是字面量；这里同时收集前端文件中的 /api 字面量。
_API_LITERAL = re.compile(r"[`'\"](/api/[^`'\"\s]+)[`'\"]")
_BACKEND_ROUTE = re.compile(
    r"(?:app|router)\.(get|post|put|delete|patch)\s*\(\s*[`'\"]([^`'\"]+)[`'\"]",
    re.IGNORECASE,
)
_DYNAMIC_SEGMENT = re.compile(r"\$\{[^}]+\}|\{[^}]+\}")
_NUMERIC_SEGMENT = re.compile(r"^\d+$")
_UUID_SEGMENT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def normalize_api_path(path: str) -> str:
    text = _DYNAMIC_SEGMENT.sub(":id", str(path or "").strip())
    text = text.split("?", 1)[0]
    segments = []
    for segment in text.split("/"):
        if (
            segment.startswith(":")
            or _NUMERIC_SEGMENT.match(segment)
            or _UUID_SEGMENT.match(segment)
        ):
            # 契约常写 :id，CodeGen 会写 :facility_id / :task_id；比较时
            # 统一视为同一个占位符，避免把命名差异误报为缺失路由。
            segments.append(":id")
        else:
            segments.append(segment)
    return "/".join(segments).rstrip("/") or "/"


def extract_frontend_calls(root: Path, config: dict[str, Any]) -> list[dict[str, str]]:
    calls: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    ignore = set(config.get("ignore_paths") or [])
    for rel_path, text in iter_text_files(root, config.get("frontend_globs")):
        urls = [match.group(1) for match in _FRONTEND_FETCH.finditer(text)]
        urls.extend(match.group(1) for match in _API_LITERAL.finditer(text))
        for url in urls:
            if not url.startswith("/api") or url in ignore:
                continue
            path = normalize_api_path(url)
            key = (path, rel_path)
            if key in seen:
                continue
            seen.add(key)
            calls.append({"path": path, "file": rel_path})
    return calls


def extract_backend_routes(root: Path, config: dict[str, Any]) -> list[dict[str, str]]:
    routes: list[dict[str, str]] = []
    for rel_path, text in iter_text_files(root, config.get("backend_globs")):
        for match in _BACKEND_ROUTE.finditer(text):
            routes.append(
                {
                    "method": match.group(1).upper(),
                    "path": normalize_api_path(match.group(2)),
                    "file": rel_path,
                }
            )
    # 核心 CRUD 通过 COLLECTION_PATH / ITEM_PATH 常量注册，正则看不到；
    # 生成的 api-routes.json 是同一份路由清单，作为补充事实源。
    manifest = read_json(root / "server" / "api-routes.json")
    for item in manifest.get("routes") or []:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        routes.append(
            {
                "method": str(item.get("method") or "GET").upper(),
                "path": normalize_api_path(str(item["path"])),
                "file": "server/api-routes.json",
            }
        )
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, str]] = []
    for route in routes:
        key = (route["method"], route["path"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(route)
    return deduped


def _route_matches(call: dict[str, str], route: dict[str, str]) -> bool:
    if call["path"] != route["path"]:
        return False
    # 前端 fetch 未显式写 method 时按 GET 处理；其余使用通用匹配。
    return True


def eval_api_consistency(
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    calls = extract_frontend_calls(root, config)
    routes = extract_backend_routes(root, config)
    add_check(
        checks,
        "发现前端 /api 调用",
        bool(calls),
        f"{len(calls)} 处",
        points=10,
    )
    add_check(
        checks,
        "发现后端路由",
        bool(routes),
        f"{len(routes)} 条",
        points=10,
    )
    unmatched = [
        call
        for call in calls
        if not any(_route_matches(call, route) for route in routes)
    ]
    match_ratio = (
        round(1 - len(unmatched) / len(calls), 3) if calls else 0.0
    )
    min_ratio = float(config.get("min_match_ratio") or 0.8)
    add_check(
        checks,
        "前后端路径匹配率",
        bool(calls) and match_ratio >= min_ratio,
        f"{match_ratio:.0%}（阈值 {min_ratio:.0%}）",
        points=50,
    )
    add_check(
        checks,
        "无未匹配调用",
        bool(calls) and not unmatched,
        "全部匹配" if calls and not unmatched else f"未匹配 {len(unmatched)} 处",
        points=30,
    )
    return category_result(
        "api_consistency",
        checks,
        {
            "frontendCalls": len(calls),
            "backendRoutes": len(routes),
            "matchRatio": match_ratio,
            "unmatched": unmatched[:20],
            "routesSample": routes[:20],
        },
    )


# ------------------------------------------------------------
# 契约一致性：产物是否真的按确认契约实现
# ------------------------------------------------------------
def _find_contract(
    root: Path, config: dict[str, Any]
) -> tuple[Path | None, dict[str, Any]]:
    for candidate in config.get("contract_candidates") or []:
        path = (root / candidate).resolve()
        if path.is_file():
            return path, read_json(path)
    return None, {}


def _selected_stack_text(contract: dict[str, Any]) -> str:
    """从契约正文提取“技术选型”段落，兼容 techStack 字段为空的情况。"""
    markdown = str(contract.get("contractMarkdown") or "")
    for marker in ("技术选型", "技术栈", "Tech Stack"):
        index = markdown.find(marker)
        if index >= 0:
            return markdown[index : index + 1500].lower()
    assumptions = " ".join(
        str(item) for item in (contract.get("assumptions") or [])
    )
    if assumptions.strip():
        return assumptions.lower()
    return markdown[:1500].lower()


def eval_contract_fidelity(
    root: Path,
    config: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    contract_path, contract = _find_contract(root, config)
    add_check(
        checks,
        "确认契约存在",
        contract_path is not None,
        str(contract_path) if contract_path else "未找到 contract.json",
        points=10,
    )
    tech = contract.get("techStack") or {}
    selected_stack = _selected_stack_text(contract)
    frontend_text = (
        str(tech.get("frontend") or "") + " " + selected_stack
    ).lower()
    backend_text = (
        " ".join(
            str(tech.get(key) or "")
            for key in ("backend", "database", "orm", "migration", "deploy")
        )
        + " "
        + selected_stack
    ).lower()
    package = read_json(root / "package.json")
    dependencies = {
        **(package.get("dependencies") or {}),
        **(package.get("devDependencies") or {}),
    }
    for token in config.get("frontend_tokens") or []:
        if token in frontend_text:
            add_check(
                checks,
                f"前端契约 {token}",
                token in dependencies,
                f"contract={token}, package.json="
                + ("有" if token in dependencies else "缺失"),
                points=10,
            )

    generated_backend_text = " ".join(
        text for _rel, text in iter_text_files(root, ["server/**/*"])
    ).lower()
    generated_node = bool(
        (root / "server" / "package.json").is_file()
        and not (root / "pom.xml").is_file()
    )
    if "spring" in backend_text or "java" in backend_text:
        add_check(
            checks,
            "后端契约语言",
            not generated_node,
            f"contract=Java/Spring, generated="
            + ("Node/Express" if generated_node else "非 Node"),
            points=20,
        )
    elif "node" in backend_text or "express" in backend_text:
        add_check(
            checks,
            "后端契约语言",
            generated_node,
            f"contract=Node/Express, generated="
            + ("Node/Express" if generated_node else "非 Node"),
            points=20,
        )
    else:
        add_check(checks, "后端契约语言", True, "契约未声明后端语言", points=20)

    if "jpa" in backend_text or "hibernate" in backend_text:
        add_check(
            checks,
            "ORM 契约",
            "sequelize" not in generated_backend_text,
            "contract=JPA/Hibernate, generated="
            + ("Sequelize" if "sequelize" in generated_backend_text else "未识别"),
            points=20,
        )
    elif "sequelize" in backend_text:
        add_check(
            checks,
            "ORM 契约",
            "sequelize" in generated_backend_text,
            "contract=Sequelize, generated="
            + ("Sequelize" if "sequelize" in generated_backend_text else "未识别"),
            points=20,
        )
    else:
        add_check(checks, "ORM 契约", True, "契约未声明 ORM", points=20)

    for token in config.get("database_tokens") or []:
        if token in backend_text:
            add_check(
                checks,
                f"数据库契约 {token}",
                token in generated_backend_text,
                f"contract={token}, generated="
                + ("命中" if token in generated_backend_text else "未命中"),
                points=10,
            )

    contract_routes = {
        normalize_api_path(str(route.get("path") or ""))
        for route in (contract.get("apiRoutes") or [])
        if route.get("path")
    }
    api_routes = read_json(root / "server" / "api-routes.json")
    generated_routes = {
        normalize_api_path(str(route.get("path") or ""))
        for route in (api_routes.get("routes") or [])
        if route.get("path")
    }
    if contract_routes:
        matched = len(contract_routes & generated_routes)
        ratio = round(matched / len(contract_routes), 3)
        min_ratio = float(config.get("min_route_ratio") or 0.8)
        add_check(
            checks,
            "契约 API 覆盖",
            ratio >= min_ratio,
            f"{ratio:.0%}（{matched}/{len(contract_routes)}，阈值 {min_ratio:.0%}）",
            points=20,
        )
    else:
        add_check(checks, "契约 API 覆盖", True, "契约未声明 API", points=20)
    return category_result(
        "contract_fidelity",
        checks,
        {
            "contractPath": str(contract_path) if contract_path else None,
            "techStack": tech,
            "frontendDependencies": sorted(dependencies),
            "contractRoutes": sorted(contract_routes)[:30],
            "generatedRoutes": sorted(generated_routes)[:30],
        },
    )


# ------------------------------------------------------------
# 溯源
# ------------------------------------------------------------
def eval_traceability(
    root: Path,
    config: dict[str, Any],
    metadata: dict[str, Any],
    source_dir: Path | None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    source_map_path = None
    for candidate in config.get("source_map_candidates") or []:
        path = root / candidate
        if path.is_file():
            source_map_path = path
            break
    source_map = read_json(source_map_path) if source_map_path else {}
    entries = source_map.get("entries") if isinstance(source_map, dict) else None
    if entries is None and isinstance(source_map, dict):
        entries = source_map.get("files") or source_map
    entry_count = len(entries) if isinstance(entries, (dict, list)) else 0
    add_check(
        checks,
        "source map 存在",
        source_map_path is not None,
        rel(root, source_map_path) if source_map_path else "未找到 vibe-source-map.json",
        points=30,
    )
    add_check(
        checks,
        "source map 有映射条目",
        entry_count > 0,
        f"{entry_count} 条",
        points=20,
    )

    oid_attributes = config.get("oid_attributes") or ["data-oid", "data-node-id"]
    oid_hits = 0
    oid_files: set[str] = set()
    for rel_path, text in iter_text_files(root, ["src/**/*", "**/*.jsx", "**/*.tsx"]):
        for attribute in oid_attributes:
            count = text.count(attribute)
            if count:
                oid_hits += count
                oid_files.add(rel_path)
    min_nodes = int(config.get("min_oid_nodes") or 1)
    add_check(
        checks,
        "生成代码带稳定 oid",
        oid_hits >= min_nodes,
        f"{oid_hits} 处 / {len(oid_files)} 个文件",
        points=25,
    )

    contract_found = False
    for candidate in config.get("contract_candidates") or []:
        path = (root / candidate).resolve()
        if path.is_file():
            contract_found = True
            break
    contract_summary = metadata.get("contractSummary") or {}
    add_check(
        checks,
        "契约证据",
        contract_found or bool(contract_summary),
        f"file={contract_found}, summary={bool(contract_summary)}",
        points=15,
    )
    add_check(
        checks,
        "冻结视觉哈希",
        bool(metadata.get("visualContentHash")),
        str(metadata.get("visualContentHash") or "未提供")[:32],
        points=10,
    )
    return category_result(
        "traceability",
        checks,
        {
            "sourceMap": rel(root, source_map_path) if source_map_path else None,
            "sourceMapEntries": entry_count,
            "oidHits": oid_hits,
            "oidFiles": sorted(oid_files)[:20],
            "contractSummary": contract_summary,
        },
    )


# ------------------------------------------------------------
# 安全
# ------------------------------------------------------------
def eval_security(
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    forbidden = config.get("forbidden_patterns") or []
    secret_patterns = [
        re.compile(pattern) for pattern in (config.get("secret_patterns") or [])
    ]
    allow_files = set(config.get("allow_secret_files") or [])
    forbidden_hits: list[dict[str, str]] = []
    secret_hits: list[dict[str, str]] = []
    for rel_path, text in iter_text_files(root):
        if rel_path in allow_files:
            continue
        for pattern in forbidden:
            if pattern in text:
                forbidden_hits.append({"file": rel_path, "pattern": pattern})
        for pattern in secret_patterns:
            match = pattern.search(text)
            if match:
                secret_hits.append(
                    {"file": rel_path, "match": match.group(0)[:80]}
                )
    add_check(
        checks,
        "无危险 CORS 通配",
        not forbidden_hits,
        f"命中 {len(forbidden_hits)} 处",
        points=15,
    )
    add_check(
        checks,
        "无硬编码密钥/口令",
        not secret_hits,
        f"命中 {len(secret_hits)} 处",
        points=35,
    )
    env_file = root / ".env"
    add_check(
        checks,
        "未提交真实 .env",
        not env_file.exists(),
        "存在 .env" if env_file.exists() else "未发现 .env",
        points=10,
    )
    auth_keywords = [keyword.lower() for keyword in (config.get("auth_keywords") or [])]
    auth_hits: set[str] = set()
    for rel_path, text in iter_text_files(root):
        lower = text.lower()
        for keyword in auth_keywords:
            if keyword in lower:
                auth_hits.add(keyword)
    require_auth = bool(config.get("require_auth", True))
    add_check(
        checks,
        "认证/权限信号",
        (not require_auth) or len(auth_hits) >= 2,
        "命中：" + "、".join(sorted(auth_hits)) if auth_hits else "未发现认证信号",
        points=40,
    )
    return category_result(
        "security",
        checks,
        {
            "forbiddenHits": forbidden_hits[:10],
            "secretHits": secret_hits[:10],
            "authHits": sorted(auth_hits),
        },
    )


# ------------------------------------------------------------
# GIS 领域
# ------------------------------------------------------------
def eval_gis_domain(
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    package = read_json(root / "package.json")
    dependencies = {
        **(package.get("dependencies") or {}),
        **(package.get("devDependencies") or {}),
    }
    map_deps = [
        dependency
        for dependency in (config.get("frontend_dependencies") or [])
        if dependency in dependencies
    ]
    add_check(
        checks,
        "地图前端依赖",
        bool(map_deps),
        "、".join(map_deps) or "未发现 leaflet/maplibre/openlayers 等",
        points=30,
    )
    source_keywords = [str(item).lower() for item in (config.get("source_keywords") or [])]
    backend_keywords = [str(item).lower() for item in (config.get("backend_keywords") or [])]
    render_keywords = [
        str(item).lower() for item in (config.get("render_keywords") or [])
    ]
    source_hits: set[str] = set()
    backend_hits: set[str] = set()
    source_text = ""
    for rel_path, text in iter_text_files(root):
        lower = text.lower()
        if not rel_path.startswith("server/"):
            source_text += " " + lower
        target = backend_hits if rel_path.startswith("server/") else source_hits
        for keyword in source_keywords:
            if keyword in lower:
                target.add(keyword)
        for keyword in backend_keywords:
            if keyword in lower:
                backend_hits.add(keyword)
    render_hits = [keyword for keyword in render_keywords if keyword in source_text]
    min_hits = int(config.get("min_keyword_hits") or 2)
    add_check(
        checks,
        "前端 GIS 语义",
        len(source_hits) >= min_hits,
        "命中：" + "、".join(sorted(source_hits)[:10]),
        points=20,
    )
    add_check(
        checks,
        "地图真实渲染",
        bool(render_hits),
        "命中：" + "、".join(render_hits)
        if render_hits
        else "只发现依赖/文案，未发现 MapContainer/TileLayer/maplibre",
        points=30,
    )
    add_check(
        checks,
        "后端空间语义",
        len(backend_hits) >= min_hits,
        "命中：" + "、".join(sorted(backend_hits)[:10]),
        points=20,
    )
    return category_result(
        "gis_domain",
        checks,
        {
            "mapDependencies": map_deps,
            "sourceKeywordHits": sorted(source_hits),
            "renderKeywordHits": render_hits,
            "backendKeywordHits": sorted(backend_hits),
        },
    )


# ------------------------------------------------------------
# 可部署性
# ------------------------------------------------------------
def eval_deployability(
    root: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for name in config.get("required_files") or []:
        add_check(
            checks,
            f"部署文件 {name}",
            (root / name).is_file(),
            "存在" if (root / name).is_file() else "缺失",
            points=15,
        )
    for name in config.get("recommended_files") or []:
        add_check(
            checks,
            f"推荐文件 {name}",
            (root / name).is_file(),
            "存在" if (root / name).is_file() else "缺失",
            points=8,
        )
    health_paths = config.get("health_paths") or []
    health_hits = []
    for rel_path, text in iter_text_files(root):
        for path in health_paths:
            if path in text:
                health_hits.append({"file": rel_path, "path": path})
    add_check(
        checks,
        "健康检查端点",
        bool(health_hits),
        f"命中 {len(health_hits)} 处" if health_hits else "未发现 /health",
        points=20,
    )
    dockerfile = ""
    if (root / "Dockerfile").is_file():
        dockerfile = (root / "Dockerfile").read_text(
            encoding="utf-8", errors="replace"
        )
    for token in config.get("dockerfile_checks") or []:
        add_check(
            checks,
            f"Dockerfile {token}",
            token in dockerfile,
            "存在" if token in dockerfile else "缺失",
            points=10,
        )
    if config.get("prefer_non_root"):
        add_check(
            checks,
            "Dockerfile 非 root 用户",
            "USER " in dockerfile,
            "存在 USER" if "USER " in dockerfile else "未声明 USER",
            points=10,
        )
    return category_result(
        "deployability",
        checks,
        {
            "healthHits": health_hits[:10],
            "dockerfileBytes": len(dockerfile),
        },
    )


# ------------------------------------------------------------
# 汇总
# ------------------------------------------------------------
def hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def grade_for(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    return "D"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# GIS 可上线评测报告",
        "",
        f"- 生成时间：`{report['generatedAt']}`",
        f"- 项目：`{report['source']}`",
        f"- 总分：**{report['score']:.1f} / 100**（{report['grade']}）",
        f"- 源码哈希：`{report['sourceHash'][:16]}…`",
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
    parser.add_argument("--source", required=True, help="生成项目根目录")
    parser.add_argument("--metadata", default="", help="E2E metadata.json")
    parser.add_argument(
        "--checklist",
        default=str(
            Path(__file__).resolve().parents[1]
            / "config"
            / "gis_eval"
            / "launch_checklist.yaml"
        ),
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parents[1] / "reports" / "gis"),
    )
    parser.add_argument("--baseline", action="store_true", help="同时写 baseline.json")
    parser.add_argument("--run-build", action="store_true", help="执行 npm run build")
    parser.add_argument("--fail-under", type=float, default=0.0)
    args = parser.parse_args()

    source = Path(args.source).resolve()
    if not source.is_dir():
        print(f"source 不存在: {source}", file=sys.stderr)
        return 2
    checklist_path = Path(args.checklist).resolve()
    if not checklist_path.is_file():
        print(f"checklist 不存在: {checklist_path}", file=sys.stderr)
        return 2
    config = load_checklist(checklist_path)

    metadata_path = Path(args.metadata).resolve() if args.metadata else None
    if metadata_path is None:
        for candidate in (source.parent / "metadata.json", source / "metadata.json"):
            if candidate.is_file():
                metadata_path = candidate
                break
    metadata = read_json(metadata_path) if metadata_path else {}

    started = time.time()
    categories = {
        "structure": eval_structure(source, config.get("structure") or {}, metadata),
        "build": eval_build(
            source,
            config.get("build") or {},
            metadata,
            run_build=args.run_build,
        ),
        "api_consistency": eval_api_consistency(
            source, config.get("api_consistency") or {}
        ),
        "contract_fidelity": eval_contract_fidelity(
            source, config.get("contract_fidelity") or {}, metadata
        ),
        "traceability": eval_traceability(
            source,
            config.get("traceability") or {},
            metadata,
            source,
        ),
        "security": eval_security(source, config.get("security") or {}),
        "gis_domain": eval_gis_domain(source, config.get("gis_domain") or {}),
        "deployability": eval_deployability(
            source, config.get("deployability") or {}
        ),
    }
    weights = {
        str(key): float(value)
        for key, value in (config.get("weights") or {}).items()
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
        "source": str(source),
        "metadata": str(metadata_path) if metadata_path else "",
        "checklist": str(checklist_path),
        "weights": weights,
        "score": score,
        "grade": grade_for(score),
        "sourceHash": hash_tree(source),
        "elapsedSeconds": round(time.time() - started, 2),
        "categories": categories,
        "metrics": {
            "contract": metadata.get("contractSummary") or {},
            "solutionChoice": metadata.get("solutionChoice") or {},
            "previewUrl": metadata.get("previewUrl"),
            "sandboxId": metadata.get("sandboxId"),
            "sourceFiles": sum(
                1
                for path in source.rglob("*")
                if path.is_file()
                and not any(part in SKIP_DIRS for part in path.parts)
            ),
        },
    }

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"gis-eval-{stamp}.json"
    md_path = out_dir / f"gis-eval-{stamp}.md"
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
        print(f"  {name:16s} {category['score']:5.1f}")
    print(f"json={json_path}")
    print(f"md={md_path}")
    if score < args.fail_under:
        print(f"低于 --fail-under {args.fail_under}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EvalError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
