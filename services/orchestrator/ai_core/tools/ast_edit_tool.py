"""唯一写工具：``apply_ast_edit``（Phase 1）。

设计目标：让 Coder 从“自由生成 diff / 整文件”退化为“提交结构化编辑指令”。
工具本身不相信模型参数，所有硬门禁都在这里执行：

  1. 锚点必须存在且状态为 RESOLVED（不允许模型自造 anchorId）；
  2. 目标文件必须在 Resolver 给出的白名单内；
  3. 保护标识为真时，绝对禁止修改公共组件定义文件 / 定义范围；
  4. modify / delete 的区间必须与锚点完全一致（禁止扩大范围误删父节点）；
  5. insert 支持锚点前 / 后，或按 Resolver 白名单插入容器内部
     （inside_start / inside_end / inside_index），父级必须是 JSX 容器；
  6. delete 由 Babel 执行器做 AST 同构校验（原树减目标子树 = 结果树）。

工具只计算新文件内容，不直接落盘；Graph 会把结果交给 Applier，
继续复用草稿隔离与实例作用域门禁。
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool, tool

from ai_core.domain.resolution import (
    ANCHOR_NOT_FOUND,
    AST_EDIT_FAILED,
    EDIT_CONTEXT_REQUIRED,
    FILE_NOT_ALLOWED,
    INVALID_INSERTION_POINT,
    INVALID_JSX_SNIPPET,
    INVALID_OPERATION,
    OPERATION_NOT_ALLOWED,
    PROTECTED_COMPONENT,
    RANGE_NOT_ALLOWED,
    RESOLVED,
    VALID_OPERATIONS,
    find_anchor,
)
from ai_core.ports.sandbox import SandboxError, SandboxProvider
from ai_core.tools.base import ToolDescriptor, ToolRegistry

logger = logging.getLogger(__name__)

# 单次 JSX 片段上限：防止模型把整页代码塞进一次编辑，变相恢复“全量写文件”。
MAX_JSX_SNIPPET_CHARS = 20_000

# 插入位置：
#  - before / after：以锚点为兄弟插入（旧协议）；
#  - inside_start / inside_end / inside_index：以锚点为父容器插入子节点，
#    供“暂存组件拖入某个盒子”使用。是否允许由 Resolver 按节点形态给出。
VALID_INSERT_POSITIONS = (
    "before",
    "after",
    "inside_start",
    "inside_end",
    "inside_index",
)


@dataclass(frozen=True)
class AstEditContext:
    """一次工具调用所需的服务器上下文（模型不可见、不可伪造）。"""

    sandbox: SandboxProvider
    sandbox_id: str
    draft_id: str | None
    resolution: dict[str, Any]


# 使用 ContextVar 而不是实例属性：同一 ToolRegistry 会被多个 Run 并发复用，
# 上下文必须跟随当前执行流，不能串到别的沙盒。
_EDIT_CONTEXT: ContextVar[AstEditContext | None] = ContextVar(
    "ast_edit_context", default=None
)


def bind_ast_edit_context(
    sandbox: SandboxProvider,
    sandbox_id: str,
    resolution: dict[str, Any],
    draft_id: str | None = None,
):
    """绑定当前执行流的编辑上下文；返回值用于 ``reset_ast_edit_context``。"""
    return _EDIT_CONTEXT.set(
        AstEditContext(
            sandbox=sandbox,
            sandbox_id=sandbox_id,
            draft_id=draft_id,
            resolution=dict(resolution or {}),
        )
    )


def reset_ast_edit_context(token: Any) -> None:
    """恢复上一个上下文；必须在 finally 中调用。"""
    try:
        _EDIT_CONTEXT.reset(token)
    except (ValueError, LookupError):  # pragma: no cover - 防御性兜底
        _EDIT_CONTEXT.set(None)


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {
        "status": "ERROR",
        "code": code,
        "message": message,
        "details": details,
    }


def _range_offsets(raw: Any) -> tuple[int, int] | None:
    """从模型参数中提取 offset；缺失时尝试由行列换算失败即返回 None。"""
    if not isinstance(raw, dict):
        return None
    try:
        start = int(raw.get("startOffset"))
        end = int(raw.get("endOffset"))
    except (TypeError, ValueError):
        return None
    if start < 0 or end <= start:
        return None
    return start, end


def _anchor_offsets(anchor: dict[str, Any]) -> tuple[int, int] | None:
    return _range_offsets(anchor.get("range"))


def _overlaps(start: int, end: int, forbidden: dict[str, Any]) -> bool:
    other = _range_offsets(forbidden)
    if other is None:
        return False
    other_start, other_end = other
    return start < other_end and other_start < end


def execute_ast_edit(
    request: dict[str, Any],
    context: AstEditContext,
) -> dict[str, Any]:
    """执行门禁 + Babel 编辑；返回可直接 JSON 序列化的结果。

    该函数与 LangChain Tool 解耦，便于单元测试直接注入上下文，
    也便于未来切换为原生 Function Calling 时复用同一套规则。
    """
    operation = str(request.get("operation") or "").lower().strip()
    anchor_id = str(request.get("anchor_id") or request.get("anchorId") or "").strip()
    jsx_code = str(request.get("jsx_code") or request.get("jsxCode") or "")
    insert_position = (
        str(request.get("insert_position") or request.get("insertPosition") or "after")
        .lower()
        .strip()
    )
    # inside_index 需要显式序号；其余位置忽略该字段。
    raw_insert_index = request.get("insert_index", request.get("insertIndex"))
    insert_index: int | None = None
    if raw_insert_index is not None and str(raw_insert_index).strip() != "":
        try:
            insert_index = int(raw_insert_index)
        except (TypeError, ValueError):
            return _error(
                INVALID_INSERTION_POINT,
                "insert_index 必须是整数",
                insertIndex=raw_insert_index,
            )

    # ---- 参数级门禁 ----
    if operation not in VALID_OPERATIONS:
        return _error(
            INVALID_OPERATION,
            f"operation 必须是 {list(VALID_OPERATIONS)} 之一",
            operation=operation,
        )
    if anchor_id == "":
        return _error(ANCHOR_NOT_FOUND, "缺少 anchorId；禁止自行推断目标")
    if operation in {"insert", "modify"} and not jsx_code.strip():
        return _error(INVALID_JSX_SNIPPET, "insert / modify 必须提供 jsxCode")
    if len(jsx_code) > MAX_JSX_SNIPPET_CHARS:
        return _error(
            INVALID_JSX_SNIPPET,
            f"jsxCode 超过 {MAX_JSX_SNIPPET_CHARS} 字符，疑似整文件重写",
        )
    if insert_position not in VALID_INSERT_POSITIONS:
        return _error(
            INVALID_INSERTION_POINT,
            f"insertPosition 只能是 {list(VALID_INSERT_POSITIONS)} 之一",
            insertPosition=insert_position,
        )
    if insert_position == "inside_index" and insert_index is None:
        return _error(
            INVALID_INSERTION_POINT,
            "inside_index 必须提供 insert_index（从 0 开始的子节点序号）",
        )

    # ---- 锚点门禁 ----
    anchor = find_anchor(context.resolution, anchor_id)
    if anchor is None:
        return _error(
            ANCHOR_NOT_FOUND,
            f"锚点不存在或已过期: {anchor_id}；请只使用解析结果中给出的 anchorId",
        )
    if str(anchor.get("status") or "") != RESOLVED:
        return _error(
            ANCHOR_NOT_FOUND,
            f"锚点状态不是 RESOLVED: {anchor.get('status')}",
        )
    anchor_range = _anchor_offsets(anchor)
    if anchor_range is None:
        return _error(ANCHOR_NOT_FOUND, "锚点缺少有效 range")

    allowed_operations = [str(item) for item in anchor.get("allowedOperations") or []]
    if allowed_operations and operation not in allowed_operations:
        return _error(
            OPERATION_NOT_ALLOWED,
            f"该锚点不允许 {operation} 操作",
            allowedOperations=allowed_operations,
        )

    # 节点形态门禁：自闭合元素不能 inside_*，否则会生成非法 JSX。
    allowed_positions = [
        str(item) for item in anchor.get("allowedInsertPositions") or []
    ]
    if (
        operation == "insert"
        and allowed_positions
        and insert_position not in allowed_positions
    ):
        return _error(
            INVALID_INSERTION_POINT,
            f"锚点 {anchor_id} 不支持 insertPosition={insert_position}",
            allowedInsertPositions=allowed_positions,
        )

    file = str(anchor.get("file") or "")
    allowed_files = [str(item) for item in anchor.get("allowedFiles") or [] if item]
    if file not in allowed_files:
        return _error(
            FILE_NOT_ALLOWED,
            f"目标文件不在白名单内: {file}",
            allowedFiles=allowed_files,
        )

    # 保护标识：公共组件定义文件零写入。即使模型换了一个文件，只要
    # 该文件是组件定义文件也同样拒绝，避免“改个路径绕过”的提示注入。
    if bool(anchor.get("protected")):
        component_file = str(anchor.get("componentFile") or file)
        if file == component_file:
            return _error(
                PROTECTED_COMPONENT,
                "目标是公共组件定义，禁止修改；请在 callSite 文件中做实例级修改",
                componentFile=component_file,
                callSiteFile=anchor.get("callSiteFile") or "",
            )

    # ---- 范围门禁 ----
    target_range = _range_offsets(
        request.get("target_range") or request.get("targetRange")
    )
    if operation in {"modify", "delete"}:
        # modify / delete 必须精确命中锚点；扩大范围可能误删父节点 / 兄弟节点。
        if target_range != anchor_range:
            return _error(
                RANGE_NOT_ALLOWED,
                "modify / delete 的目标范围必须与锚点完全一致",
                anchorRange=anchor.get("range"),
                targetRange=request.get("target_range") or request.get("targetRange"),
            )
    else:  # insert
        if target_range is not None and target_range not in {
            anchor_range,
            (anchor_range[0], anchor_range[0]),
            (anchor_range[1], anchor_range[1]),
        }:
            return _error(
                RANGE_NOT_ALLOWED,
                "insert 的目标范围必须等于锚点，或锚点起点 / 终点的零宽位置",
                anchorRange=anchor.get("range"),
            )

    start, end = target_range or anchor_range
    for forbidden in anchor.get("forbiddenRanges") or []:
        if isinstance(forbidden, dict) and _overlaps(start, end, forbidden):
            return _error(
                PROTECTED_COMPONENT,
                "编辑范围与公共组件定义区重叠，已阻断",
                forbiddenRange=forbidden,
            )

    # ---- Babel 精确编辑 ----
    edit_spec = {
        "file": file,
        "operation": operation,
        "anchorRange": anchor.get("range") or {},
        "targetRange": (
            (request.get("target_range") or request.get("targetRange")) or {}
        ),
        "insertPosition": insert_position,
        "insertIndex": insert_index,
        "jsxCode": jsx_code,
    }
    # 由 Graph 在“同文件多编辑”时注入上一条结果；模型参数不参与该字段。
    override = request.get("content_override")
    if isinstance(override, str):
        edit_spec["contentOverride"] = override
    try:
        result = context.sandbox.apply_ast_edit(
            context.sandbox_id,
            edit_spec,
            draft_id=context.draft_id,
        )
    except SandboxError as exc:
        logger.warning("apply_ast_edit sandbox failed: %s", exc)
        return _error(AST_EDIT_FAILED, f"AST 编辑执行失败: {exc}")
    except Exception as exc:  # noqa: BLE001 - 工具层必须 fail-closed
        logger.exception("apply_ast_edit crashed")
        return _error(AST_EDIT_FAILED, f"AST 编辑内部错误: {exc}")

    if not isinstance(result, dict):
        return _error(AST_EDIT_FAILED, "沙盒未返回 AST 编辑结果")
    if str(result.get("status") or "") != "OK":
        code = str(result.get("code") or AST_EDIT_FAILED)
        message = str(result.get("message") or "AST 编辑被拒绝")
        details = (
            result.get("details") if isinstance(result.get("details"), dict) else {}
        )
        return _error(code, message, **details)

    content = result.get("content")
    if not isinstance(content, str) or not content.strip():
        return _error(AST_EDIT_FAILED, "AST 编辑结果为空，拒绝写入")
    return {
        "status": "OK",
        "anchorId": anchor_id,
        "annotationId": anchor.get("annotationId"),
        "file": file,
        "operation": operation,
        "content": content,
    }


def build_apply_ast_edit_tool() -> BaseTool:
    """构造 LangChain Tool（供 ToolNode / 原生 Function Calling 使用）。

    工具从 ContextVar 读取沙盒与解析结果；未绑定上下文时返回
    ``EDIT_CONTEXT_REQUIRED``，绝不在无沙盒环境下静默成功。
    """

    @tool("apply_ast_edit")
    def apply_ast_edit(
        anchor_id: str,
        operation: str,
        target_range: dict[str, Any],
        jsx_code: str = "",
        insert_position: str = "after",
        insert_index: int | None = None,
    ) -> str:
        """按解析锚点精确编辑 JSX（唯一写操作）。

        Args:
            anchor_id: 解析节点给出的 anchorId，禁止自造。
            operation: insert / delete / modify。
            target_range: 目标区间，含 startOffset / endOffset。
            jsx_code: insert / modify 使用的新 JSX 片段（单个元素或 Fragment）。
            insert_position: insert 位置：before / after / inside_start /
                inside_end / inside_index（后三者要求锚点是成对 JSX 容器）。
            insert_index: inside_index 的子节点序号（从 0 开始）。
        """
        context = _EDIT_CONTEXT.get()
        if context is None:
            return json.dumps(
                _error(EDIT_CONTEXT_REQUIRED, "未绑定沙盒上下文，禁止执行写操作"),
                ensure_ascii=False,
            )
        result = execute_ast_edit(
            {
                "anchor_id": anchor_id,
                "operation": operation,
                "target_range": target_range,
                "jsx_code": jsx_code,
                "insert_position": insert_position,
                "insert_index": insert_index,
            },
            context,
        )
        return json.dumps(result, ensure_ascii=False)

    return apply_ast_edit


def register_apply_ast_edit_tool(registry: ToolRegistry) -> None:
    """注册 Coder 唯一写权限；danger_level=write 供审计 / 前端权限展示。"""
    registry.register(
        ToolDescriptor(
            name="apply_ast_edit",
            description=(
                "按 Resolver 锚点精确执行 JSX 编辑（insert/delete/modify），"
                "带文件白名单、公共组件保护与删除同构校验。"
            ),
            danger_level="write",
            metadata={
                "operations": list(VALID_OPERATIONS),
                "writesFiles": False,
                "requiresAnchor": True,
                "maxSnippetChars": MAX_JSX_SNIPPET_CHARS,
            },
        ),
        build_apply_ast_edit_tool(),
    )


__all__ = [
    "AstEditContext",
    "MAX_JSX_SNIPPET_CHARS",
    "bind_ast_edit_context",
    "build_apply_ast_edit_tool",
    "execute_ast_edit",
    "register_apply_ast_edit_tool",
    "reset_ast_edit_context",
]
