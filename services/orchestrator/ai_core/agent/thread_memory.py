"""标注级多轮会话记忆。

每个画布标注拥有独立的 ``threadId``。同一 threadId 的后续请求需要携带：
  - 前几轮用户指令；
  - 助手回复；
  - 上一次 Run 的修改摘要 / 变更文件 / patch；
  - 上一次沙盒中的完整源码快照（用于沙盒丢失时重建）。

M1 使用进程内存储，保证同一次 python-ai 生命周期内多轮对话可用；前端同时
把 chatHistory 与 sandboxId 传回服务端，因此 python-ai 重启后仍能继续。
M2 将替换为 Redis / MySQL 持久化，本类接口保持不变。
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

MAX_HISTORY = 60
MAX_PROMPT_HISTORY = 24
MAX_RUNS = 8
MAX_CHECKPOINTS = 8
MAX_PATCH_CHARS = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _message_key(message: dict[str, Any]) -> tuple[str, ...]:
    """助手消息按 runId 去重；用户消息优先使用前端生成的唯一 id。"""
    role = str(message.get("role") or "")
    if role == "user" and message.get("id"):
        return (role, str(message["id"]))
    return (
        role,
        str(message.get("runId") or message.get("run_id") or ""),
        str(message.get("content") or ""),
    )


def _normalize_message(message: Any) -> dict[str, Any] | None:
    """兼容 Pydantic 模型与前端 JSON，统一成 camelCase 字典。"""
    if hasattr(message, "model_dump"):
        raw = message.model_dump(by_alias=True, exclude_none=True)
    elif isinstance(message, dict):
        raw = dict(message)
    else:
        return None

    role = str(raw.get("role") or "").strip()
    content = str(raw.get("content") or "").strip()
    if role not in {"user", "assistant", "system"} or not content:
        return None

    created_at = raw.get("createdAt") or raw.get("created_at") or _now()
    if not isinstance(created_at, str):
        created_at = str(created_at)
    normalized = {
        "role": role,
        "content": content[:8000],
        "createdAt": created_at,
    }
    if raw.get("id"):
        normalized["id"] = str(raw["id"])
    run_id = raw.get("runId") or raw.get("run_id")
    if run_id:
        normalized["runId"] = str(run_id)
    commit_hash = raw.get("commitHash") or raw.get("commit_hash")
    if commit_hash:
        normalized["commitHash"] = str(commit_hash)
    turn_count = raw.get("turnCount") or raw.get("turn_count")
    if turn_count is not None:
        try:
            normalized["turnCount"] = int(turn_count)
        except (TypeError, ValueError):
            pass
    return normalized


@dataclass
class ThreadRecord:
    """一个标注圈的会话记忆。"""

    thread_id: str
    chat_history: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    sandbox_id: str | None = None
    updated_at: str = field(default_factory=_now)


class ThreadMemoryStore:
    """线程安全的内存会话存储。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._threads: dict[str, ThreadRecord] = {}

    def _record(self, thread_id: str) -> ThreadRecord:
        record = self._threads.get(thread_id)
        if record is None:
            record = ThreadRecord(thread_id=thread_id)
            self._threads[thread_id] = record
        return record

    def _append_messages(self, record: ThreadRecord, messages: Iterable[Any]) -> None:
        existing = {_message_key(item) for item in record.chat_history}
        for raw in messages:
            message = _normalize_message(raw)
            if message is None:
                continue
            key = _message_key(message)
            if key in existing:
                continue
            record.chat_history.append(message)
            existing.add(key)
        if len(record.chat_history) > MAX_HISTORY:
            record.chat_history = record.chat_history[-MAX_HISTORY:]

    def merge_context(
        self,
        thread_id: str,
        provided_history: Iterable[Any] | None = None,
    ) -> dict[str, Any]:
        """合并前端历史与服务端记忆，返回给模型的 threadContext。"""
        with self._lock:
            record = self._record(thread_id)
            if provided_history:
                self._append_messages(record, provided_history)
            record.updated_at = _now()
            return {
                "threadId": thread_id,
                "chatHistory": list(record.chat_history[-MAX_PROMPT_HISTORY:]),
                "previousRuns": list(record.runs[-3:]),
            }

    def next_turn(
        self,
        thread_id: str,
        provided_history: Iterable[Any] | None = None,
    ) -> int:
        """计算该圈下一轮 turnCount，作为 Git commit message 的轮次坐标。

        服务端 run 记录是主真源；python-ai 重启后 run 记录为空，此时用前端
        chatHistory 中带 commitHash 的历史助手消息恢复轮次，避免回滚后
        turn 从头开始。
        """
        with self._lock:
            record = self._record(thread_id)
            if provided_history:
                self._append_messages(record, provided_history)
            turns: list[int] = []
            for run in record.runs:
                if run.get("turnCount") is not None:
                    turns.append(int(run["turnCount"]))
            for message in record.chat_history:
                if message.get("turnCount") is not None:
                    turns.append(int(message["turnCount"]))
            return max(turns, default=0) + 1

    def checkpoint(
        self,
        thread_id: str,
        *,
        run_id: str | None = None,
        turn_count: int | None = None,
        commit_hash: str | None = None,
    ) -> dict[str, Any] | None:
        """按 Run / turn / commit 任一坐标查找 thread checkpoint 元数据。"""
        with self._lock:
            record = self._threads.get(thread_id)
            if record is None:
                return None
            checkpoint = self._find_checkpoint(
                record,
                run_id=run_id,
                turn_count=turn_count,
                commit_hash=commit_hash,
            )
            if checkpoint is None:
                return None
            return self._checkpoint_meta(thread_id, checkpoint)

    @staticmethod
    def _find_checkpoint(
        record: ThreadRecord,
        *,
        run_id: str | None = None,
        turn_count: int | None = None,
        commit_hash: str | None = None,
    ) -> dict[str, Any] | None:
        for checkpoint in reversed(record.checkpoints):
            if run_id and checkpoint.get("runId") != run_id:
                continue
            if turn_count and checkpoint.get("turnCount") != turn_count:
                continue
            if commit_hash and checkpoint.get("commitHash") != commit_hash:
                continue
            return checkpoint
        return None

    @staticmethod
    def _checkpoint_meta(thread_id: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
        return {
            "threadId": thread_id,
            "runId": checkpoint.get("runId"),
            "turnCount": checkpoint.get("turnCount"),
            "commitHash": checkpoint.get("commitHash"),
            "sandboxId": checkpoint.get("sandboxId"),
            "createdAt": checkpoint.get("createdAt"),
        }

    def sandbox_id(self, thread_id: str) -> str | None:
        with self._lock:
            record = self._threads.get(thread_id)
            return record.sandbox_id if record else None

    def seed_files(self, thread_ids: Iterable[str]) -> dict[str, str]:
        """沙盒丢失时，用最近一次成功 Run 的源码快照重建。"""
        with self._lock:
            for thread_id in thread_ids:
                record = self._threads.get(thread_id)
                if record and record.files:
                    return dict(record.files)
        return {}

    def record_run(
        self,
        thread_ids: Iterable[str],
        *,
        run_id: str,
        user_messages: dict[str, Any],
        assistant_message: str,
        success: bool,
        sandbox_id: str,
        files: dict[str, str],
        patch: str,
        changed_files: list[str],
        retry_count: int,
        turn_counts: dict[str, int] | None = None,
        commit_hash: str | None = None,
        baseline_commit: str | None = None,
    ) -> None:
        """把本轮用户指令、助手回复与修改记录写入每个相关 thread。

        只有真正拿到 Git commit 的成功 Run 才生成 checkpoint。checkpoint
        保存该轮结束时的聊天历史、runs、源码快照与沙盒 ID，回滚时整体恢复，
        等价于 LangGraph Checkpointer 的 thread 级快照语义。
        """
        with self._lock:
            for thread_id in thread_ids:
                if not thread_id:
                    continue
                record = self._record(thread_id)
                user_message = user_messages.get(thread_id)
                if user_message is not None:
                    self._append_messages(record, [user_message])
                turn_count = (turn_counts or {}).get(thread_id)
                assistant_payload = {
                    "role": "assistant",
                    "content": assistant_message,
                    "runId": run_id,
                    "createdAt": _now(),
                }
                if turn_count is not None:
                    assistant_payload["turnCount"] = turn_count
                if commit_hash:
                    assistant_payload["commitHash"] = commit_hash
                self._append_messages(record, [assistant_payload])
                run_payload = {
                    "runId": run_id,
                    "success": success,
                    "summary": assistant_message,
                    "changedFiles": changed_files,
                    "retryCount": retry_count,
                    "patch": patch[-MAX_PATCH_CHARS:] if patch else "",
                    "createdAt": _now(),
                }
                if turn_count is not None:
                    run_payload["turnCount"] = turn_count
                if commit_hash:
                    run_payload["commitHash"] = commit_hash
                if baseline_commit:
                    run_payload["baselineCommit"] = baseline_commit
                record.runs.append(run_payload)
                record.runs = record.runs[-MAX_RUNS:]
                if files:
                    record.files = dict(files)
                record.sandbox_id = sandbox_id
                record.updated_at = _now()

                if success and commit_hash and turn_count is not None:
                    # deep copy：回滚后继续对话时，旧 checkpoint 不能被后续
                    # 的 append / 修改污染。
                    record.checkpoints.append(
                        {
                            "runId": run_id,
                            "turnCount": turn_count,
                            "commitHash": commit_hash,
                            "baselineCommit": baseline_commit,
                            "sandboxId": sandbox_id,
                            "chatHistory": copy.deepcopy(record.chat_history),
                            "runs": copy.deepcopy(record.runs),
                            "files": dict(record.files),
                            "createdAt": _now(),
                        }
                    )
                    record.checkpoints = record.checkpoints[-MAX_CHECKPOINTS:]

    def rollback_to_checkpoint(
        self,
        thread_id: str,
        *,
        run_id: str | None = None,
        turn_count: int | None = None,
        commit_hash: str | None = None,
    ) -> dict[str, Any] | None:
        """恢复 thread 到指定成功轮次，并丢弃其后的历史与 checkpoint。"""
        with self._lock:
            record = self._threads.get(thread_id)
            if record is None:
                return None
            target = self._find_checkpoint(
                record,
                run_id=run_id,
                turn_count=turn_count,
                commit_hash=commit_hash,
            )
            if target is None:
                return None
            target_index = next(
                index
                for index, checkpoint in enumerate(record.checkpoints)
                if checkpoint is target
            )
            record.chat_history = copy.deepcopy(target.get("chatHistory") or [])
            record.runs = copy.deepcopy(target.get("runs") or [])
            record.files = dict(target.get("files") or {})
            record.sandbox_id = target.get("sandboxId") or record.sandbox_id
            record.checkpoints = record.checkpoints[: target_index + 1]
            record.updated_at = _now()
            return self._checkpoint_meta(thread_id, target)


_default_store = ThreadMemoryStore()


def get_thread_store() -> ThreadMemoryStore:
    """进程级单例，供 RunExecutionService 复用。"""
    return _default_store
