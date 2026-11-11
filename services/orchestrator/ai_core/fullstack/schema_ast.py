"""ORM / Express 产物的 Babel AST 校验封装。

ConsistencyVerifier 与生成期断言都不再使用正则读取 ``server/db.js``：
正则只能看到声明文本，无法识别 ``COLUMN_DEFS.push(['name', 'TEXT'])``
这类运行期追加，也无法区分 ``JSON.parse(process.env.COLUMNS)`` 与真实
静态字段。本模块把源码交给 ``schema_ast_check.mjs``，由 Babel 建立
AST 后输出确定的表名 / 字段集合 / 静态性错误。

依赖边界：
  - Node 与 @babel/parser 由镜像提供；生产镜像缺少任一组件时，
    校验器抛 ``SchemaAstUnavailable``，由上层显式阻断并提示环境问题，
    绝不静默回退到正则，避免“看起来通过、上线 500”。
  - 模块本身只使用标准库，可在测试与验收脚本中复用。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

CHECKER_ENGINE = "babel-ast"
DEFAULT_PATHS: tuple[str, ...] = ("server/db.js", "server/index.js")
DEFAULT_TIMEOUT_SECONDS = 20.0


class SchemaAstError(RuntimeError):
    """AST 校验失败（代码层面无法静态验证）。"""


class SchemaAstUnavailable(SchemaAstError):
    """校验器自身不可用：Node / Babel 缺失、脚本异常或输出非法。"""


@dataclass(frozen=True)
class JsFileSchema:
    """单个 JS 文件的 AST 分析结果。"""

    path: str
    table: str
    columns: tuple[str, ...]
    column_defs: tuple[tuple[str, str], ...] = ()
    meta_columns: tuple[str, ...] = ()
    attribute_columns: tuple[str, ...] = ()
    define_tables: tuple[str, ...] = ()
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.errors


def _checker_script() -> Path:
    configured = (os.environ.get("SCHEMA_AST_SCRIPT") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name("schema_ast_check.mjs")


def _node_binary() -> str:
    configured = (os.environ.get("SCHEMA_AST_NODE") or "").strip()
    if configured:
        return configured
    node = shutil.which("node")
    if node:
        return node
    raise SchemaAstUnavailable(
        "未找到 node，无法执行 Babel AST 校验；请检查 Python AI 镜像是否安装 nodejs"
    )


def _require_root() -> str:
    """返回 Babel 依赖解析锚点。

    生产镜像：/app/sandbox_template/package.json 已随镜像提供 Babel；
    宿主开发：services/orchestrator/sandbox_template/package.json。
    """
    configured = (os.environ.get("VIBE_AST_REQUIRE_ROOT") or "").strip()
    if configured:
        return configured
    candidates = (
        Path("/app/sandbox_template/package.json"),
        Path(__file__).resolve().parents[2] / "sandbox_template" / "package.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def _timeout_seconds() -> float:
    raw = os.environ.get("SCHEMA_AST_TIMEOUT_SECONDS", "")
    try:
        value = float(raw) if raw.strip() else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        value = DEFAULT_TIMEOUT_SECONDS
    return max(1.0, min(value, 120.0))


def _payload(files: Mapping[str, str], paths: tuple[str, ...]) -> dict[str, object]:
    selected = {
        path: files[path]
        for path in paths
        if isinstance(files.get(path), str) and files.get(path)
    }
    return {"files": selected}


def _analyze_payload(payload: dict[str, object]) -> dict[str, object]:
    files = payload.get("files") or {}
    if not isinstance(files, dict) or not files:
        return {"engine": CHECKER_ENGINE, "ok": True, "files": {}}

    node = _node_binary()
    script = _checker_script()
    if not script.is_file():
        raise SchemaAstUnavailable(f"AST 校验脚本不存在: {script}")

    env = dict(os.environ)
    require_root = _require_root()
    if require_root:
        env.setdefault("VIBE_AST_REQUIRE_ROOT", require_root)

    try:
        completed = subprocess.run(
            [node, str(script)],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=_timeout_seconds(),
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SchemaAstUnavailable(
            f"Babel AST 校验超时（>{_timeout_seconds():.0f}s）"
        ) from exc
    except OSError as exc:
        raise SchemaAstUnavailable(f"无法启动 Babel AST 校验器: {exc}") from exc

    if completed.returncode == 2:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise SchemaAstUnavailable(f"Babel AST 校验器不可用: {detail[:500]}")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise SchemaAstUnavailable(
            f"Babel AST 校验器异常退出（{completed.returncode}）: {detail[:500]}"
        )

    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SchemaAstUnavailable(
            f"Babel AST 校验器输出不是 JSON: {completed.stdout[:500]}"
        ) from exc
    if not isinstance(result, dict) or result.get("engine") != CHECKER_ENGINE:
        raise SchemaAstUnavailable(
            f"Babel AST 校验器返回了未知结果: {str(result)[:500]}"
        )
    return result


def analyze_js_files(
    files: Mapping[str, str],
    *,
    paths: tuple[str, ...] = DEFAULT_PATHS,
) -> dict[str, JsFileSchema]:
    """分析指定 JS 文件，返回 ``{路径: JsFileSchema}``。

    传入文件中不存在的路径会被忽略；全部缺失时返回空字典，保持
    局部单元测试（只校验路由）的兼容性。
    """
    payload = _payload(files, paths)
    if not payload["files"]:
        return {}
    raw = _analyze_payload(payload)
    raw_files = raw.get("files")
    if not isinstance(raw_files, dict):
        raise SchemaAstUnavailable("Babel AST 校验器响应缺少 files")

    result: dict[str, JsFileSchema] = {}
    for path, item in raw_files.items():
        if not isinstance(item, dict):
            raise SchemaAstUnavailable(f"Babel AST 校验器响应格式错误: {path}")
        errors = item.get("errors") or []
        messages: list[str] = []
        if isinstance(errors, list):
            for error in errors:
                if not isinstance(error, dict):
                    continue
                line = error.get("line") or 1
                column = error.get("column") or 1
                message = str(error.get("message") or "未知 AST 错误")
                messages.append(f"第 {line} 行第 {column} 列：{message}")
        column_defs: list[tuple[str, str]] = []
        for pair in item.get("columnDefs") or []:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                column_defs.append((str(pair[0]), str(pair[1])))
        result[str(path)] = JsFileSchema(
            path=str(path),
            table=str(item.get("table") or "").strip(),
            columns=tuple(
                str(name) for name in (item.get("columns") or []) if str(name).strip()
            ),
            column_defs=tuple(column_defs),
            meta_columns=tuple(
                str(name)
                for name in (item.get("metaColumns") or [])
                if str(name).strip()
            ),
            attribute_columns=tuple(
                str(name)
                for name in (item.get("attributeColumns") or [])
                if str(name).strip()
            ),
            define_tables=tuple(
                str(name)
                for name in (item.get("defineTables") or [])
                if str(name).strip()
            ),
            errors=tuple(messages),
        )
    return result


__all__ = [
    "CHECKER_ENGINE",
    "DEFAULT_PATHS",
    "JsFileSchema",
    "SchemaAstError",
    "SchemaAstUnavailable",
    "analyze_js_files",
]
