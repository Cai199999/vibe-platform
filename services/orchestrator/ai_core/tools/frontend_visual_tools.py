"""前端 Agent 的语义编辑工具门面。

设计原则：

  - **不重复造轮子**：所有写操作复用 ``DirectOperationService``，
    统一经过目标解析、ScopePolicy、revision 校验、AST Gate、ChangeLog
    与 Git checkpoint；
  - **只暴露意图级工具**：Agent 不接触 anchorRange / JSX 源码 / 文件写入；
  - **fail-closed**：未绑定项目、沙盒或 revision 时拒绝执行；
  - **可回滚**：每个工具都返回 revision、changeId 与结构化错误码。

这些工具是 ``apply_ast_edit`` 的上层门面，不是新的执行器。后续新增
样式 / 属性 / wrap 等能力时，应继续扩展 Direct Ops，而不是在 Tool 层
直接修改 AST。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool, tool

from ai_core.direct_ops.service import DirectActionError, DirectOperationService
from ai_core.domain.direct_ops import (
    DirectActionOptions,
    DirectActionPayload,
    DirectActionTarget,
    DirectErrorCode,
)
from ai_core.tools.base import ToolDescriptor, ToolRegistry

logger = logging.getLogger(__name__)

VALID_INSERT_POSITIONS = ("inside_start", "inside_end", "inside_index")
MAX_TEXT_CHARS = 2000
# CodeGen / SDK 注入的 data-node-id 只允许安全字符，避免选择器注入。
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


class FrontendToolError(ValueError):
    """工具参数或上下文错误；会被转成稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FrontendToolContext:
    """一次前端 Agent Run 的服务器上下文（模型不可见、不可伪造）。"""

    service: DirectOperationService
    project_id: str
    sandbox_id: str
    base_revision: str | None = None
    run_id: str | None = None


_CONTEXT: ContextVar[FrontendToolContext | None] = ContextVar(
    "frontend_visual_tool_context", default=None
)


def bind_frontend_tool_context(
    service: DirectOperationService,
    project_id: str,
    sandbox_id: str,
    base_revision: str | None = None,
    run_id: str | None = None,
):
    """绑定当前执行流的项目上下文；返回值用于 reset。"""
    return _CONTEXT.set(
        FrontendToolContext(
            service=service,
            project_id=str(project_id),
            sandbox_id=str(sandbox_id),
            base_revision=str(base_revision).strip() if base_revision else None,
            run_id=str(run_id).strip() if run_id else None,
        )
    )


def reset_frontend_tool_context(token: Any) -> None:
    try:
        _CONTEXT.reset(token)
    except (ValueError, LookupError):  # pragma: no cover - 防御性兜底
        _CONTEXT.set(None)


def _context() -> FrontendToolContext:
    context = _CONTEXT.get()
    if context is None:
        raise FrontendToolError(
            "FRONTEND_TOOL_CONTEXT_REQUIRED",
            "前端编辑工具缺少项目上下文，已拒绝执行。",
        )
    return context


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _node_selector(node_id: str) -> str:
    value = str(node_id or "").strip()
    if not value:
        raise FrontendToolError("DIRECT_TARGET_REQUIRED", "node_id 不能为空")
    if not _NODE_ID_RE.fullmatch(value):
        raise FrontendToolError(
            "DIRECT_TARGET_INVALID",
            "node_id 含非法字符，只允许字母、数字、_ . : -",
        )
    # 构建期 oid 与 CodeGen data-node-id 都是稳定身份；Agent 可以传任意一种。
    attr = "data-oid" if value.startswith("oid_") else "data-node-id"
    return f'[{attr}="{value}"]'


def _target(
    *,
    node_id: str = "",
    selector: str = "",
    file: str = "",
    line: int | None = None,
    column: int | None = None,
    anchor_id: str = "",
    component: str = "",
    instance: int | None = None,
    placement: dict[str, Any] | None = None,
) -> DirectActionTarget:
    node_id = str(node_id or "").strip()
    selector = str(selector or "").strip()
    file = str(file or "").strip()
    if node_id:
        selector = _node_selector(node_id)
    if not selector and not file:
        raise FrontendToolError(
            "DIRECT_TARGET_REQUIRED",
            "必须提供 node_id、selector 或 file 之一。",
        )
    if line is not None and line < 1:
        raise FrontendToolError("DIRECT_TARGET_INVALID", "line 必须大于等于 1")
    if column is not None and column < 1:
        raise FrontendToolError("DIRECT_TARGET_INVALID", "column 必须大于等于 1")
    return DirectActionTarget(
        anchor_id=anchor_id.strip() or None,
        selector=selector or None,
        file=file or None,
        line=line,
        column=column,
        component=component.strip() or None,
        instance=instance,
        placement=dict(placement or {}),
    )


