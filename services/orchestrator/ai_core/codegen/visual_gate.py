"""VisualDocument CodeGen 编译门禁。

所有门禁共用同一个 Babel AST 入口，不存在“正则回退”或“只检查文件后缀”
的绕过路径：

  - 语法必须可被 Babel 解析；
  - import 只允许 react 与本地模块 / 本地样式；
  - 每个 VisualNode 必须且只能出现一次 ``data-node-id``；
  - 禁止 ``dangerouslySetInnerHTML``；
  - 禁止 position:absolute/fixed 与绝对定位 Tailwind 类。

门禁脚本自身不可用（node / Babel 缺失）时抛
``VisualCodegenGateUnavailable``，由调用方按环境故障处理，绝不静默放行。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

GATE_ENGINE = "visual-codegen-babel"
DEFAULT_TIMEOUT_SECONDS = 30.0
ALLOWED_IMPORTS = frozenset(
    {
        "react",
        "react-dom",
        "react/jsx-runtime",
    }
)


class VisualCodegenGateError(ValueError):
    """AST 门禁判定产物不合法。"""

    def __init__(self, message: str, *, errors: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.errors = tuple(errors)


class VisualCodegenGateUnavailable(RuntimeError):
    """门禁引擎不可用（node / Babel 缺失、脚本异常）。"""


@dataclass(frozen=True)
class VisualCodegenGateReport:
    ok: bool
    node_ids: tuple[str, ...] = ()
    imports: tuple[str, ...] = ()
    duplicate_node_ids: tuple[str, ...] = ()
    missing_node_ids: tuple[str, ...] = ()
    unexpected_node_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    engine: str = GATE_ENGINE

    def summary(self) -> str:
        if self.ok:
            return f"CodeGen AST 门禁通过（{len(self.node_ids)} 个 data-node-id）"
        return f"CodeGen AST 门禁发现 {len(self.errors)} 处缺陷"

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "engine": self.engine,
            "nodeIds": list(self.node_ids),
            "imports": list(self.imports),
            "duplicateNodeIds": list(self.duplicate_node_ids),
            "missingNodeIds": list(self.missing_node_ids),
            "unexpectedNodeIds": list(self.unexpected_node_ids),
            "errors": list(self.errors),
        }


def _script_path() -> Path:
    configured = (os.environ.get("VISUAL_CODEGEN_AST_SCRIPT") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name("visual_codegen_check.mjs")


def _node_binary() -> str:
    configured = (os.environ.get("VISUAL_CODEGEN_AST_NODE") or "").strip()
    if configured:
        return configured
    node = shutil.which("node")
    if node:
        return node
    raise VisualCodegenGateUnavailable(
        "未找到 node，无法执行 CodeGen Babel AST 门禁；请检查 AI 镜像是否安装 nodejs"
    )


def _require_root() -> str:
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
    raw = os.environ.get("VISUAL_CODEGEN_AST_TIMEOUT_SECONDS", "")
    try:
        value = float(raw) if raw.strip() else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        value = DEFAULT_TIMEOUT_SECONDS
    return max(1.0, min(value, 120.0))


def _import_allowed(source: str) -> bool:
    if source in ALLOWED_IMPORTS:
        return True
    return source.startswith("./") or source.startswith("../")


def verify_visual_source(
    files: Mapping[str, str],
    expected_node_ids: Iterable[str],
) -> VisualCodegenGateReport:
    """执行一次完整 CodeGen AST 门禁。"""
    expected = [str(item) for item in expected_node_ids]
    payload = {
        "files": {
            path: content
            for path, content in files.items()
            if isinstance(content, str) and path.endswith((".jsx", ".js"))
        },
        "expectedNodeIds": expected,
    }
    if not payload["files"]:
        return VisualCodegenGateReport(
            ok=False,
            missing_node_ids=tuple(expected),
            errors=("CodeGen 没有生成任何 JS/JSX 文件",),
        )

    node = _node_binary()
    script = _script_path()
    if not script.is_file():
        raise VisualCodegenGateUnavailable(f"CodeGen AST 门禁脚本不存在: {script}")

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
        raise VisualCodegenGateUnavailable(
            f"CodeGen Babel AST 门禁超时（>{_timeout_seconds():.0f}s）"
        ) from exc
    except OSError as exc:
        raise VisualCodegenGateUnavailable(
            f"无法启动 CodeGen Babel AST 门禁: {exc}"
        ) from exc

    if completed.returncode == 2:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise VisualCodegenGateUnavailable(
            f"CodeGen Babel AST 门禁不可用: {detail[:500]}"
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise VisualCodegenGateUnavailable(
            f"CodeGen Babel AST 门禁异常退出（{completed.returncode}）: "
            f"{detail[:500]}"
        )

    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise VisualCodegenGateUnavailable(
            f"CodeGen Babel AST 门禁输出不是 JSON: {completed.stdout[:500]}"
        ) from exc
    if not isinstance(result, dict) or result.get("engine") != GATE_ENGINE:
        raise VisualCodegenGateUnavailable(
            f"CodeGen Babel AST 门禁返回未知结果: {str(result)[:500]}"
        )

    errors = [
        (
            f"{item.get('file', '')}: {item.get('message', '')}"
            if item.get("file")
            else str(item.get("message", ""))
        )
        for item in result.get("errors") or []
        if isinstance(item, dict)
    ]
    imports = tuple(str(item) for item in result.get("imports") or [])
    forbidden_imports = [item for item in imports if not _import_allowed(item)]
    if forbidden_imports:
        errors.extend(f"import 不在白名单内: {item}" for item in forbidden_imports)

    return VisualCodegenGateReport(
        ok=bool(result.get("ok")) and not forbidden_imports,
        node_ids=tuple(str(item) for item in result.get("nodeIds") or []),
        imports=imports,
        duplicate_node_ids=tuple(
            str(item) for item in result.get("duplicateNodeIds") or []
        ),
        missing_node_ids=tuple(
            str(item) for item in result.get("missingNodeIds") or []
        ),
        unexpected_node_ids=tuple(
            str(item) for item in result.get("unexpectedNodeIds") or []
        ),
        errors=tuple(errors),
    )


__all__ = [
    "ALLOWED_IMPORTS",
    "GATE_ENGINE",
    "VisualCodegenGateError",
    "VisualCodegenGateReport",
    "VisualCodegenGateUnavailable",
    "verify_visual_source",
]
