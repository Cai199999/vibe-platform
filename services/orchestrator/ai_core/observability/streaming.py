"""模型 Token 流式回调上下文。

应用层（Graph / Architect）在节点执行期间通过 ``collect_tokens`` 注入一个
回调；模型适配器检测到回调后改用 ``stream`` 而不是 ``invoke``，把增量文本
实时推给 SSE。没有回调时（单测 / Mock / 后台批处理）保持原有一次性调用，
因此该能力对现有端口是纯增量、可关闭的。

使用 ContextVar 而不是修改 ``ModelClient.generate`` 签名：
  - 端口保持极窄，第三方网关实现不需要理解流式协议；
  - 并发 Run 各自持有自己的回调，不会串流；
  - Coder 超时线程通过 ``copy_context()`` 可以继承同一个回调。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

# (task, delta) -> None
TokenSink = Callable[[str, str], None]

_TOKEN_SINK: ContextVar[TokenSink | None] = ContextVar(
    "ai_core_token_sink",
    default=None,
)


def get_token_sink() -> TokenSink | None:
    """返回当前节点注册的流式回调；未启用时返回 None。"""
    return _TOKEN_SINK.get()


@contextmanager
def collect_tokens(sink: TokenSink) -> Iterator[None]:
    """在 with 块内把模型增量文本交给 ``sink``。"""
    token = _TOKEN_SINK.set(sink)
    try:
        yield
    finally:
        _TOKEN_SINK.reset(token)


class TokenStreamEmitter:
    """把高频 token 合并成低频 SSE 事件。

    DeepSeek 每 1~3 个字符就产出一个 chunk；逐条写事件会让 SSE / React
    每秒重渲染上百次。这里按「80ms 或 48 字符」聚合，既保留打字机观感，
    又把事件数量压到可接受范围。
    """

    def __init__(
        self,
        emit: Callable[[str, str], None],
        *,
        min_interval_ms: int = 80,
        max_chars: int = 48,
    ) -> None:
        self._emit = emit
        self._min_interval = max(0, min_interval_ms) / 1000.0
        self._max_chars = max(1, max_chars)
        self._buffer: list[str] = []
        self._task = ""
        self._last_flush = 0.0
        self._lock = threading.Lock()
        self._active = True

    def feed(self, task: str, delta: str) -> None:
        """模型适配器每收到一个 chunk 调用一次。"""
        if not delta:
            return
        with self._lock:
            if not self._active:
                return
            if task:
                self._task = task
            self._buffer.append(delta)
            size = sum(len(part) for part in self._buffer)
            now = time.monotonic()
            due = now - self._last_flush >= self._min_interval
            if size < self._max_chars and not due:
                return
            text = "".join(self._buffer)
            self._buffer.clear()
            self._last_flush = now
        self._emit(text, self._task)

    def flush(self) -> None:
        """节点结束时把残余文本立即推送。"""
        with self._lock:
            if not self._buffer:
                return
            text = "".join(self._buffer)
            self._buffer.clear()
            task = self._task
            self._last_flush = time.monotonic()
        self._emit(text, task)

    def close(self) -> None:
        """节点超时 / 结束后停止接收后台线程的迟到 token。"""
        with self._lock:
            self._active = False
            self._buffer.clear()


__all__ = [
    "TokenSink",
    "TokenStreamEmitter",
    "collect_tokens",
    "get_token_sink",
]
