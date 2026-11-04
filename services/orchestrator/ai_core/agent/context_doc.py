"""生成导出包里的 VIBE_CONTEXT.md。

用户在画布上圈选组件并提交意图后，Agent 在沙盒里修改代码。导出源码包时，
本地 Cursor / Claude Code 等工具看不到画布与标注，因此把本次 Run 的
AnnotationEnvelope 渲染成 Markdown，随源码一起打包。

文件放在项目根目录，命名固定为 VIBE_CONTEXT.md。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ai_core.agent.schemas import ExecuteRunRequest

VIBE_CONTEXT_FILENAME = "VIBE_CONTEXT.md"


def render_vibe_context(
    request: ExecuteRunRequest,
    run_id: str,
    sandbox_id: str,
    target_file: str,
) -> str:
    """把 Run 的画布标注与意图渲染为 Markdown。

    内容同时面向人和模型：
      - 上半部分是概要、意图、验收标准与选区，便于快速理解；
      - 下半部分保留完整 AnnotationEnvelope JSON，便于工具精确解析。
    """
    annotation = request.annotation
    intent = annotation.intent
    ui = intent.ui if intent else None
    payload_text = str(annotation.payload.get("text") or "").strip()
    summary = (ui.summary if ui else None) or payload_text or "（未填写补充说明）"
    acceptance = list(ui.acceptance) if ui and ui.acceptance else []
    target = annotation.target
    source = target.source if target else None
    world = annotation.world_target or {}

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    created_at = (
        annotation.created_at.isoformat() if annotation.created_at else "（未知）"
    )

    lines: list[str] = [
        "# VIBE_CONTEXT · 画布修改上下文",
        "",
        "> 本文件由 vibe-platform 在 Agent 执行时自动生成，记录用户在画布上",
        "> 圈选组件后提交的修改意图。下面的源码已经包含本次修改。",
        "> 在 Cursor / Claude Code 等工具中继续开发时，请先阅读本文件，",
        "> 保持与以下意图一致；除用户明确要求外，不要改动未提及的布局、",
        "> 文案与交互。",
        "",
        "## 概要",
        "",
        f"- 项目：`{request.project_id}`",
        f"- Run：`{run_id}`",
        f"- 沙盒：`{sandbox_id}`",
        f"- 目标文件：`{target_file}`",
        f"- 组件：`{source.component if source and source.component else '未知'}`",
        f"- 选择器：`{target.selector if target and target.selector else '未提供'}`",
        f"- 意图类型：`{intent.kind if intent else 'UI_ONLY'}`",
        f"- 标注时间：{created_at}",
        f"- 导出上下文生成时间：{generated_at}",
        "",
        "## 用户修改意图",
        "",
        payload_text or "（用户未填写自然语言说明，请以下方的 UI 摘要为准）",
        "",
        "## UI 变更摘要",
        "",
        summary,
        "",
        "## 验收标准",
        "",
    ]

    if acceptance:
        lines.extend(f"- {item}" for item in acceptance)
    else:
        lines.append("- （未提供，保持现有布局与文案不变）")

    lines.extend(
        [
            "",
            "## 画布选区（归一化坐标 0~1）",
            "",
        ]
    )
    if world:
        for key in ("x", "y", "width", "height", "coordinateSpace"):
            if key in world:
                lines.append(f"- {key}: `{world[key]}`")
    else:
        lines.append("- （未提供）")

    lines.extend(
        [
            "",
            "## 目标源码定位",
            "",
            f"- 文件：`{source.file if source and source.file else target_file}`",
        ]
    )
    if source:
        location = ":".join(
            str(part) for part in (source.line, source.column) if part is not None
        )
        if location:
            lines.append(f"- 行:列：`{location}`")
    if target and target.snippet and target.snippet.hash:
        lines.append(f"- 片段 hash：`{target.snippet.hash}`")

    envelope_json = json.dumps(
        annotation.model_dump(by_alias=True, exclude_none=True),
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    lines.extend(
        [
            "",
            "## 原始 AnnotationEnvelope（机器可读）",
            "",
            "~~~json",
            envelope_json,
            "~~~",
            "",
            "## 给 AI 编码助手的提示",
            "",
            "1. 这是一个 React 18 + Vite 项目，入口为 `src/main.jsx`，",
            "   本次修改目标是上方列出的文件与组件。",
            "2. 上述修改已经由 Agent 应用并通过构建校验，当前源码即修改后的",
            "   版本；继续开发时请在此基础上做增量修改。",
            "3. 如果新的需求与本文件描述冲突，以用户最新的自然语言指令为准。",
            "4. 不要提交 `node_modules`、`.env`、构建产物或任何密钥。",
            "",
        ]
    )
    return "\n".join(lines)
