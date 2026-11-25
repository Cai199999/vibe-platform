"""强制澄清桥：任意 Agent 卡住时，原地暂停并恢复。

原型只实现状态机，不绑定具体图执行器；接入时用 LangGraph checkpoint /
interrupt 承载 ``resume_token``，语义保持不变。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class ClarificationError(ValueError):
    """澄清请求不符合协议或状态非法。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


@dataclass(frozen=True)
class ClarificationOption:
    option_id: str
    label: str
    impact: str = ""

    def __post_init__(self) -> None:
        if not str(self.label or "").strip():
            raise ClarificationError("选项 label 不能为空")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.option_id,
            "label": self.label,
            "impact": self.impact,
        }


@dataclass(frozen=True)
class ClarificationRequest:
    request_id: str
    run_id: str
    asked_by: str
    task_id: str
    context_summary: str
    question: str
    options: tuple[ClarificationOption, ...]
    allow_free_text: bool = True
    round: int = 1
    state: str = "requested"
    resume_token: str = ""
    answers: tuple[dict[str, Any], ...] = ()
    created_at: str = ""

    def __post_init__(self) -> None:
        if not str(self.asked_by or "").strip():
            raise ClarificationError("askedBy 不能为空")
        if not str(self.task_id or "").strip():
            raise ClarificationError("taskId 不能为空")
        if not str(self.context_summary or "").strip():
            raise ClarificationError("contextSummary 不能为空")
        if not str(self.question or "").strip():
            raise ClarificationError("question 不能为空")
        if not 2 <= len(self.options) <= 4:
            raise ClarificationError("必须提供 2–4 个选项")
        if self.state not in {
            "requested",
            "awaiting_user",
            "answered",
            "resumed",
            "cancelled",
            "timeout",
        }:
            raise ClarificationError(f"未知状态: {self.state}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "runId": self.run_id,
            "askedBy": self.asked_by,
            "taskId": self.task_id,
            "contextSummary": self.context_summary,
            "question": self.question,
            "options": [item.to_dict() for item in self.options],
            "allowFreeText": self.allow_free_text,
            "round": self.round,
            "state": self.state,
            "resumeToken": self.resume_token,
            "answers": [dict(item) for item in self.answers],
            "createdAt": self.created_at or _now(),
        }


