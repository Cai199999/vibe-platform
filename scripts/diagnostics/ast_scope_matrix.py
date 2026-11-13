#!/usr/bin/env python3
"""AST 实例门禁取证矩阵（不启动 AI Core）。

把 before / after 夹具与 spec.json 复制进一个真实沙盒容器，用容器内
Babel 运行 ``ast_scope_check.mjs``，逐条打印 PASS / BLOCK。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
APP_FILE = "src/App.jsx"
CARD_FILE = "src/components/Card.jsx"

APP_SOURCE = """import Card from './components/Card.jsx'

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

FOURTH_CARD = "  { id: 'card-4', title: '新增指标', value: '99' },\n"

CARD_SCOPE = {
    "annotationId": "ann_ast",
    "action": "MODIFY_EXISTING",
    "file": CARD_FILE,
    "sourceLine": 6,
    "component": "Card",
    "instance": 2,
    "instanceCount": 3,
    "instanceKey": "card-2",
    "instanceProps": {"card.id": "card-2", "card.title": "活跃用户"},
    "callSiteFile": APP_FILE,
    "callSiteLine": 21,
    "callSiteComponent": "Card",
    "protectBase": True,
    "requireGuard": True,
}


def replace_once(text: str, old: str, new: str) -> str:
    if old not in text:
        raise AssertionError(f"fixture 文本不匹配: {old!r}")
    return text.replace(old, new, 1)


def cases() -> list[dict[str, Any]]:
    guarded = replace_once(APP_SOURCE, MAP_BLOCK, GUARDED_MAP)
    unguarded = replace_once(APP_SOURCE, MAP_BLOCK, UNGUARDED_MAP)
    unrelated = replace_once(APP_SOURCE, MAP_BLOCK, UNRELATED_GUARD_MAP)
    fourth = replace_once(
        APP_SOURCE, "  { id: 'card-3'", FOURTH_CARD + "  { id: 'card-3'"
    )
    header_text = replace_once(APP_SOURCE, "新建报表", "新建")
    base_text = replace_once(CARD_SOURCE, "查看详情", "查看")
    missing_scope = {
        k: v for k, v in CARD_SCOPE.items() if k not in {"callSiteFile", "callSiteLine"}
    }
    return [
        {
            "name": "guarded_map",
            "expected": "PASS",
            "before": {APP_FILE: APP_SOURCE, CARD_FILE: CARD_SOURCE},
            "after": {APP_FILE: guarded, CARD_FILE: CARD_SOURCE},
            "scopes": [CARD_SCOPE],
        },
        {
            "name": "unguarded_map",
            "expected": "BLOCK",
            "before": {APP_FILE: APP_SOURCE, CARD_FILE: CARD_SOURCE},
            "after": {APP_FILE: unguarded, CARD_FILE: CARD_SOURCE},
            "scopes": [CARD_SCOPE],
        },
        {
            "name": "unrelated_guard",
            "expected": "BLOCK",
            "before": {APP_FILE: APP_SOURCE, CARD_FILE: CARD_SOURCE},
            "after": {APP_FILE: unrelated, CARD_FILE: CARD_SOURCE},
            "scopes": [CARD_SCOPE],
        },
        {
            "name": "base_component_changed",
            "expected": "BLOCK",
            "before": {APP_FILE: APP_SOURCE, CARD_FILE: CARD_SOURCE},
            "after": {APP_FILE: APP_SOURCE, CARD_FILE: base_text},
            "scopes": [CARD_SCOPE],
        },
        {
            "name": "shared_data_changed",
            "expected": "BLOCK",
            "before": {APP_FILE: APP_SOURCE, CARD_FILE: CARD_SOURCE},
            "after": {APP_FILE: fourth, CARD_FILE: CARD_SOURCE},
            "scopes": [CARD_SCOPE],
        },
        {
            "name": "missing_callsite_unguarded",
            "expected": "BLOCK",
            "before": {APP_FILE: APP_SOURCE, CARD_FILE: CARD_SOURCE},
            "after": {APP_FILE: unguarded, CARD_FILE: CARD_SOURCE},
            "scopes": [missing_scope],
        },
        {
            "name": "unrelated_header_edit",
            "expected": "PASS",
            "before": {APP_FILE: APP_SOURCE, CARD_FILE: CARD_SOURCE},
            "after": {APP_FILE: header_text, CARD_FILE: CARD_SOURCE},
            "scopes": [CARD_SCOPE],
        },
        {
            "name": "guarded_reformat_only",
            "expected": "PASS",
            "before": {APP_FILE: APP_SOURCE, CARD_FILE: CARD_SOURCE},
            "after": {
                APP_FILE: APP_SOURCE.replace("  return (\n", "  return (\n\n"),
                CARD_FILE: CARD_SOURCE,
            },
            "scopes": [CARD_SCOPE],
        },
    ]


