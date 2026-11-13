"""目标解析（Resolver）领域契约。

Phase 1 的核心是“先定位、后写入”，让大模型不再猜目标节点：

  AnnotationEnvelope + 沙盒 AST  -->  TargetResolution
                                        ├─ TargetAnchor（精确范围 / 白名单 / 禁止区）
                                        └─ TargetCandidate（歧义候选，交前端选择）

约束：
  - 本模块只描述数据结构与状态常量，不依赖 Babel / Docker / LLM；
  - 状态里保存的是 ``to_payload()`` 后的纯 JSON，便于 LangGraph
    Checkpointer 序列化；
  - ``anchorId`` 必须由服务端按 ``annotationId`` 确定性生成，模型只能
    引用，不能自造（工具层会校验）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---- 解析状态 ----
RESOLVED = "RESOLVED"
TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
SKIPPED = "SKIPPED"

# ---- AST 编辑工具错误码（Coder 据此修正参数，禁止盲猜重试）----
ANCHOR_NOT_FOUND = "ANCHOR_NOT_FOUND"
ANCHOR_RANGE_MISMATCH = "ANCHOR_RANGE_MISMATCH"
FILE_NOT_ALLOWED = "FILE_NOT_ALLOWED"
RANGE_NOT_ALLOWED = "RANGE_NOT_ALLOWED"
PROTECTED_COMPONENT = "PROTECTED_COMPONENT"
OPERATION_NOT_ALLOWED = "OPERATION_NOT_ALLOWED"
INVALID_OPERATION = "INVALID_OPERATION"
INVALID_JSX_SNIPPET = "INVALID_JSX_SNIPPET"
INVALID_INSERTION_POINT = "INVALID_INSERTION_POINT"
DELETE_SCOPE_MISMATCH = "DELETE_SCOPE_MISMATCH"
EDIT_CONTEXT_REQUIRED = "EDIT_CONTEXT_REQUIRED"
AST_EDIT_FAILED = "AST_EDIT_FAILED"

VALID_OPERATIONS = ("insert", "delete", "modify")


def anchor_id_for(annotation_id: str) -> str:
    """由 annotationId 推导稳定 anchorId。

    前端 / 模型只看到这个 ID；即使模型改动大小写或拼写，工具层也会
    直接回 ``ANCHOR_NOT_FOUND``，不会命中其它标注的目标。
    """
    raw = str(annotation_id or "").strip()
    return f"anc_{raw}" if raw else ""


@dataclass(frozen=True)
class SourceRange:
    """源码区间：行列用于展示，offset 用于精确切片。"""

    start_line: int
    start_column: int
    end_line: int
    end_column: int
    start_offset: int
    end_offset: int

    def to_payload(self) -> dict[str, int]:
        return {
            "startLine": self.start_line,
            "startColumn": self.start_column,
            "endLine": self.end_line,
            "endColumn": self.end_column,
            "startOffset": self.start_offset,
            "endOffset": self.end_offset,
        }


@dataclass(frozen=True)
class TargetCandidate:
    """歧义候选节点：交给前端做多轮选择。"""

    candidate_id: str
    file: str
    range: SourceRange
    node_type: str
    component: str
    preview: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "candidateId": self.candidate_id,
            "file": self.file,
            "range": self.range.to_payload(),
            "nodeType": self.node_type,
            "component": self.component,
            "preview": self.preview,
        }


@dataclass(frozen=True)
class TargetAnchor:
    """一个标注解析后的唯一可写锚点。"""

    anchor_id: str
    annotation_id: str
    file: str
    range: SourceRange
    node_type: str = "JSXElement"
    component: str = ""
    node_source: str = ""
    # 公共组件：同一源码位置在页面中渲染多次，或被其它文件 import。
    is_shared: bool = False
    # 保护标识；为真时工具硬拒“修改公共组件定义”。
    protected: bool = False
    shared_reason: str = ""
    # 组件定义所在文件；共享组件只允许在 callSite 文件中做实例级修改。
    component_file: str = ""
    call_site_file: str = ""
    # 允许写入的文件白名单（绝对权威，工具层不再自行推断）。
    allowed_files: tuple[str, ...] = ()
    # 禁止修改的源码区间（公共组件定义体）。
    forbidden_ranges: tuple[SourceRange, ...] = ()
    allowed_operations: tuple[str, ...] = VALID_OPERATIONS
    # 父容器锚点允许的插入位置。自闭合元素只能 before / after；
    # 成对元素 / Fragment 才允许 inside_start / inside_end / inside_index。
    allowed_insert_positions: tuple[str, ...] = ("before", "after")
    # 前端拖拽落位事实（父容器 instanceId、兄弟锚点、顺序索引）。
    # 只作为 Coder 的确定性提示，写入仍由 apply_ast_edit 校验。
    placement: dict[str, Any] | None = None
    status: str = RESOLVED
    candidates: tuple[TargetCandidate, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "anchorId": self.anchor_id,
            "annotationId": self.annotation_id,
            "file": self.file,
            "range": self.range.to_payload(),
            "nodeType": self.node_type,
            "component": self.component,
            "nodeSource": self.node_source,
            "isShared": self.is_shared,
            "protected": self.protected,
            "sharedReason": self.shared_reason,
            "componentFile": self.component_file,
            "callSiteFile": self.call_site_file,
            "allowedFiles": list(self.allowed_files),
            "forbiddenRanges": [item.to_payload() for item in self.forbidden_ranges],
            "allowedOperations": list(self.allowed_operations),
            "allowedInsertPositions": list(self.allowed_insert_positions),
            "placement": dict(self.placement) if self.placement else None,
            "status": self.status,
            "candidates": [item.to_payload() for item in self.candidates],
        }


@dataclass(frozen=True)
class TargetResolution:
    """一次 Run 的全部解析结果。"""

    status: str = SKIPPED
    anchors: tuple[TargetAnchor, ...] = ()
    reason: str = ""
    # 解析器版本 / 证据，便于审计“为什么判定为公共组件”。
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def has_editable_anchor(self) -> bool:
        return any(anchor.status == RESOLVED for anchor in self.anchors)

    @property
    def ambiguous(self) -> tuple[TargetAnchor, ...]:
        return tuple(
            anchor for anchor in self.anchors if anchor.status == TARGET_AMBIGUOUS
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "evidence": dict(self.evidence),
            "anchors": [anchor.to_payload() for anchor in self.anchors],
        }


def resolution_from_payload(payload: Any) -> dict[str, Any]:
    """把外部（Provider / Checkpoint）返回的解析结果规整为纯 JSON。

    Provider 只需返回 ``{"status": ..., "anchors": [...]}``；未知字段
    原样保留，避免适配层与领域层版本耦合。非法输入一律降级为 SKIPPED，
    绝不让解析异常把整次 Run 判死。
    """
    if not isinstance(payload, dict):
        return TargetResolution(reason="解析器未返回结果").to_payload()
    status = str(payload.get("status") or SKIPPED)
    raw_anchors = payload.get("anchors")
    anchors: list[dict[str, Any]] = []
    if isinstance(raw_anchors, list):
        anchors = [item for item in raw_anchors if isinstance(item, dict)]
    return {
        "status": status,
        "reason": str(payload.get("reason") or ""),
        "evidence": (
            dict(payload.get("evidence") or {})
            if isinstance(payload.get("evidence"), dict)
            else {}
        ),
        "anchors": anchors,
    }


def find_anchor(resolution: dict[str, Any], anchor_id: str) -> dict[str, Any] | None:
    """按 anchorId 精确查找；大小写 / 空白差异不做容错。"""
    target = str(anchor_id or "")
    if not target:
        return None
    for anchor in resolution.get("anchors") or []:
        if isinstance(anchor, dict) and anchor.get("anchorId") == target:
            return anchor
    return None


def anchor_prompt_view(anchor: dict[str, Any]) -> dict[str, Any]:
    """给 Coder Prompt 的锚点视图：只保留定位与边界，不注入业务字段。"""
    return {
        "anchorId": anchor.get("anchorId"),
        "annotationId": anchor.get("annotationId"),
        "file": anchor.get("file"),
        "range": anchor.get("range"),
        "nodeType": anchor.get("nodeType"),
        "component": anchor.get("component"),
        "isShared": bool(anchor.get("isShared")),
        "protected": bool(anchor.get("protected")),
        "allowedFiles": anchor.get("allowedFiles") or [],
        "allowedOperations": anchor.get("allowedOperations") or [],
        "allowedInsertPositions": anchor.get("allowedInsertPositions")
        or ["before", "after"],
        # 拖拽落位：让 Coder 明确“插到父容器内部第几个兄弟位置”，
        # 而不是从自然语言里猜绝对坐标。
        "placement": anchor.get("placement") or None,
        "nodeSource": str(anchor.get("nodeSource") or "")[:4000],
    }


__all__ = [
    "ANCHOR_NOT_FOUND",
    "ANCHOR_RANGE_MISMATCH",
    "AST_EDIT_FAILED",
    "DELETE_SCOPE_MISMATCH",
    "EDIT_CONTEXT_REQUIRED",
    "FILE_NOT_ALLOWED",
    "INVALID_INSERTION_POINT",
    "INVALID_JSX_SNIPPET",
    "INVALID_OPERATION",
    "OPERATION_NOT_ALLOWED",
    "PROTECTED_COMPONENT",
    "RANGE_NOT_ALLOWED",
    "RESOLVED",
    "SKIPPED",
    "SourceRange",
    "TARGET_AMBIGUOUS",
    "TARGET_NOT_FOUND",
    "TargetAnchor",
    "TargetCandidate",
    "TargetResolution",
    "VALID_OPERATIONS",
    "anchor_id_for",
    "anchor_prompt_view",
    "find_anchor",
    "resolution_from_payload",
]
