"""兼容层：Agent 状态已迁移至 ``ai_core.domain.state``。"""

from ai_core.domain.state import AgentState, VerifyState

__all__ = ["AgentState", "VerifyState"]
