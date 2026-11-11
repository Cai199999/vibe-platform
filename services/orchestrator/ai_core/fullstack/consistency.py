"""ConsistencyVerifier：进入沙盒构建前的跨端接口一致性校验。

职责边界：

  - **只做静态比对**：读取已落盘的 ``src/App.jsx`` /
    ``src/fullstack-api.jsx`` 与后端 ``server/api-routes.json``，
    不启动服务、不发起真实网络请求；
  - **唯一事实来源**：后端路由以 BackendCoder 生成的
    ``server/api-routes.json`` 为准；契约文件只用于补充字段清单；
  - **失败即阻断**：发现 Method / Path / Payload 任一不一致时抛出
    ``ConsistencyError``，由 FullstackGraph 在 InfraCoder 之后、
    BuildVerifier 之前终止构建，并把可执行的差异报告回传前端。

校验覆盖的调用来源：

  1. 前端骨架 ``const INTERACTIONS = [...]`` 中所有 ``/api/`` 请求；
  2. 全栈数据面板 ``src/fullstack-api.jsx`` 的 COLLECTION_PATH
     （固定 GET 列表 + POST 提交）；
  3. ``LOCAL`` 交互与非 ``/api/`` 路径视为纯前端行为，不参与比对。

路径形状匹配会忽略参数名差异（``{id}`` / ``:todoId`` 都归一为
``:param``）：Express 的 ``request.params.id`` 只取决于注册顺序，
不影响路由是否命中，因此参数名不作为阻断条件。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from ai_core.domain.architect import ArchitectContract
from ai_core.fullstack.coders import (
    check_orm_schema,
    collection_path,
    core_table_for_contract,
    item_path,
    parse_ddl_tables,
)
from ai_core.fullstack.schema_ast import SchemaAstUnavailable

# 后端 Express 模板固定注册的通用路由：健康检查 + 通用数据别名。
HEALTH_PATH = "/api/health"
GENERIC_DATA_PATH = "/api/data"

# 前端交互里允许携带请求体的方法。
_WRITE_METHODS = {"POST", "PUT", "PATCH"}

_INTERACTIONS_MARKER = "const INTERACTIONS"
_COLLECTION_MARKER = "COLLECTION_PATH"

# DDL 中由数据库 / 后端自动维护的列，不允许前端提交。
_NON_WRITABLE_COLUMNS = {"id", "created_at", "updated_at", "deleted_at"}


@dataclass(frozen=True)
class BackendRoute:
    """后端实际注册的一条路由。"""

    method: str
    path: str
    source: str = "express"

    def to_dict(self) -> dict[str, str]:
        return {"method": self.method, "path": self.path, "source": self.source}


@dataclass(frozen=True)
class FrontendCall:
    """前端发出的一次接口调用（静态解析结果）。"""

    component: str
    method: str
    path: str
    payload_key: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "component": self.component,
            "method": self.method,
            "path": self.path,
            "payloadKey": self.payload_key,
            "source": self.source,
        }


@dataclass(frozen=True)
class ConsistencyIssue:
    """一处可定位、可修复的跨端差异。

    ``fix_action`` 是给 Coder 的机器可读指令，不是给用户看的文案：
      - ``ADD_BACKEND_ROUTE``：BackendCoder 补齐缺失的 Method + Path；
      - ``EXTEND_BACKEND_FIELD``：BackendCoder 扩展可写字段/请求体映射。
    一致性检查的职责是**业务语义对齐**，绝不能把前端请求改成某个无关的
    现有路径来“消掉”差异。
    """

    kind: str
    component: str
    method: str
    path: str
    message: str
    suggestion: str
    fix_action: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "component": self.component,
            "method": self.method,
            "path": self.path,
            "message": self.message,
            "suggestion": self.suggestion,
            "fixAction": self.fix_action,
        }


@dataclass(frozen=True)
class ConsistencyReport:
    """一次一致性检查的完整结果。"""

    calls_checked: int
    backend_routes: tuple[BackendRoute, ...]
    issues: tuple[ConsistencyIssue, ...]
    checked_files: tuple[str, ...] = ()
    # MCP 真实数据库结构验收结果（见 schema_mcp.SchemaCheck.to_dict）。
    schema: dict[str, object] | None = None

    @property
    def ok(self) -> bool:
        return not self.issues

    def missing_routes(self) -> tuple[dict[str, str], ...]:
        """需要 BackendCoder 补齐的路由（按 Method + Path 去重）。

        只包含可通过“注册后端路由”解决的差异；Payload 字段问题无法靠
        新增路由修复，因此不进入自愈路由列表，避免无意义的重复重试。
        """
        routes: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for issue in self.issues:
            if issue.kind not in {"path", "method"}:
                continue
            key = (issue.method, issue.path)
            if key in seen:
                continue
            seen.add(key)
            routes.append(
                {
                    "method": issue.method,
                    "path": issue.path,
                    "description": f"{issue.component}：{issue.message}",
                }
            )
        return tuple(routes)

    @property
    def repairable(self) -> bool:
        """是否存在可自动修复的差异（缺失路由或 ORM/DDL 字段错位）。"""
        return bool(self.missing_routes()) or self.needs_orm_repair

    @property
    def needs_orm_repair(self) -> bool:
        """是否需要 DBACoder 按 DDL 重新生成 ORM 与迁移。"""
        return any(issue.fix_action == "REGENERATE_ORM" for issue in self.issues)

    def summary(self) -> str:
        if self.ok:
            return (
                f"接口一致性检查通过：{self.calls_checked} 个前端请求"
                f"与 {len(self.backend_routes)} 条后端路由匹配"
            )
        return (
            f"接口一致性检查未通过：发现 {len(self.issues)} 处差异"
            f"（已检查 {self.calls_checked} 个前端请求）"
        )

    def failure_message(self) -> str:
        """生成给 Coder / 用户看的差异报告。"""
        if self.ok:
            return self.summary()
        lines = [self.summary()]
        for index, issue in enumerate(self.issues, start=1):
            lines.append(
                f"{index}. [{issue.kind}] {issue.component} 请求 "
                f"{issue.method} {issue.path}：{issue.message}"
            )
            lines.append(
                f"   修复动作（{issue.fix_action or 'MANUAL'}）：{issue.suggestion}"
            )
        if self.missing_routes():
            lines.append(
                "请 BackendCoder 补齐以下路由；前端业务请求保持不变："
                + "；".join(
                    f"{item['method']} {item['path']}" for item in self.missing_routes()
                )
            )
        lines.append(
            "后端已注册路由："
            + "；".join(f"{route.method} {route.path}" for route in self.backend_routes)
        )
        if self.schema:
            lines.append(
                "数据库结构 MCP 校验："
                f"ok={self.schema.get('ok')} "
                f"table={self.schema.get('table')} "
                f"missing={self.schema.get('missingColumns')}"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": self.summary(),
            "callsChecked": self.calls_checked,
            "checkedFiles": list(self.checked_files),
            "backendRoutes": [route.to_dict() for route in self.backend_routes],
            "missingRoutes": list(self.missing_routes()),
            "repairable": self.repairable,
            "needsOrmRepair": self.needs_orm_repair,
            "issues": [issue.to_dict() for issue in self.issues],
            "schema": self.schema,
        }


class ConsistencyError(ValueError):
    """跨端接口不一致；携带完整报告供事件层序列化。"""

    code = "FULLSTACK_CONSISTENCY_FAILED"

    def __init__(self, report: ConsistencyReport) -> None:
        super().__init__(report.failure_message())
        self.report = report


# ------------------------------------------------------------
# 对外入口
# ------------------------------------------------------------
def verify_consistency(
    files: Mapping[str, str],
    contract: ArchitectContract,
) -> ConsistencyReport:
    """比对前端请求与后端路由，返回报告（不抛异常）。

    ``files`` 是沙盒中与本次校验相关的文件快照，至少包含
    ``server/api-routes.json``；缺少该清单时退化为按契约推导路由。
    """
    backend_routes = extract_backend_routes(files, contract)
    frontend_calls, checked_files = extract_frontend_calls(files)
    route_by_shape: dict[tuple[str, ...], list[BackendRoute]] = {}
    for route in backend_routes:
        route_by_shape.setdefault(_path_shape(route.path), []).append(route)

    writable_columns = _writable_columns(contract)
    issues: list[ConsistencyIssue] = []
    seen: set[tuple[str, str, str, str]] = set()
    for call in frontend_calls:
        key = (call.component, call.method, call.path, call.payload_key)
        if key in seen:
            continue
        seen.add(key)
        shape = _path_shape(call.path)
        candidates = route_by_shape.get(shape, [])
        if not candidates:
            issues.append(_path_issue(call))
            continue
        methods = {route.method for route in candidates}
        if call.method not in methods:
            issues.append(
                ConsistencyIssue(
                    kind="method",
                    component=call.component,
                    method=call.method,
                    path=call.path,
                    message=(
                        f"后端已注册该路径，但只支持 "
                        f"{'/'.join(sorted(methods))}，前端使用了 {call.method}"
                    ),
                    suggestion=(
                        f"由 BackendCoder 为 {call.path} 增加 {call.method} "
                        "方法及对应业务处理逻辑；前端 Method 保持不变"
                    ),
                    fix_action="ADD_BACKEND_ROUTE",
                )
            )
            continue
        payload_issue = _payload_issue(call, writable_columns)
        if payload_issue is not None:
            issues.append(payload_issue)

    # 维度三：ORM 模型 vs 生产迁移 DDL。路由全对但字段错位时，
    # 服务启动后仍会 500 Unknown column；必须在构建前阻断。
    orm_issues, orm_checked = _orm_schema_issues(files, contract)
    issues.extend(orm_issues)
    checked_files = sorted(set(checked_files) | set(orm_checked))

    return ConsistencyReport(
        calls_checked=len(frontend_calls),
        backend_routes=tuple(backend_routes),
        issues=tuple(issues),
        checked_files=tuple(checked_files),
    )


def ensure_consistency(
    files: Mapping[str, str],
    contract: ArchitectContract,
) -> ConsistencyReport:
    """校验并在失败时抛出 ``ConsistencyError``。"""
    report = verify_consistency(files, contract)
    if not report.ok:
        raise ConsistencyError(report)
    return report


# ------------------------------------------------------------
# 后端路由提取
# ------------------------------------------------------------
def extract_backend_routes(
    files: Mapping[str, str],
    contract: ArchitectContract,
) -> list[BackendRoute]:
    """从 ``server/api-routes.json`` 读取后端路由，并补齐固定通用路由。

    新清单的 ``routes`` 是 BackendCoder **实际注册**的完整路由表（含
    抓取 / 改写 / 投递 / 对话等业务端点）；只有旧清单才回退到
    ``collection`` / ``item`` / ``contractRoutes``。
    """
    manifest = _read_route_manifest(files.get("server/api-routes.json"))
    result: list[BackendRoute] = [
        BackendRoute("GET", HEALTH_PATH, "express.template"),
        BackendRoute("GET", GENERIC_DATA_PATH, "express.template"),
        BackendRoute("POST", GENERIC_DATA_PATH, "express.template"),
    ]

    manifest_routes = manifest.get("routes")
    if isinstance(manifest_routes, list) and manifest_routes:
        for raw in manifest_routes:
            if not isinstance(raw, dict):
                continue
            method = str(raw.get("method") or "GET").upper().strip()
            path = _normalize_path(str(raw.get("path") or ""))
            if not method or not path.startswith("/api/"):
                continue
            result.append(
                BackendRoute(method, path, str(raw.get("source") or "express"))
            )
        return _dedupe_routes(result)

    routes = contract.api_routes or []
    collection = manifest.get("collection") or _express_path(collection_path(routes))
    item = manifest.get("item") or _express_path(item_path(routes, collection))
    result.extend(
        [
            BackendRoute("GET", collection, "express.collection"),
            BackendRoute("POST", collection, "express.collection"),
        ]
    )
    if item and item != collection:
        for method in ("GET", "PUT", "PATCH", "DELETE"):
            result.append(BackendRoute(method, item, "express.item"))

    # 旧版清单可能已经写入 contractRoutes，但仍缺少完整 routes 表；
    # 这些路由同样是后端实际注册的业务端点，必须参与一致性比对。
    contract_routes = manifest.get("contractRoutes")
    if isinstance(contract_routes, list):
        for raw in contract_routes:
            if not isinstance(raw, dict):
                continue
            method = str(raw.get("method") or "GET").upper().strip()
            path = _normalize_path(str(raw.get("path") or ""))
            if method and path.startswith("/api/"):
                result.append(BackendRoute(method, path, "express.contract"))
    return _dedupe_routes(result)


def _read_route_manifest(content: str | None) -> dict[str, object]:
    if not content:
        return {}
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


# ------------------------------------------------------------
# 前端调用提取
# ------------------------------------------------------------
def extract_frontend_calls(
    files: Mapping[str, str],
) -> tuple[list[FrontendCall], list[str]]:
    """静态解析前端文件中的接口调用。

    只认确定性模板产出的两种来源：
      - ``const INTERACTIONS = [...]``（骨架交互）；
      - ``src/fullstack-api.jsx`` 中的 ``COLLECTION_PATH``。
    任意 JS 的正则扫描容易误伤注释与字符串，不做。
    """
    calls: list[FrontendCall] = []
    checked_files: list[str] = []

    for path, content in sorted(files.items()):
        if not path.startswith("src/") or not path.endswith((".jsx", ".js", ".tsx")):
            continue
        if not isinstance(content, str) or not content:
            continue
        found = False

        if _INTERACTIONS_MARKER in content:
            checked_files.append(path)
            found = True
            for raw in _json_array_after(content, _INTERACTIONS_MARKER):
                call = _interaction_call(raw, path)
                if call is not None:
                    calls.append(call)

        if _COLLECTION_MARKER in content:
            collection = _js_string_constant(content, _COLLECTION_MARKER)
            if collection:
                if path not in checked_files:
                    checked_files.append(path)
                found = True
                calls.append(
                    FrontendCall(
                        component="FullstackDataPanel",
                        method="GET",
                        path=collection,
                        source=path,
                    )
                )
                calls.append(
                    FrontendCall(
                        component="FullstackDataPanel",
                        method="POST",
                        path=collection,
                        payload_key=_first_writable_hint(content),
                        source=path,
                    )
                )
        if found and path not in checked_files:
            checked_files.append(path)

    return calls, sorted(set(checked_files))


def _interaction_call(raw: object, source: str) -> FrontendCall | None:
    """把一条 INTERACTIONS 记录转成调用；LOCAL / 非 /api 请求跳过。"""
    if not isinstance(raw, dict):
        return None
    method = str(raw.get("method") or "").upper()
    path = str(raw.get("apiRoute") or "").strip()
    if method in {"", "LOCAL"} or not path.startswith("/api/"):
        return None
    return FrontendCall(
        component=str(raw.get("component") or raw.get("componentId") or "未知组件"),
        method=method,
        path=path,
        payload_key=str(raw.get("payloadKey") or ""),
        source=source,
    )


def _first_writable_hint(content: str) -> str:
    """从数据面板表单里提取提交字段名。

    优先读取 ``JSON.stringify({ <key>: ... })`` 的实际字段；旧模板没有
    该结构时再按 name / title / label 回退。
    """
    match = re.search(
        r"JSON\.stringify\(\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*:",
        content,
    )
    if match:
        return match.group(1)
    for candidate in ("name", "title", "label"):
        if re.search(rf"\b{candidate}\b\s*:", content):
            return candidate
    return ""


def _json_array_after(content: str, marker: str) -> list[object]:
    """定位 ``const X = [...]`` 并用 JSONDecoder 精确截取数组。

    不能简单用正则匹配 ``\\[.*?\\]``：字符串里可能出现 ``]``。
    """
    index = content.find(marker)
    if index < 0:
        return []
    equals = content.find("=", index + len(marker))
    start = content.find("[", equals)
    if equals < 0 or start < 0:
        return []
    try:
        value, _end = json.JSONDecoder().raw_decode(content[start:])
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _js_string_constant(content: str, name: str) -> str:
    match = re.search(
        rf"const\s+{re.escape(name)}\s*=\s*(['\"])([^'\"]+)\1",
        content,
    )
    return match.group(2).strip() if match else ""


# ------------------------------------------------------------
# 比对工具
# ------------------------------------------------------------
def _path_issue(call: FrontendCall) -> ConsistencyIssue:
    """路径不存在：生成“让后端补齐”的 FixAction，绝不改前端到无关路径。

    业务语义对齐的前提是：前端调用什么动作，后端就该有同名端点。
    把“聊天 / 投递”改成“请求简历接口”只会让业务逻辑彻底崩溃。
    """
    return ConsistencyIssue(
        kind="path",
        component=call.component,
        method=call.method,
        path=call.path,
        message="后端未注册该路径（Method + Path 完全匹配失败）",
        suggestion=(
            f"由 BackendCoder 补充注册 {call.method} {call.path}，"
            f"并实现与「{call.component}」语义匹配的业务逻辑或 Mock 响应；"
            "前端请求路径保持不变"
        ),
        fix_action="ADD_BACKEND_ROUTE",
    )


def _payload_issue(
    call: FrontendCall,
    writable_columns: list[str],
) -> ConsistencyIssue | None:
    """写入请求的 payloadKey 必须来自 DDL 可写列。"""
    if call.method not in _WRITE_METHODS or not call.payload_key:
        return None
    lowered = {column.casefold() for column in writable_columns}
    if call.payload_key.casefold() in lowered:
        return None
    columns = "、".join(writable_columns) or "（无）"
    return ConsistencyIssue(
        kind="payload",
        component=call.component,
        method=call.method,
        path=call.path,
        message=(
            f"请求体字段 `{call.payload_key}` 不在后端可写列中" f"（可写列：{columns}）"
        ),
        suggestion=(
            f"由 BackendCoder 在数据库可写列或请求体映射中支持字段 "
            f"`{call.payload_key}`（当前可写列：{columns}）；"
            "不要把前端请求改到无关业务接口"
            if writable_columns
            else "由 BackendCoder 在 databaseDdl 中补充该字段，或确认该请求字段是否应写入持久层"
        ),
        fix_action="EXTEND_BACKEND_FIELD",
    )


def _writable_columns(contract: ArchitectContract) -> list[str]:
    # 可写列必须来自核心业务表，而不是多表 DDL 的第一张 users。
    columns = core_table_for_contract(contract).column_names
    return [
        column for column in columns if column.casefold() not in _NON_WRITABLE_COLUMNS
    ]


def _orm_schema_issues(
    files: Mapping[str, str],
    contract: ArchitectContract,
) -> tuple[list[ConsistencyIssue], list[str]]:
    """ORM 模型（server/db.js）与生产迁移 DDL 的字段一致性校验。

    这是 ``Unknown column 'name'`` 的构建前门禁：

      - 解析 ``server/db.js`` 的 ``const TABLE`` 与 ``COLUMN_DEFS``；
      - 解析 ``server/migrations/001_init.sql`` 中核心业务表的真实字段；
      - 代码引用了 DDL 不存在的字段（如多表 DDL 里把 users 当业务表）
        立即产出 ``REGENERATE_ORM`` 阻断项，驱动 DBACoder 重写。

    快照里没有 ``server/db.js`` 时返回空列表，保证只做路由校验的
    单元测试与局部调用保持兼容；完整全栈图一定会带上该文件。
    """
    db_js = files.get("server/db.js")
    if not isinstance(db_js, str) or not db_js.strip():
        return [], []
    core = core_table_for_contract(contract)
    migration = files.get("server/migrations/001_init.sql")
    migration_text = (
        migration if isinstance(migration, str) and migration.strip() else ""
    )
    migration_tables = {
        table.name.lower(): table for table in parse_ddl_tables(migration_text)
    }
    migration_table = migration_tables.get(core.name.lower())
    expected = (
        list(migration_table.column_names)
        if migration_table is not None
        else list(core.column_names)
    )
    try:
        messages = check_orm_schema(
            files,
            table=core.name,
            expected_columns=expected,
        )
    except SchemaAstUnavailable as exc:
        # 校验器缺失属于环境问题，不能伪装成“代码可自愈”去重跑 DBACoder。
        # 直接阻断，并给出可执行的修复方向。
        return [
            ConsistencyIssue(
                kind="ast_unavailable",
                component="ConsistencyVerifier",
                method="",
                path="schema_ast",
                message=f"ORM/DDL AST 校验器不可用：{exc}",
                suggestion=(
                    "检查 Python AI 镜像是否安装 nodejs，且 "
                    "/app/sandbox_template/node_modules/@babel/parser 可读；"
                    "禁止回退到正则校验后继续构建"
                ),
                fix_action="FIX_ENVIRONMENT",
            )
        ], ["schema_ast"]
    if migration_text and migration_table is None:
        messages.insert(
            0,
            f"迁移 001_init.sql 未包含核心业务表 {core.name}，" "部署后 CRUD 必然失败",
        )
    issues = [
        ConsistencyIssue(
            kind="orm_schema",
            component="DBACoder",
            method="",
            path="server/db.js",
            message=message,
            suggestion=(
                "由 DBACoder 按 databaseDdl 的核心业务表重新生成 "
                "server/db.js 与 server/migrations/001_init.sql，"
                "再由 BackendCoder 重新绑定同一张表；"
                "禁止使用 users / name 作为默认占位符"
            ),
            fix_action="REGENERATE_ORM",
        )
        for message in messages
    ]
    checked = ["server/db.js"]
    if migration_text:
        checked.append("server/migrations/001_init.sql")
    return issues, checked


def _express_path(path: str) -> str:
    """把契约 ``/todos/{id}`` 转为 Express ``/todos/:id``。"""
    return re.sub(r"\{(\w+)\}", r":\1", str(path or "").strip())


def _path_shape(path: str) -> tuple[str, ...]:
    """把 ``{id}`` / ``:todoId`` 归一为 ``:param``，用于结构匹配。"""
    clean = _normalize_path(path)
    segments: list[str] = []
    for segment in clean.strip("/").split("/"):
        if segment.startswith("{") or segment.startswith(":"):
            segments.append(":param")
        elif segment:
            segments.append(segment)
    return tuple(segments)


def _normalize_path(path: str) -> str:
    clean = str(path or "").split("?", 1)[0].split("#", 1)[0].strip()
    if not clean.startswith("/"):
        clean = "/" + clean
    clean = re.sub(r"/{2,}", "/", clean)
    return clean.rstrip("/") or "/"


def _dedupe_routes(routes: Iterable[BackendRoute]) -> list[BackendRoute]:
    seen: set[tuple[str, str]] = set()
    result: list[BackendRoute] = []
    for route in routes:
        key = (route.method, _normalize_path(route.path))
        if key in seen:
            continue
        seen.add(key)
        result.append(route)
    return result


__all__ = [
    "BackendRoute",
    "ConsistencyError",
    "ConsistencyIssue",
    "ConsistencyReport",
    "FrontendCall",
    "ensure_consistency",
    "extract_backend_routes",
    "extract_frontend_calls",
    "verify_consistency",
]