def write_case(root: Path, case: dict[str, Any]) -> Path:
    case_dir = root / case["name"]
    before = case_dir / "before"
    after = case_dir / "after"
    for base, files in ((before, case["before"]), (after, case["after"])):
        for rel, content in files.items():
            target = base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    spec = {
        "workdir": "/app",
        "beforeRoot": "/tmp/ast-scope-matrix/" + case["name"] + "/before",
        "afterRoot": "/tmp/ast-scope-matrix/" + case["name"] + "/after",
        "changedFiles": sorted(set(case["after"]) | set(case["before"])),
        "scopes": case["scopes"],
    }
    (case_dir / "spec.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return case_dir


def docker_exec(container: str, command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", container, *command],
        capture_output=True,
        text=True,
        timeout=120,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", default="vibe-sandbox-sbx_d7cee14780904abb82a4")
    parser.add_argument(
        "--out", default=str(ROOT / "reports/diagnostics/ast-scope-matrix.json")
    )
    args = parser.parse_args()

    temp = Path(tempfile.mkdtemp(prefix="ast-scope-"))
    try:
        case_dirs = [write_case(temp, case) for case in cases()]
        # 沙盒容器 cap_drop=ALL，root 没有 DAC_OVERRIDE；docker cp 会保留
        # 宿主机 uid/mode，必须提前放宽到 world-readable。
        temp.chmod(0o755)
        for path in temp.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        docker_exec(args.container, ["rm", "-rf", "/tmp/ast-scope-matrix"])
        subprocess.run(
            [
                "docker",
                "cp",
                str(temp) + "/.",
                f"{args.container}:/tmp/ast-scope-matrix",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        # docker cp 以容器 USER（node）创建目录，默认 700；root 因
        # cap_drop=ALL 不能 DAC override，必须由 node 自己放宽权限。
        subprocess.run(
            [
                "docker",
                "exec",
                "-u",
                "node",
                args.container,
                "chmod",
                "-R",
                "a+rX",
                "/tmp/ast-scope-matrix",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "docker",
                "cp",
                str(
                    ROOT / "services/orchestrator/sandbox_template/ast_scope_check.mjs"
                ),
                f"{args.container}:/opt/vibe-ast-scope-check.mjs",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        rows = []
        for case, case_dir in zip(cases(), case_dirs):
            spec_path = f"/tmp/ast-scope-matrix/{case['name']}/spec.json"
            result = docker_exec(
                args.container, ["node", "/opt/vibe-ast-scope-check.mjs", spec_path]
            )
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                payload = {"ok": False, "violations": [], "raw": result.stdout}
            actual = "PASS" if payload.get("ok") else "BLOCK"
            row = {
                "name": case["name"],
                "expected": case["expected"],
                "actual": actual,
                "exitCode": result.returncode,
                "violations": payload.get("violations", []),
                "stderr": result.stderr.strip(),
                "verdict": "OK" if actual == case["expected"] else "MISMATCH",
            }
            rows.append(row)
            print(
                f"[{row['verdict']:8}] {case['name']}: expected={case['expected']} "
                f"actual={actual} exit={result.returncode}"
            )
            for violation in row["violations"]:
                print(
                    f"    {violation.get('code')} {violation.get('file')}:"
                    f"{violation.get('line')} -> {violation.get('message')[:160]}"
                )
            if result.stderr.strip():
                print("    stderr:", result.stderr.strip()[:300])
            case_dir.joinpath("result.json").write_text(
                json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        summary = {
            "total": len(rows),
            "ok": sum(1 for row in rows if row["verdict"] == "OK"),
            "mismatch": sum(1 for row in rows if row["verdict"] != "OK"),
        }
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(
                {"summary": summary, "cases": rows}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        print("SUMMARY", json.dumps(summary, ensure_ascii=False))
        return 0 if summary["mismatch"] == 0 else 1
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
