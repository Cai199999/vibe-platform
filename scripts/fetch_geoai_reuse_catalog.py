#!/usr/bin/env python3
"""拉取 GeoAI 开源项目元数据/README，生成本地复用目录。

- 只读 GitHub API，不执行仓库代码；
- README 全文只写到 gitignored 的 data/reuse/geoai/；
- 可提交的 knowledge/geoai_reuse/catalog.md 只保留摘要、链接、许可证和建议；
- 无 GITHUB_TOKEN 时使用匿名 API（60 次/小时，足够本脚本）。

运行：
    python3 scripts/fetch_geoai_reuse_catalog.py
    GITHUB_TOKEN=... python3 scripts/fetch_geoai_reuse_catalog.py
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README_DIR = Path(os.environ.get("GEOAI_REUSE_README_DIR", "/tmp/geoai_reuse"))
CATALOG_DIR = ROOT / "services" / "orchestrator" / "knowledge" / "geoai_reuse"
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

REPOS = [
    {
        "full_name": "opengeos/geoai",
        "role": "Python GeoAI 库：地理空间数据 + AI 模型/流程",
        "reuse": "后端地理处理、遥感推理、数据准备；不适合直接生成前端",
    },
    {
        "full_name": "decision-labs/geoai.js",
        "role": "浏览器端 GeoAI：transformers.js + 前端推理",
        "reuse": "前端地图/影像推理模式参考；当前平台 CodeGen 未接该依赖",
    },
    {
        "full_name": "azavea/raster-vision",
        "role": "遥感/航拍影像深度学习框架",
        "reuse": "训练/推理流水线、切片、标注与评估参考",
    },
    {
        "full_name": "sentinel-hub/eo-learn",
        "role": "Earth Observation 机器学习处理框架",
        "reuse": "卫星影像获取、预处理、EO 工作流参考",
    },
    {
        "full_name": "MEKXH/golem",
        "role": "自然语言驱动的 GeoAI Agent（GDAL/PostGIS 工具调用）",
        "reuse": "GIS Agent 的工具编排/意图拆解参考，不是运行时依赖",
    },
    {
        "full_name": "opengeos/qgis-samgeo-plugin",
        "role": "QGIS + SAM 分割插件",
        "reuse": "桌面 GIS 交互与影像分割工作流参考",
    },
    {
        "full_name": "microsoft/torchgeo",
        "role": "PyTorch 地理空间数据集/模型库",
        "reuse": "遥感数据集、预训练模型、训练管线参考",
    },
    {
        "full_name": "deepVector/geospatial-machine-learning",
        "role": "地理空间机器学习资源清单",
        "reuse": "选型发现入口，不是代码依赖",
    },
]

HEADERS = {"User-Agent": "vibe-platform-geoai-catalog"}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def _readme(full_name: str) -> str:
    try:
        payload = _get_json(f"https://api.github.com/repos/{full_name}/readme")
        raw = base64.b64decode(payload.get("content") or "").decode(
            "utf-8", errors="replace"
        )
    except Exception as exc:  # noqa: BLE001 - README 缺失不阻塞目录
        return f"（README 获取失败：{exc}）"
    # 去掉 badges / HTML / 多余空行，避免 catalog 过于臃肿。
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", raw)
    raw = re.sub(r"\n{2,}", "\n", raw)
    return raw.strip()


def _excerpt(text: str, limit: int = 900) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned[:limit]


def main() -> int:
    README_DIR.mkdir(parents=True, exist_ok=True)
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    entries = []

    for spec in REPOS:
        full_name = spec["full_name"]
        print(f"fetching {full_name}...")
        try:
            meta = _get_json(f"https://api.github.com/repos/{full_name}")
        except urllib.error.HTTPError as exc:
            print(f"  skip {full_name}: HTTP {exc.code}")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"  skip {full_name}: {exc}")
            continue

        readme = _readme(full_name)
        slug = full_name.replace("/", "__")
        (README_DIR / f"{slug}.md").write_text(readme, encoding="utf-8")
        license_name = (meta.get("license") or {}).get("spdx_id") or "UNKNOWN"
        entry = {
            "fullName": full_name,
            "url": meta.get("html_url"),
            "description": meta.get("description") or "",
            "stars": meta.get("stargazers_count") or 0,
            "language": meta.get("language") or "",
            "license": license_name,
            "pushedAt": meta.get("pushed_at") or "",
            "topics": meta.get("topics") or [],
            "role": spec["role"],
            "reuse": spec["reuse"],
            "excerpt": _excerpt(readme),
        }
        entries.append(entry)

    (CATALOG_DIR / "repos.json").write_text(
        json.dumps(
            {"generatedAt": generated_at, "repos": entries},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    lines = [
        "# GeoAI 复用目录（本地）",
        "",
        f"> 生成时间：{generated_at}",
        "> 只读参考：不是运行时依赖，也不会自动安装；许可证与维护状态需在复用前复核。",
        "",
        "## 怎么用",
        "",
        "- 需求涉及遥感/测绘/GIS + AI 时，先看本目录有没有可复用方案；",
        "- 输出建议时标注 **REUSE / ADAPT / BUILD**；",
        "- 当前平台 CodeGen 白名单只放行 React/Vite/Tailwind 与本地模块；",
        "  Cesium/OpenLayers/GDAL/PostGIS 等属于“外部组件/后端能力”，先出集成方案，不自动生成。",
        "",
        "## 候选项目",
        "",
    ]
    for entry in entries:
        lines.extend(
            [
                f"### {entry['fullName']}",
                "",
                f"- URL：{entry['url']}",
                f"- License：{entry['license']} · Stars：{entry['stars']} · "
                f"最近更新：{entry['pushedAt'][:10]} · 语言：{entry['language']}",
                f"- 定位：{entry['role']}",
                f"- 可复用：{entry['reuse']}",
                f"- 描述：{entry['description']}",
                f"- README 摘要：{entry['excerpt']}",
                "",
            ]
        )

    lines.extend(
        [
            "## 选型提示",
            "",
            "- **前端地图/浏览器推理**：参考 `decision-labs/geoai.js`；是否引入取决于前端依赖白名单。",
            "- **遥感训练/推理**：`microsoft/torchgeo`、`azavea/raster-vision`、`sentinel-hub/eo-learn`。",
            "- **GIS Agent/工具编排**：`MEKXH/golem` 的意图拆解与 GDAL/PostGIS 工具调用值得参考。",
            "- **QGIS 桌面工作流**：`opengeos/qgis-samgeo-plugin`。",
            "- **资源发现**：`deepVector/geospatial-machine-learning`。",
            "",
            "> 复用建议必须附带：许可证、维护状态、集成成本、与当前契约的边界；不得自动安装或执行仓库代码。",
        ]
    )
    catalog_path = CATALOG_DIR / "catalog.md"
    catalog_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"catalog: {catalog_path}")
    print(f"repos: {len(entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
