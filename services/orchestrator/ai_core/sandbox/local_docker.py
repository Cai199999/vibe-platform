"""兼容层：本地 Docker 沙盒适配器已迁移至 ``ai_core.adapters.sandbox``。"""

from ai_core.adapters.sandbox.local_docker import LocalDockerSandbox

__all__ = ["LocalDockerSandbox"]
