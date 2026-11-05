"""Supervisor 确定性安全策略。

策略必须在 LLM 之前执行，且不允许被 ``targetAgent`` / ``scopeHint`` 绕过。
当前只覆盖一条最高风险边界：**禁止把数据库端口暴露到公网或宿主机**，
以及关闭鉴权 / 空密码等弱化数据库安全的行为。

为什么放在分类器而不是 Prompt：
  - Prompt 可能被注入或忽略，安全边界不能依赖模型；
  - 拦截结果必须是确定性的 ``rejected`` 路由，前端无需特殊错误通道；
  - 后续 PolicyEngine 扩展（脱敏、危险命令、迁移风险）沿用同一入口。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 数据库 / 数据中间件标识。中文与英文都覆盖，避免只拦截一个写法。
_DATABASE_TERMS: tuple[str, ...] = (
    "数据库",
    "mysql",
    "mariadb",
    "postgres",
    "postgresql",
    "mongodb",
    "mongo",
    "redis",
    "3306",
    "5432",
    "6379",
    "27017",
)

# “把端口交出去”的目标：公网、外网、宿主机、所有网卡。
_EXPOSURE_TARGETS: tuple[str, ...] = (
    "公网",
    "外网",
    "互联网",
    "0.0.0.0",
    "宿主端口",
    "宿主机",
    "所有网卡",
    "对外开放",
    "公开访问",
)

# 暴露动作。只出现“端口”但没有暴露动作时，不应误杀正常的内部端口设计。
_EXPOSURE_ACTIONS: tuple[str, ...] = (
    "暴露",
    "开放",
    "映射",
    "发布",
    "绑定",
    "直连",
    "转发",
)

# 弱化数据库安全的写法。
_AUTH_WEAKENING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(关闭|禁用|去掉|移除).{0,6}(鉴权|认证|密码|口令)"),
    re.compile(r"(root|管理员).{0,6}(空密码|无密码|免密码)"),
    re.compile(r"skip[-_ ]grant[-_ ]tables", re.IGNORECASE),
    re.compile(r"trust\s+authentication", re.IGNORECASE),
)


@dataclass(frozen=True)
class PolicyVerdict:
    """确定性策略判定结果。"""

    blocked: bool
    code: str = "ALLOW"
    reason: str = ""
    signals: list[str] = field(default_factory=list)
    # 对前端暴露的缺失项；保持与 annotations / customAgentRuntime 同一语义。
    missing_inputs: list[str] = field(default_factory=list)


def _contains_any(message: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if term in message]


def evaluate_policy(message: str) -> PolicyVerdict:
    """对用户自然语言做安全判定。

    判定规则：
      1. 同时命中“数据库标识 + 暴露动作 + 外部目标” → 拦截；
      2. 命中“数据库标识 + 弱化鉴权” → 拦截；
      3. 其余放行。

    规则刻意保守：正常架构设计（例如“数据库用 MySQL，端口 3306”）不会
    命中，因为缺少“公网 / 宿主机 / 0.0.0.0”等暴露目标。
    """
    text = (message or "").casefold().strip()
    if not text:
        return PolicyVerdict(blocked=False)

    database_hits = _contains_any(text, _DATABASE_TERMS)
    if not database_hits:
        return PolicyVerdict(blocked=False)

    target_hits = _contains_any(text, _EXPOSURE_TARGETS)
    action_hits = _contains_any(text, _EXPOSURE_ACTIONS)
    auth_hits = [
        pattern.pattern for pattern in _AUTH_WEAKENING_PATTERNS if pattern.search(text)
    ]

    if (target_hits and action_hits) or auth_hits:
        signals = [
            "policy:db_exposure",
            *(f"db:{term}" for term in database_hits),
            *(f"target:{term}" for term in target_hits),
            *(f"action:{term}" for term in action_hits),
            *(f"auth:{pattern}" for pattern in auth_hits),
        ]
        return PolicyVerdict(
            blocked=True,
            code="SECURITY_DB_EXPOSURE",
            reason=(
                "安全策略拒绝：禁止将数据库端口暴露到公网、宿主机或所有网卡，"
                "也禁止关闭数据库鉴权；远程访问必须通过受控代理并单独审批。"
            ),
            signals=signals,
            missing_inputs=["安全策略"],
        )

    return PolicyVerdict(blocked=False)
