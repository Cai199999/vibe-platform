"""平台可执行栈：让契约默认值与 CodeGen 模板保持一致。

配置在 ``config/codegen/stack_capabilities.yaml``。平台核心只做通用
加载与冲突检测，不写具体行业或框架分支。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StackCapabilities:
    default: dict[str, str]
    executable: dict[str, tuple[str, ...]]
    known_unsupported: dict[str, tuple[str, ...]]

    def prompt_block(self) -> str:
        default = self.default
        return (
            "【平台可执行栈 · 最高优先级】\n"
            f"- 前端：{default.get('frontend', 'React + Vite + Tailwind')}；\n"
            f"- 后端：{default.get('backend', 'Node.js + Express')}；\n"
            f"- 数据库：{default.get('database', 'MySQL')}（本地沙盒可回退 SQLite）；\n"
            f"- ORM：{default.get('orm', 'Sequelize')}；\n"
            "用户未明确指定技术栈时必须使用上述栈，并写入 assumptions；"
            "用户明确要求平台不支持的栈时，必须在 assumptions 中说明"
            "无法直接执行，并给出上述可执行替代；禁止静默改用另一套模板。"
        )

    def conflicts(self, tech_stack: dict[str, Any] | None) -> list[str]:
        if not tech_stack:
            return []
        text = json.dumps(tech_stack, ensure_ascii=False, default=str).lower()
        conflicts: list[str] = []
        for category, tokens in self.known_unsupported.items():
            for token in tokens:
                if token.lower() in text:
                    conflicts.append(f"{category}: {token}")
        return conflicts


def _default_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "config"
        / "codegen"
        / "stack_capabilities.yaml"
    )


@lru_cache(maxsize=2)
def _load(path: str) -> StackCapabilities:
    fallback = StackCapabilities(
        default={
            "frontend": "React + Vite + Tailwind CSS",
            "backend": "Node.js + Express",
            "database": "MySQL",
            "orm": "Sequelize",
            "migration": "SQL migration",
            "deploy": "Docker",
        },
        executable={},
        known_unsupported={},
    )
    if yaml is None:
        return fallback
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("读取平台可执行栈配置失败 %s: %s", path, exc)
        return fallback
    if not isinstance(payload, dict):
        return fallback
    default = payload.get("default") or {}
    executable = payload.get("executable") or {}
    unsupported = payload.get("known_unsupported") or {}
    return StackCapabilities(
        default={
            str(key): str(value)
            for key, value in default.items()
            if str(value).strip()
        }
        or fallback.default,
        executable={
            str(key): tuple(str(item) for item in values)
            for key, values in executable.items()
            if isinstance(values, list)
        },
        known_unsupported={
            str(key): tuple(str(item) for item in values)
            for key, values in unsupported.items()
            if isinstance(values, list)
        },
    )


def get_stack_capabilities() -> StackCapabilities:
    return _load(str(_default_path()))


__all__ = ["StackCapabilities", "get_stack_capabilities"]
