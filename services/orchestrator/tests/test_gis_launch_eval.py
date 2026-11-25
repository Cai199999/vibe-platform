"""GIS 可上线评测脚本的确定性测试。

用临时 fixture 项目验证结构 / API 一致性 / 溯源 / GIS 领域检查，
不访问网络、不执行构建。
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "gis_launch_eval.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("gis_launch_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


gis_eval = _load_module()


class GisLaunchEvalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "server").mkdir()
        (self.root / "package.json").write_text(
            json.dumps(
                {
                    "name": "gis-fixture",
                    "scripts": {"build": "vite build", "start": "node server/index.js"},
                    "dependencies": {
                        "express": "^4.0.0",
                        "leaflet": "^1.9.0",
                        "react-leaflet": "^4.0.0",
                        "sequelize": "^6.0.0",
                    },
                }
            ),
            encoding="utf-8",
        )
        (self.root / "src" / "App.jsx").write_text(
            """
import { MapContainer } from 'react-leaflet'
export default function App() {
  const load = () => fetch('/api/facilities')
  const one = (id) => fetch(`/api/facilities/${id}`)
  return (
    <MapContainer data-oid="nd_map">
      <div data-oid="nd_list" onClick={load}>lng lat geojson</div>
    </MapContainer>
  )
}
""".strip(),
            encoding="utf-8",
        )
        (self.root / "server" / "package.json").write_text(
            json.dumps(
                {
                    "dependencies": {
                        "express": "^4.0.0",
                        "sequelize": "^6.0.0",
                        "mysql2": "3.11.5",
                    }
                }
            ),
            encoding="utf-8",
        )
        (self.root / "server" / "api-routes.json").write_text(
            json.dumps(
                {
                    "routes": [
                        {"method": "GET", "path": "/api/facilities"},
                        {"method": "GET", "path": "/api/facilities/:id"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        (self.root / "server" / "index.js").write_text(
            """
