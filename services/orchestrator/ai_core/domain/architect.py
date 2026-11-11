"""Architect 领域契约。

与局部 UI Run 完全独立：请求 / 事件 / 快照都归属 ``architect.*`` 命名空间。
对外 JSON 使用 camelCase，Python 内部使用 snake_case。

本模块属于纯领域层：只依赖标准库与 Pydantic。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Union

from pydantic import Field, field_validator, model_validator

from ai_core.domain.common import CamelModel

AppShape = Literal["lightweight_web", "script_automation", "full_stack_system"]

APP_SHAPE_LABELS: dict[str, str] = {
    "lightweight_web": "轻量网页工具",
    "script_automation": "脚本自动化",
    "full_stack_system": "全栈系统",
}

DATABASE_ENGINE_LABELS: dict[str, str] = {
    "mysql": "MySQL 8（生产部署）",
    "postgresql": "PostgreSQL（生产部署）",
    "sqlite": "SQLite（仅供本地测试）",
}


def normalize_database_engine(value: Any) -> str:
    """把模型 / 用户输入归一为稳定的数据库引擎枚举。

    契约里 ``tech_stack.database`` 可能写成 "MySQL 8"、"PostgreSQL 16"、
    "Postgres" 甚至带驱动名；下游 Coder 只接受 ``mysql``、``postgresql``、
    ``sqlite`` 三种值。无法识别时返回空字符串，由调用方决定默认值。
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text.startswith(("postgres", "pgsql")):
        return "postgresql"
    if text.startswith(("mysql", "mariadb")):
        return "mysql"
    if text.startswith("sqlite"):
        return "sqlite"
    return ""


class ArchitectSource(CamelModel):
    """当前前端代码来源。

    有 ``sandboxId`` 时由服务端从沙盒读取最新源码；``files`` 只在没有
    沙盒时作为回退，避免前端持有过期模板。
    """

    sandbox_id: str | None = Field(default=None, max_length=64)
    base_run_id: str | None = Field(default=None, max_length=64)
    base_commit_hash: str | None = Field(default=None, max_length=64)
    files: dict[str, str] = Field(default_factory=dict)


class ArchitectRunOptions(CamelModel):
    """Architect 执行选项。

    ``confidence_threshold`` 与 ``max_clarification_rounds`` 可由请求覆盖，
    但服务端会做硬上限，避免前端绕过安全边界。
    """

    llm_provider: Literal["auto", "mock", "deepseek"] = "auto"
    confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    max_clarification_rounds: int = Field(default=10, ge=1, le=10)
    rag_provider: Literal["hybrid", "markdown", "chroma"] = "hybrid"
    rag_top_k: int = Field(default=5, ge=1, le=10)
    max_source_bytes: int = Field(default=200_000, ge=1_000, le=2_000_000)
    contract_repair_limit: int = Field(default=1, ge=0, le=2)


class ArchitectRunRequest(CamelModel):
    """启动一次全局架构澄清。"""

    run_id: str | None = Field(default=None, max_length=64)
    tenant_id: str | None = None
    project_id: str = Field(min_length=1, max_length=64)
    project_name: str = Field(default="未命名项目", max_length=128)
    source: ArchitectSource = Field(default_factory=ArchitectSource)
    ui_history: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    # 全栈澄清的上下文增强：
    #  - projectSummary：最近一次保存快照后生成的项目全局摘要；
    #  - latestChanges：本次快照（或最近一轮 UI Run）的改动说明。
    # Architect 必须基于摘要理解既定技术选型，不得重复询问已确认内容。
    project_summary: str = Field(default="", max_length=12000)
    latest_changes: str = Field(default="", max_length=4000)
    options: ArchitectRunOptions = Field(default_factory=ArchitectRunOptions)


class ArchitectQuestion(CamelModel):
    """一个澄清问题。"""

    question_id: str
    slot: str
    type: Literal[
        "single_choice",
        "multi_choice",
        "free_text",
        "entity_fields",
        "schema_fields",
    ] = "free_text"
    prompt: str = Field(min_length=1, max_length=400)
    required: bool = True
    options: list[dict[str, str]] = Field(default_factory=list)
    default_value: Any | None = None


