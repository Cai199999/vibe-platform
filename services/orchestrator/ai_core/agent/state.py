"""LangGraph AgentState。

LangGraph 按字段合并节点返回值；messages 使用 operator.add 累加，
其余字段为“最后写入者胜出”，由节点显式覆盖。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict


class VerifyState(TypedDict, total=False):
    """沙盒校验结果。"""

    success: bool
    error_logs: str
    exit_code: int
    duration_ms: int


class AgentState(TypedDict, total=False):
    """单 Agent 闭环状态。"""

    # ---- 运行标识 ----
    run_id: str
    tenant_id: str
    project_id: str
    sandbox_id: str
    preview_url: str
    target_file: str

    # ---- 输入上下文 ----
    # 包含 AnnotationEnvelope、意图摘要、目标文件 / 片段等。
    annotation_context: dict[str, Any]
    # 对话 / 节点执行轨迹：[{role, node, content}]
    messages: Annotated[list[dict[str, Any]], operator.add]
    current_code: str

    # ---- 生成结果 ----
    plan: str
    generated_patch: str
    generated_files: dict[str, str]

    # ---- 校验与修复 ----
    verify_result: VerifyState
    apply_failed: bool
    retry_count: int
    max_retries: int

    # ---- 路由 / 终态 ----
    next_action: Literal["applier", "retry", "approve", "fail"]
    status: Literal[
        "RUNNING",
        "PLANNING",
        "CODING",
        "APPLYING",
        "VERIFYING",
        "REPAIRING",
        "AWAITING_APPROVAL",
        "FAILED",
    ]
    error: str | None
    error_code: str | None
