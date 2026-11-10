"""全栈代码生成器：DBACoder / BackendCoder / FrontendCoder。

设计原则：

1. **确定性优先**：不依赖模型随机输出，契约字段直接映射为可运行代码，
   保证同一份契约生成结果可复现、可测试。
2. **Express + SQLite**：沙盒镜像预装 ``express`` / ``sqlite3``；后端按
   契约生成 ``server/index.js`` 与 ``server/db.js``，数据真实落盘。
3. **DDL 可执行**：``server/schema.sql`` 是沙盒内可直接执行的 SQLite
   DDL，``server/schema.mysql.sql`` 保留契约原始 MySQL DDL。
4. **前端渐进增强**：不重写用户界面，而是在 ``src/main.jsx`` 挂载一个
   ``FullstackDataPanel``，用真实 API 数据替代原先的 Mock 数据展示。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ai_core.domain.architect import (
    ArchitectApiRoute,
    ArchitectContract,
    normalize_database_engine,
)
from ai_core.fullstack.templates import (
    BACKEND_DOCKERFILE_TEMPLATE,
    DEPLOY_GUIDE_TEMPLATE,
    DEV_SCRIPT_TEMPLATE,
    DOCKERIGNORE_TEMPLATE,
    ENV_EXAMPLE_TEMPLATE,
    EXPRESS_SERVER_TEMPLATE,
    MIGRATE_SCRIPT_TEMPLATE,
    RENDER_YAML_TEMPLATE,
    SEQUELIZE_DB_TEMPLATE,
)

API_PORT = 3000
DEFAULT_COLLECTION_PATH = "/api/v1/records"
DEFAULT_TABLE = "app_records"

DATABASE_ENGINE_LABELS = {
    "mysql": "MySQL 8（生产部署）",
    "postgresql": "PostgreSQL（生产部署）",
    "sqlite": "SQLite（仅供本地测试）",
}
PRODUCTION_ENGINES = {"mysql", "postgresql"}
SERVER_DEPENDENCIES = {
    "express": "4.21.2",
    # 生产 ORM 与两套驱动；沙盒未配置 DATABASE_URL 时不会加载它们。
    "sequelize": "6.37.5",
    "mysql2": "3.11.5",
    "pg": "8.13.1",
    "pg-hstore": "2.3.4",
}

_SQL_IDENTIFIER = re.compile(r"[^a-zA-Z0-9_]")
_CREATE_TABLE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?[`\"]?" r"([a-zA-Z0-9_]+)[`\"]?",
    re.IGNORECASE,
)
_COLUMN_LINE = re.compile(r"^\s*[`\"]?([a-zA-Z0-9_]+)[`\"]?\s+")
_COLUMN_STOPWORDS = {
    "key",
    "unique",
    "primary",
    "constraint",
    "index",
    "foreign",
    "fulltext",
    "spatial",
    "check",
}


@dataclass(frozen=True)
class GeneratedBundle:
    """一次节点生成的产物。"""

    files: dict[str, str]
    summary: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------
# 契约解析
# ------------------------------------------------------------
def _safe_identifier(value: str, fallback: str) -> str:
    cleaned = _SQL_IDENTIFIER.sub("_", (value or "").strip()).strip("_")
    return cleaned or fallback


def contract_database_engine(contract: ArchitectContract) -> str:
    """解析契约目标数据库；旧契约从 tech_stack 兜底。

    返回值限定为 mysql / postgresql / sqlite。历史契约没有
    ``databaseEngine`` 字段，但只要技术栈写了 MySQL 就应继续生成生产路径，
    绝不能因为字段缺失而静默回退 SQLite。
    """
    engine = normalize_database_engine(contract.database_engine)
    if not engine:
        engine = normalize_database_engine(
            contract.tech_stack.get("databaseEngine")
            or contract.tech_stack.get("database")
        )
    return engine or "mysql"


def extract_table_name(ddl: str) -> str:
    """从 DDL 提取第一张表名；缺失时回退为 app_records。"""
    match = _CREATE_TABLE.search(ddl or "")
    if not match:
        return DEFAULT_TABLE
    return _safe_identifier(match.group(1), DEFAULT_TABLE)


def extract_columns(ddl: str) -> list[str]:
    """从 ``CREATE TABLE (...)`` 中提取字段名。

    只做轻量解析，用于生成种子数据和前端展示；它不是 SQL Parser，
    遇到无法识别的行会跳过，不影响 DDL 原样落盘。
    """
    match = re.search(r"\((.*)\)\s*;?\s*$", ddl or "", re.DOTALL)
    if not match:
        return ["id", "name", "status"]
    columns: list[str] = []
    for raw_line in match.group(1).split(","):
        line = raw_line.strip()
        if not line:
            continue
        name_match = _COLUMN_LINE.match(line)
        if not name_match:
            continue
        name = name_match.group(1).lower()
        if name in _COLUMN_STOPWORDS:
            continue
        if name not in columns:
            columns.append(name)
    return columns or ["id", "name", "status"]


def _split_definitions(body: str) -> list[str]:
    """按顶层逗号切分 CREATE TABLE 字段定义。

    ``DECIMAL(10,2)`` 这类类型内部也有逗号，必须跟踪括号深度，
    否则会把一个字段拆成两段，生成的 SQLite DDL 直接损坏。
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    for char in body:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    return parts


