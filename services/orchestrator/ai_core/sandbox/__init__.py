"""Sandbox 工厂。

M1 固定使用 LocalDockerSandbox；M2 可通过配置切换到远程沙盒节点。
"""

from __future__ import annotations

from functools import lru_cache

from ai_core.sandbox.base import SandboxProvider
from ai_core.sandbox.local_docker import LocalDockerSandbox


@lru_cache
def get_sandbox_provider() -> SandboxProvider:
    """进程内复用同一个 Provider，保证沙盒注册表一致。"""
    return LocalDockerSandbox()
