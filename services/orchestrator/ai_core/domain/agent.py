"""Agent / Run 领域契约。

字段命名与 docs/design/03.API接口设计.md 的 AnnotationEnvelope 保持一致：
对外 JSON 使用 camelCase，Python 内部使用 snake_case。

本模块属于纯领域层：只依赖标准库与 Pydantic，不感知 LangGraph、FastAPI、
沙盒或模型 SDK。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_serializer, model_validator

from ai_core.domain.actions import CREATE_NEW, MODIFY_EXISTING, normalize_action
from ai_core.domain.common import CamelModel, iso_datetime


# ------------------------------------------------------------
# AnnotationEnvelope（M1 只使用 source 模式）
# ------------------------------------------------------------
class AnnotationSource(CamelModel):
    file: str | None = None
    line: int | None = None
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None
    component: str | None = None
    source_kind: str | None = None


class AnnotationSnippet(CamelModel):
    start_line: int | None = None
    end_line: int | None = None
    content: str | None = None
    hash: str | None = None


class AnnotationTarget(CamelModel):
    mode: Literal["source", "visual"] = "source"
    source: AnnotationSource | None = None
    selector: str | None = None
    snippet: AnnotationSnippet | None = None


class AnnotationIntentUI(CamelModel):
    summary: str | None = None
    acceptance: list[str] = Field(default_factory=list)
    element_role: str | None = None
    new_route: str | None = None


class AnnotationIntent(CamelModel):
    kind: Literal["UI_ONLY", "UI_AND_BUSINESS", "FULLSTACK"] = "UI_ONLY"
    ui: AnnotationIntentUI | None = None
    business: dict[str, Any] | None = None
    full_stack: dict[str, Any] | None = None


class AnnotationChatMessage(CamelModel):
    """同一个标注（thread）内的一轮对话。

    前端负责展示会话，后端负责把同一 threadId 的历史与上一次修改记录
    一起送入 Planner / Coder，实现“不，我要蓝色”这类增量修改。

    ``commitHash`` / ``turnCount`` 只出现在成功 Run 的助手消息上：它们是
    前端“回滚到此步”按钮定位 Git checkpoint 与 thread checkpoint 的坐标。
    """

    id: str | None = Field(default=None, max_length=128)
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1, max_length=8000)
    run_id: str | None = Field(default=None, max_length=64)
    commit_hash: str | None = Field(default=None, max_length=64)
    turn_count: int | None = Field(default=None, ge=1)
    created_at: datetime | None = None

    @field_serializer("created_at")
    def _serialize_created_at(self, value: datetime | None) -> str | None:
        """即使调用方使用 mode=python 的 model_dump，也输出 ISO 字符串。"""
        return iso_datetime(value)


class AnnotationEnvelope(CamelModel):
    """画布标注信封。M1 接受 UI_ONLY / UI_AND_BUSINESS，拒绝 FULLSTACK。"""

    version: int = 1
    annotation_id: str = Field(min_length=1, max_length=128)
    type: Literal["note", "draw", "drag", "visual-draw"] = "note"
    # 操作类型：
    #   - CREATE_NEW：新增组件。Agent 只负责生成组件主体，不得强制写死
    #     绝对坐标；前端会把它作为可拖拽悬浮层，用户松手后再发送坐标 Patch。
    #   - MODIFY_EXISTING：修改、移动或重新定位已有元素。
    #   - MOVE：直接拖拽产生的确定性位移。payload.move 携带累计 dx/dy
    #     与选择器，UI Graph 不再调用规划模型，直接走 move_component Tool。
    #   - BATCH_MOVE：用户连续拖拽多个组件后统一提交。payload.moves 是
    #     [{source, file, selector, dx, dy}] 数组，Coder 一次生成合并补丁，
    #     避免多个 Run 并发写同一文件。
    # 旧客户端仍发送 MODIFY / ADD，领域层通过 actions.normalize_action()
    # 统一语义，避免历史数据失效。
    action: Literal[
        "MODIFY",
        "ADD",
        "CREATE_NEW",
        "MODIFY_EXISTING",
        "MOVE",
        "BATCH_MOVE",
    ] = "MODIFY"
    world_target: dict[str, Any] = Field(default_factory=dict)
    # ADD 专用：圈选区域相对“最近 data-source 父容器”的位置。
    # 契约同时包含两套坐标：
    #   - x/y/width/height：归一化 0~1，兼容旧客户端，适合粗粒度布局判断；
    #   - relativeX/relativeY/widthPx/heightPx/parentWidth/parentHeight：
    #     CSS 像素，模型据此使用 absolute / margin / flex / grid 精确落位。
    # 像素字段可能为 None（旧客户端或无法测量父容器时），模型必须降级到
    # 归一化逻辑。
    relative_target: dict[str, Any] = Field(default_factory=dict)
    target: AnnotationTarget | None = None
    intent: AnnotationIntent | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    screenshot: dict[str, Any] | None = None
    created_at: datetime | None = None
    # 每个标注独立的多轮会话：同一个圈后续发送指令时复用 thread_id。
    thread_id: str | None = Field(default=None, max_length=64)
    chat_history: list[AnnotationChatMessage] = Field(
        default_factory=list, max_length=40
    )

    @field_serializer("created_at")
    def _serialize_created_at(self, value: datetime | None) -> str | None:
        """AnnotationEnvelope 会整体进入 Planner / Coder Prompt 的 JSON。"""
        return iso_datetime(value)

    @property
    def intent_action(self) -> str:
        """新版动作语义：ADD → CREATE_NEW，其余 → MODIFY_EXISTING。"""
        return normalize_action(self.action)

    @property
    def is_create_new(self) -> bool:
        return self.intent_action == CREATE_NEW

    @property
    def is_modify_existing(self) -> bool:
        return self.intent_action == MODIFY_EXISTING


# ------------------------------------------------------------
# Run 执行
# ------------------------------------------------------------
class RunOptions(CamelModel):
    """执行选项。

    mock_scenario 仅对 Mock LLM 生效：
      - fail_first : 第一次生成故意写错，用于验证自动修复闭环；
      - always_fail: 每次生成都写错，用于验证 3 次硬上限；
      - add_button : 忽略具体的颜色 / 文案解析，直接插入一个新增按钮，
                     用于无密钥环境验证 ADD 组件的完整 E2E；
      - none       : 直接生成正确代码。

    clarification_mode：
      - auto  : 置信度低于阈值或缺少关键槽位时中断（默认）；
      - always: 即使高置信度也先确认范围（调试用）；
      - off   : 禁用 HITL，按当前最佳计划执行（降级 / 回归用）。

    require_manual_approval：
      - true : 构建通过后暂停，等待用户逐文件查看 Diff 后批准/拒绝；
      - false: 构建通过后直接提交 checkpoint 并返回 SUCCEEDED（默认）。

    approval_mode：
      - required: 旧协议，等同于 requireManualApproval=true；
      - off     : 旧协议默认值，等同于 requireManualApproval=false。

    两者同时出现时 ``require_manual_approval`` 优先；新客户端只发送
    ``requireManualApproval``。
    """

    llm_provider: Literal["auto", "mock", "deepseek"] = "auto"
    mock_scenario: Literal["none", "fail_first", "always_fail", "add_button"] = (
        "fail_first"
    )
    max_retries: int = Field(default=3, ge=0, le=3)
    keep_sandbox: bool = True
    clarification_mode: Literal["auto", "always", "off"] = "auto"
    max_clarification_rounds: int = Field(default=3, ge=1, le=3)
    # None 时使用服务端配置 AGENT_CLARIFICATION_THRESHOLD。
    confidence_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    # 产品前端开关：代码生成并验证通过后是否暂停等待人工审批。
    # None 表示未显式传值，此时回退到旧字段 approval_mode。
    require_manual_approval: bool | None = None
    # “转后台运行”：大型重构不占用前端阻塞浮层，使用更长的 Coder 超时；
    # 前端可继续圈选 / 浏览，完成后通过 SSE 终态 + Toast 通知。
    background: bool = False
    # 旧字段：required / off。保留以兼容既有 E2E 与外部调用方。
    approval_mode: Literal["required", "off"] = "off"

    @property
    def manual_approval_required(self) -> bool:
        """解析出本次 Run 是否需要在构建通过后暂停。

        新字段优先；旧字段仅在调用方没有发送 ``requireManualApproval``
        时生效，保证前后端灰度期间两套协议可以并存。
        """
        if self.require_manual_approval is not None:
            return self.require_manual_approval
        return self.approval_mode == "required"


class ExecuteRunRequest(CamelModel):
    """一次 Run 的输入。

    M1 升级后支持画布一次提交最多 3 个标注：
      - 新客户端发送 ``annotations: AnnotationEnvelope[]``；
      - 旧客户端仍可发送单个 ``annotation``，服务端自动归一化为数组，
        避免既有脚本 / 调用方失效。
    """

    run_id: str | None = Field(default=None, max_length=64)
    tenant_id: str | None = None
    project_id: str = Field(min_length=1, max_length=64)
    # 新协议：批量标注，顺序即画布上的标注顺序。
    annotations: list[AnnotationEnvelope] = Field(default_factory=list, max_length=3)
    # 兼容旧协议：单个标注；与 annotations 同时出现时以 annotations 为准。
    annotation: AnnotationEnvelope | None = None
    # 增量修改：复用该项目当前的沙盒，避免每一轮对话都创建新容器。
    sandbox_id: str | None = Field(default=None, max_length=64)
    # 沙盒初始文件；为空时使用 sandbox_template 自带页面。
    files: dict[str, str] = Field(default_factory=dict)
    options: RunOptions = Field(default_factory=RunOptions)

    @model_validator(mode="after")
    def _normalize_annotations(self) -> "ExecuteRunRequest":
        """把单标注旧请求归一化为数组，并执行 1~3 的数量约束。"""
        if self.annotations:
            items = list(self.annotations)
        elif self.annotation is not None:
            items = [self.annotation]
        else:
            raise ValueError("annotations 至少包含 1 个标注")

        if len(items) > 3:
            raise ValueError("一次最多提交 3 个标注")
        self.annotations = items
        return self

    @property
    def effective_annotations(self) -> list[AnnotationEnvelope]:
        """统一读取入口；调用方不要直接判断 annotation / annotations。"""
        return self.annotations


class VerifyResultModel(CamelModel):
    success: bool
    error_logs: str = ""
    exit_code: int = 0
    duration_ms: int = 0


class RunCommitRecord(CamelModel):
    """一次成功 Run 对应的沙盒 Git checkpoint。

    批量提交时一个 Run 只产生一个全局 commit，但每个参与标注都有自己的
    ``turnCount``。前端逐条助手消息挂载对应 thread 的 turn / commit。
    """

    thread_id: str
    turn_count: int = Field(ge=1)
    commit_hash: str
    run_id: str


# ------------------------------------------------------------
# HITL 澄清
# ------------------------------------------------------------
class ClarificationOption(CamelModel):
    value: str
    label: str
    description: str = ""


class ClarificationDecisionOption(CamelModel):
    """核心决策的一键选项。

    与问题内的 ``options`` 不同，本结构直接包含 ``questionId``，前端可在
    其余必填问题都有默认值时立即提交，实现真正的“一键选择”。
    """

    option_id: str
    label: str
    description: str = ""
    recommended: bool = False
    question_id: str = ""
    value: Any = None


class ClarificationQuestion(CamelModel):
    """问题字段与 03.API接口设计.md §3.7.1 对齐。"""

    question_id: str
    type: Literal[
        "single_choice",
        "multi_choice",
        "free_text",
        "entity_fields",
        "schema_fields",
    ]
    prompt: str
    required: bool = True
    options: list[ClarificationOption] = Field(default_factory=list)
    default_value: Any | None = None
    schema_payload: dict[str, Any] | None = Field(default=None, alias="schema")


class Clarification(CamelModel):
    """一次待回答的澄清轮次。"""

    clarification_id: str
    run_id: str | None = None
    annotation_id: str | None = None
    thread_id: str | None = None
    round: int = Field(default=1, ge=1)
    max_rounds: int = Field(default=3, ge=1, le=3)
    status: Literal["PENDING", "ANSWERED", "EXPIRED"] = "PENDING"
    reason: str = "ambiguous_intent"
    # 分级决策：只有 CRITICAL 可进入本模型；MAJOR / MINOR 不会触发
    # interrupt。旧值仅用于兼容历史检查点与已持久化事件。
    decision_impact: Literal[
        "CRITICAL",
        "MAJOR",
        "MINOR",
        "CORE_BUSINESS",
        "IMPLEMENTATION_DETAIL",
    ] = "CRITICAL"
    decision_summary: str = ""
    questions: list[ClarificationQuestion] = Field(default_factory=list)
    # 首轮一键决策按钮；为空时前端回退渲染 questions[].options。
    clarification_options: list[ClarificationDecisionOption] = Field(
        default_factory=list
    )
    expires_at: datetime | None = None
    answered_at: datetime | None = None


class ClarificationAnswer(CamelModel):
    question_id: str
    value: Any = None
    text: str | None = Field(default=None, max_length=1000)


class ClarificationAnswersRequest(CamelModel):
    answers: list[ClarificationAnswer] = Field(default_factory=list, max_length=10)
    use_defaults: bool = False
    comment: str | None = Field(default=None, max_length=1000)
    expected_version: int | None = None
    # 内部兼容：POST /runs/{runId}/clarifications 没有 clarificationId
    # 路径参数时，可放在 body；公开 BFF 仍以路径参数为准。
    clarification_id: str | None = Field(default=None, max_length=128)


class ClarificationStatus(CamelModel):
    clarification_id: str
    status: Literal["ANSWERED"]
    answered_at: datetime


class ClarificationRunStatus(CamelModel):
    run_id: str
    status: str


class ClarificationAnswerResponse(CamelModel):
    clarification: ClarificationStatus
    run: ClarificationRunStatus


class ClarificationListResponse(CamelModel):
    run_id: str
    clarifications: list[Clarification] = Field(default_factory=list)


# ------------------------------------------------------------
# HITL 审批（Diff Approve / Reject）
# ------------------------------------------------------------
class ApprovalFileDiff(CamelModel):
    """单个文件的审批证据；diff 过长时由后端截断并标记 ``truncated``。"""

    path: str
    change_type: Literal["added", "modified", "deleted"] = "modified"
    insertions: int = 0
    deletions: int = 0
    diff: str = ""
    truncated: bool = False


class ApprovalEvidence(CamelModel):
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    files: list[ApprovalFileDiff] = Field(default_factory=list)
    build: dict[str, Any] = Field(default_factory=dict)


class Approval(CamelModel):
    """一次待决策的 Diff 审批。

    构建通过后 Graph 在 ``approval_gate`` 调用 ``interrupt()``，payload 即
    本模型；用户决策后通过 ``Command(resume=...)`` 恢复同一状态机，
    只有批准才会写入 Git checkpoint。
    """

    approval_id: str
    run_id: str | None = None
    status: Literal[
        "PENDING",
        "APPROVED",
        "REJECTED",
        "CANCELLED",
        "EXPIRED",
    ] = "PENDING"
    summary: str = ""
    risk_level: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"
    options: list[str] = Field(default_factory=lambda: ["approve", "reject"])
    evidence: ApprovalEvidence | None = None
    sandbox_id: str | None = None
    preview_url: str | None = None
    expires_at: datetime | None = None
    decided_at: datetime | None = None
    comment: str | None = None


class ApprovalDecisionRequest(CamelModel):
    """审批决策请求；兼容 approve/APPROVED 等写法。"""

    decision: str = Field(min_length=1, max_length=32)
    comment: str | None = Field(default=None, max_length=1000)
    expected_version: int | None = None
    approval_id: str | None = Field(default=None, max_length=128)


class ApprovalStatus(CamelModel):
    approval_id: str
    status: Literal["APPROVED", "REJECTED", "CANCELLED", "EXPIRED"]
    decided_at: datetime


class ApprovalDecisionResponse(CamelModel):
    approval: ApprovalStatus
    run: ClarificationRunStatus


class ApprovalListResponse(CamelModel):
    run_id: str
    approvals: list[Approval] = Field(default_factory=list)


class CancelRunRequest(CamelModel):
    reason: str | None = Field(default=None, max_length=200)


class CancelRunResponse(CamelModel):
    run_id: str
    status: str
    cancel_requested_at: datetime


class RunSnapshotResponse(CamelModel):
    """GET /runs/{runId} 的轻量快照；完整结果仍以 SSE 终态 payload 为准。"""

    run_id: str
    status: str
    sandbox_id: str = ""
    preview_url: str = ""
    clarification: Clarification | None = None
    approval: Approval | None = None
    cancel_requested: bool = False
    assistant_message: str = ""
    error: str | None = None
    last_seq: int = 0


class ExecuteRunResponse(CamelModel):
    run_id: str
    status: Literal[
        "AWAITING_APPROVAL",
        "AWAITING_CLARIFICATION",
        "SUCCEEDED",
        "REJECTED",
        "CANCELLED",
        "EXPIRED",
        "FAILED",
    ]
    sandbox_id: str
    preview_url: str
    reused_sandbox: bool = False
    plan: str = ""
    generated_patch: str = ""
    generated_files: dict[str, str] = Field(default_factory=dict)
    verify_result: VerifyResultModel | None = None
    retry_count: int = 0
    messages: list[dict[str, Any]] = Field(default_factory=list)
    # 前端直接展示在迷你 Chatbox 中的助手回复，与服务端 thread 记录同文案。
    assistant_message: str = ""
    # Git checkpoint：成功 Run 的全局提交 + 每个 thread 的轮次坐标。
    commit_hash: str | None = None
    turn_count: int = 0
    baseline_commit: str | None = None
    commits: list[RunCommitRecord] = Field(default_factory=list)
    # HITL：仅 AWAITING_CLARIFICATION 时 non-null。
    clarification: Clarification | None = None
    # HITL：仅 AWAITING_APPROVAL（等待审批）时 non-null。
    approval: Approval | None = None
    # 本次 Run 的 Token 用量与费用（含所有模型调用）。
    token_usage: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    # 结构化错误码与可恢复标记：CODER_TIMEOUT_RECOVERABLE 会展示
    # “转后台运行”，而不是普通失败。
    error_code: str | None = None
    background_available: bool = False


class RollbackRunRequest(CamelModel):
    """把某个标注回滚到指定成功轮次。

    ``commitHash`` 是全局代码快照；``threadId`` + ``turnCount`` / ``runId``
    用于恢复 ThreadMemoryStore 中该圈对应的对话与修改记录 checkpoint。
    """

    sandbox_id: str = Field(min_length=1, max_length=64)
    # 租户 / 项目作用域：ThreadMemoryStore 的物理 key 由二者共同决定。
    # 缺省 project_id 时无法定位项目内记忆，只执行 Git 回滚，绝不会退化为
    # “跨项目读取同名 threadId”。
    tenant_id: str | None = None
    project_id: str | None = Field(default=None, max_length=64)
    thread_id: str = Field(min_length=1, max_length=64)
    commit_hash: str = Field(pattern=r"^[0-9a-f]{40}$")
    run_id: str | None = Field(default=None, max_length=64)
    turn_count: int | None = Field(default=None, ge=1)


class RollbackRunResponse(CamelModel):
    """回滚结果。

    ``previewUrl`` 带 cache-busting 参数，前端替换 result.previewUrl 后
    iframe 会强制重新加载，避免 reset 后仍显示旧 DOM。
    """

    sandbox_id: str
    thread_id: str
    commit_hash: str
    turn_count: int | None = None
    run_id: str | None = None
    checkpoint_restored: bool = False
    preview_url: str
    preview_revision: str
