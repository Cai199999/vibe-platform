"""分级决策与实现偏好提取。

本模块只做确定性规则，不依赖 LangGraph / Pydantic / 模型 SDK：

1. ``decision_impact``：把需求分为「核心业务决策」与「实现细节」。
   只有前者允许触发 HITL 澄清；后者必须由 Agent 自主完成。
2. 偏好证据：从用户明确指令中提取可复用的设计偏好（主色、圆角、密度），
   由 Reflection 节点产出、Service 在 Run 成功落库后持久化。
3. 一键决策选项：把澄清问题里的 single_choice 选项收敛为前端按钮。

所有函数都是纯函数，便于单元测试与在 Mock / DeepSeek 两条路径复用。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from ai_core.domain.state import DecisionImpact

# 新枚举是唯一真源；旧常量保留为别名，避免历史调用方 / 检查点失效。
CRITICAL = DecisionImpact.CRITICAL.value
MAJOR = DecisionImpact.MAJOR.value
MINOR = DecisionImpact.MINOR.value
CORE_BUSINESS = CRITICAL
IMPLEMENTATION_DETAIL = MINOR
DECISION_IMPACTS = {CRITICAL, MAJOR, MINOR}

# 会改变业务结果 / 系统契约 / 破坏性影响的关键词。
# 注意：单独的“登录”不算核心决策——“加个登录框”只是 UI 实现；
# 只有“登录校验 / 密码校验 / 后端接口 / 数据库”等才需要用户拍板。
_CORE_KEYWORDS = (
    "接口",
    "api",
    "后端",
    "服务端",
    "数据库",
    "数据表",
    "建表",
    "新增表",
    "删表",
    "删库",
    "迁移",
    "依赖",
    "package.json",
    "契约",
    "鉴权",
    "认证服务",
    "密码校验",
    "登录校验",
    "权限",
    "角色",
    "支付",
    "扣款",
    "退款",
    "库存",
    "扣库存",
    "邮件",
    "短信",
    "通知",
    "删除整个",
    "移除整个",
    "删掉整个",
    "清空全部",
    "drop table",
    "truncate",
)

# 破坏性操作即使没有其它关键词也必须视为核心决策。
_DESTRUCTIVE_PATTERNS = (
    re.compile(r"(删除|移除|删掉|清空).{0,8}(整个|全部|所有)"),
    re.compile(r"(删除|移除|删掉).{0,8}(表|库|模块|页面|组件)"),
    re.compile(r"\bdrop\s+(table|database)\b", re.IGNORECASE),
)

# 颜色词：中文 -> (稳定值, 展示文案)。
_COLOR_WORDS: tuple[tuple[str, str, str], ...] = (
    ("红色", "red", "红色"),
    ("蓝色", "blue", "蓝色"),
    ("绿色", "green", "绿色"),
    ("紫色", "purple", "紫色"),
    ("橙色", "orange", "橙色"),
    ("黄色", "yellow", "黄色"),
    ("粉色", "pink", "粉色"),
    ("青色", "cyan", "青色"),
    ("黑色", "black", "黑色"),
    ("白色", "white", "白色"),
    ("red", "red", "红色"),
    ("blue", "blue", "蓝色"),
    ("green", "green", "绿色"),
    ("purple", "purple", "紫色"),
    ("orange", "orange", "橙色"),
    ("yellow", "yellow", "黄色"),
    ("pink", "pink", "粉色"),
    ("cyan", "cyan", "青色"),
    ("black", "black", "黑色"),
    ("white", "white", "白色"),
)

_PRIMARY_COLOR_MARKERS = (
    "主色",
    "主色调",
    "主题色",
    "品牌色",
    "强调色",
    "按钮颜色",
    "背景色",
)
_TEXT_COLOR_MARKERS = ("文字颜色", "字体颜色", "文本颜色", "字色")


def normalize_decision_impact(raw: Any) -> str | None:
    """把模型输出 / 旧字段收敛为 DecisionImpact；无法识别时返回 None。

    兼容旧枚举是渐进升级的关键：历史检查点、旧提示词和已持久化事件
    仍可能携带 ``CORE_BUSINESS`` / ``IMPLEMENTATION_DETAIL``。读取时统一
    映射到新枚举，写回时只写 CRITICAL / MAJOR / MINOR。
    """
    value = str(getattr(raw, "value", raw) or "").strip().upper()
    aliases = {
        "CORE": CRITICAL,
        "BUSINESS": CRITICAL,
        "CORE_BUSINESS": CRITICAL,
        "CORE_BUSINESS_DECISION": CRITICAL,
        "CRITICAL": CRITICAL,
        "MAJOR": MAJOR,
        "IMPORTANT": MAJOR,
        "DETAIL": MINOR,
        "IMPLEMENTATION": MINOR,
        "IMPLEMENTATION_DETAIL": MINOR,
        "STYLE": MINOR,
        "UI_DETAIL": MINOR,
        "MINOR": MINOR,
    }
    value = aliases.get(value, value)
    return value if value in DECISION_IMPACTS else None


def infer_decision_impact(annotations: Iterable[dict[str, Any]]) -> str:
    """按确定性关键词判断决策影响面。

    这是 Planner 未输出 ``decision_impact`` 时的兜底，也用于 Mock 与测试。
    只要命中一个核心业务 / 破坏性信号，就升级为 ``CRITICAL``；否则
    默认 ``MINOR``，避免样式类需求被误中断。
    """
    text = "\n".join(_annotation_texts(annotations)).lower()
    if not text:
        return MINOR
    if any(pattern.search(text) for pattern in _DESTRUCTIVE_PATTERNS):
        return CRITICAL
    if any(keyword.lower() in text for keyword in _CORE_KEYWORDS):
        return CRITICAL
    return MINOR


def should_interrupt(
    *,
    decision_impact: str | None,
    needs_clarification: bool,
    confidence_score: float,
    threshold: float,
    mode: str = "auto",
) -> bool:
    """分级决策的唯一中断判定。

    - ``CRITICAL``：缺失关键信息 / 多种方案 / 低置信度时中断；
    - ``MAJOR``：默认自主执行，只有显式标记缺信息时才中断；
    - ``MINOR``：无论置信度多低都不中断；
    - ``off`` 完全禁用；``always`` 只强制核心决策（用于调试）。
    """
    impact = normalize_decision_impact(decision_impact) or MINOR
    if mode == "off":
        return False
    if impact == MINOR:
        return False
    if impact == MAJOR:
        return bool(needs_clarification)
    if mode == "always":
        return True
    return bool(needs_clarification) or float(confidence_score) < float(threshold)


def build_clarification_options(
    questions: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从问题集提取一键决策按钮。

    只取第一个带选项的单选题；核心决策通常只有一个“拍板点”。选项上限 3，
    与产品要求的 A/B/C 对齐。模型若已显式提供 ``clarificationOptions``，
    调用方应优先使用模型结果。
    """
    for question in questions:
        if not isinstance(question, dict):
            continue
        question_type = question.get("type") or question.get("question_type")
        options = question.get("options")
        if question_type != "single_choice" or not isinstance(options, list):
            continue
        question_id = question.get("questionId") or question.get("question_id")
        default_value = question.get("defaultValue", question.get("default_value"))
        quick: list[dict[str, Any]] = []
        for index, option in enumerate(options[:3], start=1):
            if not isinstance(option, dict):
                continue
            value = str(option.get("value") or "").strip()
            if not value:
                continue
            label = str(option.get("label") or value).strip()
            quick.append(
                {
                    "optionId": str(option.get("optionId") or value or f"opt{index}"),
                    "label": label,
                    "description": str(option.get("description") or "").strip(),
                    "recommended": bool(
                        option.get("recommended")
                        or (default_value is not None and str(default_value) == value)
                    ),
                    "questionId": str(question_id or ""),
                    "value": value,
                }
            )
        if quick:
            return quick
    return []


