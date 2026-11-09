"""兼容层：沙盒工厂已迁移至 ``ai_core.adapters.sandbox``。"""

from ai_core.adapters.sandbox import LocalDockerSandbox, get_sandbox_provider

__all__ = ["LocalDockerSandbox", "get_sandbox_provider"]
