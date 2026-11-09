"""图片导入：视觉模型识别页面结构，生成可运行的 React + Vite 初始代码。

没有配置 ``VISION_API_KEY`` 时使用确定性 Mock 模板，保证离线 / CI 环境
也能完整走通“上传图片 -> 新建 Tab -> 预览”的链路。真实调用只接受
服务端配置的 Base URL / 模型，绝不从浏览器传入。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ai_core.config import Settings
from ai_core.ports.sandbox import SandboxValidationError
from ai_core.sandbox.importer import ImportedProject

logger = logging.getLogger(__name__)

_SUPPORTED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
}
_MAX_TEXT = 120


@dataclass
class VisionSpec:
    """视觉模型输出的强约束结构。"""

    title: str
    subtitle: str
    description: str
    primary_action: str
    sections: list[dict[str, str]] = field(default_factory=list)
    accent: str = "#2563eb"

    def normalized(self) -> "VisionSpec":
        def clean(value: Any, fallback: str, limit: int = _MAX_TEXT) -> str:
            text = str(value or "").strip()
            return (text or fallback)[:limit]

        accent = str(self.accent or "").strip()
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
            accent = "#2563eb"
        sections: list[dict[str, str]] = []
        for item in self.sections[:6]:
            if not isinstance(item, dict):
                continue
            sections.append(
                {
                    "heading": clean(item.get("heading"), "核心能力", 40),
                    "body": clean(item.get("body"), "来自导入图片的内容区块", 120),
                }
            )
        if not sections:
            sections = [
                {
                    "heading": "清晰的信息层级",
                    "body": "还原图片中的主标题、说明与行动按钮。",
                },
                {
                    "heading": "可直接编辑",
                    "body": "所有文案与布局都是真实 JSX，可继续在画布上修改。",
                },
            ]
        return VisionSpec(
            title=clean(self.title, "从图片导入的页面", 48),
            subtitle=clean(self.subtitle, "Vision Import", 48),
            description=clean(
                self.description,
                "根据上传图片生成的首屏页面，可在画布上继续标注修改。",
                180,
            ),
            primary_action=clean(self.primary_action, "开始体验", 20),
            sections=sections,
            accent=accent,
        )


def _fallback_spec(filename: str) -> VisionSpec:
    stem = filename.rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip()
    return VisionSpec(
        title=(stem[:40] or "从图片导入的页面"),
        subtitle="Vision Import",
        description="视觉模型未配置或调用失败，已生成结构化占位页面；图片本身作为首屏主视觉。",
        primary_action="开始体验",
        sections=[
            {"heading": "上传即预览", "body": "图片已写入沙盒，后续可以继续圈选修改。"},
            {"heading": "结构可编辑", "body": "标题、说明与按钮均为真实 JSX 节点。"},
        ],
    ).normalized()


def _extract_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        text = match.group(0)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("视觉模型没有返回 JSON 对象")
    return payload


def describe_image(data: bytes, mime_type: str, settings: Settings) -> VisionSpec:
    """调用 OpenAI 兼容视觉模型；任何失败都降级为确定性模板。"""
    if not settings.vision_api_key:
        logger.info("VISION_API_KEY 未配置，使用图片导入 Mock 模板")
        return _fallback_spec("imported-image.png")

    try:
        from langchain_core.messages import HumanMessage
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - 依赖缺失时降级
        logger.warning("视觉模型依赖缺失，使用 Mock: %s", exc)
        return _fallback_spec("imported-image.png")

    encoded = base64.b64encode(data).decode("ascii")
    data_url = f"data:{mime_type};base64,{encoded}"
    prompt = (
        "你是资深 UI 设计师。请分析这张产品界面截图，输出一个 JSON 对象，"
        "字段严格为：title（主标题，<=40字）、subtitle（小标题，<=30字）、"
        "description（说明，<=120字）、primaryAction（主按钮文案，<=12字）、"
        "accent（主色 #RRGGBB）、sections（2~4 个内容区块，每项含 heading / body）。"
        "只输出 JSON，不要 Markdown。文案使用截图中的语言。"
    )

    try:
        model = ChatOpenAI(
            api_key=settings.vision_api_key,
            base_url=settings.vision_base_url,
            model=settings.vision_model,
            temperature=0.2,
            timeout=settings.vision_timeout_seconds,
            max_retries=1,
        )
        response = model.invoke(
            [
                HumanMessage(
                    content=[
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ]
                )
            ]
        )
        raw = response.content
        if isinstance(raw, list):
            raw = "".join(str(part) for part in raw)
        payload = _extract_json(str(raw))
        spec = VisionSpec(
            title=str(payload.get("title") or ""),
            subtitle=str(payload.get("subtitle") or ""),
            description=str(payload.get("description") or ""),
            primary_action=str(
                payload.get("primaryAction") or payload.get("primary_action") or ""
            ),
            sections=payload.get("sections") or [],
            accent=str(payload.get("accent") or ""),
        ).normalized()
        return spec
    except Exception as exc:  # noqa: BLE001 - 视觉模型失败必须可降级
        logger.warning("视觉模型调用失败，使用 Mock 模板: %s", exc)
        return _fallback_spec("imported-image.png")


def _render_app(spec: VisionSpec) -> str:
    """把结构化内容渲染为 React 页面。

    文案通过 ``JSON.stringify`` 生成 JS 常量，绝不直接拼进 JSX 文本，
    防止模型输出中的 ``{`` / ``</script>`` 破坏源码结构。
    """
    content = json.dumps(
        {
            "title": spec.title,
            "subtitle": spec.subtitle,
            "description": spec.description,
            "primaryAction": spec.primary_action,
            "sections": spec.sections,
            "accent": spec.accent,
        },
        ensure_ascii=False,
    )
    return f"""const CONTENT = {content}

export default function App() {{
  return (
    <main data-source="page" data-source-kind="container" className="vision-page">
      <section
        data-source="hero"
        data-source-kind="container"
        className="vision-card"
      >
        <div className="vision-copy">
          <p data-source="kicker" className="vision-kicker">{{CONTENT.subtitle}}</p>
          <h1 data-source="title" className="vision-title">{{CONTENT.title}}</h1>
          <p data-source="description" className="vision-lede">{{CONTENT.description}}</p>
          <button
            data-source="primary-action"
            className="vision-cta"
            style={{{{ backgroundColor: CONTENT.accent }}}}
            type="button"
          >
            {{CONTENT.primaryAction}}
          </button>
        </div>
        <div className="vision-art" aria-hidden="true">
          <img src="/imported-image.png" alt="" />
        </div>
      </section>

      <section
        data-source="features"
        data-source-kind="container"
        className="vision-features"
      >
        {{CONTENT.sections.map((item, index) => (
          <article
            key={{item.heading}}
            data-source={{`feature-${{index + 1}}`}}
            className="vision-feature"
          >
            <h2>{{item.heading}}</h2>
            <p>{{item.body}}</p>
          </article>
        ))}}
      </section>
    </main>
  )
}}
"""


_APP_CSS = """*{box-sizing:border-box}
html,body,#root{margin:0;min-height:100%;background:#0b1120;color:#e2e8f0;font-family:"Inter","Segoe UI",sans-serif}
.vision-page{min-height:100vh;display:flex;flex-direction:column;justify-content:center;gap:24px;padding:32px;max-width:1180px;margin:0 auto}
.vision-card{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,.95fr);gap:28px;align-items:center;padding:32px;border-radius:24px;background:linear-gradient(135deg,rgba(30,41,59,.92),rgba(15,23,42,.92));border:1px solid rgba(148,163,184,.22);box-shadow:0 30px 80px rgba(0,0,0,.45)}
.vision-kicker{margin:0;color:#38bdf8;font-size:12px;letter-spacing:.28em;text-transform:uppercase}
.vision-title{margin:12px 0 14px;font-size:42px;line-height:1.12}
.vision-lede{margin:0;color:#94a3b8;font-size:15px;line-height:1.75;max-width:52ch}
.vision-cta{margin-top:24px;padding:13px 26px;border:0;border-radius:999px;color:#fff;font-size:14px;font-weight:700;cursor:pointer}
.vision-art{min-height:260px;border-radius:18px;overflow:hidden;background:rgba(15,23,42,.6);border:1px solid rgba(148,163,184,.18)}
.vision-art img{width:100%;height:100%;object-fit:cover;display:block}
.vision-features{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}
.vision-feature{padding:20px;border-radius:16px;background:rgba(15,23,42,.72);border:1px solid rgba(148,163,184,.16)}
.vision-feature h2{margin:0 0 8px;font-size:17px}
.vision-feature p{margin:0;color:#94a3b8;font-size:13.5px;line-height:1.65}
@media(max-width:760px){.vision-card{grid-template-columns:1fr;padding:24px}.vision-title{font-size:32px}.vision-art{min-height:200px}}
"""

_INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Vision Import</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
"""

_MAIN_JSX = """import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import './styles.css'

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
"""

_PACKAGE_JSON = """{
  "name": "vibe-vision-import",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "18.3.1",
    "react-dom": "18.3.1"
  },
  "devDependencies": {
    "@vitejs/plugin-react": "4.3.4",
    "vite": "5.4.21"
  }
}
"""

_VITE_CONFIG = """import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 沙盒容器通过 5173 暴露预览；host 必须为 0.0.0.0。
export default defineConfig({
  plugins: [react()],
  server: { host: '0.0.0.0', port: 5173 },
})
"""


def import_image(
    data: bytes,
    filename: str,
    content_type: str,
    settings: Settings,
) -> ImportedProject:
    """校验图片并生成 React 项目；图片本身作为首屏主视觉。"""
    if not data:
        raise SandboxValidationError("上传的图片为空")
    if len(data) > settings.import_max_image_bytes:
        raise SandboxValidationError(
            f"图片超过大小限制（{len(data)} > {settings.import_max_image_bytes} 字节）"
        )
    mime = (content_type or "").split(";")[0].strip().lower()
    suffix = _SUPPORTED_IMAGE_TYPES.get(mime)
    if suffix is None:
        suffix = ".png" if mime in {"", "application/octet-stream"} else ""
        # 扩展名兜底：部分浏览器上传时 content-type 不可靠。
        lowered = filename.lower()
        if suffix == "" and lowered.endswith((".png", ".jpg", ".jpeg")):
            suffix = ".png" if lowered.endswith(".png") else ".jpg"
    if not suffix:
        raise SandboxValidationError("只支持 .png / .jpg 图片")

    spec = describe_image(
        data, "image/png" if suffix == ".png" else "image/jpeg", settings
    )
    files: dict[str, bytes] = {
        "package.json": _PACKAGE_JSON.encode(),
        "vite.config.js": _VITE_CONFIG.encode(),
        "index.html": _INDEX_HTML.encode(),
        "src/main.jsx": _MAIN_JSX.encode(),
        "src/App.jsx": _render_app(spec).encode(),
        "src/styles.css": _APP_CSS.encode(),
        "public/imported-image.png": data,
    }
    text_files = {
        path: content.decode("utf-8")
        for path, content in files.items()
        if path != "public/imported-image.png"
    }
    return ImportedProject(
        kind="react",
        files=files,
        text_files=text_files,
        entry_file="src/App.jsx",
        start_command=(
            f"npm install --no-audit --no-fund && "
            f"npm run dev -- --host 0.0.0.0 --port {settings.sandbox_dev_port}"
        ),
        file_count=len(files),
        template="react-vite",
        warnings=[],
    )
