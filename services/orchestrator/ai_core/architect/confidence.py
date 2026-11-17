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

from ai_core.domain.architect import (
    AppShape,
    ArchitectAnalysis,
    ArchitectQuestion,
)

REQUIRED_SLOTS: dict[str, list[str]] = {
    "lightweight_web": ["ui.primary_flow", "ui.layout"],
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
        "ui.layout",
    ],
}

# 缺失槽位对应的置信度上限。设计目标是“缺关键信息绝不超过 0.85”，
# 但不同槽位对方案完整性的影响不同。
SLOT_CAPS: dict[str, float] = {
    "app.shape": 0.42,
    "ui.primary_flow": 0.72,
    "ui.layout": 0.70,
    "script.runtime": 0.66,
    "script.input": 0.70,
    "script.output": 0.70,
    "script.logic": 0.73,
    "backend.framework": 0.60,
    "database.engine": 0.65,
    "business.key_flows": 0.75,
    "data.entities": 0.70,
}

# 对话前置模式不再提供「全栈 / 轻量 / 脚本」三选一。应用形态由
# slots.infer_app_shape 从自然语言目标推断；只有完全无法判断时，才用
# 自由文本询问用户群体与核心功能，由下一轮对话继续推断。
APP_SHAPE_PROMPT = (
    "请描述这个应用面向谁、核心要解决什么问题，以及是否需要多人协作、"
    "登录或长期保存数据。"
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


def required_slots(
    shape: str | None,
    project_mode: str = "NEW_PROJECT",
    output_phase: str = "FULL",
) -> list[str]:
    if not shape:
        return ["app.shape"]
    slots = list(REQUIRED_SLOTS.get(shape, []))
    # V0 只关心页面原型；后端槽位留到 BACKEND_BINDING 阶段。
    if output_phase == "VISUAL_V0":
        return [slot for slot in slots if slot in {"ui.layout", "ui.primary_flow"}]
    # 存量项目 / 后端绑定阶段已有真实页面：禁止再追问 ui.layout。
    if project_mode == "EXISTING_SOURCE" or output_phase == "BACKEND_BINDING":
        slots = [slot for slot in slots if slot != "ui.layout"]
    return slots


def slot_is_filled(value: Any) -> bool:
    """槽位是否有可用值：None / 空白字符串 / 空集合都视为未填写。"""
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    if isinstance(value, (list, tuple, dict)) and len(value) == 0:
        return False
    return True


def missing_slots_for(
    shape: str | None,
    slots: dict[str, Any],
    project_mode: str = "NEW_PROJECT",
    output_phase: str = "FULL",
) -> list[str]:
    if not is_known_shape(shape):
        return ["app.shape"]
    return [
        slot
        for slot in required_slots(shape, project_mode, output_phase)
        if not slot_is_filled(slots.get(slot))
    ]


def apply_confidence_guard(
    analysis: ArchitectAnalysis,
    slots: dict[str, Any],
    threshold: float,
    project_mode: str = "NEW_PROJECT",
    output_phase: str = "FULL",
) -> tuple[ArchitectAnalysis, ConfidenceGuardResult]:
    """用动态槽位封顶 LLM 置信度，并补齐需要追问的槽位。

    只有当前应用形态的「必需槽位」才参与封顶与阻塞。LLM 自报的
    ``auth.required`` / ``api.style`` 等可选槽位可以出现在问题里，但绝不能
    把置信度压到 0.78 并导致 CONFIDENCE_EXHAUSTED——用户明确说“不需要
    登录”时，系统不应继续扣分。
    """
    shape = analysis.app_shape if is_known_shape(analysis.app_shape) else None
    merged_slots = {**slots, **analysis.slots}
    required = set(required_slots(shape, project_mode, output_phase))
    missing = missing_slots_for(shape, merged_slots, project_mode, output_phase)
    # LLM 自报的缺失槽位只保留必需项；可选槽位不参与封顶和路由。
    for slot in analysis.missing_slots:
        if slot in required and slot not in missing:
            missing.append(slot)

    cap = 1.0
    reasons: list[str] = []
    for slot in missing:
        # 必需槽位都有明确上限；未知槽位不应出现，兜底也保持可完成。
        slot_cap = SLOT_CAPS.get(slot, 0.80)
        if slot_cap < cap:
            cap = slot_cap
            reasons.append(f"缺少 {slot}，置信度封顶 {slot_cap:.2f}")
    # 假设（assumptions）是正常交付的一部分：用户给出默认技术栈后，LLM
    # 会把默认字段写进 assumptions。此时不应把分数压到阈值以下，否则
    # 槽位已齐却仍被迫继续澄清。只在“仍有必需槽位缺失”时保留 0.84 上限，
    # 其余情况用 0.90 上限提示“可交付但仍有假设”。
    assumption_cap = 0.84 if missing else 0.90
    if analysis.assumptions and cap > assumption_cap:
        cap = assumption_cap
        reasons.append(f"存在未确认假设，置信度封顶 {assumption_cap:.2f}")

    raw = max(0.0, min(1.0, float(analysis.confidence_score)))
    effective = min(raw, cap)
    result = ConfidenceGuardResult(
        app_shape=shape,
        confidence_score=effective,
        cap=cap,
        missing_slots=missing,
        reasons=reasons,
    )

    # 必需槽位优先：LLM 的“锦上添花”问题不能挤掉真正阻塞的必需槽位。
    questions = _normalize_questions(
        analysis.questions,
        missing,
        shape,
        filled=merged_slots,
        project_mode=project_mode,
        output_phase=output_phase,
    )
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
    *,
    filled: dict[str, Any] | None = None,
    project_mode: str = "NEW_PROJECT",
    output_phase: str = "FULL",
) -> list[ArchitectQuestion]:
    """优先补齐必需槽位，再补充 LLM 的可选问题。

    旧实现先塞满 LLM 的 3 个问题，导致 ``data.entities`` 这类必需槽位
    永远排不进去；用户连续几轮都在回答“登录 / API 风格”等可选问题，
    必需槽位却一直缺失，最终 CONFIDENCE_EXHAUSTED。
    """
    filled = filled or {}
    # 质量分级是契约确认 UI 的一次性决策，不是每轮澄清问题。
    raw_questions = [
        question
        for question in raw_questions
        if str(question.slot or "").strip().lower()
        not in {"quality", "quality.tier", "quality_tier", "quality_level"}
        and not (
            "L1" in str(question.prompt or "")
            and "L2" in str(question.prompt or "")
            and "L3" in str(question.prompt or "")
        )
    ]
    # 已填写的必需槽位不再重复追问；空字符串 / 空集合视为未填写。
    filled_required = {
        slot
        for slot in required_slots(shape, project_mode, output_phase)
        if slot_is_filled(filled.get(slot))
    }

    # 先把 LLM 问题按槽位归一化，避免同一槽位重复提问。
    llm_questions: dict[str, ArchitectQuestion] = {}
    extras: list[ArchitectQuestion] = []
    for index, question in enumerate(raw_questions, start=1):
        slot = (question.slot or "").strip()
        prompt = (question.prompt or "").strip()
        if not prompt:
            continue
        if not slot:
            slot = missing[0] if missing else "general.context"
        normalized = question.model_copy(
            update={
                "question_id": question.question_id or f"q_llm_{index}",
                "slot": slot,
                "prompt": prompt[:400],
            }
        )
        if slot in missing:
            llm_questions.setdefault(slot, normalized)
        elif slot not in required_slots(
            shape, project_mode, output_phase
        ) and slot not in {item.slot for item in extras}:
            # 必需槽位要么已填、要么在 missing 中；不再作为“可选问题”重复问。
            extras.append(normalized)

    questions: list[ArchitectQuestion] = []
    covered: set[str] = set()
    # 1) 必需槽位：优先使用 LLM 的定制问题，否则用模板兜底。
    for slot in missing:
        if len(questions) >= 2:
            break
        if slot in filled_required or slot in covered:
            continue
        question = llm_questions.get(slot) or question_for_slot(slot, shape)
        if question is None:
            continue
        questions.append(question)
        covered.add(slot)
    # 2) 仍有空位时，允许 LLM 追问可选信息（最多 3 个问题）。
    for question in extras:
        if len(questions) >= 2:
            break
        if question.slot in covered:
            continue
        questions.append(question)
        covered.add(question.slot)
    return questions