def _placement(
    insert_position: str,
    sibling_index: int | None,
    *,
    parent_selector: str = "",
) -> dict[str, Any]:
    position = str(insert_position or "inside_end").strip().lower()
    if position not in VALID_INSERT_POSITIONS:
        raise FrontendToolError(
            "DIRECT_TARGET_INVALID",
            f"insert_position 只能是 {list(VALID_INSERT_POSITIONS)} 之一",
        )
    placement: dict[str, Any] = {"insertPosition": position}
    if parent_selector:
        placement["parentSelector"] = parent_selector
    if position == "inside_index":
        if sibling_index is None or int(sibling_index) < 0:
            raise FrontendToolError(
                "DIRECT_TARGET_INVALID",
                "inside_index 必须提供大于等于 0 的 sibling_index",
            )
        placement["siblingIndex"] = int(sibling_index)
    elif sibling_index is not None:
        if int(sibling_index) < 0:
            raise FrontendToolError("DIRECT_TARGET_INVALID", "sibling_index 不能小于 0")
        placement["siblingIndex"] = int(sibling_index)
    return placement


def _failure(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, FrontendToolError):
        return {
            "ok": False,
            "code": exc.code,
            "message": str(exc),
            "requiresHuman": False,
            "retryable": False,
        }
    if isinstance(exc, DirectActionError):
        return {
            "ok": False,
            "code": exc.code,
            "message": str(exc),
            "diagnostic": dict(exc.diagnostic or {}),
            "requiresHuman": exc.code
            in {
                DirectErrorCode.UNSAFE_OPERATION,
                DirectErrorCode.TARGET_AMBIGUOUS,
                DirectErrorCode.ROUTE_TO_LLM,
            },
            "retryable": exc.code
            in {
                DirectErrorCode.REVISION_MISMATCH,
                DirectErrorCode.PROJECT_BUSY,
                DirectErrorCode.RESOLVER_UNAVAILABLE,
            },
        }
    # Pydantic 的 DirectActionPayload 校验失败也属于参数错误；不能把它
    # 包装成“内部错误”，否则 Agent 无法根据错误码修正参数。
    if isinstance(exc, ValueError):
        return {
            "ok": False,
            "code": "FRONTEND_TOOL_INVALID_ARGUMENT",
            "message": str(exc)[:500],
            "requiresHuman": False,
            "retryable": False,
        }
    logger.exception("frontend visual tool crashed")
    return {
        "ok": False,
        "code": "FRONTEND_TOOL_INTERNAL_ERROR",
        "message": f"前端编辑工具内部错误：{type(exc).__name__}",
        "requiresHuman": False,
        "retryable": False,
    }


def _execute(
    action: str,
    target: DirectActionTarget,
    payload: dict[str, Any] | None = None,
    *,
    dry_run: bool = False,
    base_revision: str = "",
    verify_build: bool = False,
) -> dict[str, Any]:
    context = _context()
    revision = str(base_revision or "").strip() or context.base_revision or ""
    auto_revision = False
    if not dry_run and not revision:
        # Agent 通常不持有 revision；这里读取当前 HEAD，真正的并发校验
        # 仍由 DirectOperationService 在写锁内完成。
        revision = str(
            context.service.revision(context.project_id, context.sandbox_id).get(
                "revision"
            )
            or ""
        )
        auto_revision = True

    request = DirectActionPayload(
        direct_action_id=f"dact_{uuid.uuid4().hex[:20]}",
        project_id=context.project_id,
        sandbox_id=context.sandbox_id,
        run_id=context.run_id,
        base_revision=revision or None,
        target=target,
        action=action,
        payload=dict(payload or {}),
        options=DirectActionOptions(
            source="FRONTEND_AGENT_TOOL",
            dry_run=dry_run,
            verify_build=verify_build,
        ),
    )
    response = context.service.execute(request)
    result = response.model_dump(mode="json", by_alias=True)
    result["ok"] = True
    if auto_revision:
        result["baseRevisionAutoFetched"] = True
    return result


