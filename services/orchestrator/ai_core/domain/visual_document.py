"""离线优先可视化文档（VisualDocument）领域契约。

这是“生成前”的唯一事实源：用户自由增删、拖拽、修改视觉树时，
不触碰真实 React 源码、沙盒或 Git。只有冻结后的文档才允许进入
CodeGen 编译阶段。

设计原则：
  - 树只表达视觉、层级与业务语义，不表达像素 x/y；
  - 每个节点有稳定 nodeId，CodeGen 会把它注入 data-node-id；
  - 交互与数据绑定独立于视觉节点，避免把业务逻辑塞进样式树；
  - 空白画布合法（root 可以为 None）。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from ai_core.domain.common import CamelModel

VISUAL_SCHEMA_VERSION = "1.0"
MAX_VISUAL_NODES = 5000
MAX_VISUAL_DEPTH = 20

VisualNodeType = Literal[
    "page",
    "section",
    "container",
    "grid",
    "stack",
    "text",
    "button",
    "input",
    "image",
    "list",
    "table",
    "form",
    "modal",
    "component",
]

VisualDocumentStatus = Literal[
    "DRAFT",
    "REVIEW_READY",
    "FROZEN",
    "COMPILED",
]

CONTAINER_NODE_TYPES = frozenset(
    {
        "page",
        "section",
        "container",
        "grid",
        "stack",
        "list",
        "table",
        "form",
        "modal",
        "component",
    }
)
REPEAT_NODE_TYPES = frozenset({"list", "table"})
_NODE_ID_RE = re.compile(r"^nd_[A-Za-z0-9_-]{3,64}$")
_FORBIDDEN_COORDINATE_KEYS = frozenset(
    {"x", "y", "absoluteX", "absoluteY", "absolute_x", "absolute_y"}
)
_FORBIDDEN_PROP_KEYS = frozenset(
    {"dangerouslySetInnerHTML", "dangerously_set_inner_html", "__html"}
)
_MARKUP_RE = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")


class VisualValidationError(ValueError):
    """VisualDocument 语义校验错误。"""


class VisualDesignTokens(CamelModel):
    """设计令牌；CodeGen 与画布共用，避免两边视觉逐渐漂移。"""

    colors: dict[str, str] = Field(default_factory=dict)
    spacing: dict[str, str] = Field(default_factory=dict)
    radius: dict[str, str] = Field(default_factory=dict)
    typography: dict[str, str] = Field(default_factory=dict)


class VisualLayout(CamelModel):
    """受限布局属性；不接受像素 x/y 或绝对坐标。"""

    display: Literal["block", "flex", "grid", "inline", "inline-flex"] = "block"
    direction: Literal["row", "column"] = "column"
    gap: str = ""
    padding: str = ""
    align: Literal["start", "center", "end", "stretch"] = "stretch"
    justify: Literal["start", "center", "between", "end"] = "start"
    wrap: bool = False
    columns: int = Field(default=1, ge=1, le=12)
    min_width: str = ""
    max_width: str = ""

    @model_validator(mode="after")
    def _reject_coordinates(self) -> "VisualLayout":
        extra = self.model_extra or {}
        forbidden = sorted(_FORBIDDEN_COORDINATE_KEYS.intersection(extra))
        if forbidden:
            raise ValueError("VisualLayout 禁止像素坐标字段: " + ", ".join(forbidden))
        return self


class VisualStyle(CamelModel):
    token_refs: list[str] = Field(default_factory=list, max_length=64)
    class_name: str = Field(default="", max_length=2000)
    tailwind: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("class_name")
    @classmethod
    def _safe_class_name(cls, value: str) -> str:
        if any(char in value for char in "<>{}\n\r"):
            raise ValueError("VisualStyle.className 不能包含标签或脚本字符")
        return value


class VisualRepeat(CamelModel):
    """列表 / 表格的循环声明；source 必须能关联到 binding。"""

    source: str = Field(min_length=1, max_length=120)
    item_alias: str = Field(default="item", min_length=1, max_length=80)
    key_field: str = Field(default="id", max_length=80)
    sample_count: int = Field(default=3, ge=0, le=20)
    empty_text: str = Field(default="", max_length=200)


class VisualSemantics(CamelModel):
    role: str = Field(default="", max_length=120)
    purpose: str = Field(default="", max_length=2000)
    importance: Literal["normal", "core", "critical"] = "normal"
    business_entity: str = Field(default="", max_length=120)


class VisualA11y(CamelModel):
    label: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=2000)


class VisualState(CamelModel):
    default_visible: bool = True
    disabled: bool = False


class VisualCodegen(CamelModel):
    mode: Literal["primitive", "composite", "custom"] = "primitive"
    component_name: str = Field(default="", max_length=120)
    extract_component: bool = False
    imports: list[str] = Field(default_factory=list, max_length=64)


class VisualProvenance(CamelModel):
    created_by: Literal["architect", "user", "agent"] = "user"
    revision: int = Field(default=1, ge=0)
    change_id: str = Field(default="", max_length=80)


class VisualNode(CamelModel):
    """虚拟组件树节点；children 顺序就是最终渲染顺序。"""

    id: str = Field(pattern=_NODE_ID_RE.pattern, max_length=80)
    type: VisualNodeType
    name: str = Field(default="", max_length=160)
    component_ref: str = Field(default="", max_length=120)
    props: dict[str, Any] = Field(default_factory=dict)
    layout: VisualLayout = Field(default_factory=VisualLayout)
    style: VisualStyle = Field(default_factory=VisualStyle)
    children: list["VisualNode"] = Field(default_factory=list, max_length=500)
    repeat: VisualRepeat | None = None
    semantics: VisualSemantics = Field(default_factory=VisualSemantics)
    a11y: VisualA11y = Field(default_factory=VisualA11y)
    state: VisualState = Field(default_factory=VisualState)
    codegen: VisualCodegen = Field(default_factory=VisualCodegen)
    provenance: VisualProvenance = Field(default_factory=VisualProvenance)
    locked: bool = False

    @model_validator(mode="after")
    def _validate_node(self) -> "VisualNode":
        extra = self.model_extra or {}
        forbidden = sorted(_FORBIDDEN_COORDINATE_KEYS.intersection(extra))
        if forbidden:
            raise ValueError(
                f"节点 {self.id} 禁止像素坐标字段: " + ", ".join(forbidden)
            )
        forbidden_props = sorted(_FORBIDDEN_PROP_KEYS.intersection(self.props))
        if forbidden_props:
            raise ValueError(
                f"节点 {self.id} 禁止原始 HTML 注入: " + ", ".join(forbidden_props)
            )
        for key, value in self.props.items():
            if isinstance(value, str) and _MARKUP_RE.search(value):
                raise ValueError(
                    f"节点 {self.id} 的 props.{key} 含 JSX/HTML 标记；"
                    "VisualDocument 只允许纯文案"
                )
        if self.children and self.type not in CONTAINER_NODE_TYPES:
            raise ValueError(f"节点 {self.id} 类型 {self.type} 不能包含子节点")
        if self.repeat and self.type not in REPEAT_NODE_TYPES:
            raise ValueError(f"节点 {self.id} 只有 list/table 支持 repeat")
        return self


VisualNode.model_rebuild()


class VisualInteraction(CamelModel):
    id: str = Field(min_length=1, max_length=80)
    node_id: str = Field(min_length=1, max_length=80)
    event: str = Field(default="click", max_length=40)
    action: Literal[
        "export",
        "submit",
        "navigate",
        "open_modal",
        "call_api",
        "custom",
    ] = "custom"
    intent: str = Field(default="", max_length=2000)
    binding_id: str = Field(default="", max_length=80)
    status: Literal["UNRESOLVED", "CONFIRMED"] = "UNRESOLVED"


class VisualBinding(CamelModel):
    id: str = Field(min_length=1, max_length=80)
    node_id: str = Field(min_length=1, max_length=80)
    kind: Literal["static", "state", "api", "computed"] = "static"
    entity: str = Field(default="", max_length=120)
    field: str = Field(default="", max_length=120)
    endpoint_hint: str = Field(default="", max_length=500)
    method: str = Field(default="", max_length=16)
    required: bool = True
    status: Literal["UNRESOLVED", "CONFIRMED"] = "UNRESOLVED"
    source: str = Field(default="", max_length=120)


class VisualReview(CamelModel):
    frozen_at: datetime | None = None
    question_rounds: int = Field(default=0, ge=0)
    answers: list[dict[str, Any]] = Field(default_factory=list, max_length=200)


class VisualMetadata(CamelModel):
    created_by: str = Field(default="user", max_length=40)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_modified_by: str = Field(default="user", max_length=40)
    content_hash: str = Field(default="", max_length=100)


class VisualDocument(CamelModel):
    """离线画布文档。root=None 表示合法空白画布。"""

    schema_version: Literal["1.0"] = VISUAL_SCHEMA_VERSION
    doc_id: str = Field(min_length=1, max_length=80)
    project_id: str = Field(default="", max_length=80)
    revision: int = Field(default=0, ge=0)
    status: VisualDocumentStatus = "DRAFT"
    title: str = Field(default="未命名页面", max_length=200)
    design_tokens: VisualDesignTokens = Field(default_factory=VisualDesignTokens)
    root: VisualNode | None = None
    interactions: list[VisualInteraction] = Field(default_factory=list, max_length=2000)
    bindings: list[VisualBinding] = Field(default_factory=list, max_length=2000)
    review: VisualReview = Field(default_factory=VisualReview)
    metadata: VisualMetadata = Field(default_factory=VisualMetadata)

    @model_validator(mode="after")
    def _validate_tree_and_refs(self) -> "VisualDocument":
        node_ids: set[str] = set()
        node_count = 0
        max_depth = 0

        def walk(node: VisualNode, depth: int) -> None:
            nonlocal node_count, max_depth
            node_count += 1
            max_depth = max(max_depth, depth)
            if node_count > MAX_VISUAL_NODES:
                raise ValueError(f"VisualDocument 节点数超过上限 {MAX_VISUAL_NODES}")
            if depth > MAX_VISUAL_DEPTH:
                raise ValueError(f"VisualDocument 层级超过上限 {MAX_VISUAL_DEPTH}")
            if node.id in node_ids:
                raise ValueError(f"重复的 nodeId: {node.id}")
            node_ids.add(node.id)
            for child in node.children:
                walk(child, depth + 1)

        if self.root is not None:
            walk(self.root, 1)

        interaction_ids: set[str] = set()
        binding_ids: set[str] = set()
        for binding in self.bindings:
            if binding.id in binding_ids:
                raise ValueError(f"重复的 bindingId: {binding.id}")
            binding_ids.add(binding.id)
            if self.root is not None and binding.node_id not in node_ids:
                raise ValueError(
                    f"binding {binding.id} 指向不存在的节点 {binding.node_id}"
                )
        for interaction in self.interactions:
            if interaction.id in interaction_ids:
                raise ValueError(f"重复的 interactionId: {interaction.id}")
            interaction_ids.add(interaction.id)
            if self.root is not None and interaction.node_id not in node_ids:
                raise ValueError(
                    f"interaction {interaction.id} 指向不存在的节点 "
                    f"{interaction.node_id}"
                )
            if interaction.binding_id and interaction.binding_id not in binding_ids:
                raise ValueError(
                    f"interaction {interaction.id} 引用了不存在的 binding "
                    f"{interaction.binding_id}"
                )

        # 业务审查前允许列表数据源暂时未绑定；冻结 / 编译阶段必须闭环。
        if self.status in {"FROZEN", "COMPILED"}:
            for node in _iter_nodes(self.root):
                if not node.repeat:
                    continue
                source = node.repeat.source
                if not any(
                    binding.id == source or binding.entity == source
                    for binding in self.bindings
                ):
                    raise ValueError(
                        f"节点 {node.id} 的 repeat.source={source} " "没有对应 binding"
                    )
        return self


class VisualValidationReport(CamelModel):
    ok: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    node_count: int = 0
    max_depth: int = 0


def _iter_nodes(node: VisualNode | None):
    if node is None:
        return
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def compute_content_hash(document: VisualDocument) -> str:
    """对除 contentHash 外的文档内容做稳定哈希。"""
    payload = document.model_dump(
        mode="json",
        by_alias=True,
        exclude={"metadata": {"content_hash"}},
    )
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def freeze_visual_document(document: VisualDocument) -> VisualDocument:
    """冻结文档并写 contentHash；CodeGen 只接受冻结版本。"""
    frozen = document.model_copy(deep=True)
    frozen.status = "FROZEN"
    frozen.review.frozen_at = datetime.now(timezone.utc)
    # revision 也是冻结内容的一部分，必须先自增再计算 hash。
    frozen.revision += 1
    frozen.metadata.content_hash = compute_content_hash(frozen)
    return frozen


def validate_visual_document(
    payload: dict[str, Any] | VisualDocument,
) -> VisualValidationReport:
    """校验任意输入；返回稳定报告，不向上抛 Pydantic 异常。"""
    try:
        document = (
            payload
            if isinstance(payload, VisualDocument)
            else VisualDocument.model_validate(payload)
        )
    except ValidationError as exc:
        return VisualValidationReport(
            ok=False,
            errors=[
                f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                for item in exc.errors()
            ],
        )
    except ValueError as exc:
        return VisualValidationReport(ok=False, errors=[str(exc)])

    nodes = list(_iter_nodes(document.root))
    warnings: list[str] = []
    if document.status == "DRAFT":
        for node in nodes:
            if not node.repeat:
                continue
            source = node.repeat.source
            if not any(
                binding.id == source or binding.entity == source
                for binding in document.bindings
            ):
                warnings.append(
                    f"节点 {node.id} 的 repeat.source={source} 尚未绑定，"
                    "将在业务审查阶段补齐"
                )
    return VisualValidationReport(
        ok=True,
        warnings=warnings,
        node_count=len(nodes),
        max_depth=_tree_depth(document.root),
    )


def _tree_depth(node: VisualNode | None) -> int:
    if node is None:
        return 0
    return 1 + max((_tree_depth(child) for child in node.children), default=0)


__all__ = [
    "CONTAINER_NODE_TYPES",
    "MAX_VISUAL_DEPTH",
    "MAX_VISUAL_NODES",
    "REPEAT_NODE_TYPES",
    "VISUAL_SCHEMA_VERSION",
    "VisualA11y",
    "VisualBinding",
    "VisualCodegen",
    "VisualDesignTokens",
    "VisualDocument",
    "VisualDocumentStatus",
    "VisualInteraction",
    "VisualLayout",
    "VisualMetadata",
    "VisualNode",
    "VisualNodeType",
    "VisualProvenance",
    "VisualRepeat",
    "VisualReview",
    "VisualSemantics",
    "VisualState",
    "VisualStyle",
    "VisualValidationError",
    "VisualValidationReport",
    "compute_content_hash",
    "freeze_visual_document",
    "validate_visual_document",
]
