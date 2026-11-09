"""DeepSeek（OpenAI 兼容协议）模型适配器。

实现 ``ModelClient`` 端口，把应用层的 ``generate(task, system, human)``
映射为 ModelRouter 的模型分级 + LangChain 消息调用。所有厂商相关细节
（ChatOpenAI、消息类型、异常类型）都封装在这里。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ai_core.adapters.model.router import ModelRouter
from ai_core.config import Settings, get_settings
from ai_core.ports.model import ModelClient, ModelClientError

logger = logging.getLogger(__name__)


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

        return self._content_to_text(response.content)

    def generate_json(
        self,
        task: str,
        system: str,
        human: str,
        *,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        """强制模型输出 JSON 对象（DeepSeek JSON Output 模式）。

        DeepSeek 的 OpenAI 兼容接口支持 ``response_format={"type":
        "json_object"}``，但不保证所有网关都实现了它。因此：
          1. 提示词明确要求 JSON，并附上 Schema；
          2. 绑定 response_format 调用；
          3. 首次调用失败时回退为普通调用，避免网关不兼容导致整次请求失败。
        真正的字段校验与重试仍由应用层完成。
        """
        if json_schema:
            schema_text = json.dumps(
                json_schema,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            system = (
                f"{system}\n\n"
                "你必须输出一个符合以下 JSON Schema 的 JSON 对象，"
                "不要输出 Markdown 围栏或解释文字：\n"
                f"{schema_text}"
            )

        messages = [SystemMessage(content=system), HumanMessage(content=human)]
        try:
            model = self._router.build(task)
        except Exception as exc:  # noqa: BLE001 - 统一转换为端口异常
            raise ModelClientError(f"DeepSeek 调用失败: {exc}") from exc
        response_format = {"type": "json_object"}
        if self.settings.architect_structured_output:
            try:
                response = model.bind(response_format=response_format).invoke(messages)
                return self._content_to_text(response.content)
            except Exception as exc:  # noqa: BLE001 - 兼容不支持该参数的网关
                logger.warning(
                    "response_format 调用失败，回退普通 JSON 提示 task=%s: %s",
                    task,
                    exc,
                )

        try:
            response = model.invoke(messages)
        except Exception as exc:  # noqa: BLE001 - 统一转换为端口异常
            raise ModelClientError(f"DeepSeek 调用失败: {exc}") from exc
        return self._content_to_text(response.content)

    @staticmethod
    def _content_to_text(content: Any) -> str:
        """ChatOpenAI 的 content 可能是 str 或分段列表，统一拼接。"""
        if isinstance(content, list):
            content = "".join(str(part) for part in content)
        return str(content)


# 兼容旧命名：早期文档 / 调用方使用 DeepSeekModelClient。
DeepSeekModelClient = DeepSeekClient

__all__ = ["DeepSeekClient", "DeepSeekModelClient"]
