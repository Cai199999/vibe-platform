"""Agent / Run 的数据契约。

字段命名与 docs/design/03.API接口设计.md 的 AnnotationEnvelope 保持一致：
对外 JSON 使用 camelCase，Python 内部使用 snake_case。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class CamelModel(BaseModel):
    """同时接受 camelCase 与 snake_case，序列化默认 camelCase。"""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="allow",
    )


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


class AnnotationEnvelope(CamelModel):
    """画布标注信封。M1 接受 UI_ONLY / UI_AND_BUSINESS，拒绝 FULLSTACK。"""

    version: int = 1
    annotation_id: str = Field(min_length=1, max_length=128)
    type: Literal["note", "draw", "drag", "visual-draw"] = "note"
    # 操作类型：MODIFY 修改已有元素；ADD 在圈选区域新增组件。
    # 前端可在浮层内切换，也会根据“圈中没有命中元素”与“添加/新建”等
    # 指令自动推断。ADD 时 target 可以为空，模型需依据 worldTarget 选区
    # 与源码结构自行选择父级 / 兄弟级插入点。
    action: Literal["MODIFY", "ADD"] = "MODIFY"
    world_target: dict[str, Any] = Field(default_factory=dict)
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
    questions: list[ClarificationQuestion] = Field(default_factory=list)
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


class RunSnapshotResponse(CamelModel):
    """GET /runs/{runId} 的轻量快照；完整结果仍以 SSE 终态 payload 为准。"""

    run_id: str
    status: str
    sandbox_id: str = ""
    preview_url: str = ""
    clarification: Clarification | None = None
    assistant_message: str = ""
    error: str | None = None
    last_seq: int = 0


class ExecuteRunResponse(CamelModel):
    run_id: str
    status: Literal[
        "AWAITING_APPROVAL",
        "AWAITING_CLARIFICATION",
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
    # HITL：仅 AWAITING_CLARIFICATION 时非空。
    clarification: Clarification | None = None
    error: str | None = None


class RollbackRunRequest(CamelModel):
    """把某个标注回滚到指定成功轮次。

    ``commitHash`` 是全局代码快照；``threadId`` + ``turnCount`` / ``runId``
    用于恢复 ThreadMemoryStore 中该圈对应的对话与修改记录 checkpoint。
    """

    sandbox_id: str = Field(min_length=1, max_length=64)
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
