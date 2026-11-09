"""兼容层：Agent / Run 契约已迁移至 ``ai_core.domain.agent``。

保留本模块是为了让既有调用方与测试无需一次性改 import；新代码请直接
从领域层导入。
"""

from ai_core.domain.agent import *  # noqa: F401,F403
from ai_core.domain.common import CamelModel, to_camel as _to_camel  # noqa: F401
