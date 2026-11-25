"""平台核心不得硬编码某一专业方向。"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "ai_core"
# 只拦截“领域专用词”；source map 等通用词不在此列。
FORBIDDEN_PATTERNS = (
    r"\bGIS\b",
    r"GeoAI",
    r"geospatial",
    r"leaflet",
    r"postgis",
    r"地图",
    r"遥感",
    r"地理",
)


class NoDomainHardcodingTests(unittest.TestCase):
    def test_core_python_has_no_vertical_keywords(self) -> None:
        violations: list[str] = []
        for path in CORE.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for pattern in FORBIDDEN_PATTERNS:
                if re.search(pattern, text):
                    violations.append(f"{path.relative_to(CORE)}: {pattern}")
        self.assertEqual(
            violations,
            [],
            "平台核心出现领域硬编码；请移到 config/domain_profiles 或 config/codegen："
            + "；".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
