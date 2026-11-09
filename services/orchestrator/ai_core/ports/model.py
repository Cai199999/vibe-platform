"""驱动端口：大模型客户端（ModelClient）。

应用层只依赖本接口；具体使用 DeepSeek、OpenAI 兼容网关还是本地模型，
由 ``ai_core.adapters.model`` 中的适配器决定。

接口刻意保持极窄：一次调用只传入任务名、System Prompt 与 Human Prompt，
返回纯文本。JSON 解析、重试、业务校验全部留在应用层，避免把某家模型
SDK 的消息结构泄漏进用例。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ModelClientError(RuntimeError):
    """模型调用失败（网络、鉴权、限流或返回空内容）。"""


class ModelClient(ABC):
    """所有大模型适配器必须实现的端口。"""

    @abstractmethod
    def generate(self, task: str, system: str, human: str) -> str:
        """按任务能力层级调用模型并返回纯文本结果。

        Args:
            task: 逻辑任务名（planner / coder / summary 等），由适配器
                映射到具体模型与温度。
            system: 系统提示词。
            human: 用户消息。
        """

    def generate_json(
        self,
        task: str,
        system: str,
        human: str,
        *,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        """要求模型输出 JSON 对象。

        默认实现退化为 ``generate``，保证只实现 ``generate`` 的测试替身 /
        企业网关仍然可用；DeepSeek 适配器会绑定 ``response_format`` 并在
        提示词中附加 JSON Schema，降低结构漂移概率。

        Args:
            task: 逻辑任务名。
            system: 系统提示词。
            human: 用户消息。
            json_schema: 期望的 JSON Schema；为 None 时只要求合法 JSON。
        """
        return self.generate(task, system, human)


__all__ = ["ModelClient", "ModelClientError"]
