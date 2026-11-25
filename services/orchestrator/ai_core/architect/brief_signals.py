"""抽象需求判定词表加载器（配置驱动，无行业硬编码）。"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - 容器内始终有 PyYAML
    yaml = None

logger = logging.getLogger(__name__)

# 配置缺失时的最小兜底：只保留产品形态词，不包含任何行业名词。
_FALLBACK_ABSTRACT = ("工作台", "看板", "系统", "平台", "应用")
_FALLBACK_BUSINESS = ("登录", "用户", "数据", "列表", "表单")


def _default_path() -> Path:
    # ai_core/architect/brief_signals.py -> <orchestrator>/config/architect
    return (
        Path(__file__).resolve().parents[2]
        / "config"
        / "architect"
        / "brief_signals.yaml"
    )


@lru_cache(maxsize=2)
def _load(path: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if yaml is None:
        return _FALLBACK_ABSTRACT, _FALLBACK_BUSINESS
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("读取需求信号配置失败 %s: %s", path, exc)
        return _FALLBACK_ABSTRACT, _FALLBACK_BUSINESS
    if not isinstance(payload, dict):
        return _FALLBACK_ABSTRACT, _FALLBACK_BUSINESS
    abstract = tuple(
        str(item).strip() for item in (payload.get("abstract_subjects") or [])
    )
    business = tuple(
        str(item).strip() for item in (payload.get("business_signals") or [])
    )
    return (
        tuple(item for item in abstract if item) or _FALLBACK_ABSTRACT,
        tuple(item for item in business if item) or _FALLBACK_BUSINESS,
    )


def get_abstract_subjects() -> tuple[str, ...]:
    return _load(str(_default_path()))[0]


def get_business_signals() -> tuple[str, ...]:
    return _load(str(_default_path()))[1]


__all__ = ["get_abstract_subjects", "get_business_signals"]
