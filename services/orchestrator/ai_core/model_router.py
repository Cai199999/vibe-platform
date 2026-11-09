"""按任务能力分级的模型路由。

设计目标：不让简单任务与复杂任务共用同一个模型。

- FAST：分类、摘要、格式校验、Planner 等短输出任务；
- STRONG：代码生成、Architect 契约等质量敏感任务。

配置优先级：

    任务级覆盖（MODEL_<TASK>_NAME）
      > 层级模型（MODEL_FAST_NAME / MODEL_STRONG_NAME）
      > DEEPSEEK_MODEL
      > deepseek-chat

温度同理：FAST 默认 0.0，STRONG 默认 0.2。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from langchain_openai import ChatOpenAI

from ai_core.config import Settings, get_settings

logger = logging.getLogger(__name__)

Capability = Literal["fast", "strong"]
TaskName = Literal[
    "routing",
    "validation",
    "summary",
    "planner",
    "coder",
    "architect_analyze",
    "architect_contract",
]

# 任务 -> 能力层级。新增节点时必须显式登记，避免默默落到昂贵模型。
TASK_CAPABILITY: dict[str, Capability] = {
    # Supervisor 当前是确定性规则路由，本身不调用模型；该能力位留给未来
    # 的 LLM 分类器，保证届时仍走廉价模型。
    "routing": "fast",
    # 表单 / 契约格式校验优先确定性代码，模型只做兜底。
    "validation": "fast",
    # 历史记忆压缩：输入长、输出短，用最快最便宜的模型。
    "summary": "fast",
    # UI Planner 只输出结构化计划，不需要强推理。
    "planner": "fast",
    # Coder 直接决定构建成功率，必须用强模型。
    "coder": "strong",
    # 应用形态判别是分类 + 少量字段抽取。
    "architect_analyze": "fast",
    # 契约起草 / 修订是长上下文 + 结构化输出，质量优先。
    "architect_contract": "strong",
}

# 任务级环境变量对应的 Settings 字段。
TASK_SETTING_FIELD: dict[str, str] = {
    "routing": "model_routing_name",
    "validation": "model_validation_name",
    "summary": "model_summary_name",
    "planner": "model_planner_name",
    "coder": "model_coder_name",
    "architect_analyze": "model_architect_analyze_name",
    "architect_contract": "model_architect_contract_name",
}


@dataclass(frozen=True)
class ModelProfile:
    """一次模型调用的完整路由结果。"""

    task: str
    capability: Capability
    model: str
    temperature: float

    def describe(self) -> dict[str, object]:
        return {
            "task": self.task,
            "capability": self.capability,
            "model": self.model,
            "temperature": self.temperature,
        }


class ModelRouter:
    """按任务选择模型；客户端按首次使用惰性构建并缓存。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._clients: dict[str, ChatOpenAI] = {}

    # ------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------
    def resolve(self, task: str) -> ModelProfile:
        """解析任务对应的模型与温度。未知任务直接失败，避免静默降级。"""
        if task not in TASK_CAPABILITY:
            raise ValueError(f"未知模型任务: {task}")

        capability = TASK_CAPABILITY[task]
        fast_model = (
            self._settings.model_fast_name.strip() or self._settings.deepseek_model
        )
        strong_model = self._settings.model_strong_name.strip() or fast_model
        tier_model = fast_model if capability == "fast" else strong_model

        field = TASK_SETTING_FIELD.get(task)
        override = getattr(self._settings, field, "").strip() if field else ""
        model = override or tier_model

        temperature = (
            self._settings.model_fast_temperature
            if capability == "fast"
            else self._settings.model_strong_temperature
        )
        profile = ModelProfile(
            task=task,
            capability=capability,
            model=model,
            temperature=temperature,
        )
        logger.debug("model route: %s", profile.describe())
        return profile

    def describe(self) -> dict[str, dict[str, object]]:
        """返回全部任务的解析结果，供启动日志 / 管理端核对。"""
        return {task: self.resolve(task).describe() for task in TASK_CAPABILITY}

    # ------------------------------------------------------------
    # 客户端
    # ------------------------------------------------------------
    def build(self, task: str) -> ChatOpenAI:
        """获取（并缓存）任务对应的 ChatOpenAI 客户端。"""
        profile = self.resolve(task)
        cached = self._clients.get(profile.model)
        if cached is not None:
            return cached

        if not self._settings.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY 未配置，无法构建真实模型客户端")

        kwargs: dict[str, object] = {
            "model": profile.model,
            "api_key": self._settings.deepseek_api_key,
            "base_url": self._settings.deepseek_base_url,
            "timeout": 120,
            "max_retries": 2,
        }
        # deepseek-reasoner 不接受 temperature / top_p 等采样参数；
        # 显式省略，避免切换到推理模型时 400。
        if "reasoner" not in profile.model.lower():
            kwargs["temperature"] = profile.temperature

        client = ChatOpenAI(**kwargs)  # type: ignore[arg-type]
        self._clients[profile.model] = client
        return client

    def clear_cache(self) -> None:
        """配置热更新后丢弃已构建客户端。"""
        self._clients.clear()
