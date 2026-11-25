"""确定性拓扑规划：不调用 LLM。"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ai_core.prototype.spec import ApplicationSpec


class TopologyError(ValueError):
    """契约无法形成合法拓扑。"""


@dataclass(frozen=True)
class Topology:
    services: tuple[dict, ...]
    flows: tuple[dict, ...]
    test_plan: tuple[str, ...]
    deployment_order: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def plan_topology(spec: ApplicationSpec) -> Topology:
    names = {service.name for service in spec.services}
    if len(names) != len(spec.services):
        raise TopologyError("服务名重复")

    for event in spec.events:
        if event.producer not in names:
            raise TopologyError(f"事件生产者不存在: {event.producer}")
        if event.consumer not in names:
            raise TopologyError(f"事件消费者不存在: {event.consumer}")

    for agent in spec.agents:
        if not any(event.topic == agent.trigger for event in spec.events):
            raise TopologyError(f"Agent 触发器不存在: {agent.trigger}")

    kinds = {service.kind for service in spec.services}
    for required in ("api", "worker", "agent"):
        if required not in kinds:
            raise TopologyError(f"缺少必要服务类型: {required}")

    flows = tuple(
        {
            "topic": event.topic,
            "producer": event.producer,
            "consumer": event.consumer,
            "idempotencyKey": event.idempotency_key,
            "agent": next(
                (agent.name for agent in spec.agents if agent.trigger == event.topic),
                "",
            ),
        }
        for event in spec.events
    )
    services = tuple(
        {
            "name": service.name,
            "kind": service.kind,
            "runtime": service.runtime,
            "port": service.port,
        }
        for service in spec.services
    )
    deployment_order = tuple(
        ["migration", "api", "consumer", "agent"]
        + [service.name for service in spec.services if service.kind == "web"]
    )
    return Topology(
        services=services,
        flows=flows,
        test_plan=(
            "unit: 领域规则与幂等键",
            "integration: 事件 → 消费者 → Agent → 写回",
            "contract: 事件 schema 与 Agent 输出字段",
            "smoke: 重复事件不重复写回",
        ),
        deployment_order=deployment_order,
    )


__all__ = ["Topology", "TopologyError", "plan_topology"]