const express = require('express')
const bcrypt = require('bcrypt')
const jwt = require('jsonwebtoken')
const app = express()
app.get('/api/health', (req, res) => res.json({ ok: true }))
app.get('/api/facilities', (req, res) => res.json([]))
app.get('/api/facilities/:id', (req, res) => res.json({}))
// ST_Contains / postgis geometry lng lat
""".strip(),
            encoding="utf-8",
        )
        (self.root / "Dockerfile").write_text(
            "FROM node:20\nEXPOSE 3000\nUSER node\n",
            encoding="utf-8",
        )
        (self.root / ".env.example").write_text(
            "DATABASE_URL=mysql://user:pass@db:3306/gis\n",
            encoding="utf-8",
        )
        (self.root / "vibe-source-map.json").write_text(
            json.dumps({"entries": {"nd_map": "src/App.jsx"}}),
            encoding="utf-8",
        )
        (self.root / "contract.json").write_text(
            json.dumps(
                {
                    "contractId": "c1",
                    "techStack": {
                        "frontend": "React + Leaflet + MapLibre",
                        "backend": "Spring Boot",
                        "database": "mysql",
                        "orm": "JPA",
                    },
                    "apiRoutes": [
                        {"path": "/api/facilities"},
                        {"path": "/api/facilities/:id"},
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_normalize_api_path(self) -> None:
        self.assertEqual(
            gis_eval.normalize_api_path("/api/facilities/${id}"),
            "/api/facilities/:id",
        )
        self.assertEqual(
            gis_eval.normalize_api_path("/api/facilities/123?x=1"),
            "/api/facilities/:id",
        )
        self.assertEqual(
            gis_eval.normalize_api_path("/api/facilities/:facility_id"),
            "/api/facilities/:id",
        )

    def test_api_consistency_matches_frontend_and_backend(self) -> None:
        config = {
            "frontend_globs": ["src/**/*.jsx"],
            "backend_globs": ["server/**/*.js"],
            "ignore_paths": ["/api/health"],
            "min_match_ratio": 0.8,
        }
        result = gis_eval.eval_api_consistency(self.root, config)
        self.assertEqual(result["score"], 100.0)
        self.assertEqual(result["metrics"]["unmatched"], [])
        self.assertEqual(result["metrics"]["frontendCalls"], 2)

    def test_contract_fidelity_flags_tech_stack_mismatch(self) -> None:
        result = gis_eval.eval_contract_fidelity(
            self.root,
            {
                "contract_candidates": ["contract.json"],
                "frontend_tokens": ["react", "maplibre"],
                "backend_tokens": ["spring", "express"],
                "database_tokens": ["mysql"],
                "orm_tokens": ["jpa", "sequelize"],
                "min_route_ratio": 0.8,
            },
            {},
        )
        checks = {check["name"]: check for check in result["checks"]}
        self.assertFalse(checks["前端契约 maplibre"]["ok"])
        self.assertFalse(checks["后端契约语言"]["ok"])
        self.assertTrue(checks["契约 API 覆盖"]["ok"])
        self.assertLess(result["score"], 80.0)

    def test_contract_fidelity_reads_stack_from_markdown(self) -> None:
        (self.root / "contract.json").write_text(
            json.dumps(
                {
                    "contractId": "c1",
                    "techStack": {},
                    "contractMarkdown": (
                        "## 技术选型与取舍\n"
                        "前端 React 18 + Vite + Tailwind；地图 Leaflet；"
                        "后端 Node.js + Express + Sequelize；数据库 MySQL。\n"
                    ),
                    "assumptions": ["地圖方案使用 Leaflet"],
                    "apiRoutes": [
                        {"path": "/api/facilities"},
                        {"path": "/api/facilities/:id"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result = gis_eval.eval_contract_fidelity(
            self.root,
            {
                "contract_candidates": ["contract.json"],
                "frontend_tokens": ["react", "leaflet"],
                "backend_tokens": ["spring", "express"],
                "database_tokens": ["mysql"],
                "orm_tokens": ["jpa", "sequelize"],
                "min_route_ratio": 0.8,
            },
            {},
        )
        checks = {check["name"]: check for check in result["checks"]}
        self.assertTrue(checks["前端契约 leaflet"]["ok"])
        self.assertTrue(checks["后端契约语言"]["ok"])
        self.assertTrue(checks["ORM 契约"]["ok"])
        self.assertGreaterEqual(result["score"], 90.0)

    def test_traceability_detects_source_map_and_oid(self) -> None:
        result = gis_eval.eval_traceability(
            self.root,
            {
                "source_map_candidates": ["vibe-source-map.json"],
                "oid_attributes": ["data-oid"],
                "contract_candidates": ["contract.json"],
                "min_oid_nodes": 1,
            },
            {"visualContentHash": "sha256:" + "a" * 64},
            self.root,
        )
        self.assertEqual(result["score"], 100.0)
        self.assertEqual(result["metrics"]["oidHits"], 2)

    def test_gis_domain_detects_map_library_and_spatial_keywords(self) -> None:
        result = gis_eval.eval_gis_domain(
            self.root,
            {
                "frontend_dependencies": ["leaflet", "react-leaflet", "maplibre-gl"],
                "source_keywords": ["map", "lng", "lat", "geojson"],
                "render_keywords": ["MapContainer", "TileLayer"],
                "backend_keywords": ["ST_", "postgis", "geometry"],
                "min_keyword_hits": 2,
            },
        )
        self.assertGreaterEqual(result["score"], 99.9)
        self.assertIn("react-leaflet", result["metrics"]["mapDependencies"])

    def test_gis_domain_requires_real_render(self) -> None:
        result = gis_eval.eval_gis_domain(
            self.root,
            {
                "frontend_dependencies": ["leaflet", "react-leaflet"],
                "source_keywords": ["map", "lng", "lat", "geojson"],
                "render_keywords": ["__no_such_render_keyword__"],
                "backend_keywords": ["ST_", "postgis", "geometry"],
                "min_keyword_hits": 2,
            },
        )
        checks = {check["name"]: check for check in result["checks"]}
        self.assertFalse(checks["地图真实渲染"]["ok"])
        self.assertLess(result["score"], 100.0)

    def test_security_flags_missing_auth(self) -> None:
        result = gis_eval.eval_security(
            self.root,
            {
                "forbidden_patterns": ["Access-Control-Allow-Origin: *"],
                "secret_patterns": [
                    r"(?i)(password|secret|api[_-]?key|token)\s*[:=]\s*['\"][^'\"]{6,}['\"]"
                ],
                "allow_secret_files": [".env.example"],
                "auth_keywords": ["bcrypt", "jsonwebtoken", "/auth/login"],
                "require_auth": True,
            },
        )
        self.assertGreaterEqual(result["score"], 80.0)
        self.assertIn("bcrypt", result["metrics"]["authHits"])


if __name__ == "__main__":
    unittest.main()
