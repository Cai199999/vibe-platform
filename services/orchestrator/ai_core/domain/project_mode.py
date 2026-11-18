"""项目模式（双轨制）确定性判定。

- NEW_PROJECT：无源码的新建项目 → VisualDocument → CodeGen；
- EXISTING_SOURCE：已有源码的存量项目 → 只读解析 → 源码直改 / 后端增量。

判定只依赖请求携带的源码事实，不交给 LLM 猜测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

ProjectMode = Literal["NEW_PROJECT", "EXISTING_SOURCE"]
NEW_PROJECT: ProjectMode = "NEW_PROJECT"
EXISTING_SOURCE: ProjectMode = "EXISTING_SOURCE"


@dataclass(frozen=True)
class ProjectModeDecision:
    mode: ProjectMode
    reason: str
    file_count: int = 0
    has_sandbox: bool = False


def resolve_project_mode(
    *,
    declared: str | None = None,
    files: Mapping[str, str] | None = None,
    sandbox_id: str | None = None,
) -> ProjectModeDecision:
    """按确定性规则判定项目模式。

    优先级：
      1. 有 sandbox → EXISTING_SOURCE（服务端会读取最新源码）；
      2. 有非空 files → EXISTING_SOURCE；
      3. 没有源码事实 → NEW_PROJECT。

    ``declared`` 不能覆盖“已有源码”事实；反过来，仅声明存量但没有
    files / sandbox 时也不能凭空进入 EXISTING_SOURCE。否则存量 Prompt、
    跳过 DDL 自检、禁止前端 CodeGen 都会作用在一个没有源码的项目上，
    等于绕开双轨制事实判定。
    """
    source_files = {
        str(path): str(content)
        for path, content in (files or {}).items()
        if isinstance(content, str) and content.strip()
    }
    if sandbox_id:
        return ProjectModeDecision(
            EXISTING_SOURCE,
            reason="项目已有 sandbox 源码",
            file_count=len(source_files),
            has_sandbox=True,
        )
    if source_files:
        return ProjectModeDecision(
            EXISTING_SOURCE,
            reason="请求携带非空源码文件",
            file_count=len(source_files),
        )
    if declared == EXISTING_SOURCE:
        return ProjectModeDecision(
            NEW_PROJECT,
            reason="声明为存量但缺少 files / sandbox 源码事实，按新建项目处理",
        )
    return ProjectModeDecision(NEW_PROJECT, reason="无源码，按新建项目处理")


__all__ = [
    "EXISTING_SOURCE",
    "NEW_PROJECT",
    "ProjectMode",
    "ProjectModeDecision",
    "resolve_project_mode",
]
