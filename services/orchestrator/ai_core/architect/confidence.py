"""应用形态判别与动态槽位置信度校验。

核心原则：
  - 先判断 app_shape，再决定需要哪些槽位；
  - 轻量 Web 不追问后端 / 数据库；
  - 脚本自动化不追问 UI；
  - 只有 full_stack_system 强制后端 + 数据库；
  - LLM 置信度必须经过确定性槽位校验封顶，不能虚高。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ai_core.architect.schemas import (
    AppShape,
    ArchitectAnalysis,
    ArchitectQuestion,
)

REQUIRED_SLOTS: dict[str, list[str]] = {
    "lightweight_web": ["ui.primary_flow"],
    "script_automation": [
        "script.runtime",
        "script.input",
        "script.output",
        "script.logic",
    ],
    "full_stack_system": [
        "backend.framework",
        "database.engine",
        "business.key_flows",
        "data.entities",
    ],
}

# 缺失槽位对应的置信度上限。设计目标是“缺关键信息绝不超过 0.85”，
# 但不同槽位对方案完整性的影响不同。
SLOT_CAPS: dict[str, float] = {
    "app.shape": 0.42,
    "ui.primary_flow": 0.72,
    "script.runtime": 0.66,
    "script.input": 0.70,
    "script.output": 0.70,
    "script.logic": 0.73,
    "backend.framework": 0.60,
    "database.engine": 0.65,
    "business.key_flows": 0.75,
    "data.entities": 0.70,
}

SHAPE_OPTIONS = [
    {
        "value": "lightweight_web",
        "label": "轻量网页工具（纯前端 / LocalStorage / Serverless）",
    },
    {
        "value": "script_automation",
        "label": "后台自动化脚本（Python / Node，无 UI）",
    },
    {
        "value": "full_stack_system",
        "label": "完整全栈系统（前端 + 后端 + 数据库）",
    },
]

APP_SHAPE_PROMPT = (
    "您是希望做一个只有网页特效的轻量工具，还是需要能保存数据的全栈系统，"
    "或者是一个后台自动运行的脚本？"
)


@dataclass(frozen=True)
class ConfidenceGuardResult:
    app_shape: str | None
    confidence_score: float
    cap: float
    missing_slots: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def is_known_shape(value: Any) -> bool:
    return value in REQUIRED_SLOTS


def required_slots(shape: str | None) -> list[str]:
    if not shape:
        return ["app.shape"]
    return list(REQUIRED_SLOTS.get(shape, []))


def missing_slots_for(shape: str | None, slots: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not is_known_shape(shape):
        return ["app.shape"]
    for slot in required_slots(shape):
        value = slots.get(slot)
        if value is None:
            missing.append(slot)
            continue
        if isinstance(value, str) and not value.strip():
            missing.append(slot)
            continue
        if isinstance(value, (list, tuple, dict)) and len(value) == 0:
            missing.append(slot)
    return missing


def apply_confidence_guard(
    analysis: ArchitectAnalysis,
    slots: dict[str, Any],
    threshold: float,
) -> tuple[ArchitectAnalysis, ConfidenceGuardResult]:
    """用动态槽位封顶 LLM 置信度，并补齐需要追问的槽位。"""
    shape = analysis.app_shape if is_known_shape(analysis.app_shape) else None
    merged_slots = {**slots, **analysis.slots}
    missing = missing_slots_for(shape, merged_slots)
    # LLM 自己报告的缺失槽位也要保留，但去重。
    for slot in analysis.missing_slots:
        if slot and slot not in missing:
            missing.append(slot)

    cap = 1.0
    reasons: list[str] = []
    for slot in missing:
        slot_cap = SLOT_CAPS.get(slot, 0.78)
        if slot_cap < cap:
            cap = slot_cap
            reasons.append(f"缺少 {slot}，置信度封顶 {slot_cap:.2f}")
    if analysis.assumptions and cap > 0.84:
        cap = 0.84
        reasons.append("存在未确认假设，置信度封顶 0.84")

    raw = max(0.0, min(1.0, float(analysis.confidence_score)))
    effective = min(raw, cap)
    result = ConfidenceGuardResult(
        app_shape=shape,
        confidence_score=effective,
        cap=cap,
        missing_slots=missing,
        reasons=reasons,
    )

    questions = _normalize_questions(analysis.questions, missing, shape)
    if effective < threshold and not questions:
        questions = fallback_questions(missing, shape, round_no=1)
    if effective < threshold and not questions:
        questions = [
            ArchitectQuestion(
                question_id="q_more_context",
                slot="general.context",
                type="free_text",
                prompt="还需要补充哪些关键信息，才能让方案可执行？",
                required=True,
            )
        ]

    updated = analysis.model_copy(
        update={
            "app_shape": shape,
            "confidence_score": effective,
            "missing_slots": missing,
            "slots": merged_slots,
            "questions": questions,
        }
    )
    return updated, result


def _normalize_questions(
    raw_questions: list[ArchitectQuestion],
    missing: list[str],
    shape: str | None,
) -> list[ArchitectQuestion]:
    """保留 LLM 问题并补齐未覆盖的关键槽位。"""
    questions: list[ArchitectQuestion] = []
    covered: set[str] = set()
    for index, question in enumerate(raw_questions[:3], start=1):
        slot = (question.slot or "").strip()
        prompt = (question.prompt or "").strip()
        if not prompt:
            continue
        if not slot:
            slot = missing[0] if missing else "general.context"
        if slot in covered:
            continue
        covered.add(slot)
        questions.append(
            question.model_copy(
                update={
                    "question_id": question.question_id or f"q_llm_{index}",
                    "slot": slot,
                    "prompt": prompt[:400],
                }
            )
        )
    for slot in missing:
        if len(questions) >= 3:
            break
        if slot in covered:
            continue
        template = question_for_slot(slot, shape)
        if template is not None:
            questions.append(template)
            covered.add(slot)
    return questions


def fallback_questions(
    missing: list[str],
    shape: str | None,
    *,
    round_no: int,
) -> list[ArchitectQuestion]:
    questions: list[ArchitectQuestion] = []
    for slot in missing[:3]:
        template = question_for_slot(slot, shape)
        if template is None:
            continue
        questions.append(
            template.model_copy(
                update={
                    "question_id": f"q_{slot.replace('.', '_')}_{round_no}",
                }
            )
        )
    if not questions and missing:
        questions.append(
            ArchitectQuestion(
                question_id=f"q_more_context_{round_no}",
                slot=missing[0],
                type="free_text",
                prompt="请补充这个方案的输入、处理过程和期望输出。",
                required=True,
            )
        )
    return questions


def question_for_slot(slot: str, shape: str | None = None) -> ArchitectQuestion | None:
    if slot == "app.shape":
        return ArchitectQuestion(
            question_id="q_app_shape",
            slot=slot,
            type="single_choice",
            prompt=APP_SHAPE_PROMPT,
            required=True,
            options=SHAPE_OPTIONS,
            default_value="lightweight_web",
        )
    if slot == "ui.primary_flow":
        return ArchitectQuestion(
            question_id="q_ui_primary_flow",
            slot=slot,
            type="free_text",
            prompt=(
                "这个轻量工具的核心操作流程是什么？"
                "例如：输入待办 -> 标记完成 -> 保存到浏览器。"
            ),
            required=True,
        )
    if slot == "script.runtime":
        return ArchitectQuestion(
            question_id="q_script_runtime",
            slot=slot,
            type="single_choice",
            prompt="脚本使用哪个运行环境？",
            required=True,
            options=[
                {"value": "python", "label": "Python 3.12"},
                {"value": "node", "label": "Node.js 20"},
            ],
            default_value="python",
        )
    if slot == "script.input":
        return ArchitectQuestion(
            question_id="q_script_input",
            slot=slot,
            type="free_text",
            prompt="脚本的输入是什么？例如：一个 CSV 文件、命令行参数或 HTTP API。",
            required=True,
        )
    if slot == "script.output":
        return ArchitectQuestion(
            question_id="q_script_output",
            slot=slot,
            type="free_text",
            prompt="脚本的期望输出是什么？例如：生成 JSON、写入文件或调用外部接口。",
            required=True,
        )
    if slot == "script.logic":
        return ArchitectQuestion(
            question_id="q_script_logic",
            slot=slot,
            type="free_text",
            prompt="脚本的核心处理逻辑和异常处理要求是什么？",
            required=True,
        )
    if slot == "backend.framework":
        return ArchitectQuestion(
            question_id="q_backend_framework",
            slot=slot,
            type="single_choice",
            prompt="后端希望使用哪种技术栈？",
            required=True,
            options=[
                {"value": "java_spring_boot", "label": "Java Spring Boot 3"},
                {"value": "node_nestjs", "label": "Node.js NestJS"},
                {"value": "python_fastapi", "label": "Python FastAPI"},
            ],
            default_value="java_spring_boot",
        )
    if slot == "database.engine":
        return ArchitectQuestion(
            question_id="q_database_engine",
            slot=slot,
            type="single_choice",
            prompt="数据库使用哪一种？",
            required=True,
            options=[
                {"value": "mysql", "label": "MySQL 8"},
                {"value": "postgresql", "label": "PostgreSQL"},
                {"value": "sqlite", "label": "SQLite（轻量部署）"},
            ],
            default_value="mysql",
        )
    if slot == "business.key_flows":
        return ArchitectQuestion(
            question_id="q_business_flows",
            slot=slot,
            type="free_text",
            prompt="关键业务动作是什么？例如：点击“提交”后创建订单并扣减库存。",
            required=True,
        )
    if slot == "data.entities":
        return ArchitectQuestion(
            question_id="q_data_entities",
            slot=slot,
            type="free_text",
            prompt="需要保存哪些数据实体？请列出核心字段和关联关系。",
            required=True,
        )
    if slot == "auth.required":
        return ArchitectQuestion(
            question_id="q_auth_required",
            slot=slot,
            type="single_choice",
            prompt="是否需要登录、权限或租户隔离？",
            required=False,
            options=[
                {"value": "yes", "label": "需要"},
                {"value": "no", "label": "不需要"},
            ],
            default_value="no",
        )
    return None