def fallback_questions(
    missing: list[str],
    shape: str | None,
    *,
    round_no: int,
) -> list[ArchitectQuestion]:
    questions: list[ArchitectQuestion] = []
    for slot in missing[:2]:
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
            type="free_text",
            prompt=APP_SHAPE_PROMPT,
            required=True,
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
    if slot == "ui.layout":
        return ArchitectQuestion(
            question_id="q_ui_layout",
            slot=slot,
            type="single_choice",
            prompt=(
                "页面的基础版式希望采用哪一种？后续骨架会按 Grid / Flex "
                "栅格化生成，避免所有组件垂直堆叠。"
            ),
            required=True,
            options=[
                {
                    "value": "sidebar_main",
                    "label": "A. 左侧边栏 + 主内容区（适合管理台）",
                },
                {
                    "value": "topnav_main",
                    "label": "B. 顶部导航 + 主内容区（适合工具页）",
                },
                {
                    "value": "topnav_cards",
                    "label": "C. 顶部导航 + 响应式三列卡片（适合列表 / 看板）",
                },
            ],
            default_value="topnav_cards",
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
            prompt=(
                "数据库使用哪一种？SQLite 仅用于本地测试；"
                "需要部署上线时请选择 MySQL 或 PostgreSQL。"
            ),
            required=True,
            options=[
                {"value": "mysql", "label": "MySQL 8（用于生产部署）"},
                {"value": "postgresql", "label": "PostgreSQL（用于生产部署）"},
                {"value": "sqlite", "label": "SQLite（仅供本地测试）"},
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
