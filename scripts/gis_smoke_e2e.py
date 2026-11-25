#!/usr/bin/env python3
"""GIS 生成项目的轻量冒烟 E2E（不依赖浏览器）。

检查：
  1. 预览页可访问；
  2. 前端源码包含真实地图组件（MapContainer / TileLayer）；
  3. 前端包含 API runtime；
  4. 后端 /api/health 可用；
  5. 主要集合端点可访问（401/403 视为受保护，不判失败）。

用法：
    python scripts/gis_smoke_e2e.py --preview-url http://127.0.0.1:44913
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def fetch(url: str, *, timeout: float = 10) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        return 0, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview-url", required=True)
    parser.add_argument(
        "--out",
        default=str(ROOT / "reports" / "gis"),
    )
    args = parser.parse_args()
    base = args.preview_url.rstrip("/")

    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail[:500]})

    status, html = fetch(f"{base}/")
    check("预览页可访问", status == 200, f"HTTP {status}")
    check("预览页有应用根节点", 'id="root"' in html, "index.html 含 #root")

    status, source = fetch(f"{base}/src/App.jsx")
    check("前端源码可访问", status == 200, f"HTTP {status}")
    map_hits = [
        keyword
        for keyword in ("MapContainer", "TileLayer", "maplibre")
        if keyword in source
    ]
    check("真实地图组件", bool(map_hits), "命中：" + "、".join(map_hits))
    check(
        "API runtime",
        "VISUAL_ACTIONS" in source or "fetch(" in source,
        "含 VISUAL_ACTIONS / fetch",
    )
    check(
        "前端包含契约端点",
        "/api/v1/" in source,
        "含 /api/v1/ 字面量",
    )
    status, data_list = fetch(f"{base}/src/DataList.jsx")
    check(
        "数据列表组件",
        status == 200 and "data-testid" in data_list,
        f"HTTP {status}，DataList 含 data-testid",
    )
    check(
        "列表已绑定端点",
        "DataList" in source,
        "App.jsx 使用 DataList",
    )

    status, health = fetch(f"{base}/api/health")
    check("后端健康检查", status == 200, f"HTTP {status} {health[:120]}")

    status, collection = fetch(f"{base}/api/v1/facilities")
    protected = status in {401, 403}
    check(
        "集合端点可访问",
        status == 200 or protected,
        f"HTTP {status}" + ("（受保护）" if protected else ""),
    )

    payload = json.dumps(
        {
            "name": "smoke-facility",
            "type": "station",
            "status": "active",
            "lng": 116.4,
            "lat": 39.9,
        }
    ).encode("utf-8")
    write_request = urllib.request.Request(
        f"{base}/api/v1/facilities",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(write_request, timeout=10) as response:
            write_status = response.status
    except urllib.error.HTTPError as exc:
        write_status = exc.code
    except (urllib.error.URLError, TimeoutError):
        write_status = 0
    check(
        "集合写入/受保护",
        write_status in {200, 201, 401, 403},
        f"HTTP {write_status}",
    )
    if write_status in {200, 201}:
        _, after_write = fetch(f"{base}/api/v1/facilities")
        check(
            "写入后列表可读取",
            "smoke-facility" in after_write,
            "GET 列表包含 smoke-facility",
        )

    ok = all(item["ok"] for item in checks)
    report = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "previewUrl": base,
        "ok": ok,
        "checks": checks,
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"smoke-{stamp}.json"
    md_path = out_dir / f"smoke-{stamp}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# GIS 冒烟 E2E",
        "",
        f"- 预览：`{base}`",
        f"- 结果：**{'PASS' if ok else 'FAIL'}**",
        "",
        "| 检查 | 结果 | 证据 |",
        "|---|---|---|",
    ]
    for item in checks:
        lines.append(
            f"| {item['name']} | {'✅' if item['ok'] else '❌'} | "
            f"{item['detail']} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{'PASS' if ok else 'FAIL'} preview={base}")
    for item in checks:
        print(f"  {'✅' if item['ok'] else '❌'} {item['name']}: {item['detail']}")
    print(f"json={json_path}")
    print(f"md={md_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