class ArchitectAnalysis(CamelModel):
    """LLM 对当前需求的结构化分析。

    ``business_flows`` 对模型输出保持宽容：DeepSeek 等模型有时返回字符串
    数组（``["用户登录", "创建订单"]``），有时返回对象数组。字段层面的
    ``field_validator`` 会把两种形态统一成对象数组，避免因为一个字段的
    类型漂移就让整次 Architect 分析 500。
    """

    app_shape: AppShape | None = None
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    missing_slots: list[str] = Field(default_factory=list)
    slots: dict[str, Any] = Field(default_factory=dict)
    questions: list[ArchitectQuestion] = Field(default_factory=list)
    tech_stack: dict[str, Any] = Field(default_factory=dict)
    business_flows: Union[
        dict[str, Any],
        str,
        list[Union[dict[str, Any], str]],
    ] = Field(default_factory=list)
    entities: Union[
        dict[str, Any],
        str,
        list[Union[dict[str, Any], str]],
    ] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    reasoning: str = ""

    @field_validator("business_flows", mode="before")
    @classmethod
    def _normalize_business_flows(cls, value: Any) -> list[dict[str, Any]]:
        """把 str / dict / 混合数组统一为 ``[{"step": ...}]``。

        模型常见输出：
          - ``"用户登录 -> 下单 -> 支付"``（整段字符串）
          - ``["用户登录", "创建订单"]``（字符串数组）
          - ``[{"step": "登录", "actor": "用户"}]``（对象数组）

        统一成对象数组后，前端与契约生成不必再兼容多种形态。
        """
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        elif isinstance(value, dict):
            value = [value]
        elif not isinstance(value, list):
            value = [str(value)]

        normalized: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    normalized.append({"step": text})
                continue
            if isinstance(item, dict):
                normalized.append(item)
                continue
            text = str(item).strip()
            if text:
                normalized.append({"step": text})
        return normalized

    @field_validator("entities", mode="before")
    @classmethod
    def _normalize_entities(cls, value: Any) -> list[dict[str, Any]]:
        """实体列表同样容忍字符串，统一包装为 ``{"name": ...}``。"""
        if value is None:
            return []
        if isinstance(value, (str, dict)):
            value = [value]
        elif not isinstance(value, list):
            value = [str(value)]

        normalized: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    normalized.append({"name": text})
                continue
            if isinstance(item, dict):
                normalized.append(item)
                continue
            text = str(item).strip()
            if text:
                normalized.append({"name": text})
        return normalized


class ArchitectClarification(CamelModel):
    """一次等待回答的全局澄清。"""

    clarification_id: str
    run_id: str
    round: int = Field(ge=1)
    max_rounds: int = Field(ge=1)
    status: Literal["PENDING", "ANSWERED", "EXPIRED"] = "PENDING"
    app_shape: AppShape | None = None
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    missing_slots: list[str] = Field(default_factory=list)
    reason: str = "missing_required_slots"
    questions: list[ArchitectQuestion] = Field(default_factory=list)
    answered_at: datetime | None = None


class ArchitectAnswer(CamelModel):
    question_id: str
    value: Any = None
    text: str | None = Field(default=None, max_length=4000)


class ArchitectAnswersRequest(CamelModel):
    answers: list[ArchitectAnswer] = Field(default_factory=list, max_length=10)
    comment: str | None = Field(default=None, max_length=2000)
    # 只能由用户在前端显式点击“一键采用建议并跳过剩余提问”时置 true。
    # 服务端不接受模型输出该字段，LLM 无权自行跳过必填问题。
    adopt_suggestions: bool = False

    @model_validator(mode="after")
    def _has_answer(self) -> "ArchitectAnswersRequest":
        if not self.answers and not self.comment and not self.adopt_suggestions:
            raise ValueError("至少回答一个问题、提供补充说明或采用建议")
        return self


class ArchitectApiRoute(CamelModel):
    method: str
    path: str
    request: str = ""
    response: str = ""
    description: str = ""
    auth_required: bool = True


