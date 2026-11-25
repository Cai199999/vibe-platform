"""全流程原型：契约 → 拓扑 → 生成 → 迁移 → 验收 → 交付 → 飞轮。"""

from ai_core.prototype.spec import ApplicationSpec, default_ticket_spec
from ai_core.prototype.planner import Topology, plan_topology

__all__ = ["ApplicationSpec", "Topology", "default_ticket_spec", "plan_topology"]
