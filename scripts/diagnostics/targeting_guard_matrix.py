#!/usr/bin/env python3
"""实例作用域守卫（_instance_scope_violation）取证矩阵。

不修改业务代码：直接导入真实的 graph 模块，构造与
``reports/diagnostics/targeting-probe-baseline2.json`` 中一致的
``target.source`` 信封，逐条调用 Applier 写盘前的守卫，记录：
  - 是否拦截（error_code）
  - 是否与安全预期一致（blocked / bypassed / false_positive）

运行（在 vibe-python-ai 容器内）：
    docker exec vibe-python-ai python /app/scripts/diagnostics/targeting_guard_matrix.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ai_core.agent import graph

APP_FILE = "src/App.jsx"
CARD_FILE = "src/components/Card.jsx"

APP_SOURCE = """import Card from './components/Card.jsx'

// 受控夹具：3 个同构卡片用 map 渲染，用于验证实例级定位。
const CARDS = [
  { id: 'card-1', title: '营收概览', value: '1,240,000' },
  { id: 'card-2', title: '活跃用户', value: '8,420' },
  { id: 'card-3', title: '转化率', value: '3.8%' },
]

export default function App() {
  return (
    <main className="dashboard">
      <header className="dashboard-header">
        <h1>运营看板</h1>
        <button type="button" data-vibe-target="header-action">
          新建报表
        </button>
      </header>
      <section className="card-grid">
        {CARDS.map((card) => (
          <Card key={card.id} card={card} />
        ))}
      </section>
    </main>
  )
}
"""

CARD_SOURCE = """export default function Card({ card }) {
  return (
    <article className="card" data-vibe-target="card">
      <h2 className="card-title">{card.title}</h2>
      <p className="card-value">{card.value}</p>
      <button type="button" className="card-action" data-vibe-target="card-action">
        查看详情
      </button>
    </article>
  )
}
"""

# 真实 SDK 信封（baseline2 取证）。card-2 是 map 渲染的第 2 个实例。
CARD_SOURCE_ENVELOPE = {
    "file": CARD_FILE,
    "line": 6,
    "column": 5,
    "component": "Card",
    "sourceKind": "element",
    "tag": "button",
    "instance": 2,
    "instanceCount": 3,
    "instanceId": f"{CARD_FILE}:6:5#2",
    "instanceKey": "card-2",
    "instanceProps": {"card.id": "card-2", "card.title": "活跃用户"},
    "callSite": {
        "file": APP_FILE,
        "line": 21,
        "column": 9,
        "component": "Card",
        "instanceKey": "card-2",
        "instanceProps": {"card.id": "card-2", "card.title": "活跃用户"},
    },
}

# 唯一按钮：不在可复用组件内，instanceCount=1，无 callSite。
UNIQUE_SOURCE_ENVELOPE = {
    "file": APP_FILE,
    "line": 15,
    "column": 7,
    "component": "App",
    "sourceKind": "element",
    "tag": "button",
    "instance": 1,
    "instanceCount": 1,
    "instanceId": f"{APP_FILE}:15:7#1",
    "instanceKey": "",
    "instanceProps": {},
    "callSite": None,
}


def replace_once(text: str, old: str, new: str) -> str:
    if old not in text:
        raise AssertionError(f"fixture 文本不匹配: {old!r}")
    return text.replace(old, new, 1)


MAP_BLOCK = """        {CARDS.map((card) => (
          <Card key={card.id} card={card} />
        ))}"""

GUARDED_MAP = """        {CARDS.map((card) => (
          <div key={card.id} className="card-slot">
            <Card card={card} />
            {card.id === 'card-2' && (
              <button type="button">详情</button>
            )}
          </div>
        ))}"""

UNGUARDED_MAP = """        {CARDS.map((card) => (
          <div key={card.id} className="card-slot">
            <Card card={card} />
            <button type="button">详情</button>
          </div>
        ))}"""

UNRELATED_GUARD_MAP = """        {CARDS.map((card) => {
          const highlighted = card.id === 'card-2'
          return (
            <div key={card.id} className="card-slot">
              <Card card={card} />
              <button type="button">详情</button>
              {highlighted ? null : null}
            </div>
          )
        })}"""

FOURTH_CARD = """  { id: 'card-4', title: '新增指标', value: '99' },
"""


def annotation(
    source: dict[str, Any], action: str = "MODIFY_EXISTING"
) -> dict[str, Any]:
    return {
        "version": 1,
        "annotationId": "ann_guard_matrix",
        "type": "visual-draw",
        "action": action,
        "target": {"mode": "source", "source": source},
        "intent": {"kind": "UI_ONLY", "ui": {"summary": "诊断用例"}},
    }


def evaluate(
    source: dict[str, Any],
    *,
    files: dict[str, str] | None = None,
    patch: str | None = None,
) -> dict[str, Any]:
    state = {
        "annotation_context": {"annotations": [{"annotation": annotation(source)}]},
        "current_files": {APP_FILE: APP_SOURCE, CARD_FILE: CARD_SOURCE},
        "target_files": [APP_FILE, CARD_FILE],
    }
    result = graph._instance_scope_violation(state, files or {}, patch)
    return {
        "decision": "BLOCK" if result else "PASS",
        "errorCode": (result or {}).get("error_code"),
        "message": ((result or {}).get("verify_result") or {}).get("error_logs"),
    }


def app_with(new_map: str) -> str:
    return replace_once(APP_SOURCE, MAP_BLOCK, new_map)


def diff_for(old: str, new: str, path: str) -> str:
    import difflib

    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def build_cases() -> list[dict[str, Any]]:
    """每条用例：name / expected / source / files|patch / note。"""
    guarded_app = app_with(GUARDED_MAP)
    unguarded_app = app_with(UNGUARDED_MAP)
    unrelated_guard_app = app_with(UNRELATED_GUARD_MAP)
    fourth_card_app = replace_once(
        APP_SOURCE, "  { id: 'card-3'", FOURTH_CARD + "  { id: 'card-3'"
    )
    truncated_app = "export default function App() {\n  return <main>精简</main>\n}\n"
    card_text_app = replace_once(APP_SOURCE, "新建报表", "新建")

    return [
        {
            "name": "base_component_files",
            "note": "直接改基础组件 Card.jsx（files 模式），应拦截",
            "expected": "BLOCK",
            "source": CARD_SOURCE_ENVELOPE,
            "files": {CARD_FILE: replace_once(CARD_SOURCE, "查看详情", "查看")},
        },
        {
            "name": "base_component_diff",
            "note": "直接改基础组件 Card.jsx（diff 模式），应拦截",
            "expected": "BLOCK",
            "source": CARD_SOURCE_ENVELOPE,
            "patch": diff_for(
                CARD_SOURCE,
                replace_once(CARD_SOURCE, "查看详情", "查看"),
                CARD_FILE,
            ),
        },
        {
            "name": "callsite_guarded_files",
            "note": "只改 callSite 父级，并用 card.id==='card-2' 守卫，应放行",
            "expected": "PASS",
            "source": CARD_SOURCE_ENVELOPE,
            "files": {APP_FILE: guarded_app},
        },
        {
            "name": "callsite_unguarded_files",
            "note": "在 map 返回体里无条件加按钮，应拦截",
            "expected": "BLOCK",
            "source": CARD_SOURCE_ENVELOPE,
            "files": {APP_FILE: unguarded_app},
        },
        {
            "name": "callsite_unguarded_diff",
            "note": "同上，但 Coder 走 diff 模式",
            "expected": "BLOCK",
            "source": CARD_SOURCE_ENVELOPE,
            "patch": diff_for(APP_SOURCE, unguarded_app, APP_FILE),
        },
        {
            "name": "unrelated_guard_false_pass",
            "note": "无条件加按钮，但在别处写了一句 card.id==='card-2'，疑似字符串蒙混",
            "expected": "BLOCK",
            "source": CARD_SOURCE_ENVELOPE,
            "files": {APP_FILE: unrelated_guard_app},
        },
        {
            "name": "far_from_callsite",
            "note": "改 CARDS 数据（影响全部实例），但距 callSite 16 行，超出 ±4 窗口",
            "expected": "BLOCK",
            "source": CARD_SOURCE_ENVELOPE,
            "files": {APP_FILE: fourth_card_app},
        },
        {
            "name": "unique_target_far_edit",
            "note": "唯一按钮目标（instanceCount=1）修改顶部文案，本应放行",
            "expected": "PASS",
            "source": UNIQUE_SOURCE_ENVELOPE,
            "files": {APP_FILE: card_text_app},
        },
        {
            "name": "unique_file_truncation",
            "note": "唯一目标 + files 模式把 26 行文件截断为 3 行，当前无删除门禁",
            "expected": "BLOCK",
            "source": UNIQUE_SOURCE_ENVELOPE,
            "files": {APP_FILE: truncated_app},
        },
        {
            "name": "missing_callsite_bypass",
            "note": "callSite 缺失时，守卫把 Card.jsx:6 当成 App.jsx 的行号，map 里的无条件新增逃逸",
            "expected": "BLOCK",
            "source": {
                k: v for k, v in CARD_SOURCE_ENVELOPE.items() if k != "callSite"
            },
            "files": {APP_FILE: unguarded_app},
        },
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/tmp/targeting-guard-matrix.json")
    args = parser.parse_args()

    rows: list[dict[str, Any]] = []
    for case in build_cases():
        result = evaluate(
            case["source"],
            files=case.get("files"),
            patch=case.get("patch"),
        )
        if result["decision"] == case["expected"]:
            verdict = "OK"
        elif result["decision"] == "PASS":
            verdict = "BYPASS"
        else:
            verdict = "FALSE_POSITIVE"
        row = {
            "name": case["name"],
            "note": case["note"],
            "expected": case["expected"],
            **result,
            "verdict": verdict,
        }
        rows.append(row)
        print(
            f"[{verdict:6}] {case['name']}: expected={case['expected']} "
            f"actual={result['decision']} code={result['errorCode'] or '-'}"
        )
        print(f"         {case['note']}")
        if result["message"]:
            print(f"         -> {result['message'][:180]}")

    summary = {
        "total": len(rows),
        "ok": sum(1 for row in rows if row["verdict"] == "OK"),
        "bypass": sum(1 for row in rows if row["verdict"] == "BYPASS"),
        "falsePositive": sum(1 for row in rows if row["verdict"] == "FALSE_POSITIVE"),
        "blockRate": round(
            sum(1 for row in rows if row["decision"] == "BLOCK") / len(rows), 4
        ),
    }
    print("\nSUMMARY " + json.dumps(summary, ensure_ascii=False))
    Path(args.out).write_text(
        json.dumps({"summary": summary, "cases": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"report={args.out}")
    # 出现 BYPASS 时返回非零，方便 CI 感知，但诊断阶段不据此中断。
    return 0 if summary["bypass"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
