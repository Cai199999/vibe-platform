"""模型适配器。"""

from ai_core.adapters.model.deepseek import DeepSeekClient, DeepSeekModelClient
from ai_core.adapters.model.router import ModelProfile, ModelRouter

__all__ = [
    "DeepSeekClient",
    "DeepSeekModelClient",
    "ModelProfile",
    "ModelRouter",
]