class ClarificationBridge:
    """按 request_id 管理的多轮澄清状态机。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[str, ClarificationRequest] = {}

    def request(
        self,
        *,
        run_id: str,
        asked_by: str,
        task_id: str,
        context_summary: str,
        question: str,
        options: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        allow_free_text: bool = True,
    ) -> ClarificationRequest:
        parsed = tuple(
            ClarificationOption(
                option_id=str(item.get("id") or _new_id("opt")),
                label=str(item.get("label") or ""),
                impact=str(item.get("impact") or ""),
            )
            for item in options
        )
        request = ClarificationRequest(
            request_id=_new_id("clr"),
            run_id=str(run_id),
            asked_by=str(asked_by),
            task_id=str(task_id),
            context_summary=str(context_summary),
            question=str(question),
            options=parsed,
            allow_free_text=allow_free_text,
        )
        with self._lock:
            self._requests[request.request_id] = request
        return request

    def _replace(self, request: ClarificationRequest) -> ClarificationRequest:
        self._requests[request.request_id] = request
        return request

    def relay(self, request_id: str) -> ClarificationRequest:
        """Architect 将问题改写为用户语言后展示。"""
        with self._lock:
            request = self._get(request_id)
            if request.state != "requested":
                raise ClarificationError("只有 requested 状态可以转交用户")
            return self._replace(
                ClarificationRequest(
                    **{
                        **_as_kwargs(request),
                        "state": "awaiting_user",
                    }
                )
            )

    def answer(
        self,
        request_id: str,
        *,
        option_id: str | None = None,
        free_text: str = "",
        followup_question: str = "",
        followup_options: list[dict[str, Any]] | None = None,
    ) -> ClarificationRequest:
        """回答一轮；需要继续追问时进入下一轮。"""
        with self._lock:
            request = self._get(request_id)
            if request.state not in {"awaiting_user", "answered"}:
                raise ClarificationError("当前状态不接受回答")
            if option_id is not None and option_id not in {
                item.option_id for item in request.options
            }:
                raise ClarificationError(f"未知选项: {option_id}")
            if option_id is None and not str(free_text).strip():
                raise ClarificationError("必须选择选项或填写自由文本")

            answer = {
                "round": request.round,
                "optionId": option_id or "",
                "freeText": str(free_text or "").strip(),
                "answeredAt": _now(),
            }
            answers = (*request.answers, answer)
            if followup_question:
                parsed = tuple(
                    ClarificationOption(
                        option_id=str(item.get("id") or _new_id("opt")),
                        label=str(item.get("label") or ""),
                        impact=str(item.get("impact") or ""),
                    )
                    for item in (followup_options or [])
                )
                next_request = ClarificationRequest(
                    request_id=request.request_id,
                    run_id=request.run_id,
                    asked_by=request.asked_by,
                    task_id=request.task_id,
                    context_summary=request.context_summary,
                    question=str(followup_question),
                    options=parsed,
                    allow_free_text=request.allow_free_text,
                    round=request.round + 1,
                    state="awaiting_user",
                    resume_token=request.resume_token,
                    answers=answers,
                    created_at=request.created_at,
                )
                return self._replace(next_request)

            return self._replace(
                ClarificationRequest(
                    request_id=request.request_id,
                    run_id=request.run_id,
                    asked_by=request.asked_by,
                    task_id=request.task_id,
                    context_summary=request.context_summary,
                    question=request.question,
                    options=request.options,
                    allow_free_text=request.allow_free_text,
                    round=request.round,
                    state="answered",
                    resume_token=request.resume_token,
                    answers=answers,
                    created_at=request.created_at,
                )
            )

    def resume(self, request_id: str) -> dict[str, Any]:
        """恢复原 Agent 进程，返回 resume_token 与最后一轮回答。"""
        with self._lock:
            request = self._get(request_id)
            if request.state != "answered":
                raise ClarificationError("只有 answered 状态可以恢复")
            token = request.resume_token or _new_id("ckpt")
            resumed = self._replace(
                ClarificationRequest(
                    request_id=request.request_id,
                    run_id=request.run_id,
                    asked_by=request.asked_by,
                    task_id=request.task_id,
                    context_summary=request.context_summary,
                    question=request.question,
                    options=request.options,
                    allow_free_text=request.allow_free_text,
                    round=request.round,
                    state="resumed",
                    resume_token=token,
                    answers=request.answers,
                    created_at=request.created_at,
                )
            )
        return {
            "requestId": resumed.request_id,
            "resumeToken": token,
            "taskId": resumed.task_id,
            "askedBy": resumed.asked_by,
            "answers": [dict(item) for item in resumed.answers],
        }

    def cancel(self, request_id: str) -> ClarificationRequest:
        with self._lock:
            request = self._get(request_id)
            return self._replace(_with_state(request, "cancelled"))

    def timeout(self, request_id: str) -> ClarificationRequest:
        with self._lock:
            request = self._get(request_id)
            return self._replace(_with_state(request, "timeout"))

    def get(self, request_id: str) -> ClarificationRequest:
        with self._lock:
            return self._get(request_id)

    def _get(self, request_id: str) -> ClarificationRequest:
        request = self._requests.get(str(request_id))
        if request is None:
            raise ClarificationError(f"澄清请求不存在: {request_id}")
        return request

    def reset(self) -> None:
        with self._lock:
            self._requests.clear()


def _with_state(
    request: ClarificationRequest,
    state: str,
) -> ClarificationRequest:
    return ClarificationRequest(
        request_id=request.request_id,
        run_id=request.run_id,
        asked_by=request.asked_by,
        task_id=request.task_id,
        context_summary=request.context_summary,
        question=request.question,
        options=request.options,
        allow_free_text=request.allow_free_text,
        round=request.round,
        state=state,
        resume_token=request.resume_token,
        answers=request.answers,
        created_at=request.created_at,
    )


def _as_kwargs(request: ClarificationRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "run_id": request.run_id,
        "asked_by": request.asked_by,
        "task_id": request.task_id,
        "context_summary": request.context_summary,
        "question": request.question,
        "options": request.options,
        "allow_free_text": request.allow_free_text,
        "round": request.round,
        "state": request.state,
        "resume_token": request.resume_token,
        "answers": request.answers,
        "created_at": request.created_at,
    }


__all__ = [
    "ClarificationBridge",
    "ClarificationError",
    "ClarificationOption",
    "ClarificationRequest",
]
