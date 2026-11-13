"""风险自适应门禁：质量分级策略。

用户确认契约时必须选择 L1 / L2 / L3。本模块把分级翻译成一组确定性的
执行开关，供 Applier、ConsistencyVerifier、BuildVerifier 与 MySQL 验收
节点共同读取，避免每个节点各自散落 ``if tier == ...`` 的判断。

分级语义：

  - L1_PROTOTYPE：极速 Demo。跳过 AST 门禁与 ORM/DDL 强比对；一致性
    问题降级为告警，构建失败最多重试 1 次，仍失败也返回告警而不阻塞；
  - L2_STANDARD：默认。保留 AST 门禁、ORM/DDL 强比对与 SQLite 结构
    验收，但不连接真实 MySQL，不做迁移验收；
  - L3_PRODUCTION：在全量门禁之上追加真实 MySQL 迁移验收。

策略对象是只读的；分级本身仍以契约为唯一事实来源。
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_core.domain.architect import (
    QUALITY_TIER_LABELS,
    ArchitectContract,
    normalize_quality_tier,
)

L1_PROTOTYPE = "L1_PROTOTYPE"
L2_STANDARD = "L2_STANDARD"
L3_PRODUCTION = "L3_PRODUCTION"

# 前端结果面板徽标文案；后端事件也原样携带，保证 UI / API 一致。
L1_WARNING_TEXT = (
    "⚠️ 当前为 L1 敏捷模式，代码未经过严格一致性校验，" "不建议直接用于生产环境。"
)
L3_PASSED_TEXT = "✅ L3 生产级校验通过，具备部署条件。"


@dataclass(frozen=True)
class QualityPolicy:
    """一个质量分级对应的执行策略。"""

    tier: str

    # Applier 是否执行 Babel AST 门禁。
    ast_gate: bool
    # ConsistencyVerifier 是否做 ORM ↔ 迁移 DDL 强比对。
    orm_schema_check: bool
    # 是否把生成 DDL 真实执行到临时 SQLite（MCP）。
    sqlite_schema_check: bool
    # 是否执行真实 MySQL 迁移验收（仅 L3）。
    mysql_migration_check: bool
    # 一致性 / 构建问题是否阻断流程；False 时统一降级为 warnings。
    blocking: bool
    # 构建失败后的自动重试次数（0 表示保持现有的一次失败即终止）。
    build_retry_limit: int
    # 是否在结果面板展示风险徽标。
    show_warning_badge: bool

    @property
    def label(self) -> str:
        return QUALITY_TIER_LABELS.get(self.tier, self.tier)

    @property
    def is_l1(self) -> bool:
        return self.tier == L1_PROTOTYPE

    @property
    def is_l3(self) -> bool:
        return self.tier == L3_PRODUCTION

    def to_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "label": self.label,
            "astGate": self.ast_gate,
            "ormSchemaCheck": self.orm_schema_check,
            "sqliteSchemaCheck": self.sqlite_schema_check,
            "mysqlMigrationCheck": self.mysql_migration_check,
            "blocking": self.blocking,
            "buildRetryLimit": self.build_retry_limit,
            "showWarningBadge": self.show_warning_badge,
        }


_POLICIES: dict[str, QualityPolicy] = {
    L1_PROTOTYPE: QualityPolicy(
        tier=L1_PROTOTYPE,
        ast_gate=False,
        orm_schema_check=False,
        sqlite_schema_check=False,
        mysql_migration_check=False,
        blocking=False,
        build_retry_limit=1,
        show_warning_badge=True,
    ),
    L2_STANDARD: QualityPolicy(
        tier=L2_STANDARD,
        ast_gate=True,
        orm_schema_check=True,
        sqlite_schema_check=True,
        mysql_migration_check=False,
        blocking=True,
        build_retry_limit=0,
        show_warning_badge=False,
    ),
    L3_PRODUCTION: QualityPolicy(
        tier=L3_PRODUCTION,
        ast_gate=True,
        orm_schema_check=True,
        sqlite_schema_check=True,
        mysql_migration_check=True,
        blocking=True,
        build_retry_limit=0,
        show_warning_badge=False,
    ),
}


def resolve_quality_tier(
    contract_or_tier: ArchitectContract | str | None,
) -> str:
    """从契约或字符串解析出稳定分级；未知值回退 L2。"""
    if isinstance(contract_or_tier, ArchitectContract):
        tier = contract_or_tier.quality_tier
    else:
        tier = contract_or_tier
    return normalize_quality_tier(tier) or L2_STANDARD


def quality_policy(
    contract_or_tier: ArchitectContract | str | None,
) -> QualityPolicy:
    """返回质量分级对应的只读策略。"""
    return _POLICIES[resolve_quality_tier(contract_or_tier)]


def warning_badge(tier: str) -> dict[str, str] | None:
    """前端结果面板徽标：L1 黄色告警 / L3 绿色通过。"""
    resolved = resolve_quality_tier(tier)
    if resolved == L1_PROTOTYPE:
        return {"tone": "warning", "text": L1_WARNING_TEXT}
    if resolved == L3_PRODUCTION:
        return {"tone": "success", "text": L3_PASSED_TEXT}
    return None


__all__ = [
    "L1_PROTOTYPE",
    "L1_WARNING_TEXT",
    "L2_STANDARD",
    "L3_PASSED_TEXT",
    "L3_PRODUCTION",
    "QualityPolicy",
    "quality_policy",
    "resolve_quality_tier",
    "warning_badge",
]