def extract_preference_observations(
    annotations: Iterable[dict[str, Any]],
    *,
    run_id: str = "",
) -> list[dict[str, Any]]:
    """从用户明确指令中提取实现偏好证据。

    只读取用户输入，不读取模型生成代码；同一轮同一 key 只保留最后一条，
    防止长对话把一次修改统计成多条证据。
    """
    observations: dict[str, dict[str, Any]] = {}
    for raw in annotations:
        text = _single_annotation_text(raw)
        if not text:
            continue
        for observation in _extract_from_text(text):
            observation.update({"runId": run_id or None})
            observations[observation["key"]] = observation
    return list(observations.values())


def _extract_from_text(text: str) -> list[dict[str, Any]]:
    lowered = text.lower()
    found: list[dict[str, Any]] = []

    # 主色 / 文字色：必须命中具体目标，避免把一次性“改成紫色”当成全局偏好。
    target_key = None
    if any(marker in text for marker in _TEXT_COLOR_MARKERS):
        target_key = "text_color"
    elif any(marker in text for marker in _PRIMARY_COLOR_MARKERS):
        target_key = "primary_color"
    if target_key:
        for word, value, label in _COLOR_WORDS:
            if word in lowered:
                found.append(
                    {
                        "key": target_key,
                        "value": value,
                        "label": label,
                        "evidence": text[:160],
                    }
                )
                break

    if "圆角" in text or "圆润" in text:
        value = (
            "compact"
            if any(word in text for word in ("小", "更小", "减少"))
            else "rounded"
        )
        label = "小圆角" if value == "compact" else "圆角"
        found.append(
            {
                "key": "corner_style",
                "value": value,
                "label": label,
                "evidence": text[:160],
            }
        )

    if any(word in text for word in ("更紧凑", "紧凑一些", "密度高", "间距小")):
        found.append(
            {
                "key": "density",
                "value": "compact",
                "label": "紧凑布局",
                "evidence": text[:160],
            }
        )
    elif any(word in text for word in ("更宽松", "宽松一些", "间距大", "留白多")):
        found.append(
            {
                "key": "density",
                "value": "relaxed",
                "label": "宽松布局",
                "evidence": text[:160],
            }
        )
    return found


