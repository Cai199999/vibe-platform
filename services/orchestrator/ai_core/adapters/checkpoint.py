"""LangGraph Checkpointer 适配器。

端口为 ``ai_core.ports.checkpoint.CheckpointerProvider``。M2 使用进程内
MemorySaver；全栈长任务需要跨进程 / 跨重启恢复 HITL 中断，生产环境可
在这里切换 RedisSaver，不修改 Graph 或 RunExecutionService。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver


class CheckpointerProvider(Protocol):
    """Graph 编译所需的 checkpointer 提供者。"""

    def get(self) -> BaseCheckpointSaver: ...

    def close(self) -> None: ...


class MemoryCheckpointerProvider:
    """进程内单例。

    MemorySaver 本身支持多 thread_id 隔离，可在多个 Run 之间共享；这里只
    负责保证同一进程内拿到同一个实例，避免每个 Run 各建一份、恢复时找不到。
    """

    def __init__(self) -> None:
        self._saver = MemorySaver()

    def get(self) -> BaseCheckpointSaver:
        return self._saver

    def close(self) -> None:
        # 内存实现无需释放；保留接口供 Redis 实现关闭连接池。
        return None


class RedisCheckpointerProvider:
    """生产实现占位。

    依赖 ``langgraph-checkpoint-redis``（或自研 RedisSaver），在 M3 全栈
    长任务接入时实现：
      - key 前缀 ``vibe:run:checkpoint:{thread_id}``；
      - TTL 与 Run / Clarification 生命周期一致；
      - 序列化使用 LangGraph 官方 serializer，禁止 pickle 不可信输入。
    """

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise NotImplementedError(
            "RedisCheckpointerProvider 将在全栈长任务阶段实现；"
            "M2 请使用 MemoryCheckpointerProvider"
        )

    def get(self) -> BaseCheckpointSaver:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


@lru_cache
def get_checkpointer_provider() -> CheckpointerProvider:
    """进程级单例；未来按环境变量返回 Redis 实现。"""
    return MemoryCheckpointerProvider()
