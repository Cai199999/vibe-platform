"""兼容层：Checkpointer 端口与适配器已拆分。

端口位于 ``ai_core.ports.checkpoint``，具体实现位于
``ai_core.adapters.checkpoint``。
"""

from ai_core.adapters.checkpoint import *  # noqa: F401,F403