def build_update_text_tool() -> BaseTool:
    @tool("update_text")
    def update_text(
        text: str,
        node_id: str = "",
        selector: str = "",
        dry_run: bool = False,
        base_revision: str = "",
    ) -> str:
        """修改一个纯文本 JSX 节点的文案。

        参数：
          - text：新文案，最长 2000 字符，不能包含 < > { } 或换行；
          - node_id：生成源码中的 data-node-id（推荐）；
          - selector：完整 CSS 选择器，如 [data-node-id="..."]；
          - dry_run：只验证不落盘；
          - base_revision：可选，缺省时读取当前 HEAD。

        返回 JSON，包含 ok / risk / resolvedTarget / revision / changeId。
        """
        try:
            if len(str(text or "")) > MAX_TEXT_CHARS:
                raise FrontendToolError(
                    "DIRECT_TARGET_INVALID",
                    f"text 超过 {MAX_TEXT_CHARS} 字符",
                )
            target = _target(node_id=node_id, selector=selector)
            return _json(
                _execute(
                    "UPDATE_TEXT",
                    target,
                    {"text": str(text)},
                    dry_run=dry_run,
                    base_revision=base_revision,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 统一转结构化错误
            return _json(_failure(exc))

    return update_text


def build_move_node_tool() -> BaseTool:
    @tool("move_node")
    def move_node(
        node_id: str = "",
        selector: str = "",
        parent_node_id: str = "",
        parent_selector: str = "",
        insert_position: str = "inside_end",
        sibling_index: int | None = None,
        dry_run: bool = False,
        base_revision: str = "",
    ) -> str:
        """把节点移动到同文件的另一个容器内。

        参数：
          - node_id / selector：被移动节点；
          - parent_node_id / parent_selector：目标容器；
          - insert_position：inside_start / inside_end / inside_index；
          - sibling_index：inside_index 时必填；
          - dry_run / base_revision：同其他写工具。

        跨文件移动不在本工具范围，会返回 requiresHuman 或路由给 AI。
        """
        try:
            parent = str(parent_selector or "").strip()
            if str(parent_node_id or "").strip():
                parent = _node_selector(parent_node_id)
            if not parent:
                raise FrontendToolError(
                    "DIRECT_TARGET_REQUIRED",
                    "move_node 必须提供 parent_node_id 或 parent_selector。",
                )
            placement = _placement(
                insert_position,
                sibling_index,
                parent_selector=parent,
            )
            target = _target(
                node_id=node_id,
                selector=selector,
                placement=placement,
            )
            return _json(
                _execute(
                    "MOVE_NODE",
                    target,
                    {},
                    dry_run=dry_run,
                    base_revision=base_revision,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return _json(_failure(exc))

    return move_node


def build_insert_component_tool() -> BaseTool:
    @tool("insert_component")
    def insert_component(
        component_type: str,
        parent_node_id: str = "",
        parent_selector: str = "",
        insert_position: str = "inside_end",
        sibling_index: int | None = None,
        style_context: dict[str, Any] | None = None,
        dry_run: bool = False,
        base_revision: str = "",
    ) -> str:
        """在指定容器内插入白名单基础组件。

        参数：
          - component_type：Button / Input / Text / Card / Container；
          - parent_node_id / parent_selector：目标容器；
          - insert_position / sibling_index：落位；
          - style_context：可选的既有样式上下文；
          - dry_run / base_revision：同其他写工具。

        非白名单组件会返回 requiresHuman 并路由给 AI Coder。
        """
        try:
            parent = str(parent_selector or "").strip()
            if str(parent_node_id or "").strip():
                parent = _node_selector(parent_node_id)
            if not parent:
                raise FrontendToolError(
                    "DIRECT_TARGET_REQUIRED",
                    "insert_component 必须提供 parent_node_id 或 parent_selector。",
                )
            placement = _placement(insert_position, sibling_index)
            target = _target(selector=parent, placement=placement)
            return _json(
                _execute(
                    "ADD_COMPONENT",
                    target,
                    {
                        "componentType": str(component_type or "").strip(),
                        "styleContext": dict(style_context or {}),
                    },
                    dry_run=dry_run,
                    base_revision=base_revision,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return _json(_failure(exc))

    return insert_component


def build_delete_node_tool() -> BaseTool:
    @tool("delete_node")
    def delete_node(
        node_id: str = "",
        selector: str = "",
        dry_run: bool = False,
        base_revision: str = "",
    ) -> str:
        """删除节点，或按 ScopePolicy 替换为占位符。

        参数：
          - node_id / selector：目标节点；
          - dry_run / base_revision：同其他写工具。

        共享组件、循环项、页面根节点会返回 requiresHuman 或占位替换，
        不会静默破坏源码。
        """
        try:
            target = _target(node_id=node_id, selector=selector)
            return _json(
                _execute(
                    "DELETE_NODE",
                    target,
                    {},
                    dry_run=dry_run,
                    base_revision=base_revision,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return _json(_failure(exc))

    return delete_node


def build_get_frontend_revision_tool() -> BaseTool:
    @tool("get_frontend_revision")
    def get_frontend_revision() -> str:
        """读取当前项目沙盒的源码 revision，供写操作填写 base_revision。"""
        try:
            context = _context()
            result = context.service.revision(context.project_id, context.sandbox_id)
            return _json({"ok": True, **result})
        except Exception as exc:  # noqa: BLE001
            return _json(_failure(exc))

    return get_frontend_revision


def build_undo_last_frontend_change_tool() -> BaseTool:
    @tool("undo_last_frontend_change")
    def undo_last_frontend_change() -> str:
        """撤回最近一次已落盘的前端 Direct Op。

        只有 HEAD 仍等于该动作的 commit 时才允许撤回；之后的 AI 提交
        不会被误删。
        """
        try:
            context = _context()
            return _json(
                {
                    "ok": True,
                    **context.service.undo_last(context.project_id, context.sandbox_id),
                }
            )
        except Exception as exc:  # noqa: BLE001
            return _json(_failure(exc))

    return undo_last_frontend_change


def register_frontend_visual_tools(registry: ToolRegistry) -> None:
    """注册前端 Agent 的语义编辑工具；底层全部复用 Direct Ops。"""
    registrations: list[tuple[ToolDescriptor, BaseTool]] = [
        (
            ToolDescriptor(
                name="update_text",
                description="修改纯文本节点文案，复用 Direct Ops 的 AST 门禁与回滚。",
                danger_level="write",
                metadata={
                    "reuses": "DirectOperationService",
                    "action": "UPDATE_TEXT",
                    "arguments": ["text", "node_id", "selector", "dry_run"],
                },
            ),
            build_update_text_tool(),
        ),
        (
            ToolDescriptor(
                name="move_node",
                description="同文件容器内移动节点，复用 Direct Ops 的 ScopePolicy。",
                danger_level="write",
                metadata={
                    "reuses": "DirectOperationService",
                    "action": "MOVE_NODE",
                    "arguments": [
                        "node_id",
                        "parent_node_id",
                        "insert_position",
                        "sibling_index",
                    ],
                },
            ),
            build_move_node_tool(),
        ),
        (
            ToolDescriptor(
                name="insert_component",
                description="在容器内插入白名单基础组件，复用 Direct Ops 模板与结构校验。",
                danger_level="write",
                metadata={
                    "reuses": "DirectOperationService",
                    "action": "ADD_COMPONENT",
                    "arguments": [
                        "component_type",
                        "parent_node_id",
                        "insert_position",
                        "sibling_index",
                    ],
                },
            ),
            build_insert_component_tool(),
        ),
        (
            ToolDescriptor(
                name="delete_node",
                description="删除节点或替换占位，复用 Direct Ops 的 ScopePolicy 与同构校验。",
                danger_level="write",
                metadata={
                    "reuses": "DirectOperationService",
                    "action": "DELETE_NODE",
                    "arguments": ["node_id", "selector", "dry_run"],
                },
            ),
            build_delete_node_tool(),
        ),
        (
            ToolDescriptor(
                name="get_frontend_revision",
                description="读取当前前端源码 revision。",
                danger_level="read",
                metadata={"reuses": "DirectOperationService.revision"},
            ),
            build_get_frontend_revision_tool(),
        ),
        (
            ToolDescriptor(
                name="undo_last_frontend_change",
                description="撤回最近一次前端 Direct Op。",
                danger_level="write",
                metadata={"reuses": "DirectOperationService.undo_last"},
            ),
            build_undo_last_frontend_change_tool(),
        ),
    ]
    for descriptor, built in registrations:
        registry.register(descriptor, built)


__all__ = [
    "FrontendToolContext",
    "FrontendToolError",
    "bind_frontend_tool_context",
    "build_delete_node_tool",
    "build_get_frontend_revision_tool",
    "build_insert_component_tool",
    "build_move_node_tool",
    "build_undo_last_frontend_change_tool",
    "build_update_text_tool",
    "register_frontend_visual_tools",
    "reset_frontend_tool_context",
]
