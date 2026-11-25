"""A2A 反馈的只读契约校验。

本模块不接入运行时，只做“事件是否符合协议”的确定性检查，供单元测试与
前后端 fixtures 共用。运行时仍以 ``ai_core.a2a.build_feedback`` 为准；
这里新增规则不会改变现有事件行为。
"""

from __future__ import annotations

from typing import Any, Iterable

# (from_agent, to_agent, kind, requested_action)
A2A_ALLOWED: frozenset[tuple[str, str, str, str]] = frozenset(
    {
        ("Applier", "DBACoder", "schema_repair", "REGENERATE_DB"),
        ("Applier", "BackendCoder", "code_repair", "REPAIR_CODE"),
        ("BackendCoder", "Architect", "contract_revision", "REVISE_CONTRACT"),
        ("BackendCoder", "Architect", "contract_review", "REVIEW_CONTRACT"),
        ("ConsistencyVerifier", "Architect", "contract_revision", "REVISE_CONTRACT"),
        ("ConsistencyVerifier", "Architect", "contract_review", "REVIEW_CONTRACT"),
        ("ConsistencyVerifier", "DBACoder", "schema_repair", "REGENERATE_ORM"),
        (
            "ConsistencyVerifier",
            "BackendCoder",
            "route_repair",
            "REPAIR_BACKEND_JOIN",
        ),
        ("ConsistencyVerifier", "BackendCoder", "route_repair", "ADD_BACKEND_ROUTE"),
        ("DBACoder", "BackendCoder", "schema_ready", "REBIND_ORM"),
        (
            "Architect",
            "BackendCoder",
            "contract_revision_started",
            "WAIT_FOR_CONTRACT",
        ),
        ("Architect", "BackendCoder", "contract_confirmed", "GENERATE_WITH_CONTRACT"),
    }
)

A2A_REPAIR_KINDS: frozenset[str] = frozenset(
    {"schema_repair", "code_repair", "route_repair", "contract_revision"}
)
A2A_ESCALATION_KINDS: frozenset[str] = frozenset(
    {"contract_revision", "contract_review"}
)
A2A_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"RESOLVED", "ESCALATED", "FAILED"}
)

_REQUIRED_FIELDS = (
    "feedbackId",
    "runId",
    "fromAgent",
    "toAgent",
    "kind",
    "requestedAction",
    "summary",
)


def validate_a2a_feedback(item: dict[str, Any]) -> list[str]:
    """返回单条反馈的协议错误；空列表表示合法。"""
    errors: list[str] = []
    for field in _REQUIRED_FIELDS:
        if not str(item.get(field) or "").strip():
            errors.append(f"缺少 {field}")
    if errors:
        return errors

    key = (
        str(item["fromAgent"]),
        str(item["toAgent"]),
        str(item["kind"]),
        str(item["requestedAction"]),
    )
    if key not in A2A_ALLOWED:
        errors.append(
            "不在 A2A 允许矩阵内: "
            f"{key[0]} -> {key[1]} / {key[2]} / {key[3]}"
        )
    evidence = item.get("evidence")
    if evidence is not None and not isinstance(evidence, list):
        errors.append("evidence 必须是数组")
    if item.get("payload") is not None and not isinstance(item["payload"], dict):
        errors.append("payload 必须是对象")
    return errors


def validate_a2a_sequence(
    items: Iterable[dict[str, Any]],
    *,
    run_terminal: str | None = None,
) -> dict[str, Any]:
    """校验一组反馈，并生成问题叙事所需的统计。"""
    errors: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    steps: list[dict[str, Any]] = []

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"第 {index + 1} 条不是对象")
            continue
        feedback_id = str(item.get("feedbackId") or "").strip()
        if feedback_id and feedback_id in seen:
            warnings.append(f"duplicate feedbackId {feedback_id}")
            continue
        if feedback_id:
            seen.add(feedback_id)
        item_errors = validate_a2a_feedback(item)
        if item_errors:
            errors.extend(f"{feedback_id or index}: {msg}" for msg in item_errors)
            continue
        steps.append(
            {
                "feedbackId": feedback_id,
                "at": item.get("createdAt") or "",
                "from": item["fromAgent"],
                "to": item["toAgent"],
                "kind": item["kind"],
                "problem": item["summary"],
                "action": item["requestedAction"],
                "evidence": list(item.get("evidence") or []),
                "status": str((item.get("payload") or {}).get("status") or ""),
            }
        )

    loops = sum(1 for step in steps if step["kind"] in A2A_REPAIR_KINDS)
    escalated = any(step["kind"] in A2A_ESCALATION_KINDS for step in steps)
    terminal = str(run_terminal or "").strip().upper()
    if terminal in {"SUCCEEDED", "SUCCESS", "COMPLETED"}:
        terminal_reason = "RESOLVED"
    elif terminal in {"FAILED", "CANCELLED"}:
        terminal_reason = "ESCALATED" if escalated else "FAILED"
    else:
        last_status = next(
            (
                step["status"]
                for step in reversed(steps)
                if step["status"] in A2A_TERMINAL_STATUSES
            ),
            "",
        )
        terminal_reason = last_status or ("ESCALATED" if escalated else "IN_PROGRESS")

    return {
        "stepCount": len(steps),
        "loops": loops,
        "terminalReason": terminal_reason,
        "errors": errors,
        "warnings": warnings,
        "steps": steps,
    }


__all__ = [
    "A2A_ALLOWED",
    "A2A_ESCALATION_KINDS",
    "A2A_REPAIR_KINDS",
    "A2A_TERMINAL_STATUSES",
    "validate_a2a_feedback",
    "validate_a2a_sequence",
]
