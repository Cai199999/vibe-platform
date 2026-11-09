"""沙盒适配器与组合根。

当前固定使用 ``LocalDockerSandbox``；未来切换远程沙盒 / gVisor 时，
只需在这里替换实现，应用层继续依赖 ``SandboxProvider`` 端口。
"""

from __future__ import annotations

from functools import lru_cache

from ai_core.adapters.sandbox.local_docker import LocalDockerSandbox
from ai_core.ports.sandbox import SandboxProvider


@lru_cache
def get_sandbox_provider() -> SandboxProvider:
    """进程内复用同一个 Provider，保证沙盒注册表一致。"""
    return LocalDockerSandbox()


__all__ = ["LocalDockerSandbox", "get_sandbox_provider"]
