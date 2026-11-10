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

import difflib
import json
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from ai_core.domain.architect import ArchitectContract
from ai_core.fullstack.coders import (
    collection_path,
    extract_column_defs,
    item_path,
)

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
    """一处可定位、可修复的跨端差异。"""

    kind: str
    component: str
    method: str
    path: str
    message: str
    suggestion: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "component": self.component,
            "method": self.method,
            "path": self.path,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass(frozen=True)
class ConsistencyReport:
    """一次一致性检查的完整结果。"""

    calls_checked: int
    backend_routes: tuple[BackendRoute, ...]
    issues: tuple[ConsistencyIssue, ...]
    checked_files: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues

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
            lines.append(f"   修复建议：{issue.suggestion}")
        lines.append(
            "后端已注册路由："
            + "；".join(f"{route.method} {route.path}" for route in self.backend_routes)
        )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": self.summary(),
            "callsChecked": self.calls_checked,
            "checkedFiles": list(self.checked_files),
            "backendRoutes": [route.to_dict() for route in self.backend_routes],
            "issues": [issue.to_dict() for issue in self.issues],
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
            issues.append(_path_issue(call, backend_routes, route_by_shape))
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
                        f"把前端请求方法改为 {sorted(methods)[0]}，"
                        f"或在 BackendCoder 中同时注册 {call.method} 路由"
                    ),
                )
            )
            continue
        payload_issue = _payload_issue(call, writable_columns)
        if payload_issue is not None:
            issues.append(payload_issue)

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
    """从 ``server/api-routes.json`` 读取后端路由，并补齐固定通用路由。"""
    manifest = _read_route_manifest(files.get("server/api-routes.json"))
    routes = contract.api_routes or []
    collection = manifest.get("collection") or _express_path(collection_path(routes))
    item = manifest.get("item") or _express_path(item_path(routes, collection))

    result: list[BackendRoute] = [
        BackendRoute("GET", HEALTH_PATH, "express.template"),
        BackendRoute("GET", collection, "express.collection"),
        BackendRoute("POST", collection, "express.collection"),
        BackendRoute("GET", GENERIC_DATA_PATH, "express.template"),
        BackendRoute("POST", GENERIC_DATA_PATH, "express.template"),
    ]
    if item and item != collection:
        for method in ("GET", "PUT", "PATCH", "DELETE"):
            result.append(BackendRoute(method, item, "express.item"))
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
def _path_issue(
    call: FrontendCall,
    routes: Sequence[BackendRoute],
    route_by_shape: Mapping[tuple[str, ...], Sequence[BackendRoute]],
) -> ConsistencyIssue:
    """路径不存在：优先推荐同 Method 的最相近路由。"""
    same_method = [route for route in routes if route.method == call.method]
    suggestion = _closest_route(call, same_method) or _closest_route(call, routes)
    if suggestion is None:
        hint = "后端没有任何可用路由，请先检查 BackendCoder 输出"
    else:
        hint = f"把前端请求改为 {suggestion.method} {suggestion.path}"
    return ConsistencyIssue(
        kind="path",
        component=call.component,
        method=call.method,
        path=call.path,
        message="后端未注册该路径（Method + Path 完全匹配失败）",
        suggestion=hint,
    )


def _closest_route(
    call: FrontendCall,
    routes: Iterable[BackendRoute],
) -> BackendRoute | None:
    """选择最相近的后端路由作为修复建议。

    排序键：公共路径前缀越长越优先；前缀相同时优先更短的资源根路径。
    这样 ``/api/v1/tasks/stream`` 会推荐集合路由 ``/api/v1/tasks``，
    而不是 ``/api/v1/tasks/:id``。
    """
    target_shape = _path_shape(call.path)
    target = "/".join(target_shape)
    best: tuple[tuple[int, int, float], BackendRoute] | None = None
    for route in routes:
        candidate_shape = _path_shape(route.path)
        common = 0
        for left, right in zip(target_shape, candidate_shape):
            if left != right:
                break
            common += 1
        candidate = "/".join(candidate_shape)
        score = (
            common,
            -len(candidate_shape),
            difflib.SequenceMatcher(None, target, candidate).ratio(),
        )
        if best is None or score > best[0]:
            best = (score, route)
    return best[1] if best else None


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
            f"把 payloadKey 改为 {_preferred_payload_column(writable_columns)}"
            if writable_columns
            else "在 databaseDdl 中补充可写字段，或改为纯前端交互"
        ),
    )


def _preferred_payload_column(columns: Sequence[str]) -> str:
    """优先推荐 name / title / label，避免建议用户提交 tenant_id。"""
    lowered = {column.casefold(): column for column in columns}
    for preferred in ("name", "title", "label"):
        if preferred in lowered:
            return lowered[preferred]
    business = [
        column for column in columns if column.casefold() not in {"tenant_id", "status"}
    ]
    return business[0] if business else columns[0]


def _writable_columns(contract: ArchitectContract) -> list[str]:
    columns = [name for name, _type in extract_column_defs(contract.database_ddl)]
    return [
        column for column in columns if column.casefold() not in _NON_WRITABLE_COLUMNS
    ]


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
