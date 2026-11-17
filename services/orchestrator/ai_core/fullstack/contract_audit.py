"""契约结构审计：区分“契约自相矛盾”与“生成代码写错”。

自愈重试只应解决代码缺陷：LLM / 模板写错字段、漏注册路由，都可以通过
重新生成修复。但如果契约本身矛盾——关联引用不存在的表、前端交互调用了
未声明的 API、DDL 里根本没有业务表——再重试多少次都只会得到同一个错误。
此时必须立即停止并请用户重新澄清需求，而不是浪费生成轮次。

本模块只做确定性检查，不调用模型、不读取沙盒产物：

  - 缺失 DDL / 缺少业务表 / 集合资源在 DDL 中不存在；
  - 显式 ``relations`` 引用不存在的表或字段；
  - API 路由不在 ``/api/`` 下；
  - ``component_interactions.api_route`` 调用了未声明的 API。

审计结果会同时出现在：
  - 生成开始前（服务层），避免进入注定失败的生成链路；
  - 自愈重试耗尽时（Graph），把失败归类为 ``CONTRACT`` 而不是 ``CODE``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from ai_core.domain.architect import ArchitectContract
from ai_core.domain.relations import (
    derive_table_relations,
    normalize_table_relations,
    parse_ddl_table_columns,
    relation_audit_issues,
)
from ai_core.fullstack.coders import (
    action_resource_table,
    collection_path,
    core_table_for_contract,
    is_action_resource,
)

FAILURE_KIND_CONTRACT = "CONTRACT"
FAILURE_KIND_CODE = "CODE"

# 缺陷归因：前端/编排层使用这两个稳定枚举，不再依赖自由文本。
DEFECT_CONTRACT = "CONTRACT_DEFECT"
DEFECT_CODE = "CODE_DEFECT"

# 交互记录里表示“纯前端 / 无接口”的占位写法。
_NON_API_ROUTE_VALUES = {"", "LOCAL", "NONE", "NULL", "-"}
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class ContractProblem:
    """一条契约结构问题。"""

    code: str
    message: str
    hint: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "hint": self.hint}


@dataclass(frozen=True)
class ContractAudit:
    """契约审计结果；``ok`` 表示不存在结构性矛盾。"""

    problems: tuple[ContractProblem, ...] = ()
    # 生成代码侧的诊断（可选）：只用于说明“契约没问题，是代码写错了”。
    code_issues: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def failure_kind(self) -> str:
        return FAILURE_KIND_CONTRACT if self.problems else FAILURE_KIND_CODE

    @property
    def defect_kind(self) -> str:
        return DEFECT_CONTRACT if self.problems else DEFECT_CODE

    def summary(self) -> str:
        if not self.problems:
            return "契约结构审计通过"
        lines = [f"契约存在 {len(self.problems)} 处结构性矛盾，无法通过代码重试修复："]
        lines.extend(
            f"{index}. {problem.message}"
            for index, problem in enumerate(self.problems, start=1)
        )
        return "\n".join(lines)

    def clarification_prompt(self) -> str:
        """回传给 Architect 的重新澄清提示。"""
        lines = [
            "请先修正契约中的结构性矛盾，再重新生成代码：",
        ]
        for index, problem in enumerate(self.problems, start=1):
            line = f"{index}. {problem.message}"
            if problem.hint:
                line += f"（建议：{problem.hint}）"
            lines.append(line)
        lines.append("不要继续重试代码生成；请修改契约并重新确认。")
        return "\n".join(lines)

    def payload(self) -> dict[str, object]:
        return {
            "summary": self.summary(),
            "prompt": self.clarification_prompt(),
            "problems": [problem.to_dict() for problem in self.problems],
            "defectKind": self.defect_kind,
            "codeIssues": list(self.code_issues),
        }


def _normalize_path(path: str) -> str:
    clean = str(path or "").split("?", 1)[0].split("#", 1)[0].strip()
    if not clean.startswith("/"):
        clean = "/" + clean
    clean = "/".join(segment for segment in clean.split("/") if segment)
    return "/" + clean.strip("/")


def _path_shape(path: str) -> str:
    """``{id}`` / ``:id`` / ``:userId`` 统一为 ``:param`` 再比较。"""
    segments: list[str] = []
    for segment in _normalize_path(path).strip("/").split("/"):
        if not segment:
            continue
        if segment.startswith("{") or segment.startswith(":"):
            segments.append(":param")
        else:
            segments.append(segment)
    return "/" + "/".join(segments)


def _parse_route_reference(raw: str) -> tuple[str, str]:
    """解析 ``GET /api/v1/todos`` 或 ``/api/v1/todos`` 两种写法。"""
    text = str(raw or "").strip()
    if not text:
        return "", ""
    head, _, rest = text.partition(" ")
    if head.upper() in _HTTP_METHODS and rest.strip():
        return head.upper(), rest.strip()
    return "", text


def _singular(word: str) -> str:
    # URL 路由常用 kebab-case，DDL 表名用 snake_case；比对前统一去掉
    # 分隔符，避免 /tailored-resumes 被误判为缺少 tailored_resumes 表。
    value = re.sub(r"[^a-z0-9]", "", str(word or "").lower())
    if value.endswith("ies") and len(value) > 3:
        return value[:-3] + "y"
    if value.endswith("ses") and len(value) > 3:
        return value[:-2]
    if value.endswith("s") and not value.endswith("ss") and len(value) > 1:
        return value[:-1]
    return value


def _collection_resource(path: str) -> str:
    segments = [segment for segment in _normalize_path(path).split("/") if segment]
    if len(segments) < 2:
        return ""
    # /api/v1/<resource> 或 /api/<resource>
    return segments[2] if segments[0] == "api" and len(segments) >= 3 else segments[1]


def collection_resource(path: str) -> str:
    """公开资源段解析，供 Architect 构造冲突决策使用。"""
    return _collection_resource(path)


def has_business_collection_routes(routes: Iterable[Any]) -> bool:
    """是否存在真正的实体资源路由（排除 /auth、/session 等动作端点）。"""
    for route in routes:
        resource = _collection_resource(str(getattr(route, "path", "") or ""))
        if resource and not is_action_resource(resource):
            return True
    return False


def _table_exists_for_resource(resource: str, tables: Iterable[str]) -> bool:
    target = _singular(resource)
    return any(_singular(table) == target for table in tables)


def _table_for_resource(resource: str, tables: Iterable[str]) -> str:
    target = _singular(resource)
    for table in tables:
        if _singular(table) == target:
            return table
    return ""


# 路径末段若命中这些动作词，即使以 s 结尾（status / process）也不是子表。
_ACTION_SEGMENTS = {
    "analyze",
    "analyse",
    "apply",
    "approve",
    "batch",
    "cancel",
    "confirm",
    "details",
    "download",
    "execute",
    "export",
    "generate",
    "health",
    "import",
    "login",
    "logout",
    "preview",
    "process",
    "publish",
    "refresh",
    "register",
    "reject",
    "reset",
    "retry",
    "rewrite",
    "run",
    "search",
    "send",
    "start",
    "status",
    "stop",
    "stream",
    "submit",
    "test",
    "upload",
    "validate",
}


def _looks_like_collection(segment: str) -> bool:
    """粗判一个路径段是“子资源集合”还是“动作端点”。

    ``/resumes/:id/rewrite`` 里的 ``rewrite`` 是动作，不是子表；只有
    复数形式且不在动作词表中的段才按嵌套资源审计，避免把合法业务动作
    误判成缺失表。
    """
    value = segment.strip().lower()
    if value in _ACTION_SEGMENTS:
        return False
    return len(value) > 1 and value.endswith("s") and not value.endswith("ss")


def _nested_resource_pairs(routes: Iterable[str]) -> list[tuple[str, str]]:
    """从 ``/api/v1/users/:id/resumes`` 提取 ``(users, resumes)``。"""
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for route in routes:
        segments = [segment for segment in _normalize_path(route).split("/") if segment]
        if not segments or segments[0] != "api":
            continue
        param_index = next(
            (
                index
                for index, segment in enumerate(segments)
                if segment.startswith(":") or segment.startswith("{")
            ),
            -1,
        )
        if param_index <= 1 or param_index + 1 >= len(segments):
            continue
        parent = segments[param_index - 1]
        child = segments[param_index + 1]
        if parent in {"v1", "v2", "api"} or child in {"v1", "v2", "api"}:
            continue
        if not _looks_like_collection(child):
            continue
        key = (parent.lower(), child.lower())
        if key in seen:
            continue
        seen.add(key)
        pairs.append((parent, child))
    return pairs


def _audit_nested_routes(
    contract: ArchitectContract,
    tables: dict[str, tuple[str, ...]],
) -> list[ContractProblem]:
    """嵌套子资源路由必须在契约里有对应 JOIN 关系。

    前后端都能生成 ``/users/:id/resumes``，但如果 ``relations`` / DDL
    里没有 ``users.id ← resumes.user_id``，运行期必然 500。这种矛盾
    不是 AI 拼错代码，而是契约缺少 JOIN 定义，必须判 ``CONTRACT_DEFECT``。
    """
    problems: list[ContractProblem] = []
    # 显式 relations 优先；漏写时仍按 DDL 外键列推导，避免把可推导的
    # 关系误判为契约缺陷。
    relations, _ = normalize_table_relations(
        tables,
        contract.relations,
        derive_when_empty=True,
    )
    for parent_resource, child_resource in _nested_resource_pairs(
        route.path for route in contract.api_routes or []
    ):
        parent = _table_for_resource(parent_resource, tables)
        child = _table_for_resource(child_resource, tables)
        if not child:
            problems.append(
                ContractProblem(
                    code="nested_table_missing",
                    message=(
                        f"嵌套路由引用了 DDL 中不存在的子表 {child_resource}"
                        f"（父资源 {parent_resource}）"
                    ),
                    hint=(
                        f"在 databaseDdl 中创建 snake_case 子表 "
                        f"{child_resource.replace('-', '_')}，并声明外键列；"
                        "URL 用 kebab-case，DDL 表名用 snake_case，二者语义对应"
                    ),
                )
            )
            continue
        if not parent:
            continue

        def _has_relation(candidates: list[Any]) -> bool:
            return any(
                relation.parent_table.lower() == parent.lower()
                and relation.child_table.lower() == child.lower()
                for relation in candidates
            )

        related = _has_relation(relations)  # type: ignore[arg-type]
        if not related:
            # users 等系统表默认不参与推导；当它作为嵌套路由父资源时，
            # 需要显式把 parent 当作 core table 再推导一次。
            related = _has_relation(derive_table_relations(tables, core_table=parent))
        if not related:
            problems.append(
                ContractProblem(
                    code="missing_relation",
                    message=(
                        f"嵌套路由 {parent_resource}/:id/{child_resource} 缺少 JOIN 关系："
                        f"契约没有声明 {parent} → {child}"
                    ),
                    hint=(
                        f"在 relations 中声明 parentTable={parent}、"
                        f"childTable={child}，并确保 {child} 表存在外键列"
                    ),
                )
            )
    return problems


def _audit_database(contract: ArchitectContract, mode: str) -> list[ContractProblem]:
    problems: list[ContractProblem] = []
    if mode not in {"fullstack", "dba", "backend"}:
        return problems
    # 存量项目已有真实数据库：增量 DDL 不允许被当作全量契约自检，
    # 否则会出现“父键不在父表字段”这类无中生有的错误。
    if getattr(contract, "project_mode", "NEW_PROJECT") == "EXISTING_SOURCE":
        return problems
    tables = parse_ddl_table_columns(contract.database_ddl or "")
    if mode in {"fullstack", "dba"} and not tables:
        problems.append(
            ContractProblem(
                code="missing_ddl",
                message="契约缺少可解析的 CREATE TABLE 语句，数据库层没有生成依据",
                hint="在 databaseDdl 中为每个业务实体声明表与字段",
            )
        )
        return problems
    # backend 模式允许只补 HTTP 层（DBACoder 会使用中性兜底表）；
    # 只要契约写了 DDL，就继续逐表审计。
    if tables:
        core = core_table_for_contract(contract)
        if core.name.lower() not in {name.lower() for name in tables}:
            problems.append(
                ContractProblem(
                    code="missing_core_table",
                    message=(
                        f"契约选出的核心业务表 {core.name} 不在 DDL 中，"
                        "ORM 与迁移必然错位"
                    ),
                    hint="统一 apiRoutes 的集合资源名与 databaseDdl 的表名",
                )
            )
        collection = collection_path(contract.api_routes or [])
        resource = _collection_resource(collection)
        # 认证 / 会话 / 账号是动作端点组，不是 CRUD 实体：
        #   - 若 DDL 有 users 等用户表，自动视为已映射；
        #   - 若没有用户表，也不报“缺 auth 表”，交给生成阶段使用业务表。
        # 只有真正的业务集合根才要求 DDL 同名表。
        action_mapped = bool(resource and is_action_resource(resource)) and bool(
            action_resource_table(resource, tables)
        )
        has_business = has_business_collection_routes(contract.api_routes or [])
        # 没有 API 路由时已由路由审计报告问题，这里不能再拿兜底集合
        # /api/v1/records 误判 DDL 缺表。
        if (
            contract.api_routes
            and resource
            and tables
            and has_business
            and not action_mapped
            and not is_action_resource(resource)
            and not _table_exists_for_resource(resource, tables)
        ):
            problems.append(
                ContractProblem(
                    code="collection_table_missing",
                    message=(
                        f"API 集合 {collection} 对应的业务表 {resource} 不在 DDL 中，"
                        "上线后所有 CRUD 都会失败"
                    ),
                    hint=f"在 DDL 中创建 {resource} 表，或把 API 集合改成真实表名",
                )
            )
        for message in relation_audit_issues(tables, contract.relations):
            problems.append(
                ContractProblem(
                    code="invalid_relation",
                    message=message,
                    hint="关联的父子表与外键列必须同时存在于 databaseDdl",
                )
            )
        problems.extend(_audit_nested_routes(contract, tables))
    return problems


def _audit_routes(contract: ArchitectContract, mode: str) -> list[ContractProblem]:
    problems: list[ContractProblem] = []
    if mode not in {"fullstack", "backend"}:
        return problems
    routes = list(contract.api_routes or [])
    if not routes:
        problems.append(
            ContractProblem(
                code="missing_api_routes",
                message="契约没有声明任何 API 路由，后端无法生成业务端点",
                hint="在 apiRoutes 中声明集合与详情路由",
            )
        )
        return problems

    declared: set[tuple[str, str]] = set()
    declared_shapes: set[str] = set()
    for route in routes:
        path = _normalize_path(route.path)
        method = str(route.method or "").upper()
        if not path.startswith("/api/"):
            problems.append(
                ContractProblem(
                    code="invalid_api_path",
                    message=f"API 路由 {method} {route.path} 不以 /api/ 开头",
                    hint="所有后端路由必须以 /api/ 开头",
                )
            )
            continue
        declared.add((method, _path_shape(path)))
        declared_shapes.add(_path_shape(path))

    for interaction in contract.component_interactions or []:
        raw = str(interaction.api_route or "").strip()
        if not raw or raw.upper() in _NON_API_ROUTE_VALUES:
            continue
        method, path = _parse_route_reference(raw)
        if not path.startswith("/api/"):
            if raw.upper() not in _NON_API_ROUTE_VALUES:
                problems.append(
                    ContractProblem(
                        code="invalid_interaction_route",
                        message=(
                            f"组件 {interaction.component} 的交互引用了非 API 路径：{raw}"
                        ),
                        hint="componentInteractions.apiRoute 必须是 /api/ 开头的真实接口",
                    )
                )
            continue
        shape = _path_shape(path)
        matched = (method, shape) in declared if method else shape in declared_shapes
        if not matched:
            problems.append(
                ContractProblem(
                    code="undeclared_interaction_route",
                    message=(
                        f"组件 {interaction.component} 调用了未在 apiRoutes 声明的接口："
                        f"{raw}"
                    ),
                    hint="补充对应 API 路由，或把交互改成已声明的路径",
                )
            )
    return problems


def audit_contract(
    contract: ArchitectContract,
    *,
    mode: str = "fullstack",
    code_issues: Iterable[str] = (),
) -> ContractAudit:
    """确定性审计契约结构；不依赖模型与生成产物。

    ``code_issues`` 是生成代码侧的诊断文本。它不会改变契约结论，只用于
    在归因为 ``CODE_DEFECT`` 时把证据一并回传，避免“契约没问题”变成
    无法解释的断言。
    """
    problems = [
        *_audit_database(contract, mode),
        *_audit_routes(contract, mode),
    ]
    # 去掉同一问题的重复项（例如交互与路由检查命中同一条路径）。
    unique: dict[tuple[str, str], ContractProblem] = {}
    for problem in problems:
        unique.setdefault((problem.code, problem.message), problem)
    return ContractAudit(
        problems=tuple(unique.values()),
        code_issues=tuple(str(item) for item in code_issues if str(item).strip()),
    )


def validate_contract(
    contract: ArchitectContract,
    *,
    mode: str = "fullstack",
) -> ContractAudit:
    """Architect 确认前的契约自检工具（与运行时审计同一实现）。

    只读、无副作用；Architect 在 ``validate_contract`` 节点调用，确保
    DDL 表、relations、apiRoutes 与组件交互彼此自洽，避免把矛盾带进
    生成阶段。
    """
    return audit_contract(contract, mode=mode)


__all__ = [
    "ContractAudit",
    "ContractProblem",
    "DEFECT_CODE",
    "DEFECT_CONTRACT",
    "FAILURE_KIND_CODE",
    "FAILURE_KIND_CONTRACT",
    "audit_contract",
    "validate_contract",
]