class ArchitectComponentInteraction(CamelModel):
    component: str
    event: str
    api_route: str = ""
    logic: str = ""


class ArchitectTableRelation(CamelModel):
    """主从表 / 关联表定义。

    契约不允许物理外键，但必须显式声明业务关联，否则 BackendCoder 只能
    生成单表 CRUD。``parent_table`` / ``child_table`` 指向 ``databaseDdl``
    中真实存在的表，``foreign_key`` 必须是子表真实存在的列：

        Users 1 ── N Resumes
        {"parentTable": "users", "childTable": "resumes",
         "foreignKey": "user_id", "type": "one_to_many"}
    """

    parent_table: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    child_table: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    foreign_key: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    type: Literal["one_to_many", "many_to_one", "one_to_one"] = "one_to_many"
    # 父表被引用的键，默认主键 id。
    parent_key: str = Field(default="id", max_length=64)
    # Sequelize include 的别名；为空时由生成器按表名推导。
    alias: str = Field(default="", max_length=64)
    on_delete: Literal["CASCADE", "SET NULL", "RESTRICT", "NO ACTION"] = "CASCADE"

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_relation_type(cls, value: Any) -> str:
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "has_many": "one_to_many",
            "hasmany": "one_to_many",
            "1_n": "one_to_many",
            "1_to_n": "one_to_many",
            "belongs_to": "many_to_one",
            "belongsto": "many_to_one",
            "n_1": "many_to_one",
            "has_one": "one_to_one",
            "hasone": "one_to_one",
            "1_1": "one_to_one",
        }
        return aliases.get(text, text or "one_to_many")

    @field_validator("on_delete", mode="before")
    @classmethod
    def _normalize_on_delete(cls, value: Any) -> str:
        text = str(value or "").strip().upper().replace("_", " ")
        return text or "CASCADE"


class ArchitectImplementationPhase(CamelModel):
    phase: str
    title: str
    tasks: list[str] = Field(default_factory=list)


class ArchitectCitation(CamelModel):
    doc_id: str
    title: str
    section: str
    path: str = ""
    score: float = 0.0


class ArchitectContract(CamelModel):
    """全栈 / 轻量 / 脚本三级实施契约。"""

    contract_id: str
    run_id: str
    app_shape: AppShape
    status: Literal[
        "AWAITING_CONFIRMATION",
        "CONFIRMED",
        "REVISION_REQUESTED",
        "CANCELLED",
    ] = "AWAITING_CONFIRMATION"
    confidence_score: float = Field(ge=0.0, le=1.0)
    threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    source_hash: str = ""
    contract_markdown: str
    tech_stack: dict[str, Any] = Field(default_factory=dict)
    # 数据库引擎是生产部署的关键决策：sqlite 只用于本地测试，
    # mysql / postgresql 用于生产；空字符串表示非全栈契约。
    database_engine: str = Field(default="", max_length=32)
    api_routes: list[ArchitectApiRoute] = Field(default_factory=list)
    database_ddl: str = ""
    # 主从表 / 关联表：DDL 只描述结构，relations 描述业务关联。
    # 没有声明时 DBACoder 会从 <parent>_id 列确定性推导（不猜测业务含义）。
    relations: list[ArchitectTableRelation] = Field(default_factory=list)
    component_interactions: list[ArchitectComponentInteraction] = Field(
        default_factory=list
    )
    # 对话前置模式要求契约显式给出前端可执行结构，FrontendCoder 不再
    # 从 Markdown 里猜测页面。三个字段对所有 Web 形态都有效：
    #  - frontendRoutes: [{path, name, description}]
    #  - pageLayout:     [{region, title, description}]
    #  - components:     [{name, role, section, description}]
    frontend_routes: list[dict[str, Any]] = Field(default_factory=list)
    page_layout: list[dict[str, Any]] = Field(default_factory=list)
    components: list[dict[str, Any]] = Field(default_factory=list)
    ui_logic: list[dict[str, Any]] = Field(default_factory=list)
    frontend_data_model: list[dict[str, Any]] = Field(default_factory=list)
    script_spec: dict[str, Any] = Field(default_factory=dict)
    implementation_path: list[ArchitectImplementationPhase] = Field(
        default_factory=list
    )
    assumptions: list[str] = Field(default_factory=list)
    # AI-Native 能力：为 true 时后端生成真实 OpenAI SDK 调用，
    # 前端生成对话 / 上传 / 分析界面；默认 false，旧契约完全兼容。
    include_ai_capability: bool = False
    # 抽象需求的空白骨架标记：FrontendCoder 只渲染 Header + Sidebar +
    # Main 与占位组件，不生成任何猜测出来的业务 UI。
    skeleton_mode: bool = False
    citations: list[ArchitectCitation] = Field(default_factory=list)
    created_at: datetime | None = None
    decided_at: datetime | None = None

    @field_validator("database_engine", mode="before")
    @classmethod
    def _normalize_engine(cls, value: Any) -> str:
        return normalize_database_engine(value)

    @model_validator(mode="after")
    def _derive_engine_from_tech_stack(self) -> "ArchitectContract":
        """兼容旧契约：只写了 tech_stack.database 时自动补齐引擎。

        历史契约没有 ``databaseEngine`` 字段；只要技术栈里写明了
        MySQL / PostgreSQL / SQLite，就把它提升为一级决策字段，避免
        Coder 在“旧契约 + 生产数据库”场景下回退到错误默认值。
        """
        if self.app_shape != "full_stack_system":
            return self
        if self.database_engine:
            return self
        raw = self.tech_stack.get("databaseEngine") or self.tech_stack.get("database")
        self.database_engine = normalize_database_engine(raw) or "mysql"
        return self


