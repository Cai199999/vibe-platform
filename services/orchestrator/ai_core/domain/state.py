"""Agent / Architect 运行状态领域模型。

这里的 TypedDict 是应用层 LangGraph 节点之间共享的“会话状态契约”。
它们只描述字段与取值范围，不导入 LangGraph；具体合并策略（例如
messages 使用 ``operator.add``）属于状态结构本身，因此仍保留在领域层。
"""

from __future__ import annotations

import operator
from enum import Enum
from typing import Annotated, Any, Literal, TypedDict


class DecisionImpact(str, Enum):
    """决策影响面分级。

    Agent 只能对 ``CRITICAL`` 触发 HITL 中断：
      - CRITICAL：数据库表结构、API 契约、系统架构、鉴权/支付等；
      - MAJOR：跨模块交互、状态模型等，默认自主执行；
      - MINOR：样式、文案、变量命名，必须静默生成。
    """

    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"


class VerifyState(TypedDict, total=False):
    """沙盒校验结果。"""

    success: bool
    error_logs: str
    exit_code: int
    duration_ms: int
    # 精准截断后的错误上下文（Error Message + 报错行附近代码），
    # 只把它注入 Planner / Coder，完整 error_logs 仅保留给 UI 与审计。
    error_context: str
    # 归一化错误指纹；同错连续出现时用于直接终止重试。
    error_fingerprint: str


