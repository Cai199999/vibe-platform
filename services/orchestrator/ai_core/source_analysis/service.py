"""存量 React 源码分析服务（只读）。

调用独立的 Node/Babel 脚本 ``scripts/analyze_existing_react.mjs``，
把源码解析为组件树分析报告。服务端只做进程边界与安全校验：

  - 不写源码、不生成 VisualDocument、不触碰沙盒 / Git；
  - 文件大小、扩展名、超时与输出必须显式校验；
  - 脚本不可用时明确报环境错误，绝不退化为正则猜测。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

ALLOWED_EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_SOURCE_CHARS = 500_000


class SourceAnalysisError(Exception):
    code = "SOURCE_ANALYSIS_FAILED"
    status_code = 500


class SourceAnalysisInvalid(SourceAnalysisError):
    code = "SOURCE_ANALYSIS_INVALID"
    status_code = 422


class SourceAnalysisUnavailable(SourceAnalysisError):
    code = "SOURCE_ANALYSIS_UNAVAILABLE"
    status_code = 503


def _script_path() -> Path:
    configured = (os.environ.get("SOURCE_ANALYSIS_SCRIPT") or "").strip()
    source_root = Path(__file__).resolve()
    repo_candidates = [
        parent / "scripts" / "analyze_existing_react.mjs"
        for parent in source_root.parents
    ]
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path("/app/scripts/analyze_existing_react.mjs"),
        Path.cwd() / "scripts" / "analyze_existing_react.mjs",
        *repo_candidates,
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise SourceAnalysisUnavailable(
        "未找到 scripts/analyze_existing_react.mjs；请检查脚本挂载"
    )


def _node_binary() -> str:
    configured = (os.environ.get("SOURCE_ANALYSIS_NODE") or "").strip()
    if configured:
        return configured
    node = shutil.which("node")
    if node:
        return node
    raise SourceAnalysisUnavailable(
        "未找到 node，无法执行 React 源码分析；请检查 AI 镜像是否安装 nodejs"
    )


def _timeout_seconds() -> float:
    raw = os.environ.get("SOURCE_ANALYSIS_TIMEOUT_SECONDS", "")
    try:
        value = float(raw) if raw.strip() else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        value = DEFAULT_TIMEOUT_SECONDS
    return max(1.0, min(value, 120.0))


def _validate_input(file_name: str, source: str) -> None:
    if not source.strip():
        raise SourceAnalysisInvalid("源码为空")
    if len(source) > MAX_SOURCE_CHARS:
        raise SourceAnalysisInvalid(
            f"源码过大（{len(source)} 字符），上限 {MAX_SOURCE_CHARS}"
        )
    suffix = Path(file_name).suffix.lower()
    if suffix and suffix not in ALLOWED_EXTENSIONS:
        raise SourceAnalysisInvalid(
            f"不支持的文件类型 {suffix}；仅支持 "
            + ", ".join(sorted(ALLOWED_EXTENSIONS))
        )


def analyze_existing_react(
    file_name: str,
    source: str,
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """调用 Babel 分析脚本，返回只读分析报告。"""
    _validate_input(file_name, source)
    script = _script_path()
    node = _node_binary()

    env = dict(os.environ)
    # 分析脚本与 CodeGen 门禁共用 Babel 依赖锚点。
    require_root = (os.environ.get("VIBE_AST_REQUIRE_ROOT") or "").strip()
    if not require_root:
        candidates = (
            Path("/app/sandbox_template/package.json"),
            Path(__file__).resolve().parents[2] / "sandbox_template" / "package.json",
        )
        for candidate in candidates:
            if candidate.is_file():
                require_root = str(candidate)
                break
    if require_root:
        env.setdefault("VIBE_AST_REQUIRE_ROOT", require_root)

    timeout = timeout_seconds or _timeout_seconds()
    try:
        completed = subprocess.run(
            [node, str(script), "--stdin-name", file_name],
            input=source,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceAnalysisInvalid(
            f"源码分析超时（>{timeout:.0f}s），请拆分文件后重试"
        ) from exc
    except OSError as exc:
        raise SourceAnalysisUnavailable(f"无法启动源码分析脚本: {exc}") from exc

    if completed.returncode == 2:
        raise SourceAnalysisUnavailable(
            f"源码分析脚本不可用: {(completed.stderr or '').strip()[:500]}"
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise SourceAnalysisInvalid(f"源码解析失败: {detail[:800]}")

    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SourceAnalysisUnavailable(
            f"源码分析脚本输出不是 JSON: {completed.stdout[:500]}"
        ) from exc
    if not isinstance(report, dict):
        raise SourceAnalysisUnavailable("源码分析脚本返回了非对象结果")
    # 只读契约是安全边界：报告必须显式声明不可写。
    if report.get("analysisOnly") is not True or report.get("writable") is not False:
        raise SourceAnalysisInvalid(
            "源码分析报告未声明 analysisOnly=true / writable=false"
        )
    return report


__all__ = [
    "ALLOWED_EXTENSIONS",
    "MAX_SOURCE_CHARS",
    "SourceAnalysisError",
    "SourceAnalysisInvalid",
    "SourceAnalysisUnavailable",
    "analyze_existing_react",
]