_SQLITE_INTEGER = {
    "INT",
    "INTEGER",
    "BIGINT",
    "SMALLINT",
    "TINYINT",
    "MEDIUMINT",
    "BOOLEAN",
    "BIT",
    "SERIAL",
}
_SQLITE_REAL = {"REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "MONEY"}
_SQLITE_BLOB = {"BLOB", "BINARY", "VARBINARY", "BYTEA"}


def extract_column_defs(ddl: str) -> list[tuple[str, str]]:
    """把 DDL 字段映射为 SQLite ``(name, type)`` 列表。

    只保留字段名与 SQLite 类型亲和性；``NOT NULL`` / 默认值等约束
    交给生成的 ``server/db.js`` 在写入时补全，避免不同数据库方言
    导致沙盒启动即失败。
    """
    match = re.search(r"\((.*)\)\s*;?\s*$", ddl or "", re.DOTALL)
    if not match:
        return [
            ("id", "INTEGER"),
            ("name", "TEXT"),
            ("status", "TEXT"),
        ]
    defs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in _split_definitions(match.group(1)):
        name_match = _COLUMN_LINE.match(part.strip())
        if not name_match:
            continue
        name = name_match.group(1).lower()
        if name in _COLUMN_STOPWORDS or name in seen:
            continue
        rest = part.strip()[name_match.end() :].strip()
        type_match = re.match(r"([A-Za-z]+)", rest)
        raw_type = type_match.group(1).upper() if type_match else "TEXT"
        if raw_type in _SQLITE_INTEGER:
            sqlite_type = "INTEGER"
        elif raw_type in _SQLITE_REAL:
            sqlite_type = "REAL"
        elif raw_type in _SQLITE_BLOB:
            sqlite_type = "BLOB"
        else:
            # JSON / DATETIME / TIMESTAMP / VARCHAR / TEXT 等统一存 TEXT。
            sqlite_type = "TEXT"
        defs.append((name, sqlite_type))
        seen.add(name)
    if not defs:
        return [("id", "INTEGER"), ("name", "TEXT"), ("status", "TEXT")]
    # SQLite 自增主键必须命名为 id 且类型为 INTEGER；契约没有 id 时补一列。
    if "id" not in seen:
        defs.insert(0, ("id", "INTEGER"))
    else:
        defs = [("id", "INTEGER")] + [
            (name, sqlite_type) for name, sqlite_type in defs if name != "id"
        ]
    return defs


def sqlite_schema_sql(table: str, column_defs: list[tuple[str, str]]) -> str:
    """生成可在沙盒内直接执行的 SQLite 建表语句。"""
    lines = ['  "id" INTEGER PRIMARY KEY AUTOINCREMENT']
    for name, sqlite_type in column_defs:
        if name == "id":
            continue
        lines.append(f'  "{name}" {sqlite_type}')
    body = ",\n".join(lines)
    return f'CREATE TABLE IF NOT EXISTS "{table}" (\n{body}\n);\n'


def _route_key(route: ArchitectApiRoute) -> tuple[str, str]:
    return (str(route.method or "GET").upper(), str(route.path or ""))


# 路径参数既可能出现在末尾（``/todos/{id}``），也可能出现在中间
# （``/tasks/{task_id}/stream``）。旧逻辑只识别末尾参数，导致
# ``/api/v1/tasks/{task_id}/stream`` 被误当成“集合路由”注入给前端，
# 浏览器请求的路径与 Express 实际注册的路径不一致，最终 404。
_PARAM_SEGMENT = re.compile(r"(^|/)(\{[^/{}]+\}|:[A-Za-z_]\w*)(?=/|$)")


def _has_path_params(path: str) -> bool:
    """判断路由是否包含任意路径参数（不限于末尾）。"""
    return bool(_PARAM_SEGMENT.search(str(path or "").split("?", 1)[0]))


def _is_item_path(path: str) -> bool:
    """仅末尾带参数的路径才是“单条记录”路由。"""
    return bool(re.search(r"(:\w+|\{\w+\})\s*/?$", path or ""))


# SSE / 长连接 / 动作类端点不能作为集合路由：
# 它们没有“列表 + 新增”的语义，前端数据面板也不能直接 GET。
_NON_COLLECTION_SUFFIXES = (
    "/stream",
    "/events",
    "/sse",
    "/subscribe",
    "/listen",
    "/health",
    "/fetch",
    "/search",
    "/login",
    "/logout",
    "/register",
    "/upload",
    "/download",
    "/export",
    "/import",
    "/tailor",
    "/approve",
    "/reject",
    # 业务动作不是 CRUD 集合根：/jobs/scrape、/chat/messages、
    # /applications/deliver 等必须由独立路由处理，不能被当作数据表。
    "/scrape",
    "/rewrite",
    "/deliver",
    "/messages",
    "/chat",
    "/analyze",
    "/generate",
    "/submit",
    "/send",
    "/reply",
    "/cancel",
    "/retry",
    "/execute",
    "/process",
    "/match",
    "/recommend",
    "/apply",
    "/publish",
    "/parse",
    "/extract",
)


def _is_collection_candidate(path: str) -> bool:
    """静态、以 /api/ 开头、且不是动作 / 流式端点的路径。"""
    normalized = str(path or "").split("?", 1)[0].strip()
    if not normalized.startswith("/api/"):
        return False
    if _has_path_params(normalized):
        return False
    lowered = normalized.rstrip("/").lower()
    return not any(
        lowered == suffix or lowered.endswith(suffix)
        for suffix in _NON_COLLECTION_SUFFIXES
    )


def collection_path(routes: Iterable[ArchitectApiRoute]) -> str:
    """选择前端数据面板使用的集合路径。

    选择顺序（必须与 BackendCoder 的 Express 注册路径完全一致）：
      1. 静态 GET 集合路由（``GET /api/v1/tasks``）；
      2. 任意方法的静态集合路由（例如只声明了 ``POST /api/v1/resumes``）；
      3. 从参数化路由推导出的资源集合（``/api/v1/tasks/{id}/stream``
         → ``/api/v1/tasks``）；
      4. 兜底 ``/api/v1/records``。

    所有候选都排除路径参数、``/stream``、``/events`` 等端点，避免把
    带参数或长连接的 URL 直接注入前端。
    """
    route_list = list(routes)
    static_routes = [
        str(route.path or "").split("?", 1)[0].strip()
        for route in route_list
        if _is_collection_candidate(str(route.path or ""))
    ]
    # 同一路径可能同时有 GET/POST，去重但保留声明顺序。
    unique_static = list(dict.fromkeys(static_routes))

    for route in route_list:
        path = str(route.path or "").split("?", 1)[0].strip()
        if str(route.method or "").upper() == "GET" and _is_collection_candidate(path):
            return path

    if unique_static:
        # 优先选择段数最少的资源根路径（``/api/v1/resumes`` 优先于
        # ``/api/v1/resumes/imports`` 这类子资源）。
        return sorted(unique_static, key=lambda item: (item.count("/"), len(item)))[0]

    # 没有静态路由时，从任意 /api/ 路径推导第一段资源名：
    # ``/api/v1/tasks/{task_id}/stream`` → ``/api/v1/tasks``。
    for route in route_list:
        path = str(route.path or "").split("?", 1)[0].strip()
        if not path.startswith("/api/"):
            continue
        segments = [segment for segment in path.split("/") if segment]
        # 至少保留 /api + 版本号 + 资源名；没有版本号时保留 /api + 资源名。
        if len(segments) >= 3:
            return "/" + "/".join(segments[:3])
        if len(segments) == 2:
            return "/" + "/".join(segments)
    return DEFAULT_COLLECTION_PATH


def item_path(routes: Iterable[ArchitectApiRoute], base: str) -> str:
    """返回与集合路径同资源的详情路由；没有则使用 ``<base>/:id``。

    不能随便挑契约里的第一个参数化路由：例如集合是
    ``/api/v1/resumes``，不能拿 ``/api/v1/tasks/{task_id}/stream`` 当详情。
    """
    normalized_base = str(base or "").rstrip("/")
    for route in routes:
        path = str(route.path or "").split("?", 1)[0].strip()
        if not _is_item_path(path):
            continue
        prefix = re.sub(r"/(:\w+|\{\w+\})\s*/?$", "", path).rstrip("/")
        if prefix == normalized_base:
            return re.sub(r"\s*/\s*$", "", path)
    return f"{normalized_base}/:id"


def _assert_route_pair(collection: str, detail: str) -> None:
    """生成阶段硬校验：前端与 Express 必须拿到同一条静态集合路径。

    只要集合路径里还残留 ``{param}`` / ``:param``，或不是 /api/ 开头，
    就说明契约的路由选择失败；直接抛错让 BuildVerifier 暴露问题，
    不允许把 404 路径写进前端。
    """
    if not collection.startswith("/api/"):
        raise ValueError(f"集合路由必须以 /api/ 开头: {collection}")
    if "{" in collection or "}" in collection:
        raise ValueError(f"集合路由残留路径参数: {collection}")
    if _PARAM_SEGMENT.search(collection):
        raise ValueError(f"集合路由不能包含 Express 路径参数: {collection}")
    if not detail.startswith("/api/"):
        raise ValueError(f"详情路由必须以 /api/ 开头: {detail}")


def _entity_label(contract: ArchitectContract, table: str) -> str:
    interactions = contract.component_interactions or []
    for interaction in interactions:
        name = (interaction.component or "").strip()
        if name:
            return name.replace("List", "").replace("Button", "") or table
    return table


def _seed_rows(columns: list[str], table: str, entity: str) -> list[dict[str, Any]]:
    """生成 3 条确定性种子数据，保证联调前后端立刻有内容可渲染。"""
    rows: list[dict[str, Any]] = []
    for index in range(1, 4):
        row: dict[str, Any] = {}
        for column in columns:
            lowered = column.lower()
            if lowered == "id":
                row[column] = index
            elif lowered == "tenant_id":
                row[column] = "tenant_local"
            elif lowered in {"name", "title"}:
                row[column] = f"示例{entity} {index}"
            elif lowered == "status":
                row[column] = "ACTIVE" if index < 3 else "PENDING"
            elif lowered in {"created_at", "updated_at"}:
                row[column] = f"2026-11-09T10:0{index}:00.000Z"
            elif lowered == "deleted_at":
                row[column] = None
            elif "amount" in lowered or "price" in lowered or "total" in lowered:
                row[column] = index * 100
            elif "count" in lowered or "qty" in lowered:
                row[column] = index
            else:
                row[column] = f"{table}_{column}_{index}"
        if "id" not in row:
            row["id"] = index
        rows.append(row)
    return rows


# ------------------------------------------------------------
# DBACoder
# ------------------------------------------------------------
def generate_dba_bundle(contract: ArchitectContract) -> GeneratedBundle:
    """生成 Sequelize ORM 持久化层与可执行的 SQLite 预览 DDL。

    生产环境通过 ``DATABASE_URL`` 连接契约选择的 MySQL / PostgreSQL；
    沙盒没有 DATABASE_URL 时，``server/db.js`` 自动回退到 Node 22 内置
    SQLite，保证预览与 E2E 不依赖外部数据库。
    """
    table = extract_table_name(contract.database_ddl)
    engine = contract_database_engine(contract)
    column_defs = extract_column_defs(contract.database_ddl)
    columns = [name for name, _ in column_defs]
    entity = _entity_label(contract, table)
    ddl = (contract.database_ddl or "").strip()
    if ddl and not ddl.endswith(";"):
        ddl += ";"
    header = (
        "-- 由 DBA Agent 根据《全栈实施契约》生成。\n"
        f"-- contract: {contract.contract_id}\n"
        f"-- table: {table}\n"
        f"-- 目标数据库: {engine}（生产通过 DATABASE_URL 连接）\n"
        "-- 说明：本文件是沙盒 SQLite 的预览建表语句；\n"
        "-- 生产 DDL 见 server/schema.<engine>.sql，ORM 见 server/db.js。\n\n"
    )
    db_code = SEQUELIZE_DB_TEMPLATE
    db_replacements = {
        "__TABLE__": table,
        "__ENTITY__": entity,
        "__DB_ENGINE__": engine,
        "__COLUMN_DEFS_JSON__": json.dumps(column_defs, ensure_ascii=False, indent=2),
        "__SEED_ROWS_JSON__": json.dumps(
            _seed_rows(columns, table, entity),
            ensure_ascii=False,
            indent=2,
        ),
    }
    for key, value in db_replacements.items():
        db_code = db_code.replace(key, value)

    schema_name = (
        "server/schema.sqlite.sql"
        if engine == "sqlite"
        else f"server/schema.{engine}.sql"
    )
    files = {
        "server/schema.sql": header + sqlite_schema_sql(table, column_defs),
        schema_name: (
            f"-- 契约原始 {engine} DDL（contract: {contract.contract_id}）\n\n"
            + ddl
            + "\n"
        ),
        "server/db.js": db_code,
        "server/migrate.mjs": MIGRATE_SCRIPT_TEMPLATE,
    }
    return GeneratedBundle(
        files=files,
        summary=(
            f"生成 {table} Sequelize ORM（目标 {engine}，"
            f"{len(columns)} 个字段）+ SQLite 预览 DDL"
        ),
        metadata={
            "table": table,
            "columns": columns,
            "columnTypes": column_defs,
            "databaseEngine": engine,
            "orm": "sequelize",
            "previewStorage": "sqlite",
        },
    )


# ------------------------------------------------------------
# BackendCoder
# ------------------------------------------------------------
def _express_path(path: str) -> str:
    """把契约里的 ``/todos/{id}`` 转为 Express 的 ``/todos/:id``。"""
    return re.sub(r"\{(\w+)\}", r":\1", path.strip())


_EXPRESS_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _coerce_route(
    raw: ArchitectApiRoute | dict[str, Any] | Any,
) -> ArchitectApiRoute | None:
    """把契约路由或自愈列表中的普通字典统一成 ``ArchitectApiRoute``。"""
    if isinstance(raw, ArchitectApiRoute):
        return raw
    if not isinstance(raw, dict):
        return None
    try:
        return ArchitectApiRoute.model_validate(
            {
                "method": str(raw.get("method") or "GET"),
                "path": str(raw.get("path") or ""),
                "request": str(raw.get("request") or ""),
                "response": str(raw.get("response") or ""),
                "description": str(
                    raw.get("description") or raw.get("reason") or "一致性补齐路由"
                ),
            }
        )
    except Exception:  # noqa: BLE001 - 单条脏数据不应阻断整次生成
        return None


def _merge_routes(
    routes: Iterable[ArchitectApiRoute],
    extra_routes: Iterable[ArchitectApiRoute | dict[str, Any]] | None,
) -> list[ArchitectApiRoute]:
    """合并契约路由与自愈补齐路由，按 (Method, Path) 去重。

    前端可能对同一路径声明多个动作（例如 GET/POST 同路径），因此不能只按
    Path 去重；同时过滤非法方法与非 ``/api/`` 路径，避免生成不可注册代码。
    """
    merged: list[ArchitectApiRoute] = []
    seen: set[tuple[str, str]] = set()
    for raw in [*routes, *(extra_routes or [])]:
        route = _coerce_route(raw)
        if route is None:
            continue
        method = str(route.method or "GET").upper().strip()
        path = str(route.path or "").split("?", 1)[0].strip()
        if method not in _EXPRESS_METHODS or not path.startswith("/api/"):
            continue
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            ArchitectApiRoute(
                method=method,
                path=path,
                request=route.request,
                response=route.response,
                description=route.description,
                auth_required=route.auth_required,
            )
        )
    return merged


