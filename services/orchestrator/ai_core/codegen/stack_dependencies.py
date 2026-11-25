"""从 contract.techStack 解析前端依赖（配置驱动，无行业分支）。

契约里的 ``techStack.frontend`` 可能写 "React + Vite + Leaflet"；
本模块按 ``config/codegen/stack_dependencies.yaml`` 的 token 表合并
npm 依赖与 CSS import。平台核心不认识 Leaflet / ECharts 等具体库。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - 容器内始终有 PyYAML
    yaml = None

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StackDependencies:
    dependencies: dict[str, str] = field(default_factory=dict)
    dev_dependencies: dict[str, str] = field(default_factory=dict)
    imports: tuple[str, ...] = ()


def default_config_path() -> Path:
    # ai_core/codegen/stack_dependencies.py -> <orchestrator>/config/codegen
    return (
        Path(__file__).resolve().parents[2]
        / "config"
        / "codegen"
        / "stack_dependencies.yaml"
    )


@lru_cache(maxsize=4)
def _load_tokens(path: str) -> dict[str, dict[str, Any]]:
    if yaml is None:
        return {}
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("读取技术栈依赖配置失败 %s: %s", path, exc)
        return {}
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    return tokens if isinstance(tokens, dict) else {}


def contract_stack_text(contract: Any) -> dict[str, Any]:
    """把契约的结构化字段与正文合成一份“技术栈文本”。

    LLM 有时把选型写进 ``contractMarkdown`` / ``assumptions`` 而把
    ``techStack`` 留空；解析依赖时不能只看一个字段。
    """

    def get(name: str, default: Any) -> Any:
        if isinstance(contract, dict):
            return contract.get(name, default)
        return getattr(contract, name, default)

    return {
        "techStack": get("tech_stack", {}),
        "contractMarkdown": get("contract_markdown", ""),
        "assumptions": get("assumptions", []),
        "components": get("components", []),
    }


def resolve_stack_dependencies(
    tech_stack: dict[str, Any] | None,
    *,
    config_path: str | Path | None = None,
) -> StackDependencies:
    path = Path(config_path) if config_path else default_config_path()
    tokens = _load_tokens(str(path))
    if not tokens or not tech_stack:
        return StackDependencies()
    text = json.dumps(tech_stack, ensure_ascii=False, default=str).lower()
    dependencies: dict[str, str] = {}
    dev_dependencies: dict[str, str] = {}
    imports: list[str] = []
    for token, spec in tokens.items():
        if str(token).lower() not in text or not isinstance(spec, dict):
            continue
        dependencies.update(
            {
                str(name): str(version)
                for name, version in (spec.get("dependencies") or {}).items()
            }
        )
        dev_dependencies.update(
            {
                str(name): str(version)
                for name, version in (spec.get("dev_dependencies") or {}).items()
            }
        )
        for line in spec.get("imports") or []:
            text_line = str(line).strip()
            if text_line and text_line not in imports:
                imports.append(text_line)
    return StackDependencies(
        dependencies=dependencies,
        dev_dependencies=dev_dependencies,
        imports=tuple(imports),
    )


__all__ = [
    "StackDependencies",
    "contract_stack_text",
    "resolve_stack_dependencies",
]