class ArchitectDecisionRequest(CamelModel):
    contract_id: str = Field(min_length=1, max_length=128)
    decision: Literal["CONFIRM", "REVISE", "CANCEL"]
    comment: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _revision_needs_comment(self) -> "ArchitectDecisionRequest":
        if self.decision == "REVISE" and not (self.comment or "").strip():
            raise ValueError("REVISE 必须提供 comment")
        return self


class ArchitectCancelRequest(CamelModel):
    reason: str | None = Field(default=None, max_length=500)


class ArchitectHelperMessage(CamelModel):
    """影子 LLM 的对话记录（只读问答，不参与主状态机）。"""

    role: Literal["user", "assistant"] = "user"
    content: str = Field(min_length=1, max_length=4000)


class ArchitectHelperRequest(CamelModel):
    """向 Helper Agent 提一个问题。

    严格限制历史长度，避免把整段架构对话无限回灌；Helper 只能读取
    当前 Run 的快照与 RAG 知识库，不接受任何写入 / 推进指令。
    """

    question: str = Field(min_length=1, max_length=2000)
    history: list[ArchitectHelperMessage] = Field(default_factory=list, max_length=12)
    use_knowledge_base: bool = True


class ArchitectHelperResponse(CamelModel):
    run_id: str
    answer: str
    citations: list[ArchitectCitation] = Field(default_factory=list)
    read_only: bool = True
    model: str = ""


class ArchitectClarificationStatus(CamelModel):
    clarification_id: str
    status: Literal["ANSWERED"]
    answered_at: datetime


class ArchitectRunStatus(CamelModel):
    run_id: str
    status: str


class ArchitectAnswerResponse(CamelModel):
    clarification: ArchitectClarificationStatus
    architect_run: ArchitectRunStatus


class ArchitectContractStatus(CamelModel):
    contract_id: str
    status: str
    decided_at: datetime | None = None


class ArchitectDecisionResponse(CamelModel):
    contract: ArchitectContractStatus
    architect_run: ArchitectRunStatus


class ArchitectCancelResponse(CamelModel):
    run_id: str
    status: str


class ArchitectSnapshotResponse(CamelModel):
    run_id: str
    project_id: str
    project_name: str = ""
    status: str
    app_shape: AppShape | None = None
    round: int = 0
    max_rounds: int = 10
    confidence_score: float = 0.0
    threshold: float = 0.85
    source_hash: str = ""
    last_seq: int = 0
    clarification: ArchitectClarification | None = None
    contract: ArchitectContract | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