def _merge_token_usage(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> dict[str, Any]:
    """LangGraph reducer：把每个节点的 token 用量累加到整次 Run。

    节点只返回“本节点增量”，Checkpointer 负责持久化累计值；澄清 / 审批
    恢复后仍能看到整次 Run 的历史成本，而不是最后一段节点的成本。
    """
    if not left:
        return dict(right or {})
    if not right:
        return dict(left)

    merged = dict(left)
    for field in (
        "inputTokens",
        "outputTokens",
        "cachedInputTokens",
        "totalTokens",
        "calls",
    ):
        merged[field] = int(merged.get(field, 0) or 0) + int(right.get(field, 0) or 0)
    merged["cost"] = round(
        float(merged.get("cost", 0.0) or 0.0) + float(right.get("cost", 0.0) or 0.0),
        6,
    )
    merged["currency"] = right.get("currency") or merged.get("currency") or "CNY"
    return merged


class AgentState(TypedDict, total=False):
    """单 Agent 闭环状态。

    LangGraph 按字段合并节点返回值；messages 使用 operator.add 累加，
    其余字段为“最后写入者胜出”，由节点显式覆盖。
    """

    # ---- 运行标识 ----
    run_id: str
    tenant_id: str
    project_id: str
    sandbox_id: str
    # 主工作树预览地址（审批通过 / 免审批成功后的最终预览）。
    preview_url: str
    # 隔离草稿：Applier / Verifier 只能通过 draft_id 访问影子目录，
    # 审批前主工作树保持零变化。
    draft_id: str
    draft_preview_url: str
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
    # 项目设计系统（Tailwind 主题色 / CSS 变量 / 语义类名）。只读约束，
    # 用于强制 Coder 复用 bg-primary 等既有令牌，禁止写死色值。
    design_system: dict[str, Any]
    # 本轮 Run 开始前的文件快照；审批 Diff / 拒绝回滚都以它为基线。
    # 节点不会更新该字段，始终保持“写沙盒之前”的内容。
    initial_files: dict[str, str]
    # 历史压缩后的“核心需求与约束”，由 summarize_history 节点写入。
    history_summary: str
    history_tokens_before: int
    history_tokens_after: int
    # 整次 Run 的 Token 用量与费用；由 Graph 节点按增量写入，
    # reducer 负责跨节点 / 跨澄清恢复累加。
    token_usage: Annotated[dict[str, Any], _merge_token_usage]
    # 外部检索（search_web）结果与查询词；由 research 节点写入，
    # planner 会把它追加到计划中，coder 据此引用外部事实。
    research_query: str
    research_context: str
    # 内部知识库（Hybrid RAG）检索结果：planner 写入，coder 从 plan 读取。
    # rag_context 是拼接后的可读文本；rag_citations 只保留引用元数据，
    # 避免 UI State 混入 Architect 契约字段。
    rag_provider: str
    rag_top_k: int
    rag_query: str
    rag_context: str
    rag_citations: list[dict[str, Any]]

    # ---- Phase 1：确定性目标解析 ----
    # Resolver 输出的 TargetResolution（纯 JSON）。Coder 只能依据其中的
    # anchorId / range / allowedFiles 调用 apply_ast_edit，不能自造目标。
    target_resolution: dict[str, Any]
    # resolved（全部定位成功）/ ambiguous（触发前端选择）/ skipped
    # （Provider 不支持）/ unresolved（未找到，降级旧路径）/ exhausted。
    resolver_outcome: Literal[
        "resolved", "ambiguous", "skipped", "unresolved", "exhausted"
    ]
    resolver_round: int

    # ---- 生成结果 ----
    plan: str
    generated_patch: str
    generated_files: dict[str, str]
    # 一次生成对应的补丁 ID，patch.proposed / patch.applied 共用。
    patch_id: str
    # 本轮 Coder 提交的结构化编辑调用（审计 + 前端展示），不含完整文件。
    ast_edit_calls: list[dict[str, Any]]

    # ---- HITL 澄清 ----
    # 当前计划置信度与是否需要澄清；由 Planner 写入、clarify_gate 读取。
    plan_confidence: float
    plan_needs_clarification: bool
    # 决策影响面：只有 CRITICAL 才允许打断用户；MAJOR / MINOR
    # 由 Agent 自主决定并按最佳实践生成（兼容旧值 CORE_BUSINESS 等）。
    plan_decision_impact: Literal["CRITICAL", "MAJOR", "MINOR"] | str
    # 结构化一键决策选项（A/B/C），与 questions 同时下发以兼容旧前端。
    clarification_options: list[dict[str, Any]]
    # 当前待回答的问题（暂停后由 checkpoint 保留）。
    clarification: dict[str, Any] | None
    # 已经回答的轮次与结构化答案。
    clarification_round: int
    clarification_answers: list[dict[str, Any]]
    clarification_outcome: Literal["proceed", "replan", "exhausted"]
    # 样式澄清节点结果：skipped（无需追问）/ proceed（已获得样式回答）/
    # exhausted（达到轮次上限）。与业务澄清分开，避免 Coder 误读。
    style_check_outcome: Literal["skipped", "proceed", "exhausted"]
    clarification_mode: Literal["auto", "always", "off"]
    max_clarification_rounds: int
    confidence_threshold: float
    # 澄清有效期（小时），由 Service 从 Settings 注入，供 clarify_gate
    # 计算 expiresAt。
    clarification_ttl_hours: int

    # ---- Reflection 设计偏好 ----
    # 成功路径上提取到的偏好证据；Service 仅在 SUCCEEDED 后落盘。
    reflection_preferences: list[dict[str, Any]]

    # ---- HITL Diff 审批 ----
    # 是否在构建通过后暂停等待人工审批；由 options.requireManualApproval
    # 决定（旧协议 approvalMode 作为兼容回退）。
    approval_required: bool
    # 当前待决策审批（暂停时由 checkpoint 保留）。
    approval: dict[str, Any] | None
    approval_id: str | None
    approval_decision: Literal["approve", "reject"] | None
    approval_comment: str | None
    # 审批有效期（小时），由 Service 注入。
    approval_ttl_hours: int

    # ---- 校验与修复 ----
    verify_result: VerifyState
    apply_failed: bool
    # Coder 超时 / 结构化输出耗尽：终态错误，禁止进入构建自愈循环。
    fatal_error: bool
    # 本次 Run 是否为“转后台运行”；后台模式使用更长的 Coder 超时。
    background_run: bool
    # Coder 实际使用的硬超时（秒）；Service 从 options / Settings 注入。
    coder_timeout_seconds: float
    # 首次超时后是否已用精简上下文 + FAST 模型重试。
    coder_fallback_used: bool
    # 超时是否可恢复：前端据此展示“转后台运行”，而不是只能重来。
    background_available: bool
    retry_count: int
    max_retries: int
    # 上一次自愈时的错误指纹；本次失败指纹与之相同则直接 FAILED。
    last_repair_error_fingerprint: str

    # ---- 路由 / 终态 ----
    next_action: Literal["applier", "retry", "approve", "succeed", "fail"]
    status: Literal[
        "RUNNING",
        "PLANNING",
        "CODING",
        "APPLYING",
        "VERIFYING",
        "REPAIRING",
        "AWAITING_APPROVAL",
        "AWAITING_CLARIFICATION",
        "SUCCEEDED",
        "REJECTED",
        "CANCELLED",
        "EXPIRED",
        "FAILED",
    ]
    error: str | None
    error_code: str | None


class ArchitectState(TypedDict, total=False):
    """Architect Graph 状态。"""

    run_id: str
    project_id: str
    project_name: str
    source_files: dict[str, str]
    source_hash: str
    ui_history: list[dict[str, Any]]
    # 快照保存生成的项目全局摘要 + 本次快照改动说明。
    project_summary: str
    latest_changes: str
    # 历史压缩后的核心需求与约束；超过阈值才写入。
    history_summary: str
    history_tokens_before: int
    history_tokens_after: int
    # 整次 Architect Run 的 Token 用量 / 费用；由节点增量累加。
    token_usage: Annotated[dict[str, Any], _merge_token_usage]
    threshold: float
    max_rounds: int
    round: int
    status: str
    app_shape: str
    # 风险自适应门禁：契约确认时由用户显式选择，修订 / 重新确认时保持。
    quality_tier: str
    # 用户只给出抽象目标时为 True：跳过 LLM，直接产出空白骨架契约。
    skeleton_mode: bool
    slots: dict[str, Any]
    confidence_score: float
    missing_slots: list[str]
    questions: list[dict[str, Any]]
    # 仅由用户在澄清 API 显式 adoptSuggestions=true 写入；AI 无权置位。
    adopt_suggestions: bool
    analysis: dict[str, Any]
    retrieved_chunks: list[dict[str, Any]]
    clarification: dict[str, Any]
    contract: dict[str, Any]
    contract_draft: dict[str, Any]
    contract_issues: list[str]
    contract_id: str
    contract_decision: str
    contract_feedback: str
    repair_count: int
    repair_limit: int
    contract_revision_count: int
    asked_slots: list[str]
    messages: list[dict[str, Any]]
    source_origin: str
    source_warnings: list[str]
    rag_provider: str
    rag_top_k: int
    error: str
    error_code: str


__all__ = ["AgentState", "ArchitectState", "DecisionImpact", "VerifyState"]
