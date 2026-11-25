"""判断上传文档是“长期复用”还是“一次性使用”。

规则优先：明确的一次性 / 反向语义、明确的规范 / 手册 / 长期语义，
直接给出结论；规则无法判断时才调用模型兜底。模型失败一律回退 ASK，
让用户在文档中心里手动决定，不阻塞主流程。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from ai_core.adapters.model.deepseek import DeepSeekClient
from ai_core.config import get_settings
from ai_core.ports.model import ModelClient, ModelClientError

logger = logging.getLogger(__name__)

Decision = Literal["STORE", "DISCARD", "ASK"]
Reuse = Literal["FREQUENT", "ONE_OFF", "UNCERTAIN"]

_ONE_OFF_SIGNALS = (
    "不要保存",
    "不要加入文档中心",
    "不要存",
    "仅本次",
    "只本次",
    "一次性",
    "临时",
    "草稿",
    "看完就删",
    "不作为需求",
    "无需保留",
    "无需保存",
)
_FREQUENT_SIGNALS = (
    "规范",
    "标准",
    "手册",
    "制度",
    "术语",
    "数据字典",
    "接口文档",
    "需求文档",
    "流程",
    "长期",
    "复用",
    "每次",
    "常用",
    "参考",
    "约定",
    "指南",
    "sop",
    "权限矩阵",
    "字段说明",
)
_BUSINESS_TERMS = (
    "需求",
    "规则",
    "流程",
    "实体",
    "字段",
    "接口",
    "角色",
    "权限",
    "数据",
    "表",
    "校验",
    "验收",
    "数据库",
    "存储",
)
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)


@dataclass(frozen=True)
class DocumentReuseDecision:
    decision: Decision
    reuse: Reuse
    reason: str
    confidence: float
    source: Literal["rules", "llm"]
    signals: list[str] = field(default_factory=list)


def _matched(text: str, signals: tuple[str, ...]) -> list[str]:
    lower = text.lower()
    return [signal for signal in signals if signal.lower() in lower]


def _classify_by_rules(name: str, content: str) -> DocumentReuseDecision:
    text = f"{name}\n{content}"
    one_off = _matched(text, _ONE_OFF_SIGNALS)
    if one_off:
        return DocumentReuseDecision(
            decision="DISCARD",
            reuse="ONE_OFF",
            reason=f"检测到一次性 / 反向语义：{'、'.join(one_off[:3])}",
            confidence=0.9,
            source="rules",
            signals=one_off[:5],
        )

    frequent = _matched(text, _FREQUENT_SIGNALS)
    if frequent:
        return DocumentReuseDecision(
            decision="STORE",
            reuse="FREQUENT",
            reason=f"检测到长期复用语义：{'、'.join(frequent[:3])}",
            confidence=0.85,
            source="rules",
            signals=frequent[:5],
        )

    headings = len(_HEADING_RE.findall(content))
    business = _matched(content, _BUSINESS_TERMS)
    if len(content) >= 1200 and headings >= 2 and len(business) >= 3:
        return DocumentReuseDecision(
            decision="STORE",
            reuse="FREQUENT",
            reason=(
                f"文档结构较完整（{headings} 个标题、{len(business)} 个业务词），"
                "按长期需求文档处理"
            ),
            confidence=0.7,
            source="rules",
            signals=["结构化文档", *business[:4]],
        )

    return DocumentReuseDecision(
        decision="ASK",
        reuse="UNCERTAIN",
        reason="未检测到明确的长期复用或一次性语义",
        confidence=0.4,
        source="rules",
        signals=business[:4],
    )


def _llm_classify(
    name: str,
    content: str,
    *,
    project_summary: str,
    model_client: ModelClient,
) -> DocumentReuseDecision | None:
    system = (
        "你是文档库管理员。判断用户上传的文档是长期复用的规范/需求/手册，"
        "还是一次性草稿/临时参考。只输出 JSON 对象，不要 Markdown。"
        '格式：{"reuse":"FREQUENT|ONE_OFF|UNCERTAIN",'
        '"decision":"STORE|DISCARD|ASK",'
        '"reason":"不超过 60 字","confidence":0.0到1.0}'
    )
    human = (
        f"项目摘要：{project_summary[:1200] or '（无）'}\n\n"
        f"文档名：{name}\n\n"
        f"文档内容：\n{content[:4000]}"
    )
    try:
        raw = model_client.generate_json(task="summary", system=system, human=human)
        payload = json.loads(raw)
    except (ModelClientError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("document classify llm failed: %s", exc)
        return None

    reuse = str(payload.get("reuse") or "UNCERTAIN").upper()
    decision = str(payload.get("decision") or "ASK").upper()
    if reuse not in {"FREQUENT", "ONE_OFF", "UNCERTAIN"}:
        reuse = "UNCERTAIN"
    if decision not in {"STORE", "DISCARD", "ASK"}:
        decision = "ASK"
    try:
        confidence = max(0.0, min(1.0, float(payload.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    reason = str(payload.get("reason") or "模型判断为待确认")[:200]
    return DocumentReuseDecision(
        decision=decision,  # type: ignore[arg-type]
        reuse=reuse,  # type: ignore[arg-type]
        reason=reason,
        confidence=confidence,
        source="llm",
        signals=["llm"],
    )


def classify_document_reuse(
    name: str,
    content: str,
    *,
    project_summary: str = "",
    llm_provider: str = "auto",
    model_client: ModelClient | None = None,
) -> DocumentReuseDecision:
    """规则优先，模型兜底；模型不可用时回退 ASK。"""
    rule = _classify_by_rules(name, content)
    if rule.decision != "ASK":
        return rule

    provider = (llm_provider or "auto").strip().lower()
    if provider == "mock":
        return rule
    settings = get_settings()
    if not settings.deepseek_api_key:
        return rule

    client = model_client or DeepSeekClient(settings)
    llm = _llm_classify(
        name,
        content,
        project_summary=project_summary,
        model_client=client,
    )
    return llm or rule
