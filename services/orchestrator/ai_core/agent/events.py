"""Run 事件总线：事件模型、seq 分配、重放与 SSE 订阅。

设计要点：
  - 同一个 Run 的事件由本模块**唯一**分配 seq，BFF 只透传；
  - Run 执行与 HTTP 连接解耦：没有订阅者时事件写缓冲，客户端重连后按
    ``afterSeq`` 补齐；
  - 订阅先注册队列、再回放历史，避免回放与实时事件之间出现空洞；
  - ``clarification.required`` / ``approval.required`` 也会结束当前 SSE
    连接，但它们表示暂停而非失败；用户决策后用 ``afterSeq`` 重放继续。
  - ``run.cancelled`` / ``run.rejected`` 是用户主动结束 Run 的终态。
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Protocol

TERMINAL_EVENT_TYPES = {
    "run.succeeded",
    "run.failed",
    "run.cancelled",
    "run.rejected",
    "clarification.required",
    # 审批暂停与澄清暂停一样会结束当前 SSE 连接；用户决策后通过
    # afterSeq 重放继续同一条事件流。
    "approval.required",
    # Architect（M2.5）使用独立事件命名空间，但复用同一 SSE 缓冲与 seq
    # 语义。澄清 / 契约确认是暂停态；其余为终态。
    "architect.clarification.required",
    "architect.contract.ready",
    "architect.contract.confirmed",
    "architect.cancelled",
    "architect.failed",
}
SSE_EVENT_NAME = "run.event"
HEARTBEAT_SECONDS = 15


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _event_id() -> str:
    return f"evt_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class RunEvent:
    """SSE 帧里的业务事件。字段名与 03.API接口设计.md 的 RunEvent 对齐。"""

    event_id: str
    run_id: str
    seq: int
    type: str
    version: int
    ts: str
    payload: dict[str, Any]
    trace_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "eventId": self.event_id,
            "runId": self.run_id,
            "seq": self.seq,
            "type": self.type,
            "version": self.version,
            "ts": self.ts,
            "payload": self.payload,
        }
        if self.trace_id:
            data["traceId"] = self.trace_id
        return data

    def to_sse(self) -> str:
        # data 必须是单行 JSON，避免不同客户端对多行 data 的拼接行为不一致。
        # default=str 是最后一道防线：任何未来新增的 datetime / Decimal /
        # Enum 字段都不会让整条 SSE 连接 500，最坏只是该字段降级为字符串。
        data = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        return f"id: {self.seq}\nevent: {SSE_EVENT_NAME}\ndata: {data}\n\n"


class EventSink(Protocol):
    """Graph 节点依赖的最小事件接口，便于同步执行时注入空实现。"""

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
    ) -> RunEvent | None: ...


class NullEventSink:
    """同步 /runs 路径使用：完全不产生事件，行为与 M1 一致。"""

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
    ) -> RunEvent | None:
        return None


class RunEventBuffer:
    """线程安全的事件缓冲 + 异步订阅扇出。

    M2.0 为进程内实现；生产替换为 Redis Stream 时保持同样接口：
    emit 写流、subscribe 从 afterSeq 读取并阻塞等待新事件。
    """

    def __init__(
        self,
        run_id: str,
        *,
        trace_id: str | None = None,
        max_events: int = 2000,
    ) -> None:
        self.run_id = run_id
        self.trace_id = trace_id
        self._max_events = max(1, max_events)
        self._lock = threading.RLock()
        self._events: deque[RunEvent] = deque(maxlen=self._max_events)
        self._seq = 0
        self._dedupe_keys: set[str] = set()
        self._subscribers: dict[
            int, tuple[asyncio.AbstractEventLoop, asyncio.Queue]
        ] = {}
        self._terminal_seq: int | None = None

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
    ) -> RunEvent | None:
        """分配 seq、写入缓冲并通知订阅者；重复 dedupe_key 直接忽略。"""
        with self._lock:
            if dedupe_key and dedupe_key in self._dedupe_keys:
                return None
            if dedupe_key:
                self._dedupe_keys.add(dedupe_key)

            self._seq += 1
            event = RunEvent(
                event_id=_event_id(),
                run_id=self.run_id,
                seq=self._seq,
                type=event_type,
                version=1,
                ts=_utc_now(),
                payload=payload,
                trace_id=self.trace_id,
            )
            self._events.append(event)
            if event_type in TERMINAL_EVENT_TYPES:
                self._terminal_seq = event.seq
            else:
                # 澄清暂停后同一 Run 会继续执行：非终态事件必须清除旧的
                # terminal 标记，否则新订阅者会在回放后立即关闭连接。
                self._terminal_seq = None
            subscribers = list(self._subscribers.values())

        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                # 订阅所在的事件循环已经关闭（浏览器断开），忽略即可。
                continue
        return event

    def snapshot(self, after_seq: int = 0) -> list[RunEvent]:
        with self._lock:
            return [event for event in self._events if event.seq > after_seq]

    async def subscribe(self, after_seq: int = 0) -> AsyncIterator[RunEvent | None]:
        """异步迭代事件；``None`` 表示心跳，由调用方发送 ``: ping``。

        先注册实时队列再回放历史，最后按 seq 去重。这样即使 Run 在回放
        期间继续推进，也不会丢事件或重复写入。
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        subscriber_id = id(queue)
        with self._lock:
            self._subscribers[subscriber_id] = (loop, queue)
            terminal_seq = self._terminal_seq

        last_seq = after_seq
        try:
            for event in self.snapshot(after_seq):
                last_seq = max(last_seq, event.seq)
                yield event
                if event.type in TERMINAL_EVENT_TYPES:
                    return

            # 终态 / 暂停事件在订阅之前就已发生，且 afterSeq 已覆盖它：
            # 没有实时事件可等，直接关闭连接。
            if terminal_seq is not None and terminal_seq <= last_seq:
                return

            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield None
                    continue
                if event.seq <= last_seq:
                    continue
                last_seq = event.seq
                yield event
                if event.type in TERMINAL_EVENT_TYPES:
                    return
        finally:
            with self._lock:
                self._subscribers.pop(subscriber_id, None)


_default_buffers: dict[str, RunEventBuffer] = {}
_default_lock = threading.RLock()


def get_event_buffer(
    run_id: str,
    *,
    trace_id: str | None = None,
    max_events: int = 2000,
) -> RunEventBuffer:
    """按 runId 复用事件缓冲；进程重启后丢失，M2.0 可接受。"""
    with _default_lock:
        buffer = _default_buffers.get(run_id)
        if buffer is None:
            buffer = RunEventBuffer(
                run_id,
                trace_id=trace_id,
                max_events=max_events,
            )
            _default_buffers[run_id] = buffer
        return buffer


def peer_event_buffer(run_id: str) -> RunEventBuffer | None:
    with _default_lock:
        return _default_buffers.get(run_id)
