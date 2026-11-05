"""Architect Graph 状态。"""

from __future__ import annotations

from typing import Any, TypedDict


class ArchitectState(TypedDict, total=False):
    run_id: str
    project_id: str
    project_name: str
    source_files: dict[str, str]
    source_hash: str
    ui_history: list[dict[str, Any]]
    threshold: float
    max_rounds: int
    round: int
    status: str
    app_shape: str
    slots: dict[str, Any]
    confidence_score: float
    missing_slots: list[str]
    questions: list[dict[str, Any]]
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
