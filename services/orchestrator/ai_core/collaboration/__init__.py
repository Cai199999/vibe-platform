"""Agent 协作协议原型：Architect 中心制。

模块边界：
- ``protocol``：AgentReport 等结构化消息；
- ``inbox``：Architect 收件箱与摘要/全文裁决；
- ``clarification``：可暂停/恢复的强制澄清桥；
- ``risk``：高危操作判定；
- ``ledger``：动作台账；
- ``user_language``：用户可见文案过滤；
- ``a2a_bridge``：把现有 A2A 反馈改道到 Architect。
"""

from ai_core.collaboration.error_router import (
    GATE_ERRORS,
    MAX_FIX_ATTEMPTS,
    GateAssignment,
    GateError,
    GateErrorLedger,
    GateErrorRouter,
)
from ai_core.collaboration.protocol import (
    ARCHITECT,
    AgentReport,
    route_a2a_to_architect,
)

__all__ = [
    "ARCHITECT",
    "AgentReport",
    "GATE_ERRORS",
    "GateAssignment",
    "GateError",
    "GateErrorLedger",
    "GateErrorRouter",
    "MAX_FIX_ATTEMPTS",
    "route_a2a_to_architect",
]
