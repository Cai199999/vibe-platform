"""Agent / Run 的数据契约。

字段命名与 docs/design/03.API接口设计.md 的 AnnotationEnvelope 保持一致：
对外 JSON 使用 camelCase，Python 内部使用 snake_case。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class AnnotationEnvelope(CamelModel):
    """画布标注信封。M1 接受 UI_ONLY / UI_AND_BUSINESS，拒绝 FULLSTACK。"""

    version: int = 1
    annotation_id: str = Field(min_length=1, max_length=128)
    type: Literal["note", "draw", "drag", "visual-draw"] = "note"
    world_target: dict[str, Any] = Field(default_factory=dict)
    target: AnnotationTarget | None = None
    intent: AnnotationIntent | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    screenshot: dict[str, Any] | None = None
    created_at: datetime | None = None


# ------------------------------------------------------------
# Run 执行
# ------------------------------------------------------------
class RunOptions(CamelModel):
    """执行选项。

    mock_scenario 仅对 Mock LLM 生效：
      - fail_first : 第一次生成故意写错，用于验证自动修复闭环；
      - always_fail: 每次生成都写错，用于验证 3 次硬上限；
      - none       : 直接生成正确代码。
    """

    llm_provider: Literal["auto", "mock", "deepseek"] = "auto"
    mock_scenario: Literal["none", "fail_first", "always_fail"] = "fail_first"
    max_retries: int = Field(default=3, ge=0, le=3)
    keep_sandbox: bool = True


class ExecuteRunRequest(CamelModel):
    run_id: str | None = Field(default=None, max_length=64)
    tenant_id: str | None = None
    project_id: str = Field(min_length=1, max_length=64)
    annotation: AnnotationEnvelope
    # 沙盒初始文件；为空时使用 sandbox_template 自带页面。
    files: dict[str, str] = Field(default_factory=dict)
    options: RunOptions = Field(default_factory=RunOptions)


class VerifyResultModel(CamelModel):
    success: bool
    error_logs: str = ""
    exit_code: int = 0
    duration_ms: int = 0


class ExecuteRunResponse(CamelModel):
    run_id: str
    status: Literal["AWAITING_APPROVAL", "FAILED"]
    sandbox_id: str
    preview_url: str
    plan: str = ""
    generated_patch: str = ""
    generated_files: dict[str, str] = Field(default_factory=dict)
    verify_result: VerifyResultModel | None = None
    retry_count: int = 0
    messages: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
