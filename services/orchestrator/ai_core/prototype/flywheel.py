"""应用层飞轮原型：真实评测候选规则，不训练模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

BASELINE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("退款", "报销", "发票"), "财务"),
    (("打印机", "网络", "登录"), "IT"),
    (("投诉", "态度"), "客诉"),
)

CANDIDATE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("退款", "报销", "发票", "结算"), "财务"),
    (("打印机", "网络", "登录", "账号"), "IT"),
    (("投诉", "态度"), "客诉"),
)

FIXTURES: tuple[tuple[str, str], ...] = (
    ("退款迟迟未到账", "财务"),
    ("报销单需要审批", "财务"),
    ("打印机无法连接", "IT"),
    ("网络中断无法登录", "IT"),
    ("客户投诉客服态度", "客诉"),
    ("发票抬头开错了", "财务"),
    ("账号被冻结", "IT"),
    ("供应商结算差异", "财务"),
)


@dataclass(frozen=True)
class Candidate:
    prompt_version: str
    description: str
    rules: tuple[tuple[tuple[str, ...], str], ...]

    def to_dict(self) -> dict:
        return {
            "promptVersion": self.prompt_version,
            "description": self.description,
            "ruleCount": len(self.rules),
        }


def classify(rules: Iterable[tuple[tuple[str, ...], str]], text: str) -> str:
    for keywords, category in rules:
        if any(word in text for word in keywords):
            return category
    return "其他"


def accuracy(
    rules: Iterable[tuple[tuple[str, ...], str]],
    fixtures: Iterable[tuple[str, str]] = FIXTURES,
) -> dict:
    items = list(fixtures)
    hits = 0
    details = []
    for text, expected in items:
        actual = classify(rules, text)
        ok = actual == expected
        hits += int(ok)
        details.append(
            {
                "text": text,
                "expected": expected,
                "actual": actual,
                "ok": ok,
            }
        )
    score = hits / len(items) if items else 0.0
    return {
        "score": round(score, 4),
        "hits": hits,
        "total": len(items),
        "details": details,
    }


def run_flywheel_evaluation(*, threshold: float = 0.85) -> dict:
    """评测 baseline 与候选；只有超过阈值且优于 baseline 才允许灰度。"""
    baseline_rules = tuple(BASELINE_RULES)
    candidate = Candidate(
        prompt_version="B",
        description="补充账号 / 结算关键词",
        rules=tuple(CANDIDATE_RULES),
    )
    baseline = accuracy(baseline_rules)
    evaluated = accuracy(candidate.rules)
    if evaluated["score"] >= threshold > baseline["score"]:
        decision = "canary"
    elif evaluated["score"] <= baseline["score"]:
        decision = "reject"
    else:
        decision = "hold"
    return {
        "candidate": candidate.to_dict(),
        "baseline": baseline,
        "candidateEval": evaluated,
        "threshold": threshold,
        "decision": decision,
        "training": "not-performed",
        "rules": [asdict_candidate(candidate)],
    }


def asdict_candidate(candidate: Candidate) -> dict:
    return asdict(candidate)


__all__ = [
    "BASELINE_RULES",
    "CANDIDATE_RULES",
    "FIXTURES",
    "Candidate",
    "accuracy",
    "classify",
    "run_flywheel_evaluation",
]
