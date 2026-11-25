"""原型运行与验收：真实执行生成物，不做假断言。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


class RuntimeCheckError(RuntimeError):
    """生成物未通过运行验收。"""


def run_migrations(workspace: Path, db_path: Path) -> list[str]:
    proc = subprocess.run(
        [
            sys.executable,
            "db/migrate.py",
            "--db",
            str(db_path),
            "--migrations",
            "db/migrations",
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeCheckError(f"迁移失败: {proc.stderr[-600:]}")
    return list(json.loads(proc.stdout).get("applied") or [])


def schema_columns(db_path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    return [str(row[1]) for row in rows]


def applied_migrations(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        conn.close()
    return [str(row[0]) for row in rows]


def run_consumer(
    workspace: Path,
    db_path: Path,
    event_path: Path,
    agent_path: Path,
) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            "services/consumer/consumer.py",
            "--db",
            str(db_path),
            "--event",
            str(event_path),
            "--agent",
            str(agent_path),
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(workspace)},
    )
    if proc.returncode != 0:
        raise RuntimeCheckError(f"消费者执行失败: {proc.stderr[-600:]}")
    return dict(json.loads(proc.stdout))


def ticket_row(db_path: Path, ticket_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, title, status, ai_category, ai_confidence, ai_suggestion "
            "FROM tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0]
    finally:
        conn.close()
    if row is None:
        raise RuntimeCheckError(f"工单不存在: {ticket_id}")
    return {
        "id": row[0],
        "title": row[1],
        "status": row[2],
        "aiCategory": row[3],
        "aiConfidence": row[4],
        "aiSuggestion": row[5],
        "processedEvents": count,
    }


def node_syntax_check(workspace: Path) -> dict:
    node = shutil.which("node")
    if not node:
        return {"ran": False, "reason": "node 不可用"}
    proc = subprocess.run(
        [node, "--check", "apps/api/server.mjs"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeCheckError(f"Node 语法检查失败: {proc.stderr[-400:]}")
    return {"ran": True, "ok": True}


def write_event(workspace: Path, filename: str, payload: dict) -> Path:
    path = workspace / "events" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "RuntimeCheckError",
    "applied_migrations",
    "node_syntax_check",
    "run_consumer",
    "run_migrations",
    "schema_columns",
    "ticket_row",
    "write_event",
]