def _business_route_code(route: ArchitectApiRoute) -> str:
    """为一个契约业务动作生成独立 Express 路由 + 确定性 Mock 处理逻辑。

    绝不能把“抓取 JD / AI 改写 / 投递 / 对话”折叠到通用 CRUD：路径必须
    与前端逐字符一致，响应必须携带 success/route/data，前端可据此渲染。
    """
    method = str(route.method).upper()
    path = _express_path(route.path)
    label = f"{method} {path}"
    description = route.description or f"{label} 业务动作"
    label_js = json.dumps(label, ensure_ascii=False)
    description_js = json.dumps(description, ensure_ascii=False)
    if method == "GET":
        payload = (
            "  response.json({\n"
            "    success: true,\n"
            f"    route: {label_js},\n"
            f"    description: {description_js},\n"
            "    data: [],\n"
            "    items: [],\n"
            "  })\n"
        )
    elif method == "DELETE":
        payload = (
            "  response.json({\n"
            "    success: true,\n"
            f"    route: {label_js},\n"
            f"    description: {description_js},\n"
            "    data: { deleted: true },\n"
            "  })\n"
        )
    else:
        status = 201 if method == "POST" else 200
        message_js = json.dumps(f"{description}（沙盒 Mock 响应）", ensure_ascii=False)
        payload = (
            "  const body = request.body && typeof request.body === 'object'"
            " ? request.body : {}\n"
            f"  response.status({status}).json({{\n"
            "    success: true,\n"
            f"    route: {label_js},\n"
            f"    description: {description_js},\n"
            "    data: Object.assign({ id: `mock-${Date.now()}` }, body),\n"
            f"    message: {message_js},\n"
            "  })\n"
        )
    return (
        f"// 契约业务动作：{description}\n"
        f"app.{method.lower()}("
        f"{json.dumps(path, ensure_ascii=False)}, asyncHandler(async (request, response) => {{\n"
        f"{payload}"
        "}))\n"
    )


