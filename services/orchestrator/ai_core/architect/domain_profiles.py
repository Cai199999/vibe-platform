"""领域配置层：把行业知识从平台核心代码里剥离出来。

平台核心（方案侦察、CodeGen、评测）只依赖本模块暴露的通用协议：
``DomainProfile`` / ``CapabilityRule`` / ``ConstraintRule``。具体行业
（例如某个垂直领域）的关键词、检索词、复用目录、推荐依赖与视觉模板
全部放在 ``config/domain_profiles/*.yaml``，不在 Python 里写 ``if gis``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError:  # pragma: no cover - 容器内始终有 PyYAML
    yaml = None

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CapabilityRule:
    label: str
    terms: tuple[str, ...]
    search_queries: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConstraintRule:
    label: str
    terms: tuple[str, ...]


@dataclass(frozen=True)
class DomainProfile:
    id: str
    label: str
    description: str = ""
    detect_terms: tuple[str, ...] = ()
    min_hits: int = 1
    capability_rules: tuple[CapabilityRule, ...] = ()
    constraint_rules: tuple[ConstraintRule, ...] = ()
    search_queries: tuple[str, ...] = ()
    local_catalog: str = ""
    mock_candidates: tuple[dict[str, Any], ...] = ()
    ranking_domain_terms: tuple[str, ...] = ()
    preferred_languages: tuple[str, ...] = ()
    preferred_capabilities: tuple[str, ...] = ()
    domain_boost: float = 0.0
    web_boost: float = 0.0
    browser_boost: float = 0.0
    build_summary: str = ""
    codegen: dict[str, Any] = field(default_factory=dict)

    @property
    def is_generic(self) -> bool:
        return self.id == "generic"

    def detect_score(self, text: str) -> int:
        lower = str(text or "").lower()
        return sum(1 for term in self.detect_terms if term.lower() in lower)


def default_profiles_dir() -> Path:
    # ai_core/architect/domain_profiles.py -> <orchestrator>/config/domain_profiles
    return Path(__file__).resolve().parents[2] / "config" / "domain_profiles"


def _builtin_generic() -> DomainProfile:
    return DomainProfile(
        id="generic",
        label="通用 Web 应用",
        description="平台默认配置；无行业关键词。",
        search_queries=(
            "open source web application framework",
            "react admin dashboard open source",
        ),
        build_summary=(
            "自研方案：按契约从零实现；复用成熟框架与基础库，"
            "不引入不可控的整站模板。"
        ),
    )


@lru_cache(maxsize=4)
def _load_profiles_cached(directory: str) -> tuple[DomainProfile, ...]:
    root = Path(directory)
    if yaml is None:
        logger.warning("PyYAML 不可用，领域配置回退 generic")
        return (_builtin_generic(),)
    profiles: list[DomainProfile] = []
    for path in sorted(root.glob("*.yaml")):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("读取领域配置失败 %s: %s", path, exc)
            continue
        if not isinstance(payload, dict):
            continue
        profile = _profile_from_dict(payload, source=str(path))
        if profile is not None:
            profiles.append(profile)
    if not any(profile.is_generic for profile in profiles):
        profiles.append(_builtin_generic())
    return tuple(profiles)


def load_domain_profiles(directory: str | Path | None = None) -> tuple[DomainProfile, ...]:
    target = Path(directory) if directory else default_profiles_dir()
    return _load_profiles_cached(str(target))


def find_profile(
    profile_id: str,
    directory: str | Path | None = None,
) -> DomainProfile:
    profiles = load_domain_profiles(directory)
    for profile in profiles:
        if profile.id == profile_id:
            return profile
    return _generic_profile(profiles)


def select_domain_profile(
    text: str,
    *,
    directory: str | Path | None = None,
) -> DomainProfile:
    """按 detect.terms 命中数选择领域；无命中回退 generic。"""
    profiles = load_domain_profiles(directory)
    best: DomainProfile | None = None
    best_score = 0
    for profile in profiles:
        if profile.is_generic:
            continue
        score = profile.detect_score(text)
        if score < max(1, profile.min_hits):
            continue
        if score > best_score:
            best = profile
            best_score = score
    return best or _generic_profile(profiles)


def profile_text(
    message: str,
    project_name: str = "",
    knowledge_documents: Iterable[Any] = (),
) -> str:
    doc_text = " ".join(
        f"{getattr(doc, 'name', '')} {getattr(doc, 'content', '')}"
        for doc in knowledge_documents
    )
    return f"{project_name}\n{message}\n{doc_text}"


def _generic_profile(profiles: Iterable[DomainProfile]) -> DomainProfile:
    for profile in profiles:
        if profile.is_generic:
            return profile
    return _builtin_generic()


def _profile_from_dict(payload: dict[str, Any], *, source: str) -> DomainProfile | None:
    profile_id = str(payload.get("id") or "").strip()
    if not profile_id:
        logger.warning("领域配置缺少 id: %s", source)
        return None
    detect = payload.get("detect") or {}
    capabilities = tuple(
        CapabilityRule(
            label=str(item.get("label") or "").strip(),
            terms=_string_tuple(item.get("terms")),
            search_queries=_string_tuple(item.get("search_queries")),
        )
        for item in (payload.get("capabilities") or [])
        if isinstance(item, dict) and str(item.get("label") or "").strip()
    )
    constraints = tuple(
        ConstraintRule(
            label=str(item.get("label") or "").strip(),
            terms=_string_tuple(item.get("terms")),
        )
        for item in (payload.get("constraints") or [])
        if isinstance(item, dict) and str(item.get("label") or "").strip()
    )
    search = payload.get("search") or {}
    ranking = payload.get("ranking") or {}
    codegen = payload.get("codegen") or {}
    mock_candidates = tuple(
        item for item in (search.get("mock_candidates") or []) if isinstance(item, dict)
    )
    return DomainProfile(
        id=profile_id,
        label=str(payload.get("label") or profile_id).strip(),
        description=str(payload.get("description") or "").strip(),
        detect_terms=_string_tuple(detect.get("terms")),
        min_hits=int(detect.get("min_hits") or 1),
        capability_rules=capabilities,
        constraint_rules=constraints,
        search_queries=_string_tuple(search.get("queries")),
        local_catalog=str(search.get("local_catalog") or "").strip(),
        mock_candidates=mock_candidates,
        ranking_domain_terms=_string_tuple(ranking.get("domain_terms")),
        preferred_languages=_string_tuple(ranking.get("preferred_languages")),
        preferred_capabilities=_string_tuple(ranking.get("preferred_capabilities")),
        domain_boost=float(ranking.get("domain_boost") or 0.0),
        web_boost=float(ranking.get("web_boost") or 0.0),
        browser_boost=float(ranking.get("browser_boost") or 0.0),
        build_summary=str(payload.get("build_summary") or "").strip(),
        codegen=codegen if isinstance(codegen, dict) else {},
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        items = []
    return tuple(item.strip() for item in items if item.strip())


__all__ = [
    "CapabilityRule",
    "ConstraintRule",
    "DomainProfile",
    "default_profiles_dir",
    "find_profile",
    "load_domain_profiles",
    "profile_text",
    "select_domain_profile",
]