def _annotation_texts(annotations: Iterable[dict[str, Any]]) -> list[str]:
    return [_single_annotation_text(item) for item in annotations]


def _single_annotation_text(annotation: dict[str, Any]) -> str:
    """兼容 AnnotationEnvelope、Planner 上下文包装与普通 dict。"""
    if not isinstance(annotation, dict):
        return ""
    candidates: list[str] = []
    payload = annotation.get("payload")
    if isinstance(payload, dict):
        candidates.append(str(payload.get("text") or ""))
    intent = annotation.get("intent")
    if isinstance(intent, dict):
        ui = intent.get("ui")
        if isinstance(ui, dict):
            candidates.append(str(ui.get("summary") or ""))
        business = intent.get("business")
        if isinstance(business, dict):
            candidates.append(str(business.get("summary") or ""))
    candidates.append(str(annotation.get("instruction") or ""))
    candidates.append(str(annotation.get("ui_summary") or ""))

    # 同一 thread 最近一条用户消息，兼容前端只更新 chatHistory 的情况。
    thread = annotation.get("threadContext")
    if isinstance(thread, dict):
        for message in reversed(thread.get("chatHistory") or []):
            if not isinstance(message, dict):
                continue
            if (
                message.get("role") == "user"
                and str(message.get("content") or "").strip()
            ):
                candidates.append(str(message.get("content")))
                break
    return next((text.strip() for text in candidates if text.strip()), "")


__all__ = [
    "CORE_BUSINESS",
    "CRITICAL",
    "IMPLEMENTATION_DETAIL",
    "MAJOR",
    "MINOR",
    "build_clarification_options",
    "extract_preference_observations",
    "infer_decision_impact",
    "normalize_decision_impact",
    "should_interrupt",
]