def generate_backend_bundle(
    contract: ArchitectContract,
    *,
    extra_routes: Iterable[ArchitectApiRoute | dict[str, Any]] | None = None,
) -> GeneratedBundle:
    """按契约 apiRoutes 生成 Express + Sequelize 后端。

    ``server/db.js`` 与 ``server/package.json`` 由 DBACoder 一并生成；
    BackendCoder 负责 HTTP 层、依赖清单与运行说明，保持节点职责清晰。

    契约铁律（确定性模板没有可编辑的 System Prompt，规则直接固化在生成器）：
    契约里的**每一个业务动作**（抓取 JD、AI 改写、投递、对话等）都必须
    注册独立且语义匹配的 Express 路由，禁止只用一个通用 CRUD 接口敷衍。
    没有专属持久化需求的动作生成确定性 Mock 处理逻辑，保证前端请求不会 404。

    ``extra_routes`` 供一致性自愈使用：ConsistencyVerifier 发现缺失路由后，
    把 ``{method, path, description}`` 列表回传给 BackendCoder 补齐。
    """
    routes = _merge_routes(contract.api_routes or [], extra_routes)
    table = extract_table_name(contract.database_ddl)
    column_defs = extract_column_defs(contract.database_ddl)
    columns = [name for name, _ in column_defs]
    engine = contract_database_engine(contract)
    engine_label = DATABASE_ENGINE_LABELS.get(engine, engine)
    # 集合路径只允许静态资源根路径；详情路径由同一资源派生。
    # 前端与后端都从这里取值，避免各自“猜路由”导致 404。
    base = collection_path(routes)
    detail = _express_path(item_path(routes, base))
    _assert_route_pair(base, detail)
    express_base = _express_path(base)
    entity = _entity_label(contract, table)

    replacements = {
        "__API_PORT__": str(API_PORT),
        "__TABLE__": table,
        "__ENTITY__": entity,
        "__COLLECTION_PATH__": express_base,
        "__ITEM_PATH__": detail,
    }
    server_code = EXPRESS_SERVER_TEMPLATE
    for key, value in replacements.items():
        server_code = server_code.replace(key, value)

    # 通用 CRUD 已覆盖的路由不再重复注册；其余契约动作逐个生成独立端点。
    # 前端请求路径与 Express 注册路径都经过 _express_path，保证逐字符一致。
    registered: list[tuple[str, str, str]] = [
        ("GET", "/api/health", "express.template"),
        ("GET", express_base, "express.collection"),
        ("POST", express_base, "express.collection"),
        ("GET", "/api/data", "express.template"),
        ("POST", "/api/data", "express.template"),
    ]
    if detail and detail != express_base:
        for method in ("GET", "PUT", "PATCH", "DELETE"):
            registered.append((method, detail, "express.item"))
    registered_keys = {(method, path) for method, path, _source in registered}

    business_snippets: list[str] = []
    for route in routes:
        method = str(route.method or "GET").upper()
        path = _express_path(str(route.path or ""))
        key = (method, path)
        if key in registered_keys:
            continue
        business_snippets.append(_business_route_code(route))
        registered.append((method, path, "express.contract"))
        registered_keys.add(key)
    server_code = server_code.replace(
        "__CONTRACT_BUSINESS_ROUTES__",
        "\n".join(business_snippets) or "// 契约未声明额外业务路由。",
    )

    route_lines = []
    for route in routes:
        method = str(route.method or "GET").upper()
        path = str(route.path or "")
        description = route.description or "契约路由"
        route_lines.append(f"- `{method} {path}` — {description}")
    readme = (
        f"# {entity} Backend\n\n"
        "由 BackendCoder 根据契约生成：Express + Sequelize ORM。\n"
        f"目标数据库：**{engine_label}**；生产通过环境变量 `DATABASE_URL` 连接。\n"
        "沙盒未配置 `DATABASE_URL` 时自动回退到 Node 22 内置 SQLite，\n"
        "保证预览与 E2E 不依赖外部数据库。\n\n"
        f"- 表：`{table}`\n"
        f"- 集合路由：`{base}`\n"
        f"- 详情路由：`{detail}`\n"
        f"- 通用数据别名：`/api/data`\n"
        f"- 健康检查：`/api/health`（生产 storage={engine}，沙盒 storage=sqlite）\n"
        f"- 本地预览库：`data/fullstack.sqlite`\n"
        "- 迁移：`npm run db:migrate`（生产需 DATABASE_URL）\n\n"
        "## 契约路由\n\n"
        + ("\n".join(route_lines) if route_lines else "- 无显式路由，使用默认 CRUD")
        + "\n\n## 运行\n\n```bash\nnode server/index.js\n```\n\n"
        "## 生产环境变量\n\n"
        "```bash\n"
        f"DATABASE_URL='<{engine} 连接串>'\n"
        f"DB_ENGINE={engine}\n"
        "DB_AUTO_MIGRATE=true\n"
        "CORS_ORIGIN='https://your-frontend.example.com'\n"
        "```\n"
    )

    files = {
        "server/index.js": server_code,
        "server/README.md": readme,
        # 前后端联调的唯一事实来源：BuildVerifier / 前端生成都读取该清单，
        # 任何一端改路由都必须同时更新，避免路径漂移。
        "server/api-routes.json": json.dumps(
            {
                "prefix": "/api",
                "collection": express_base,
                "item": detail,
                # routes 是后端**实际注册**的完整路由表；一致性检查以它为准，
                # 不再只读取 collection / item 两个通用端点。
                "routes": [
                    {"method": method, "path": path, "source": source}
                    for method, path, source in registered
                ],
                "contractRoutes": [
                    {
                        "method": str(route.method or "GET").upper(),
                        "path": str(route.path or ""),
                    }
                    for route in routes
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        "server/package.json": json.dumps(
            {
                "name": "vibe-fullstack-server",
                "private": True,
                "type": "module",
                "dependencies": SERVER_DEPENDENCIES,
                "scripts": {
                    "start": "node index.js",
                    "db:migrate": "node migrate.mjs",
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    }
    return GeneratedBundle(
        files=files,
        summary=(
            f"生成 Express + Sequelize 后端（{engine_label}），"
            f"{len(routes) or 2} 条契约路由，"
            f"{len(business_snippets)} 条独立业务端点"
        ),
        metadata={
            "table": table,
            "columns": columns,
            "collectionPath": express_base,
            "itemPath": detail,
            "routeManifest": "server/api-routes.json",
            "entity": entity,
            "runtime": "express-sequelize",
            "previewRuntime": "express-sqlite",
            "databaseEngine": engine,
            "orm": "sequelize",
            "routes": [
                {"method": str(r.method or "GET").upper(), "path": str(r.path or "")}
                for r in routes
            ],
            "registeredRoutes": [
                {"method": method, "path": path, "source": source}
                for method, path, source in registered
            ],
            "businessRouteCount": len(business_snippets),
        },
    )


# ------------------------------------------------------------
# FrontendCoder
# ------------------------------------------------------------
_FRONTEND_PANEL = r"""// 由 FrontendCoder 根据契约 componentInteractions 生成。
// 目标：用真实后端数据替代 Mock 列表，并提供一个最小可验证的写入入口。
import { useEffect, useState } from 'react'

const COLLECTION_PATH = '__COLLECTION_PATH__'
const ENTITY = '__ENTITY__'
const TABLE = '__TABLE__'
// 生产前后端通常分域部署：构建时注入 VITE_API_BASE_URL；
// 沙盒未配置时为空字符串，继续走 Vite 同源 /api 代理。
const API_BASE = String(import.meta.env.VITE_API_BASE_URL || '').replace(
  /\/$/,
  '',
)

function apiUrl(path) {
  return `${API_BASE}${path}`
}

function normalizeItems(payload) {
  if (Array.isArray(payload)) return payload
  if (Array.isArray(payload?.items)) return payload.items
  return []
}

function displayName(item) {
  return String(
    item?.name ??
      item?.title ??
      item?.label ??
      `${ENTITY} #${item?.id ?? '?'}`,
  )
}

export default function FullstackDataPanel() {
  const [status, setStatus] = useState('loading')
  const [items, setItems] = useState([])
  const [name, setName] = useState('')
  const [storage, setStorage] = useState('database')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function load() {
    setStatus('loading')
    setError('')
    setSuccess('')
    try {
      const response = await fetch(apiUrl(COLLECTION_PATH), {
        headers: { Accept: 'application/json' },
      })
      if (!response.ok) {
        throw new Error(`GET ${COLLECTION_PATH} -> HTTP ${response.status}`)
      }
      const payload = await response.json()
      setItems(normalizeItems(payload))
      setStorage(String(payload?.storage || 'database'))
      setStatus('ready')
    } catch (cause) {
      setError(String(cause?.message || cause))
      setStatus('error')
    }
  }

  useEffect(() => {
    load()
  }, [])

  async function submit(event) {
    event.preventDefault()
    if (submitting) return
    setSubmitting(true)
    setError('')
    setSuccess('')
    try {
      const response = await fetch(apiUrl(COLLECTION_PATH), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'application/json',
          'Idempotency-Key': `ui-${Date.now()}`,
        },
        // 提交字段必须与 DDL 可写列一致，避免后端静默丢弃请求体。
        body: JSON.stringify({ __PAYLOAD_KEY__: name.trim() || `新${ENTITY}` }),
      })
      if (!response.ok) {
        throw new Error(`POST ${COLLECTION_PATH} -> HTTP ${response.status}`)
      }
      const payload = await response.json()
      // 兼容直接返回记录与 { data, message } 两种后端响应。
      const created = payload?.data ?? payload
      setItems((previous) => [
        created,
        ...previous.filter((item) => item?.id !== created?.id),
      ])
      setSuccess(payload?.message || '提交成功，数据已写入数据库')
      setName('')
      setStatus('ready')
    } catch (cause) {
      setError(String(cause?.message || cause))
      setStatus('error')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <aside
      data-fullstack-panel
      data-fullstack-status={status}
      data-fullstack-table={TABLE}
      style={{
        position: 'fixed',
        left: 14,
        bottom: 14,
        zIndex: 9999,
        width: 280,
        maxHeight: '72vh',
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        padding: 12,
        borderRadius: 14,
        background: 'rgba(2,6,23,.92)',
        border: '1px solid rgba(56,189,248,.35)',
        boxShadow: '0 18px 48px rgba(0,0,0,.45)',
        color: '#e2e8f0',
        fontFamily: '"Inter","Segoe UI",sans-serif',
        fontSize: 12,
        boxSizing: 'border-box',
      }}
    >
      <header style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
        <strong style={{ color: '#38bdf8' }}>{ENTITY} · 真实 API</strong>
        <span data-fullstack-count style={{ color: '#94a3b8' }}>
          {items.length}
        </span>
      </header>
      <code style={{ fontSize: 10.5, color: '#64748b' }}>
        {apiUrl(COLLECTION_PATH)}
      </code>

      {status === 'loading' && <span>正在请求后端…</span>}
      {status === 'error' && (
        <span data-fullstack-error style={{ color: '#f87171' }}>
          {error}
        </span>
      )}
      {status === 'ready' && (
        <span
          data-fullstack-ready
          data-fullstack-storage={storage}
          style={{ color: '#34d399' }}
        >
          后端数据已加载 · {storage}
        </span>
      )}
      {success && (
        <span
          data-fullstack-success
          role="status"
          style={{ color: '#4ade80', fontWeight: 600 }}
        >
          {success}
        </span>
      )}

      <div style={{ overflowY: 'auto', display: 'grid', gap: 6, minHeight: 42 }}>
        {items.slice(0, 6).map((item, index) => (
          <div
            key={`${item?.id ?? index}`}
            data-fullstack-row
            data-record-id={String(item?.id ?? '')}
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              gap: 8,
              padding: '6px 8px',
              borderRadius: 8,
              background: 'rgba(30,41,59,.72)',
            }}
          >
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {displayName(item)}
            </span>
            <em style={{ color: '#94a3b8', fontStyle: 'normal' }}>
              {String(item?.status ?? '')}
            </em>
          </div>
        ))}
      </div>

      <form onSubmit={submit} style={{ display: 'flex', gap: 6 }}>
        <input
          data-fullstack-input
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder={`新${ENTITY}名称`}
          style={{
            flex: 1,
            minWidth: 0,
            padding: '7px 9px',
            borderRadius: 8,
            border: '1px solid rgba(148,163,184,.35)',
            background: '#0f172a',
            color: '#e2e8f0',
          }}
        />
        <button
          type="submit"
          data-fullstack-submit
          disabled={submitting}
          style={{
            padding: '7px 10px',
            borderRadius: 8,
            border: 0,
            background: '#2563eb',
            color: '#fff',
            cursor: submitting ? 'wait' : 'pointer',
          }}
        >
          {submitting ? '提交中' : '新增'}
        </button>
      </form>
    </aside>
  )
}
"""


# 契约确认后的初始 React 骨架。
# 不依赖后端 API，只把契约中的路由 / 布局 / 组件渲染成真实页面，
# 让用户在沙盒里立刻看到可圈选、可 Inspect 的产品雏形。
_FRONTEND_SKELETON_TEMPLATE = r"""// 由 FrontendCoder 根据《架构实施契约》生成。
// 该文件是初始前端骨架：路由、页面布局与组件清单全部来自契约。
// 骨架模式（skeletonMode=true）只渲染 Header + Sidebar + 空白主区域，
// 绝不猜测用户没有要求的业务表单或卡片。
import { useMemo, useState } from 'react'

const APP_NAME = __APP_NAME__
const ROUTES = __ROUTES__
const LAYOUT = __LAYOUT__
const COMPONENTS = __COMPONENTS__
// 契约里已确认的组件交互；没有交互的组件保持静态，不生成假按钮。
const INTERACTIONS = __INTERACTIONS__
const SKELETON_MODE = __SKELETON_MODE__

// 生产前后端通常分域部署：构建时注入 VITE_API_BASE_URL；
// 沙盒未配置时为空字符串，继续走 Vite 同源 /api 代理。
const API_BASE = String(import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '')

function apiUrl(path) {
  return `${API_BASE}${path}`
}

const SECTIONS = COMPONENTS.reduce((groups, component) => {
  const name = component.section || '核心工作区'
  const group = groups.find((item) => item.name === name)
  if (group) group.components.push(component)
  else groups.push({ name, components: [component] })
  return groups
}, [])

// 用真实 CSS 媒体查询实现响应式 Grid：手机 1 列、平板 2 列、桌面 3 列。
// 生成代码不依赖 Tailwind，避免沙盒模板没有安装样式框架时“类名生效但
// 视觉不变”的假成功。
const GLOBAL_CSS = `
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body, #root { min-height: 100%; margin: 0; }
  body {
    background:
      radial-gradient(circle at top left, #1e293b 0%, #0f172a 42%, #020617 100%);
  }
  .app-shell {
    display: flex;
    width: 100%;
    min-height: 100vh;
    color: #e2e8f0;
    font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
  }
  .app-sidebar {
    position: sticky;
    top: 0;
    z-index: 20;
    display: flex;
    flex-direction: column;
    gap: 18px;
    width: 240px;
    flex: 0 0 240px;
    height: 100vh;
    max-height: 100vh;
    overflow-y: auto;
    padding: 24px 18px;
    border-right: 1px solid rgba(148, 163, 184, 0.16);
    background: rgba(2, 6, 23, 0.72);
  }
  .sidebar-kicker {
    color: #38bdf8;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 0.18em;
  }
  .sidebar-title { margin-top: 6px; font-size: 17px; font-weight: 800; }
  .sidebar-nav { display: flex; flex-direction: column; gap: 6px; }
  .nav-button {
    width: 100%;
    border: 1px solid transparent;
    border-radius: 10px;
    background: transparent;
    color: #94a3b8;
    padding: 10px 12px;
    text-align: left;
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
  }
  .nav-button:hover { background: rgba(148, 163, 184, 0.08); color: #e2e8f0; }
  .nav-button.is-active {
    border-color: rgba(56, 189, 248, 0.38);
    background: rgba(56, 189, 248, 0.12);
    color: #e0f2fe;
  }
  .sidebar-foot {
    margin-top: auto;
    color: #475569;
    font-size: 11px;
    line-height: 1.6;
  }
  .app-content {
    display: flex;
    flex: 1;
    flex-direction: column;
    min-width: 0;
    min-height: 100vh;
  }
  .app-header {
    position: sticky;
    top: 0;
    z-index: 10;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    padding: 16px 32px;
    border-bottom: 1px solid rgba(148, 163, 184, 0.16);
    background: rgba(2, 6, 23, 0.82);
  }
  .app-brand { font-size: 17px; font-weight: 800; letter-spacing: -0.02em; }
  .header-meta { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .header-hint {
    overflow: hidden;
    color: #64748b;
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .header-chip {
    flex: none;
    border: 1px solid rgba(56, 189, 248, 0.28);
    border-radius: 999px;
    background: rgba(56, 189, 248, 0.1);
    color: #7dd3fc;
    padding: 5px 10px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
  }
  .app-main {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 32px;
  }
  .content-wrap { width: 100%; max-width: 1180px; margin: 0 auto; }
  .welcome-panel {
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-height: calc(100vh - 160px);
    padding: 36px;
    border: 1px solid rgba(148, 163, 184, 0.18);
    border-radius: 24px;
    background:
      linear-gradient(135deg, rgba(30, 41, 59, 0.9), rgba(15, 23, 42, 0.82));
  }
  .kicker {
    margin: 0;
    color: #38bdf8;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 0.18em;
  }
  .welcome-title {
    margin: 4px 0 0;
    font-size: clamp(28px, 4vw, 44px);
    line-height: 1.12;
    letter-spacing: -0.03em;
  }
  .welcome-copy {
    max-width: 680px;
    margin: 0;
    color: #94a3b8;
    font-size: 15px;
    line-height: 1.75;
  }
  .welcome-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 16px;
    margin-top: 10px;
  }
  .hero {
    padding: 30px;
    border-radius: 22px;
    border: 1px solid rgba(148, 163, 184, 0.2);
    background:
      linear-gradient(135deg, rgba(30, 41, 59, 0.92), rgba(15, 23, 42, 0.82));
  }
  .hero-title { margin: 8px 0 6px; font-size: clamp(26px, 4vw, 36px); }
  .hero-copy { margin: 0; color: #94a3b8; line-height: 1.7; }
  .section { margin-top: 26px; }
  .section-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 12px;
  }
  .section-title { margin: 0; font-size: 18px; }
  .section-hint { color: #64748b; font-size: 12px; }
  .component-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 16px;
    align-items: start;
  }
  .component-card {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-height: 160px;
    padding: 20px;
    border-radius: 18px;
    border: 1px solid rgba(148, 163, 184, 0.18);
    background: rgba(15, 23, 42, 0.78);
    overflow: hidden;
  }
  .card-role {
    align-self: flex-start;
    padding: 3px 8px;
    border-radius: 999px;
    background: rgba(56, 189, 248, 0.12);
    color: #7dd3fc;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .card-title { margin: 0; font-size: 16px; }
  .card-copy { margin: 0; color: #94a3b8; font-size: 13px; line-height: 1.65; }
  .card-bar {
    margin-top: auto;
    height: 8px;
    border-radius: 999px;
    background:
      linear-gradient(90deg, rgba(56, 189, 248, 0.35), rgba(148, 163, 184, 0.12));
  }
  .interaction-panel {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: auto;
    padding-top: 10px;
    border-top: 1px solid rgba(148, 163, 184, 0.14);
  }
  .interaction-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .interaction-input {
    flex: 1 1 150px;
    min-width: 0;
    padding: 8px 10px;
    border: 1px solid rgba(148, 163, 184, 0.28);
    border-radius: 9px;
    background: rgba(2, 6, 23, 0.62);
    color: #e2e8f0;
    font: inherit;
    font-size: 12.5px;
  }
  .interaction-input:focus {
    outline: none;
    border-color: rgba(56, 189, 248, 0.62);
    box-shadow: 0 0 0 3px rgba(14, 165, 233, 0.14);
  }
  .interaction-button {
    flex: none;
    padding: 8px 14px;
    border: 1px solid rgba(56, 189, 248, 0.45);
    border-radius: 9px;
    background: rgba(14, 116, 144, 0.42);
    color: #e0f2fe;
    font-size: 12.5px;
    font-weight: 700;
    cursor: pointer;
    transition: background 0.16s ease, border-color 0.16s ease;
  }
  .interaction-button:hover:not(:disabled) {
    border-color: rgba(125, 211, 252, 0.72);
    background: rgba(14, 116, 144, 0.64);
  }
  .interaction-button:disabled {
    opacity: 0.6;
    cursor: wait;
  }
  .interaction-button.is-delete {
    border-color: rgba(248, 113, 113, 0.45);
    background: rgba(127, 29, 29, 0.4);
    color: #fecaca;
  }
  .interaction-status {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin: 0;
    padding: 7px 9px;
    border-radius: 8px;
    background: rgba(148, 163, 184, 0.1);
    color: #cbd5e1;
    font-size: 11.5px;
    line-height: 1.5;
  }
  .interaction-status.is-success {
    background: rgba(6, 78, 59, 0.36);
    color: #6ee7b7;
  }
  .interaction-status.is-error {
    background: rgba(127, 29, 29, 0.32);
    color: #fca5a5;
  }
  .interaction-status button {
    padding: 3px 8px;
    border: 1px solid currentColor;
    border-radius: 6px;
    background: transparent;
    color: inherit;
    font-size: 10.5px;
    cursor: pointer;
  }
  .interaction-route {
    overflow: hidden;
    color: #475569;
    font-family: 'SFMono-Regular', Consolas, monospace;
    font-size: 10px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .placeholder-card {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 10px;
    min-height: 280px;
    padding: 28px;
    border: 1px dashed rgba(125, 211, 252, 0.4);
    border-radius: 18px;
    background: rgba(8, 47, 73, 0.22);
    text-align: center;
    overflow-y: auto;
  }
  .placeholder-icon {
    display: grid;
    place-items: center;
    width: 44px;
    height: 44px;
    border-radius: 14px;
    background: rgba(56, 189, 248, 0.14);
    color: #7dd3fc;
    font-size: 20px;
  }
  .placeholder-title { font-size: 17px; }
  .placeholder-copy { max-width: 420px; color: #94a3b8; font-size: 13px; line-height: 1.65; }
  .app-footer {
    padding: 18px 32px 28px;
    color: #475569;
    font-size: 12px;
    text-align: center;
  }
  @media (min-width: 768px) {
    .component-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .welcome-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (min-width: 1200px) {
    .component-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  }
  @media (max-width: 767px) {
    .app-shell { flex-direction: column; }
    .app-sidebar {
      position: static;
      width: 100%;
      flex: none;
      height: auto;
      max-height: none;
      padding: 16px 18px;
      border-right: 0;
      border-bottom: 1px solid rgba(148, 163, 184, 0.16);
    }
    .sidebar-nav { flex-direction: row; overflow-x: auto; }
    .nav-button { width: auto; flex: none; white-space: nowrap; }
    .sidebar-foot { display: none; }
    .app-header { padding: 14px 18px; }
    .header-hint { display: none; }
    .app-main { padding: 18px; }
    .welcome-panel { min-height: 0; padding: 24px; }
  }
`

function hasInteractions(componentId) {
  return INTERACTIONS.some((item) => item.componentId === componentId)
}

/**
 * 契约交互面板：把 componentInteractions 渲染成真实按钮 + fetch。
 *
 * 状态机严格区分 idle / loading / success / error：
 *  - loading 时按钮禁用并显示“处理中…”，防止重复提交；
 *  - 失败展示后端 message / HTTP 状态，并提供“重试”；
 *  - 成功展示后端返回的 message 或记录条数。
 * 纯前端交互（apiRoute 为空）只触发本地状态，不伪造网络请求。
 */
function InteractionPanel({ component }) {
  const actions = INTERACTIONS.filter(
    (item) => item.componentId === component.id,
  )
  const [state, setState] = useState('idle')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [value, setValue] = useState('')
  const [lastAction, setLastAction] = useState(null)
  if (actions.length === 0) return null

  const needsInput = actions.some(
    (item) => item.params.length > 0 || item.payloadKey,
  )
  const inputLabel =
    actions[0].params.length > 0
      ? '记录 ID'
      : actions[0].payloadKey || '输入内容'

  async function run(action) {
    if (!action || state === 'loading') return
    setLastAction(action)
    setError('')
    setMessage('')
    if (action.method === 'LOCAL') {
      setState('success')
      setMessage(action.logic || '本地交互已触发')
      return
    }
    const needsValue = action.params.length > 0 || action.payloadKey
    if (needsValue && !value.trim()) {
      setState('error')
      setError(
        action.params.length > 0
          ? '请先填写记录 ID'
          : `请先填写 ${action.payloadKey}`,
      )
      return
    }
    setState('loading')
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), 15000)
    try {
      let path = action.apiRoute
      action.params.forEach((name) => {
        path = path.replace(`:${name}`, encodeURIComponent(value.trim()))
      })
      const options = {
        method: action.method,
        signal: controller.signal,
        headers: { Accept: 'application/json' },
      }
      if (!['GET', 'HEAD'].includes(action.method)) {
        options.headers['Content-Type'] = 'application/json'
        options.body = JSON.stringify(
          action.payloadKey ? { [action.payloadKey]: value.trim() } : {},
        )
      }
      const response = await fetch(apiUrl(path), options)
      const payload = await response.json().catch(() => null)
      if (!response.ok) {
        throw new Error(
          payload?.message ||
            payload?.code ||
            `${action.method} ${path} -> HTTP ${response.status}`,
        )
      }
      setState('success')
      const rows = Array.isArray(payload)
        ? payload
        : Array.isArray(payload?.items)
          ? payload.items
          : null
      setMessage(
        payload?.message ||
          `${action.method} ${path} 成功${rows ? `（${rows.length} 条）` : ''}`,
      )
    } catch (cause) {
      setState('error')
      setError(
        cause?.name === 'AbortError'
          ? `请求超时：${action.method} ${action.apiRoute}`
          : String(cause?.message || cause),
      )
    } finally {
      window.clearTimeout(timer)
    }
  }

  return (
    <div
      className="interaction-panel"
      data-interaction-panel
      data-source={`interaction-${component.id}`}
      data-source-kind="container"
    >
      <div className="interaction-row">
        {needsInput && (
          <input
            className="interaction-input"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder={inputLabel}
            aria-label={inputLabel}
            disabled={state === 'loading'}
          />
        )}
        {actions.map((action, index) => (
          <button
            key={`${action.event}-${index}`}
            type="button"
            className={`interaction-button${action.method === 'DELETE' ? ' is-delete' : ''}`}
            data-interaction-run={action.method}
            data-interaction-route={action.apiRoute}
            onClick={() => run(action)}
            disabled={state === 'loading'}
            aria-disabled={state === 'loading'}
          >
            {state === 'loading' ? '处理中…' : action.label}
          </button>
        ))}
      </div>
      {state === 'loading' && (
        <p className="interaction-status" role="status" aria-live="polite">
          正在请求后端，请稍候…
        </p>
      )}
      {state === 'success' && (
        <p
          className="interaction-status is-success"
          role="status"
          aria-live="polite"
          data-interaction-status="success"
        >
          {message}
        </p>
      )}
      {state === 'error' && (
        <p
          className="interaction-status is-error"
          role="alert"
          data-interaction-status="error"
        >
          {error}
          <button type="button" onClick={() => run(lastAction)}>
            重试
          </button>
        </p>
      )}
      <span className="interaction-route">
        {actions[0].method === 'LOCAL'
          ? '本地交互 · 无网络请求'
          : `${actions[0].method} ${actions[0].apiRoute}`}
      </span>
    </div>
  )
}

function Placeholder({ title, description, id }) {
  return (
    <div
      className="placeholder-card"
      data-source={`placeholder-${id || 'pending'}`}
      data-source-kind="container"
    >
      <span className="placeholder-icon" aria-hidden="true">
        ◇
      </span>
      <strong className="placeholder-title" data-source={`placeholder-title-${id || 'pending'}`}>
        {title || '待定区域'}
      </strong>
      <span className="placeholder-copy">
        {description || '需求未明确，等待后续补充；当前只渲染占位区域。'}
      </span>
    </div>
  )
}

export default function App() {
  const [activePath, setActivePath] = useState(ROUTES[0]?.path || '/')
  const activeRoute = useMemo(
    () => ROUTES.find((route) => route.path === activePath) || ROUTES[0],
    [activePath],
  )
  const sidebarLayout = LAYOUT.find((item) => item.region === 'sidebar')
  const headerLayout = LAYOUT.find((item) => item.region === 'header')
  const mainLayout = LAYOUT.find((item) => item.region === 'main')

  // 侧边栏导航必须真正切换主内容区，而不是只改变高亮。
  // 组件若带 route 字段则严格跟随该路由；没有 route 时按 section 的
  // 稳定哈希分配到不同页面，保证每个导航项都有独立内容，避免“点了没反应”。
  const routeComponents = useMemo(() => {
    const groups = new Map(ROUTES.map((route) => [route.path, []]))
    const routePaths = ROUTES.map((route) => route.path)
    const routeIndexByName = new Map(
      ROUTES.map((route, index) => [route.name, index]),
    )
    COMPONENTS.forEach((component) => {
      let targetPath = String(component.route || '').trim()
      if (!targetPath) {
        const byName = routeIndexByName.get(component.section)
        if (byName !== undefined) {
          targetPath = routePaths[byName]
        }
      }
      if (!targetPath) {
        // 稳定分配：同一份契约每次生成的归属完全一致。
        let hash = 0
        const text = String(component.section || component.id || '')
        for (let index = 0; index < text.length; index += 1) {
          hash = (hash * 31 + text.charCodeAt(index)) >>> 0
        }
        targetPath = routePaths[hash % routePaths.length]
      }
      const bucket = groups.get(targetPath) || groups.get(routePaths[0])
      bucket.push(component)
    })
    return groups
  }, [])
  const activeComponents = routeComponents.get(activeRoute?.path) || []

  return (
    <div className="app-shell">
      <style>{GLOBAL_CSS}</style>
      <aside
        className="app-sidebar"
        data-source="app-sidebar"
        data-source-kind="container"
      >
        <div>
          <div className="sidebar-kicker">WORKSPACE</div>
          <div className="sidebar-title">{sidebarLayout?.title || '导航'}</div>
        </div>
        <nav className="sidebar-nav" data-source="app-nav" data-source-kind="container">
          {ROUTES.map((route, index) => (
            <button
              key={route.path}
              type="button"
              data-source={`nav-${index}`}
              className={`nav-button${route.path === activePath ? ' is-active' : ''}`}
              onClick={() => setActivePath(route.path)}
            >
              {route.name}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          {SKELETON_MODE
            ? '空白骨架阶段：后续通过对话或画布圈选逐步补充业务。'
            : '契约组件已就绪，可圈选组件发起增量修改。'}
        </div>
      </aside>

      <div className="app-content">
        <header
          className="app-header"
          data-source="app-header"
          data-source-kind="container"
        >
          <div className="app-brand" data-source="app-brand">
            {APP_NAME}
          </div>
          <div className="header-meta">
            <span className="header-hint">
              {headerLayout?.title || activeRoute?.name || APP_NAME}
            </span>
            <span className="header-chip">
              {SKELETON_MODE ? 'SKELETON' : 'LIVE'}
            </span>
          </div>
        </header>

        <main
          className="app-main"
          data-source="app-main"
          data-source-kind="container"
        >
          <div className="content-wrap">
            {SKELETON_MODE ? (
              <section
                className="welcome-panel"
                data-source="welcome-panel"
                data-source-kind="container"
                data-route-path={activeRoute?.path || '/'}
              >
                <p className="kicker" data-source="page-kicker">
                  {activeRoute?.path === ROUTES[0]?.path
                    ? 'INITIAL SKELETON'
                    : 'ROUTE WORKSPACE'}
                </p>
                <h1 className="welcome-title" data-source="page-title">
                  {activeRoute?.path === ROUTES[0]?.path
                    ? 'Welcome to your new app'
                    : activeRoute?.name || '待定页面'}
                </h1>
                <p className="welcome-copy" data-source="page-copy">
                  {activeRoute?.description ||
                    mainLayout?.description ||
                    '应用骨架已就绪。先确认整体布局，再通过对话或画布圈选逐步添加具体业务功能。'}
                </p>
                <div className="welcome-grid">
                  <Placeholder
                    id={`pending-${String(activeRoute?.path || '/').replace(/[^a-zA-Z0-9]+/g, '-')}`}
                    title={activeComponents[0]?.name || '待定区域'}
                    description={activeComponents[0]?.description}
                  />
                </div>
              </section>
            ) : (
              <>
                <section
                  className="hero"
                  data-source="page-hero"
                  data-source-kind="container"
                  data-route-path={activeRoute?.path || '/'}
                >
                  <p className="kicker" data-source="page-kicker">
                    {headerLayout?.title || 'INITIAL SKELETON'}
                  </p>
                  <h1 className="hero-title" data-source="page-title">
                    {activeRoute?.name || APP_NAME}
                  </h1>
                  <p className="hero-copy" data-source="page-copy">
                    {activeRoute?.description || '根据架构契约生成的初始页面骨架。'}
                  </p>
                </section>

                {activeComponents.length === 0 ? (
                  <section className="section" key={`empty-${activeRoute?.path}`}>
                    <div className="component-grid">
                      <Placeholder
                        id={`empty-${String(activeRoute?.path || '/').replace(/[^a-zA-Z0-9]+/g, '-')}`}
                        title={`${activeRoute?.name || '当前页面'} · 待补充`}
                        description="该路由尚无契约组件；可以在此页面继续圈选并发起新增。"
                      />
                    </div>
                  </section>
                ) : (
                  activeComponents.map((component) =>
                    component.isPlaceholder ? (
                      <section className="section" key={component.id}>
                        <div className="component-grid">
                          <Placeholder
                            id={component.id}
                            title={component.name}
                            description={component.description}
                          />
                        </div>
                      </section>
                    ) : (
                      <section className="section" key={component.id}>
                        <div className="component-grid">
                          <article
                            className="component-card"
                            key={component.id}
                            data-source={`component-${component.id}`}
                            data-source-kind="container"
                          >
                            <span className="card-role">
                              {component.role || 'module'}
                            </span>
                            <h3
                              className="card-title"
                              data-source={`component-title-${component.id}`}
                            >
                              {component.name}
                            </h3>
                            <p
                              className="card-copy"
                              data-source={`component-copy-${component.id}`}
                            >
                              {component.description || '等待接入具体数据与交互。'}
                            </p>
                            {hasInteractions(component.id) ? (
                              <InteractionPanel component={component} />
                            ) : (
                              <div className="card-bar" />
                            )}
                          </article>
                        </div>
                      </section>
                    ),
                  )
                )}
              </>
            )}
          </div>
        </main>

        <footer className="app-footer" data-source="app-footer">
          由 Vibe Platform Architect 契约生成 · 当前为可交互前端骨架
        </footer>
      </div>
    </div>
  )
}
"""


def _slug(value: str, fallback: str) -> str:
    """把契约中的中文组件名转换为稳定的 data-source 标识。"""
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "")).strip("-").lower()
    return text or fallback


def _skeleton_routes(contract: ArchitectContract) -> list[dict[str, str]]:
    routes: list[dict[str, str]] = []
    for index, raw in enumerate(contract.frontend_routes or [], start=1):
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path") or "").strip() or f"/page-{index}"
        name = str(raw.get("name") or raw.get("title") or f"页面 {index}").strip()
        routes.append(
            {
                "path": path if path.startswith("/") else f"/{path}",
                "name": name,
                "description": str(raw.get("description") or "").strip(),
            }
        )
    if not routes:
        routes.append(
            {
                "path": "/",
                "name": contract.tech_stack.get("frontendTitle") or "首页",
                "description": "根据架构契约生成的初始页面。",
            }
        )
    return routes


def _skeleton_layout(contract: ArchitectContract) -> list[dict[str, str]]:
    layout: list[dict[str, str]] = []
    for raw in contract.page_layout or []:
        if not isinstance(raw, dict):
            continue
        description = str(raw.get("description") or "").strip()
        title = str(raw.get("title") or raw.get("name") or "").strip()
        # 模型有时只给出 region + description（例如 {"region":"header",
        # "description":"品牌与导航"}）。此时用描述兜底标题，避免丢弃布局。
        if not title:
            title = description
        if not title:
            continue
        layout.append(
            {
                "region": str(raw.get("region") or "main").strip().lower(),
                "title": title,
                "description": description,
            }
        )
    # 新项目初始化固定是 Header + Sidebar + Main 三段式骨架。
    # 契约漏掉某个区域时补齐，而不是让页面退化成单列堆叠。
    defaults = {
        "header": {"region": "header", "title": "顶部导航", "description": ""},
        "sidebar": {"region": "sidebar", "title": "侧边栏", "description": ""},
        "main": {"region": "main", "title": "主内容区", "description": ""},
    }
    regions = {item["region"] for item in layout}
    for region in ("header", "sidebar", "main"):
        if region not in regions:
            layout.append(defaults[region])
    return layout


def _placeholder_component(
    *,
    name: str = "待定区域",
    description: str = "",
    identifier: str = "pending",
) -> dict[str, Any]:
    """构造唯一允许的占位组件。"""
    title = str(name or "待定区域").strip() or "待定区域"
    return {
        "id": identifier,
        "name": title,
        "role": "placeholder",
        "section": "主内容区",
        "description": (
            str(description or "").strip()
            or "需求未明确，等待后续补充；当前只渲染占位区域。"
        ),
        "isPlaceholder": True,
        "placeholder": f'<Placeholder title="{title}" />',
    }


def _skeleton_components(contract: ArchitectContract) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, raw in enumerate(contract.components or [], start=1):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("title") or f"Component{index}").strip()
        if not name:
            continue
        identifier = _slug(name, f"component-{index}")
        if identifier in used:
            identifier = f"{identifier}-{index}"
        used.add(identifier)
        is_placeholder = bool(raw.get("isPlaceholder") or raw.get("is_placeholder"))
        item: dict[str, Any] = {
            "id": identifier,
            "name": name,
            "role": (
                "placeholder"
                if is_placeholder
                else str(raw.get("role") or "module").strip()
            ),
            "section": str(raw.get("section") or "核心工作区").strip(),
            # 可选路由归属：模型若声明该组件属于某个页面，前端必须按
            # route 渲染；没有声明时由模板做稳定分配。
            "route": str(raw.get("route") or raw.get("page") or "").strip(),
            "description": str(raw.get("description") or "").strip(),
            "isPlaceholder": is_placeholder,
        }
        if is_placeholder:
            item["placeholder"] = (
                str(raw.get("placeholder") or "").strip()
                or f'<Placeholder title="{name}" />'
            )
        components.append(item)
    if contract.skeleton_mode:
        # 骨架模式的确定性保险丝：无论历史契约里混入了多少组件，
        # 前端只渲染一个占位区域，绝不把模型脑补的业务卡片铺出来。
        placeholder = next(
            (item for item in components if item.get("isPlaceholder")),
            None,
        )
        return [placeholder or _placeholder_component()]
    if not components:
        components.append(_placeholder_component())
    return components


# ------------------------------------------------------------
# 契约交互 → 前端事件 / 网络请求
# ------------------------------------------------------------
# 常见动作关键词：只用于在没有显式 method 时推断 HTTP 方法。
_DELETE_HINTS = ("删除", "移除", "delete", "remove", "destroy")
_READ_HINTS = (
    "加载",
    "查询",
    "搜索",
    "获取",
    "列表",
    "刷新",
    "load",
    "list",
    "fetch",
    "query",
    "search",
    "get",
)
_UPDATE_HINTS = ("更新", "修改", "编辑", "update", "edit", "patch", "rename")
_CREATE_HINTS = (
    "提交",
    "新增",
    "创建",
    "保存",
    "添加",
    "create",
    "submit",
    "save",
    "add",
    "post",
)


def _interaction_value(item: Any, name: str) -> str:
    """同时兼容 Pydantic 模型与原始 dict 的字段读取。"""
    if isinstance(item, dict):
        return str(item.get(name) or "").strip()
    return str(getattr(item, name, "") or "").strip()


def _route_shape(path: str) -> tuple[str, ...]:
    """把 ``{id}`` / ``:id`` 统一为 ``:param``，用于契约路由匹配。"""
    raw = str(path or "").split("?", 1)[0].strip().strip("/")
    if not raw:
        return ()
    segments: list[str] = []
    for segment in raw.split("/"):
        if segment.startswith("{") or segment.startswith(":"):
            segments.append(":param")
        elif segment:
            segments.append(segment)
    return tuple(segments)


def _route_param_names(path: str) -> list[str]:
    """提取路由中的参数名，前端用输入值替换后再发起真实请求。"""
    return [
        brace or colon
        for brace, colon in re.findall(r"\{(\w+)\}|:([A-Za-z_]\w*)", str(path or ""))
        if brace or colon
    ]


def _infer_interaction_method(event: str, logic: str, path: str) -> str:
    """按事件与逻辑文本推断 HTTP 方法；契约显式声明时以声明为准。

    顺序很重要：``"提交后保存并刷新列表"`` 同时包含“提交”和“刷新”，
    必须判定为 POST；只有纯加载 / 查询事件才判定为 GET。
    """
    text = f"{event} {logic}".casefold()
    normalized_event = str(event or "").strip().casefold()
    if any(hint in text for hint in _DELETE_HINTS):
        return "DELETE"
    if any(hint in text for hint in _UPDATE_HINTS):
        return "PUT"
    if any(hint in text for hint in _CREATE_HINTS):
        return "POST"
    if normalized_event in {
        "load",
        "fetch",
        "query",
        "search",
        "refresh",
        "change",
    } or any(hint in text for hint in _READ_HINTS):
        return "GET"
    # 默认：带路径参数视为更新，否则视为提交。
    return "PUT" if _route_param_names(path) else "POST"


def _resolve_interaction_method(
    contract: ArchitectContract,
    event: str,
    logic: str,
    path: str,
    declared_method: str = "",
) -> str:
    """优先采用契约 api_routes 中与路径形状一致的声明方法。

    历史契约可能把 ``api_route`` 写成 ``"GET /api/v1/todos"``；显式方法
    优先，其次采用同形路由的声明方法，最后才按动作语义推断。
    这样前端请求的 Method 与后端 Express 注册的方法天然一致；找不到
    同形路由时才交给 ConsistencyVerifier 做最终阻断。
    """
    inferred = declared_method or _infer_interaction_method(event, logic, path)
    shape = _route_shape(path)
    methods = [
        str(route.method or "GET").upper()
        for route in (contract.api_routes or [])
        if _route_shape(route.path) == shape
    ]
    if not methods:
        return inferred
    if inferred in methods:
        return inferred
    if inferred == "GET" and "GET" in methods:
        return "GET"
    # 同形路由同时存在 GET/POST 时，写入动作优先 POST。
    if inferred != "GET" and "POST" in methods:
        return "POST"
    return methods[0]


def _interaction_payload_key(contract: ArchitectContract, method: str) -> str:
    """选择 POST/PUT/PATCH 的默认写入字段。

    BackendCoder 的 ``buildValues`` 只接收 DDL 中的可写列；前端必须用
    同一个列名提交，否则请求体结构会与后端 DTO 不一致。
    """
    if method not in {"POST", "PUT", "PATCH"}:
        return ""
    columns = [name for name, _ in extract_column_defs(contract.database_ddl)]
    writable = [
        column
        for column in columns
        if column.lower()
        not in {"id", "tenant_id", "created_at", "updated_at", "deleted_at", "status"}
    ]
    if not writable:
        return ""
    for preferred in ("name", "title", "label"):
        if preferred in (column.lower() for column in writable):
            return next(column for column in writable if column.lower() == preferred)
    return writable[0]


def _interaction_label(method: str, event: str) -> str:
    if method == "LOCAL":
        return "执行"
    if method == "GET":
        return "加载数据"
    if method == "DELETE":
        return "删除"
    if method in {"PUT", "PATCH"}:
        return "保存"
    if method == "POST":
        return "提交"
    return event or "执行"


def _skeleton_interactions(
    contract: ArchitectContract,
    components: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把 componentInteractions 翻译成可直接渲染的事件描述。

    规则：
      - 只接受用户在澄清阶段确认过的交互；
      - 占位组件永远不生成交互，保持静态；
      - apiRoute 为空表示纯前端交互（弹窗 / 切换 / 本地校验），
        前端仍生成真实 onClick，但不发网络请求；
      - apiRoute 非空时保留参数名，前端用输入值替换后 fetch。
    """
    lookup: dict[str, dict[str, Any]] = {}
    for component in components:
        lookup[_slug(component.get("name", ""), component.get("id", ""))] = component
        lookup[str(component.get("name", "")).casefold()] = component
    interactions: list[dict[str, Any]] = []
    for raw in contract.component_interactions or []:
        name = _interaction_value(raw, "component")
        if not name:
            continue
        component = lookup.get(_slug(name, name)) or lookup.get(name.casefold())
        # 未匹配到组件或组件是占位符：不伪造交互，保持静态。
        if component is None or component.get("isPlaceholder"):
            continue
        event = _interaction_value(raw, "event") or "click"
        raw_route = _interaction_value(raw, "api_route")
        logic = _interaction_value(raw, "logic")
        # 兼容 "GET /api/v1/todos" 这类把方法写进 api_route 的历史契约。
        method_match = re.match(
            r"^(GET|POST|PUT|PATCH|DELETE)\s+(/\S*)$",
            raw_route,
            re.IGNORECASE,
        )
        declared_method = method_match.group(1).upper() if method_match else ""
        api_route = method_match.group(2).strip() if method_match else raw_route
        local_only = not api_route or not api_route.startswith("/api/")
        method = (
            "LOCAL"
            if local_only
            else _resolve_interaction_method(
                contract,
                event,
                logic,
                api_route,
                declared_method,
            )
        )
        route_template = re.sub(r"\{(\w+)\}", r":\1", api_route)
        params = _route_param_names(route_template)
        interactions.append(
            {
                "componentId": component.get("id"),
                "component": name,
                "event": event,
                "method": method,
                "apiRoute": route_template,
                "params": params,
                "payloadKey": (
                    "" if local_only else _interaction_payload_key(contract, method)
                ),
                "logic": logic,
                "label": _interaction_label(method, event),
            }
        )
    return interactions


def generate_frontend_skeleton_bundle(contract: ArchitectContract) -> GeneratedBundle:
    """根据契约生成初始 React 前端骨架（不依赖后端 API）。

    输出只有 ``src/App.jsx``：沙盒模板已提供 main.jsx / vite.config.js /
    package.json。所有导航、页面标题与组件卡片都来自契约的一级字段，
    并写入 ``data-source`` 供 Inspect SDK 精确定位源码。

    ``componentInteractions`` 中已确认的交互会被翻译成真实的 onClick +
    fetch（含 Loading / Error / Success 三态）；没有交互或占位组件只渲染
    静态卡片，绝不生成假的点击行为。
    """
    app_name = (
        contract.tech_stack.get("frontendTitle")
        or contract.tech_stack.get("appName")
        or "我的应用"
    )
    routes = _skeleton_routes(contract)
    layout = _skeleton_layout(contract)
    components = _skeleton_components(contract)
    # 骨架模式是“空白骨架”保险丝：不渲染任何交互。
    interactions = (
        [] if contract.skeleton_mode else _skeleton_interactions(contract, components)
    )
    content = (
        _FRONTEND_SKELETON_TEMPLATE.replace(
            "__APP_NAME__", json.dumps(str(app_name), ensure_ascii=False)
        )
        .replace("__ROUTES__", json.dumps(routes, ensure_ascii=False, indent=2))
        .replace("__LAYOUT__", json.dumps(layout, ensure_ascii=False, indent=2))
        .replace("__COMPONENTS__", json.dumps(components, ensure_ascii=False, indent=2))
        .replace(
            "__INTERACTIONS__",
            json.dumps(interactions, ensure_ascii=False, indent=2),
        )
        .replace("__SKELETON_MODE__", "true" if contract.skeleton_mode else "false")
    )
    return GeneratedBundle(
        files={"src/App.jsx": content},
        summary=(
            "按契约生成干净的应用骨架："
            f"{len(routes)} 个路由、{len(components)} 个占位/业务组件"
            if contract.skeleton_mode
            else f"按契约生成 React 骨架：{len(routes)} 个路由、"
            f"{len(components)} 个组件、{len(interactions)} 个真实交互"
        ),
        metadata={
            "routes": len(routes),
            "components": len(components),
            "interactions": len(interactions),
            "mode": "frontend",
            "skeletonMode": contract.skeleton_mode,
        },
    )


def generate_frontend_bundle(
    contract: ArchitectContract,
    *,
    existing_main: str | None,
    existing_vite: str | None,
    existing_package: str | None,
) -> GeneratedBundle:
    """生成 API 面板、开发启动脚本，并注入 main.jsx / vite / package。"""
    routes = contract.api_routes or []
    table = extract_table_name(contract.database_ddl)
    # 与 BackendCoder 使用完全相同的路由选择函数与 Express 转换，
    # 保证前端请求路径与 server/index.js 注册路径逐字符一致。
    base = _express_path(collection_path(routes))
    entity = _entity_label(contract, table)
    _assert_route_pair(base, _express_path(item_path(routes, base)))

    # 面板的提交字段同样取自 DDL：没有 name 列时退回 title / label / 首个可写列。
    payload_key = _interaction_payload_key(contract, "POST") or "name"
    panel = (
        _FRONTEND_PANEL.replace("__COLLECTION_PATH__", base)
        .replace("__ENTITY__", entity)
        .replace("__TABLE__", table)
        .replace("__PAYLOAD_KEY__", payload_key)
    )
    files: dict[str, str] = {
        "src/fullstack-api.jsx": panel,
        "scripts/dev.mjs": DEV_SCRIPT_TEMPLATE,
    }

    if not existing_main:
        raise ValueError(
            "沙盒缺少 src/main.jsx，无法挂载 FullstackDataPanel；"
            "MVP 全栈生成仅支持 React + Vite 项目"
        )
    if "FullstackDataPanel" not in existing_main:
        if "<App />" not in existing_main:
            raise ValueError("src/main.jsx 中未找到 <App />，无法注入全栈数据面板")
        patched_main = existing_main.replace(
            "<App />",
            "<React.Fragment><App /><FullstackDataPanel /></React.Fragment>",
            1,
        )
        patched_main = (
            "import FullstackDataPanel from './fullstack-api.jsx'\n" + patched_main
        )
        files["src/main.jsx"] = patched_main

    if existing_vite is None:
        raise ValueError("沙盒缺少 vite.config.js，无法配置 /api 反向代理")
    if "proxy" not in existing_vite or "'/api'" not in existing_vite:
        marker = "server: {"
        index = existing_vite.find(marker)
        if index < 0:
            raise ValueError("vite.config.js 缺少 server 配置块，无法注入 /api 代理")
        insert_at = index + len(marker)
        proxy = (
            "\n    // FullstackGraph：把同源 /api 请求转发到沙盒内 Node 后端。\n"
            "    proxy: {\n"
            "      '/api': {\n"
            "        target: process.env.FULLSTACK_API_URL || 'http://127.0.0.1:"
            + str(API_PORT)
            + "',\n"
            "        changeOrigin: true,\n"
            "      },\n"
            "    },"
        )
        files["vite.config.js"] = (
            existing_vite[:insert_at] + proxy + existing_vite[insert_at:]
        )

    if existing_package is None:
        raise ValueError("沙盒缺少 package.json，无法启动前后端双服务")
    try:
        package = json.loads(existing_package)
    except json.JSONDecodeError as exc:
        raise ValueError(f"package.json 不是合法 JSON: {exc}") from exc
    scripts = package.setdefault("scripts", {})
    scripts["dev"] = "node scripts/dev.mjs"
    scripts["dev:vite"] = "vite"
    files["package.json"] = json.dumps(package, ensure_ascii=False, indent=2) + "\n"

    return GeneratedBundle(
        files=files,
        summary=f"挂载 {entity} 数据面板，/api 代理到 {base}",
        metadata={"collectionPath": base, "entity": entity, "table": table},
    )


# ------------------------------------------------------------
# InfraCoder：一键部署配置（Infrastructure as Code）
# ------------------------------------------------------------
def generate_infra_bundle(contract: ArchitectContract) -> GeneratedBundle:
    """生成 Dockerfile、Render Blueprint、环境变量模板与部署指引。

    目标不是“能跑起来的沙盒玩具”，而是可以直接导出、推送到 Git 仓库，
    再由 Render / Railway / Vercel / 任意容器平台部署的生产级项目包。
    """
    table = extract_table_name(contract.database_ddl)
    entity = _entity_label(contract, table)
    engine = contract_database_engine(contract)
    engine_label = DATABASE_ENGINE_LABELS.get(engine, engine)
    base = collection_path(contract.api_routes or [])
    service_name = _safe_identifier(f"vibe_{table}", "vibe_app")
    service_name = service_name.replace("_", "-")[:48].strip("-") or "vibe-app"
    project_name = f"{entity} 全栈应用" if entity else f"{table} 全栈应用"

    replacements = {
        "__PROJECT_NAME__": project_name,
        "__SERVICE_NAME__": service_name,
        "__DB_ENGINE__": engine,
        "__DB_ENGINE_LABEL__": engine_label,
        "__COLLECTION_PATH__": base,
    }

    def render(template: str) -> str:
        result = template
        for key, value in replacements.items():
            result = result.replace(key, value)
        return result

    files = {
        "Dockerfile": render(BACKEND_DOCKERFILE_TEMPLATE),
        ".dockerignore": DOCKERIGNORE_TEMPLATE,
        "render.yaml": render(RENDER_YAML_TEMPLATE),
        ".env.example": render(ENV_EXAMPLE_TEMPLATE),
        "DEPLOY.md": render(DEPLOY_GUIDE_TEMPLATE),
    }
    return GeneratedBundle(
        files=files,
        summary=(
            f"生成一键部署配置（{engine_label}）：Dockerfile、"
            "render.yaml、.env.example、DEPLOY.md"
        ),
        metadata={
            "table": table,
            "entity": entity,
            "databaseEngine": engine,
            "serviceName": service_name,
            "targets": ["docker", "render", "vercel", "railway"],
        },
    )


__all__ = [
    "API_PORT",
    "DATABASE_ENGINE_LABELS",
    "GeneratedBundle",
    "PRODUCTION_ENGINES",
    "collection_path",
    "contract_database_engine",
    "extract_column_defs",
    "extract_columns",
    "extract_table_name",
    "generate_backend_bundle",
    "generate_dba_bundle",
    "generate_frontend_bundle",
    "generate_frontend_skeleton_bundle",
    "generate_infra_bundle",
    "item_path",
    "sqlite_schema_sql",
]
