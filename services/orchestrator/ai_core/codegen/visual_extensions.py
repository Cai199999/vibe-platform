"""可视化扩展包：把领域专用组件（第三方组件、图表等）从核心编译器剥离。

扩展定义在 ``config/domain_profiles/*.yaml`` 的 ``codegen.visual_extensions``：
- ``match.types`` / ``match.names`` 选择节点；
- ``imports`` / ``dependencies`` / ``render`` 提供 JSX 模板与依赖。

平台核心只做通用匹配与渲染，不认识任何具体行业。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VisualExtension:
    id: str
    types: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    imports: tuple[str, ...] = ()
    dependencies: dict[str, str] = field(default_factory=dict)
    render: str = ""
    source: str = ""

    def matches(self, node: Any) -> bool:
        node_type = _field(node, "type")
        node_name = _field(node, "name")
        if self.types and node_type not in self.types:
            return False
        if not self.names:
            return True
        lower_name = node_name.lower()
        for name in self.names:
            target = name.strip().lower()
            if target and (lower_name == target or target in lower_name):
                return True
        return False


def _profiles_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "domain_profiles"


@lru_cache(maxsize=2)
def _load(directory: str) -> tuple[VisualExtension, ...]:
    if yaml is None:
        return ()
    extensions: list[VisualExtension] = []
    seen: set[str] = set()
    for path in sorted(Path(directory).glob("*.yaml")):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("读取视觉扩展失败 %s: %s", path, exc)
            continue
        if not isinstance(payload, dict):
            continue
        codegen = payload.get("codegen") or {}
        for item in codegen.get("visual_extensions") or []:
            extension = _from_dict(item, source=str(path))
            if extension is None or extension.id in seen:
                continue
            seen.add(extension.id)
            extensions.append(extension)
    return tuple(extensions)


def _from_dict(item: Any, *, source: str) -> VisualExtension | None:
    if not isinstance(item, dict):
        return None
    extension_id = str(item.get("id") or "").strip()
    render = str(item.get("render") or "").strip()
    if not extension_id or not render:
        return None
    match = item.get("match") or {}
    dependencies = item.get("dependencies") or {}
    return VisualExtension(
        id=extension_id,
        types=_string_tuple(match.get("types")),
        names=_string_tuple(match.get("names")),
        imports=_string_tuple(item.get("imports")),
        dependencies={
            str(name): str(version)
            for name, version in dependencies.items()
        }
        if isinstance(dependencies, dict)
        else {},
        render=render,
        source=source,
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        items = []
    return tuple(item.strip() for item in items if item.strip())


def _field(value: Any, name: str) -> str:
    if isinstance(value, dict):
        return str(value.get(name) or "")
    return str(getattr(value, name, "") or "")


def load_visual_extensions(directory: str | Path | None = None) -> tuple[VisualExtension, ...]:
    return _load(str(Path(directory) if directory else _profiles_dir()))


def find_extension(
    extensions: Iterable[VisualExtension],
    node: Any,
) -> VisualExtension | None:
    for extension in extensions:
        if extension.matches(node):
            return extension
    return None


def extension_dependencies(
    extensions: Iterable[VisualExtension],
) -> dict[str, str]:
    dependencies: dict[str, str] = {}
    for extension in extensions:
        dependencies.update(extension.dependencies)
    return dependencies


def extension_imports(extensions: Iterable[VisualExtension]) -> tuple[str, ...]:
    imports: list[str] = []
    for extension in extensions:
        for line in extension.imports:
            if line not in imports:
                imports.append(line)
    return tuple(imports)


__all__ = [
    "VisualExtension",
    "extension_dependencies",
    "extension_imports",
    "find_extension",
    "load_visual_extensions",
]
