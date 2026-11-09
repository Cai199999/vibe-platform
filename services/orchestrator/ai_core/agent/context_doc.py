"""生成导出包里的 VIBE_CONTEXT.md。

用户在画布上圈选组件并提交意图后，Agent 在沙盒里修改代码。导出源码包时，
本地 Cursor / Claude Code 等工具看不到画布与标注，因此把本次 Run 的
AnnotationEnvelope 渲染成 Markdown，随源码一起打包。

M1 升级后一次 Run 最多包含 3 个标注，本文件会把它们全部列出；文件放在项目
根目录，命名固定为 VIBE_CONTEXT.md。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ai_core.domain.agent import AnnotationEnvelope, ExecuteRunRequest

VIBE_CONTEXT_FILENAME = "VIBE_CONTEXT.md"


def _instruction(annotation: AnnotationEnvelope) -> str:
    """提取用户自然语言指令；payload.text 优先，UI 摘要兜底。"""
    text = str(annotation.payload.get("text") or "").strip()
    if text:
        return text
    if annotation.intent and annotation.intent.ui and annotation.intent.ui.summary:
        return annotation.intent.ui.summary
    return "（未填写补充说明）"


def _target_file(annotation: AnnotationEnvelope, fallback: str) -> str:
    source = annotation.target.source if annotation.target else None
    if source and source.file:
        return source.file
    return fallback


def render_vibe_context(
    request: ExecuteRunRequest,
    run_id: str,
    sandbox_id: str,
    target_files: list[str],
) -> str:
    """把 Run 的全部画布标注与意图渲染为 Markdown。

    内容同时面向人和模型：
      - 上半部分是概要、逐条意图、验收标准与选区，便于快速理解；
      - 下半部分保留完整 AnnotationEnvelope[] JSON，便于工具精确解析。
    """
    annotations = request.effective_annotations
    fallback_file = target_files[0] if target_files else "src/App.jsx"
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    lines: list[str] = [
        "# VIBE_CONTEXT · 画布修改上下文",
        "",
        "> 本文件由 vibe-platform 在 Agent 执行时自动生成，记录用户在画布上",
        "> 圈选组件后提交的修改意图。下面的源码已经包含本次全部修改。",
        "> 在 Cursor / Claude Code 等工具中继续开发时，请先阅读本文件，",
        "> 保持与以下意图一致；除用户明确要求外，不要改动未提及的布局、",
        "> 文案与交互。",
        "",
        "## 概要",
        "",
        f"- 项目：`{request.project_id}`",
        f"- Run：`{run_id}`",
        f"- 沙盒：`{sandbox_id}`",
        f"- 标注数量：{len(annotations)}",
        f"- 目标文件：{', '.join(f'`{path}`' for path in target_files) or f'`{fallback_file}`'}",
        f"- 导出上下文生成时间：{generated_at}",
        "",
        "## 标注清单",
        "",
    ]

    for index, annotation in enumerate(annotations, start=1):
        intent = annotation.intent
        ui = intent.ui if intent else None
        summary = _instruction(annotation)
        action_label = "新增组件" if annotation.action == "ADD" else "修改"
        lines.append(
            f"{index}. **标注 {index}** · {action_label}"
            f" · `{_target_file(annotation, fallback_file)}`"
            f" · {summary}"
        )

    lines.extend(["", "## 用户修改意图（逐条）", ""])

    for index, annotation in enumerate(annotations, start=1):
        intent = annotation.intent
        ui = intent.ui if intent else None
        target = annotation.target
        source = target.source if target else None
        world = annotation.world_target or {}
        acceptance = list(ui.acceptance) if ui and ui.acceptance else []
        created_at = (
            annotation.created_at.isoformat() if annotation.created_at else "（未知）"
        )

        lines.extend(
            [
                f"### 标注 {index} · `{annotation.annotation_id}`",
                "",
                f"- 操作类型：`{'ADD（新增组件）' if annotation.action == 'ADD' else 'MODIFY（修改已有元素）'}`",
                f"- 目标文件：`{_target_file(annotation, fallback_file)}`",
                f"- 组件：`{source.component if source and source.component else '未知'}`",
                f"- 选择器：`{target.selector if target and target.selector else '未提供'}`",
                f"- 意图类型：`{intent.kind if intent else 'UI_ONLY'}`",
                f"- 标注时间：{created_at}",
                "",
                "**用户指令**",
                "",
                summary,
                "",
                "**UI 摘要**",
                "",
                (ui.summary if ui and ui.summary else summary),
                "",
                "**验收标准**",
                "",
            ]
        )
        if acceptance:
            lines.extend(f"- {item}" for item in acceptance)
        else:
            lines.append("- （未提供，保持现有布局与文案不变）")

        if annotation.chat_history:
            lines.extend(["", "**同圈多轮对话**", ""])
            for message in annotation.chat_history[-10:]:
                role = "用户" if message.role == "user" else "助手"
                lines.append(f"- {role}：{message.content}")

        lines.extend(["", "**画布选区（归一化坐标 0~1）**", ""])
        if world:
            for key in ("x", "y", "width", "height", "coordinateSpace"):
                if key in world:
                    lines.append(f"- {key}: `{world[key]}`")
        else:
            lines.append("- （未提供）")

        relative = annotation.relative_target or {}
        if annotation.action == "ADD":
            lines.extend(["", "**新节点相对父容器的位置（ADD）**", ""])
            if relative:
                for key in (
                    "x",
                    "y",
                    "width",
                    "height",
                    "coordinateSpace",
                    "parentSelector",
                ):
                    if key in relative:
                        lines.append(f"- {key}: `{relative[key]}`")
            else:
                lines.append("- （未提供，插入到父容器末尾）")

        lines.extend(["", "**目标源码定位**", ""])
        lines.append(
            f"- 文件：`{source.file if source and source.file else _target_file(annotation, fallback_file)}`"
        )
        if source:
            location = ":".join(
                str(part) for part in (source.line, source.column) if part is not None
            )
            if location:
                lines.append(f"- 行:列：`{location}`")
        if target and target.snippet and target.snippet.hash:
            lines.append(f"- 片段 hash：`{target.snippet.hash}`")

        lines.append("")

    envelope_json = json.dumps(
        [
            annotation.model_dump(by_alias=True, exclude_none=True)
            for annotation in annotations
        ],
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    lines.extend(
        [
            "## 原始 AnnotationEnvelope[]（机器可读）",
            "",
            "~~~json",
            envelope_json,
            "~~~",
            "",
            "## 给 AI 编码助手的提示",
            "",
            "1. 这是一个 React 18 + Vite 项目，入口为 `src/main.jsx`，",
            "   本次修改目标是上方列出的文件与组件。",
            f"2. 上述 {len(annotations)} 处修改已经由 Agent 一次性应用并通过构建校验，",
            "   当前源码即修改后的版本；继续开发时请在此基础上做增量修改。",
            "3. 如果新的需求与本文件描述冲突，以用户最新的自然语言指令为准。",
            "4. 不要提交 `node_modules`、`.env`、构建产物或任何密钥。",
            "",
        ]
    )
    return "\n".join(lines)
