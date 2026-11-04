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
    # 主目标文件（第一个标注），用于消息展示 / 兼容旧状态读取。
    target_file: str
    # 本次 Run 全部标注涉及的文件（去重，最多 3 个）。
    target_files: list[str]

    # ---- 输入上下文 ----
    # 包含 AnnotationEnvelope[]、逐标注意图、目标文件 / 片段等。
    annotation_context: dict[str, Any]
    # 对话 / 节点执行轨迹：[{role, node, content}]
    messages: Annotated[list[dict[str, Any]], operator.add]
    # 主文件内容，兼容旧逻辑；真正读取请使用 current_files。
    current_code: str
    # 所有目标文件的最新完整内容：{path: source}
    current_files: dict[str, str]

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
