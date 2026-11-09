#!/usr/bin/env python3
"""Architect 置信度专项诊断：复现“多轮澄清后 CONFIDENCE_EXHAUSTED”。

模拟流程：
  1. 用户已有按钮源码，希望“点击后扣除库存并跳转；
  2. 第 1 轮补充：后端 Java Spring Boot，数据库 MySQL；
  3. 第 2 轮补充：核心实体是订单和商品表；
  4. 第 3 轮补充：不需要登录鉴权；
  5. 第 4 轮补充：API 风格用 RESTful。

每一轮都会打印：
  - LLM 原始 confidence_score / missing_slots；
  - 确定性封顶 cap（deterministic cap）及原因；
  - 合并后的 effective confidence 与真正缺失的槽位；
  - 系统本轮实际展示给用户的问题。

运行（推荐在 Python 容器内，依赖已安装）：
    docker exec vibe-python-ai python /app/scripts/architect_confidence_debug_test.py
    docker exec vibe-python-ai python /app/scripts/architect_confidence_debug_test.py --provider mock

在宿主机有独立 venv 时也可以直接运行：
    cd services/orchestrator && PYTHONPATH=. python ../../scripts/architect_confidence_debug_test.py
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# 兼容两种运行位置：仓库根目录（services/orchestrator）与容器内（/app）。
for candidate in (ROOT / "services" / "orchestrator", ROOT):
    if (candidate / "ai_core").is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from ai_core.agent.checkpoint import MemoryCheckpointerProvider  # noqa: E402
from ai_core.architect.confidence import (  # noqa: E402
    REQUIRED_SLOTS,
    apply_confidence_guard,
)
from ai_core.architect.graph import ArchitectGraphRunner  # noqa: E402
from ai_core.architect.llm import (  # noqa: E402
    ArchitectContext,
    ArchitectLLM,
    build_architect_llm,
)
from ai_core.config import get_settings  # noqa: E402
from ai_core.domain.architect import ArchitectAnalysis  # noqa: E402

SOURCE_APP = """export default function App() {
  return (
    <main>
      <h1>商品秒杀</h1>
      <button data-action=\"buy\">立即抢购</button>
    </main>
  );
}
"""

INITIAL_REQUEST = (
    "我在页面上画了一个“立即抢购”按钮，点击后需要扣除库存并跳转到订单页。"
    "下一步请帮我构建全栈方案。"
)

# 用户后续 4 轮的自然语言补充（模拟“补充说明”输入框）。
ROUND_MESSAGES = [
    "后端用 Java Spring Boot，数据库用 MySQL。",
    "核心实体是订单和商品表。",
    "不需要登录鉴权。",
    "API 风格用 RESTful。",
]


@dataclass
class DiagnosticRecord:
    """一次 analyze 调用的完整快照。"""

    round: int
    shape: str | None
    raw_confidence: float
    raw_missing: list[str]
    raw_slots: dict[str, Any]
    cap: float
    effective: float
    missing: list[str]
    reasons: list[str] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)


class DiagnosticLLM:
    """包装真实 / Mock LLM，在确定性封顶前截获原始分析结果。"""

    def __init__(self, inner: ArchitectLLM) -> None:
        self._inner = inner
        self.records: list[DiagnosticRecord] = []

    def analyze(self, context: ArchitectContext) -> ArchitectAnalysis:
        raw = self._inner.analyze(context)
        # 与 graph._architect_analyze 使用同一套封顶规则，保证日志可信。
        guarded, guard = apply_confidence_guard(
            raw,
            dict(context.slots),
            float(context.threshold),
        )
        self.records.append(
            DiagnosticRecord(
                round=int(context.round),
                shape=(
                    context.app_shape
                    or str(context.slots.get("app.shape") or "")
                    or raw.app_shape
                ),
                raw_confidence=float(raw.confidence_score),
                raw_missing=list(raw.missing_slots),
                raw_slots=dict(raw.slots),
                cap=float(guard.cap),
                effective=float(guarded.confidence_score),
                missing=list(guard.missing_slots),
                reasons=list(guard.reasons),
                questions=[
                    item.model_dump(by_alias=True) for item in guarded.questions
                ],
            )
        )
        return raw

    # 其余方法（draft_contract / revise_contract / summarize_history）直接透传。
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _initial_state(run_id: str, *, max_rounds: int) -> dict[str, Any]:
    """构造与生产路径一致的最小 ArchitectState。"""
    return {
        "run_id": run_id,
        "project_id": f"project_{run_id}",
        "project_name": "秒杀按钮全栈方案",
        "source_files": {"src/App.jsx": SOURCE_APP},
        "source_hash": "debug_source_hash",
        "source_origin": "request",
        "source_warnings": [],
        "ui_history": [{"role": "user", "content": INITIAL_REQUEST}],
        "threshold": 0.85,
        "max_rounds": max_rounds,
        "round": 0,
        "status": "INGESTING",
        "app_shape": "",
        "slots": {},
        "confidence_score": 0.0,
        "missing_slots": [],
        "questions": [],
        "analysis": {},
        "retrieved_chunks": [],
        "clarification": {},
        "contract": {},
        "contract_draft": {},
        "contract_issues": [],
        "contract_id": "",
        "contract_decision": "",
        "contract_feedback": "",
        "repair_count": 0,
        "repair_limit": 1,
        "contract_revision_count": 0,
        "asked_slots": [],
        "messages": [],
        "rag_provider": "markdown",
        "rag_top_k": 3,
        "error": "",
        "error_code": "",
    }


def _print_separator(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _print_record(record: DiagnosticRecord) -> None:
    """输出本轮最关键的四组信息。"""
    print(f"\n[Round {record.round}] LLM 原始分数: {record.raw_confidence:.2f}")
    print(f"  LLM 原始 missing_slots : {record.raw_missing or []}")
    print(
        f"  LLM 写入 slots        : {json.dumps(record.raw_slots, ensure_ascii=False)}"
    )
    print(f"  deterministic cap     : {record.cap:.2f}")
    print(f"  effective confidence  : {record.effective:.2f}")
    print(f"  合并后 missing_slots  : {record.missing or []}")

    required = set(REQUIRED_SLOTS.get(str(record.shape or ""), []))
    optional = [slot for slot in record.missing if slot not in required]
    if optional:
        print(f"  ⚠ 非核心槽位也在扣分   : {optional}")
    if record.reasons:
        print("  封顶原因:")
        for reason in record.reasons:
            print(f"    - {reason}")

    if record.effective < 0.85:
        if not record.missing:
            print("  卡点: 槽位已齐但 LLM 自报分数不足 0.85")
        else:
            labels = {
                "backend.framework": "缺后端框架",
                "database.engine": "缺数据库引擎",
                "business.key_flows": "缺核心业务流",
                "data.entities": "缺数据实体 / 表结构",
                "app.shape": "缺应用形态",
            }
            print(
                "  卡点: "
                + "；".join(labels.get(slot, f"缺 {slot}") for slot in record.missing)
            )
    else:
        print("  结果: 已达到阈值")


def _question_brief(question: dict[str, Any]) -> str:
    return f"[{question.get('slot')}] {question.get('prompt')}"


def _answer_shape(payload: dict[str, Any]) -> dict[str, Any]:
    question = (payload.get("questions") or [{}])[0]
    return {
        "answers": [
            {
                "questionId": question.get("questionId"),
                "value": "full_stack_system",
            }
        ]
    }


def _terminal_code(payload: dict[str, Any], state: dict[str, Any]) -> str:
    """把终态归一化为简短标记；非终态返回空串。"""
    if payload.get("kind") == "contract_confirmation":
        return "contract"
    status = str(state.get("status") or "")
    error_code = payload.get("errorCode") or state.get("error_code")
    if status in {"FAILED", "CANCELLED"} or error_code:
        error = payload.get("error") or state.get("error") or ""
        return f"status={status} errorCode={error_code} error={error}"
    return ""


def run_case(provider: str, *, max_rounds: int) -> int:
    """跑一遍完整流程，返回退出码（0 = 达到契约，2 = 置信度耗尽）。"""
    run_id = f"debug_{uuid.uuid4().hex[:10]}"
    inner = build_architect_llm(provider, get_settings())
    diag = DiagnosticLLM(inner)
    runner = ArchitectGraphRunner(
        diag,
        checkpointer_provider=MemoryCheckpointerProvider(),
    )

    _print_separator(f"PROVIDER = {provider}")
    outcome = runner.run(_initial_state(run_id, max_rounds=max_rounds))
    payload = outcome.interrupt or {}
    print(
        f"首轮状态: {outcome.state.get('status')} / "
        f"reason={payload.get('reason')} / questions={len(payload.get('questions') or [])}"
    )
    if not payload.get("questions"):
        print(f"未进入澄清，interrupt={payload}")
        return 1

    # 若 LLM 没有推断出应用形态，第 0 轮先选择全栈；若已推断则直接进入补充轮。
    if any(
        str(question.get("slot") or "") == "app.shape"
        for question in payload.get("questions") or []
    ):
        print("\n>>> 用户回答第 0 轮: full_stack_system")
        outcome = runner.run({}, run_id=run_id, resume=_answer_shape(payload))
        payload = outcome.interrupt or {}
        state = outcome.state
        if diag.records:
            _print_record(diag.records[-1])
        questions = payload.get("questions") or []
        if questions:
            print("  本轮问题:")
            for question in questions:
                print(f"    - {_question_brief(question)}")
        terminal = _terminal_code(payload, state)
        if terminal == "contract":
            print("\n✅ 已生成契约，未出现 CONFIDENCE_EXHAUSTED")
            return 0
        if terminal:
            print(f"\n❌ 终止: {terminal}")
            return 2
    else:
        print(
            f"\n（LLM 已从初始需求推断应用形态: "
            f"{outcome.state.get('app_shape') or '未知'}，跳过形态选择）"
        )

    # 之后用“补充说明”模拟 4 轮自由输入。
    for index, message in enumerate(ROUND_MESSAGES, start=1):
        print(f"\n>>> 用户补充第 {index} 轮: {message}")
        outcome = runner.run({}, run_id=run_id, resume={"comment": message})
        payload = outcome.interrupt or {}
        state = outcome.state

        if not diag.records:
            print("  （本轮未调用 analyze）")
        else:
            _print_record(diag.records[-1])

        questions = payload.get("questions") or []
        if questions:
            print("  本轮问题:")
            for question in questions:
                print(f"    - {_question_brief(question)}")

        terminal = _terminal_code(payload, state)
        if terminal == "contract":
            print("\n✅ 已生成契约，未出现 CONFIDENCE_EXHAUSTED")
            return 0
        if terminal:
            print(f"\n❌ 终止: {terminal}")
            return 2

    print("\n⚠ 流程结束但既未生成契约也未显式失败，请检查 max_rounds / graph 路由")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Architect 置信度诊断")
    parser.add_argument(
        "--provider",
        default="both",
        choices=("mock", "deepseek", "auto", "both"),
        help="both = 依次运行 mock 与真实 DeepSeek（auto 有 Key 时走真实）",
    )
    parser.add_argument("--max-rounds", type=int, default=5)
    args = parser.parse_args()

    providers = ["mock", "deepseek"] if args.provider == "both" else [args.provider]
    codes: list[int] = []
    for provider in providers:
        try:
            codes.append(run_case(provider, max_rounds=args.max_rounds))
        except Exception as exc:  # noqa: BLE001 - 诊断脚本要打印完整失败原因
            print(f"\n💥 provider={provider} 运行异常: {type(exc).__name__}: {exc}")
            codes.append(1)
    return 0 if codes and all(code == 0 for code in codes) else 2


if __name__ == "__main__":
    raise SystemExit(main())
