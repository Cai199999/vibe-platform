"""原型契约模型：只保留打通全链路所需字段。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    kind: str  # web | api | worker | agent
    runtime: str  # node | python
    port: int = 0


@dataclass(frozen=True)
class EventSpec:
    topic: str
    producer: str
    consumer: str
    idempotency_key: str = "eventId"


@dataclass(frozen=True)
class AgentSpec:
    name: str
    trigger: str
    output_schema: tuple[str, ...]


@dataclass(frozen=True)
class KnowledgeSpec:
    provider: str
    sources: tuple[str, ...]
    weights: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class UsageScenario:
    roles: tuple[str, ...]
    scale: str
    deployment: str
    integrations: tuple[str, ...]
    data_sensitivity: str
    change_frequency: str


@dataclass(frozen=True)
class ApplicationSpec:
    name: str
    goal: str
    entities: tuple[str, ...]
    services: tuple[ServiceSpec, ...]
    events: tuple[EventSpec, ...]
    agents: tuple[AgentSpec, ...]
    knowledge: KnowledgeSpec
    usage_scenarios: UsageScenario
    layers: tuple[str, ...]
    extension_points: tuple[str, ...]
    quality_profile: str = "production"
    version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


def default_ticket_spec(*, version: int = 1) -> ApplicationSpec:
    """最小工单系统：足够跑通事件、Agent、迁移与交付。"""

    return ApplicationSpec(
        name="ticket-copilot",
        goal="工单进入后自动分类并给出处置建议，人工确认后派单",
        entities=(
            "Ticket(id, title, status, createdAt)",
            "AiSuggestion(ticketId, category, confidence, suggestion)",
        ),
        services=(
            ServiceSpec("web", "web", "node", 5173),
            ServiceSpec("api", "api", "node", 8080),
            ServiceSpec("consumer", "worker", "python"),
            ServiceSpec("agent", "agent", "python"),
        ),
        events=(
            EventSpec(
                topic="ticket.created",
                producer="api",
                consumer="consumer",
                idempotency_key="eventId",
            ),
        ),
        agents=(
            AgentSpec(
                name="RoutingAgent",
                trigger="ticket.created",
                output_schema=("category", "confidence", "suggestion"),
            ),
        ),
        knowledge=KnowledgeSpec(
            provider="milvus",
            sources=("business_docs", "history_tickets"),
            weights={"business_docs": 0.4, "history_tickets": 0.6},
        ),
        usage_scenarios=UsageScenario(
            roles=("客服", "主管"),
            scale="50 并发",
            deployment="客户私有 K8s",
            integrations=("现有工单系统",),
            data_sensitivity="含客户信息",
            change_frequency="每周可能调整分类规则",
        ),
        layers=("interface", "application", "domain", "infrastructure"),
        extension_points=("category_rules", "prompt_templates", "routing_policy"),
        version=version,
    )
