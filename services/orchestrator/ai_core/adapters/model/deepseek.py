"""DeepSeek（OpenAI 兼容协议）模型适配器。

实现 ``ModelClient`` 端口，把应用层的 ``generate(task, system, human)``
映射为 ModelRouter 的模型分级 + LangChain 消息调用。所有厂商相关细节
（ChatOpenAI、消息类型、异常类型）都封装在这里。
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from ai_core.adapters.model.router import ModelRouter
from ai_core.config import Settings, get_settings
from ai_core.ports.model import ModelClient, ModelClientError


class DeepSeekClient(ModelClient):
    """DeepSeek Chat 的最小生成客户端。"""

    def __init__(
        self,
        settings: Settings | None = None,
        router: ModelRouter | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._router = router or ModelRouter(self.settings)

    def generate(self, task: str, system: str, human: str) -> str:
        """按任务分级调用模型，返回纯文本。"""
        try:
            model = self._router.build(task)
            response = model.invoke(
                [SystemMessage(content=system), HumanMessage(content=human)]
            )
        except Exception as exc:  # noqa: BLE001 - 统一转换为端口异常
            raise ModelClientError(f"DeepSeek 调用失败: {exc}") from exc

        content = response.content
        if isinstance(content, list):
            content = "".join(str(part) for part in content)
        return str(content)


# 兼容旧命名：早期文档 / 调用方使用 DeepSeekModelClient。
DeepSeekModelClient = DeepSeekClient

__all__ = ["DeepSeekClient", "DeepSeekModelClient"]
