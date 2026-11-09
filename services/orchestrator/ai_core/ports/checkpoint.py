"""驱动端口：LangGraph Checkpointer 提供者。

端口只描述应用层需要的两个能力：取得 checkpointer、关闭资源。返回值使用
``Any``，避免领域 / 端口层直接依赖 LangGraph；具体 BaseCheckpointSaver
类型由 ``ai_core.adapters.checkpoint`` 提供。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CheckpointerProvider(Protocol):
    """Graph 编译所需的 checkpointer 提供者。"""

    def get(self) -> Any: ...

    def close(self) -> None: ...


__all__ = ["CheckpointerProvider"]
