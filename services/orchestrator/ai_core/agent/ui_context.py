"""UI Coder 的极简上下文组装。

设计原则：Architect 负责业务、接口与数据模型，FrontendCoder 只负责
“视觉与 DOM”。因此这里只向 UI Coder 提供：

  - 目标组件附近的 JSX 结构（最多保留父级 3 层与兄弟节点）；
  - 纯视觉指令与验收标准；
  - 目标文件的行号窗口，绝不整文件倾倒。

当上下文超过预算时，会丢弃无关文件并强制模型输出局部 unified diff，
避免“大模型一次性重写整个文件”带来的超时与回滚风险。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ai_core.agent.memory import count_tokens

# 小文件允许整文件发送；超过该 Token 数只发送标注附近窗口。
UI_FULL_FILE_TOKEN_LIMIT = 1800
# 单次 UI Coder 源码上下文总预算；超过后强制局部 diff。
UI_CONTEXT_TOKEN_BUDGET = 5200
# 超过该预算即视为“复杂结构”，要求只输出局部补丁。
UI_DIFF_TOKEN_THRESHOLD = 3200
# 默认窗口：父级最多 3 层，180 行足以覆盖常见卡片 / 表单。
UI_WINDOW_MAX_LINES = 180
UI_WINDOW_MAX_TOKENS = 1600

# JSX 自闭合 / 空元素；避免把 <img /> 等压入标签栈。
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

# 只匹配真正的标签：`<` 后必须是字母（组件 / HTML）或 `/`，且内部不能
# 出现另一个 `<` / `>`。泛型 `<T>` 偶尔会误命中，但不会造成栈损坏，
# 因为只有成对标签才会形成窗口。
_JSX_TAG_RE = re.compile(r"<(/?)([A-Za-z][\w.:-]*)?([^<>]*?)(/?)>", re.DOTALL)
_QUOTED_ATTR_RE = re.compile(
    r"\[([\w:-]+)\s*=\s*[\"']([^\"']+)[\"']\]",
)
_UNQUOTED_ATTR_RE = re.compile(r"\[([\w:-]+)\s*=\s*([^\]\s]+)\]")

# 前端业务字段：UI Coder 不需要，也严禁出现在它的上下文中。
_BUSINESS_KEYS = {
    "business",
    "businessRules",
    "apiContract",
    "apiContracts",
    "apiRoute",
    "apiRoutes",
    "routes",
    "ddl",
    "dbSchema",
    "database",
    "backend",
    "authentication",
    "authorization",
}


@dataclass(frozen=True)
class SourceWindow:
    """目标文件的局部源码窗口。"""

    text: str
    start_line: int
    end_line: int
    total_lines: int
    truncated: bool

    @property
    def location(self) -> str:
        if not self.truncated:
            return f"完整文件（{self.total_lines} 行）"
        return f"第 {self.start_line}-{self.end_line} 行 / 共 {self.total_lines} 行"


@dataclass(frozen=True)
class UiCoderContext:
    """UI Coder 的最终 Prompt 上下文。"""

    annotations_text: str
    files_text: str
    tokens: int
    truncated: bool
    force_diff: bool
    included_files: tuple[str, ...] = ()
    omitted_files: tuple[str, ...] = ()
    windows: dict[str, SourceWindow] = field(default_factory=dict)

    def render(self) -> str:
        blocks = [f"视觉标注:\n{self.annotations_text}"]
        blocks.append(f"目标源码（仅窗口）:\n{self.files_text}")
        if self.omitted_files:
            blocks.append(
                "未加载文件（与本轮视觉修改无直接关系，禁止修改）: "
                + ", ".join(self.omitted_files)
            )
        return "\n\n".join(blocks)


@dataclass(frozen=True)
class _JsxPair:
    tag: str
    open_start: int
    open_end: int
    close_start: int
    close_end: int


# SDK 视觉签名 / 参考组件允许进入 Prompt 的字段。只保留真正决定视觉
# 结果的属性，避免把整份 computedStyle 倒进上下文。
_STYLE_SIGNATURE_KEYS = (
    "backgroundColor",
    "color",
    "borderColor",
    "borderWidth",
    "borderRadius",
    "fontSize",
    "fontWeight",
    "lineHeight",
    "letterSpacing",
    "padding",
    "margin",
    "gap",
    "boxShadow",
    "fontFamily",
    "textTransform",
    "display",
    "alignItems",
    "justifyContent",
)


def _compact_style_signature(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    compact: dict[str, Any] = {}
    for key in _STYLE_SIGNATURE_KEYS:
        value = raw.get(key)
        if value in (None, "", "none", "normal", "auto", "0px"):
            continue
        compact[key] = str(value)[:120]
    classes = [
        str(token).strip()
        for token in (raw.get("tailwindClasses") or raw.get("classes") or [])
        if str(token).strip()
    ]
    if classes:
        compact["tailwindClasses"] = classes[:40]
    return compact


def compact_annotations(
    annotations: list[dict[str, Any]],
    *,
    max_history: int = 4,
    include_history: bool = True,
) -> list[dict[str, Any]]:
    """把 AnnotationEnvelope 压缩成纯视觉指令。

    只保留：动作、选择器、组件、文件、视觉指令、视觉验收、相对坐标与
    最近几轮同圈对话。``business`` / API 契约 / DDL 等业务字段全部丢弃。
    """
    compacted: list[dict[str, Any]] = []
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        target = (
            annotation.get("target")
            if isinstance(annotation.get("target"), dict)
            else {}
        )
        source = (
            target.get("source")
            if isinstance(target.get("source"), dict)
            else (
                annotation.get("source")
                if isinstance(annotation.get("source"), dict)
                else {}
            )
        )
        intent = (
            annotation.get("intent")
            if isinstance(annotation.get("intent"), dict)
            else {}
        )
        ui = intent.get("ui") if isinstance(intent.get("ui"), dict) else {}
        payload = (
            annotation.get("payload")
            if isinstance(annotation.get("payload"), dict)
            else {}
        )
        # finalIntent 是后端归一化后的“唯一有效意图”。多轮对话中用户
        # 纠正过颜色 / 方案时，旧指令只保留在聊天记录中供审计，绝不能
        # 再次进入 Coder Prompt，否则模型会把“红色 + 蓝色”同时实现。
        instruction = str(
            payload.get("finalIntent")
            or payload.get("text")
            or ui.get("summary")
            or annotation.get("instruction")
            or ""
        ).strip()
        acceptance = ui.get("acceptance") or annotation.get("acceptance") or []
        if not isinstance(acceptance, list):
            acceptance = [str(acceptance)]

        item: dict[str, Any] = {
            "annotationId": annotation.get("annotationId")
            or annotation.get("annotation_id"),
            "action": annotation.get("action")
            or annotation.get("intentAction")
            or annotation.get("intent_action"),
            "file": source.get("file") or annotation.get("target_file"),
            "selector": target.get("selector") or annotation.get("selector"),
            "component": source.get("component") or annotation.get("component"),
            "instruction": instruction,
            "acceptance": [
                str(value).strip() for value in acceptance if str(value).strip()
            ][:6],
        }
        # 实例级定位：同一个源文件行可能在页面中渲染多次；把序号、
        # 总数与父级调用点一并交给模型，禁止它改基础组件来“批量生效”。
        source_view = {
            key: source.get(key)
            for key in (
                "file",
                "line",
                "column",
                "component",
                "sourceKind",
                "instance",
                "instanceCount",
                "instanceId",
                "callSite",
                "styleSignature",
            )
            if source.get(key) not in (None, "", 0)
        }
        style_signature = _compact_style_signature(source.get("styleSignature"))
        if style_signature:
            # 目标节点当前的绝对样式：模型做“更大 / 更圆 / 换个色”时
            # 必须基于这些真实值计算，而不是凭空猜。
            item["current_styles"] = style_signature
        if source_view:
            item["source"] = source_view
        for key in ("instanceId", "relativeTarget", "relative_target", "position"):
            value = annotation.get(key)
            if value is not None:
                item[key] = value
            elif target.get(key) is not None:
                item[key] = target[key]
        # 顶层业务字段显式剔除；即使调用方直接传裸 dict 也不会泄漏。
        for key, value in annotation.items():
            if key in _BUSINESS_KEYS or key in item or key in {"intent", "target"}:
                continue
            if key in {"chatHistory", "threadContext", "snippet"}:
                continue
            # 时间戳只做审计与排序，保留但不参与视觉推理；json.dumps 的
            # default=str 会把 datetime 安全序列化，避免整个 Prompt 500。
            if key in {"createdAt", "updatedAt", "timestamp"}:
                item[key] = value
                continue
            if isinstance(value, (str, int, float, bool)) and key.endswith("Id"):
                item[key] = value

        # 可视化选型：reference_styles 是 Coder 唯一允许复制的视觉来源。
        reference = (
            payload.get("reference")
            if isinstance(payload.get("reference"), dict)
            else None
        )
        if reference:
            reference_styles = _compact_style_signature(reference.get("styles"))
            reference_classes = [
                str(token).strip()
                for token in (reference.get("classes") or [])
                if str(token).strip()
            ]
            if reference_classes:
                reference_styles["tailwindClasses"] = reference_classes[:40]
            if reference_styles:
                item["reference_styles"] = reference_styles
            reference_view: dict[str, Any] = {}
            label = reference.get("label") or reference.get("tag")
            if label:
                reference_view["label"] = str(label)[:80]
            reference_source = (
                reference.get("source")
                if isinstance(reference.get("source"), dict)
                else {}
            )
            location = {
                key: reference_source.get(key)
                for key in ("file", "line", "component", "instance")
                if reference_source.get(key) not in (None, "", 0)
            }
            if location:
                reference_view["source"] = location
            if reference_view:
                item["reference"] = reference_view

        thread = (
            annotation.get("threadContext")
            if isinstance(annotation.get("threadContext"), dict)
            else {}
        )
        # Coder 只允许看到最终意图：历史用户消息包含被用户否定过的
        # “红色 / 大一点 / 用方案 A”，一旦进入 Prompt 就会被模型重新执行。
        # Planner 仍可看历史用于理解指代，Coder 则完全剥离。
        if include_history:
            history = thread.get("chatHistory") or []
            if history:
                item["chatHistory"] = [
                    {
                        "role": message.get("role"),
                        "content": str(message.get("content") or "")[:240],
                    }
                    for message in history[-max_history:]
                    if isinstance(message, dict)
                ]
            previous = thread.get("previousRuns") or []
            if previous:
                item["previousRuns"] = [
                    {
                        "summary": str(run.get("summary") or "")[:200],
                        "status": run.get("status"),
                    }
                    for run in previous[-2:]
                    if isinstance(run, dict)
                ]
        compacted.append(
            {key: value for key, value in item.items() if value not in (None, "", [])}
        )
    return compacted


def _selector_hints(
    selector: str | None,
    component: str | None,
) -> list[str]:
    """把 CSS 选择器转换为源码中可搜索的属性片段。"""
    hints: list[str] = []
    text = (selector or "").strip()
    if text:
        for match in _QUOTED_ATTR_RE.finditer(text):
            hints.append(f'{match.group(1)}="{match.group(2)}"')
            hints.append(f"{match.group(1)}='{match.group(2)}'")
        for match in _UNQUOTED_ATTR_RE.finditer(text):
            hints.append(f'{match.group(1)}="{match.group(2)}"')
            hints.append(f"{match.group(1)}='{match.group(2)}'")
        if text.startswith("#") and len(text) > 1:
            hints.append(f'id="{text[1:]}"')
        elif text.startswith(".") and len(text) > 1:
            hints.append(f"className=")
        elif re.fullmatch(r"[a-zA-Z][\w-]*", text):
            hints.append(f"<{text}")
        else:
            hints.append(text)
    if component:
        hints.append(f"<{component}")
        hints.append(f'data-source="{component}"')
    # 去重但保持顺序，优先精确属性。
    return list(dict.fromkeys(hint for hint in hints if hint))


# selector 缺失（真实画布无法注入 data-source 时的兜底）时，按“主内容区”
# 优先级定位锚点。绝不能回退到文件开头：文件头通常是 import/常量，会让
# Coder 误以为“无法定位插入位置”，从而只输出无意义的 import 补丁。
_STRUCTURAL_ANCHORS = (
    'data-source="app-main"',
    "<main",
    'role="main"',
    'data-source="page"',
    'data-source-kind="container"',
)
# `return (` 是组件主体入口，取最后一次出现（函数末尾的最终返回）。
_STRUCTURAL_RETURN = "return ("


def _structural_anchor(source: str) -> int:
    """在没有可用 selector 时定位主内容区；找不到再退回文件开头。"""
    lowered = source.lower()
    for pattern in _STRUCTURAL_ANCHORS:
        index = lowered.find(pattern)
        if index >= 0:
            # 回退到所在行行首，保证行窗口从完整标签开始。
            return lowered.rfind("\n", 0, index) + 1
    index = lowered.rfind(_STRUCTURAL_RETURN)
    if index >= 0:
        return lowered.rfind("\n", 0, index) + 1
    return 0


def _find_anchor(source: str, selector: str | None, component: str | None) -> int:
    """返回标注目标的字符偏移。

    找不到目标时回退到“主内容区”而不是文件开头：真实画布的 live iframe
    无法注入 ``data-source``，标注 ``selector`` 往往为空，此时只有主内容区
    的结构锚点才能给 Coder 一个可插入的父容器。
    """
    lines = source.splitlines(keepends=True)
    hints = _selector_hints(selector, component)
    offset = 0
    lowered_hints = [hint.lower() for hint in hints]
    for line in lines:
        lowered = line.lower()
        for index, hint in enumerate(lowered_hints):
            if not hint:
                continue
            if hints[index].startswith("className="):
                # 类选择器只要求 className 行包含类名，避免匹配整段选择器。
                token = (selector or "").lstrip(".")
                if "classname" in lowered and token.lower() in lowered:
                    return offset
                continue
            if hint in lowered:
                return offset
        offset += len(line)
    return _structural_anchor(source)


def _jsx_pairs(source: str) -> list[_JsxPair]:
    """轻量 JSX 标签配对。

    不引入 Babel / TypeScript 解析器：本项目只需要“包含目标节点的完整
    元素”，栈匹配足以覆盖正常 JSX。未闭合标签会被忽略，宁可回退到
    行窗口，也不会生成错位源码。
    """
    stack: list[tuple[str, int, int]] = []
    pairs: list[_JsxPair] = []
    for match in _JSX_TAG_RE.finditer(source):
        closing = match.group(1) == "/"
        tag = (match.group(2) or "").lower()
        self_closing = match.group(4) == "/" or tag in _VOID_TAGS
        if closing:
            for index in range(len(stack) - 1, -1, -1):
                open_tag, start, end = stack[index]
                if open_tag == tag:
                    del stack[index:]
                    pairs.append(
                        _JsxPair(
                            tag=tag,
                            open_start=start,
                            open_end=end,
                            close_start=match.start(),
                            close_end=match.end(),
                        )
                    )
                    break
            continue
        if self_closing:
            continue
        stack.append((tag, match.start(), match.end()))
    return pairs


def _line_bounds(source: str, start: int, end: int) -> tuple[int, int]:
    """字符区间 -> 1-based 行号闭区间。"""
    start_line = source.count("\n", 0, start) + 1
    end_line = source.count("\n", 0, end) + 1
    return start_line, end_line


def _slice_lines(
    lines: list[str],
    start_line: int,
    end_line: int,
) -> str:
    return "".join(lines[start_line - 1 : end_line])


def extract_jsx_window(
    source: str,
    *,
    selector: str | None = None,
    component: str | None = None,
    line: int | None = None,
    max_lines: int = UI_WINDOW_MAX_LINES,
    max_tokens: int = UI_WINDOW_MAX_TOKENS,
) -> SourceWindow:
    """提取目标节点附近窗口：父级最多 3 层 + 兄弟节点。

    ``line`` 是 SDK 回传的精确源码行号（live 画布的 data-source 是构建期
    注入的，源码里并不存在），优先级高于 selector 文本匹配。
    """
    lines = source.splitlines(keepends=True)
    total_lines = len(lines)
    if not source.strip():
        return SourceWindow("", 1, 1, total_lines, False)
    if count_tokens(source) <= UI_FULL_FILE_TOKEN_LIMIT:
        return SourceWindow(source, 1, max(total_lines, 1), total_lines, False)

    if line and 1 <= int(line) <= total_lines:
        anchor = sum(len(item) for item in lines[: int(line) - 1])
    else:
        anchor = _find_anchor(source, selector, component)
    pairs = _jsx_pairs(source)
    containing = sorted(
        (pair for pair in pairs if pair.open_start <= anchor < pair.close_end),
        key=lambda pair: pair.close_end - pair.open_start,
    )
    # 父级最多 3 层：最内层目标 + 两层父容器；同层兄弟自然包含在内。
    candidates = containing[:3]
    if candidates:
        start_char = min(pair.open_start for pair in candidates)
        end_char = max(pair.close_end for pair in candidates)
    else:
        start_line = max(1, source.count("\n", 0, anchor) + 1 - max_lines // 2)
        end_line = min(total_lines, start_line + max_lines - 1)
        start_char = sum(len(line) for line in lines[: start_line - 1])
        end_char = sum(len(line) for line in lines[:end_line])

    start_line, end_line = _line_bounds(source, start_char, end_char)
    # 前后留 4 行，帮助模型理解插入位置与缩进。
    start_line = max(1, start_line - 4)
    end_line = min(total_lines, end_line + 4)
    if end_line - start_line + 1 > max_lines:
        # 优先保留最内层完整节点；仍超限时围绕目标裁剪。
        innermost = containing[0] if containing else None
        if innermost is not None:
            inner_start, inner_end = _line_bounds(
                source, innermost.open_start, innermost.close_end
            )
            if inner_end - inner_start + 1 <= max_lines:
                start_line, end_line = inner_start, inner_end
            else:
                center = source.count("\n", 0, anchor) + 1
                start_line = max(1, center - max_lines // 2)
                end_line = min(total_lines, start_line + max_lines - 1)
        else:
            center = source.count("\n", 0, anchor) + 1
            start_line = max(1, center - max_lines // 2)
            end_line = min(total_lines, start_line + max_lines - 1)

    text = _slice_lines(lines, start_line, end_line)
    # Token 仍超限时进一步收缩，但必须保留目标锚点所在行。
    while count_tokens(text) > max_tokens and end_line - start_line > 30:
        anchor_line = source.count("\n", 0, anchor) + 1
        if anchor_line - start_line >= end_line - anchor_line:
            start_line += 8
        else:
            end_line -= 8
        text = _slice_lines(lines, start_line, end_line)
    return SourceWindow(
        text=text,
        start_line=start_line,
        end_line=end_line,
        total_lines=total_lines,
        truncated=True,
    )


def _annotation_file(annotation: dict[str, Any]) -> str | None:
    target = (
        annotation.get("target") if isinstance(annotation.get("target"), dict) else {}
    )
    source = target.get("source") if isinstance(target.get("source"), dict) else {}
    return source.get("file") or annotation.get("target_file") or annotation.get("file")


def _annotation_selector(annotation: dict[str, Any]) -> str | None:
    target = (
        annotation.get("target") if isinstance(annotation.get("target"), dict) else {}
    )
    return target.get("selector") or annotation.get("selector")


def _annotation_component(annotation: dict[str, Any]) -> str | None:
    target = (
        annotation.get("target") if isinstance(annotation.get("target"), dict) else {}
    )
    source = target.get("source") if isinstance(target.get("source"), dict) else {}
    return source.get("component") or annotation.get("component")


def _annotation_line(annotation: dict[str, Any]) -> int | None:
    """读取 SDK / Inspect 回传的精确源码行号。"""
    target = (
        annotation.get("target") if isinstance(annotation.get("target"), dict) else {}
    )
    source = target.get("source") if isinstance(target.get("source"), dict) else {}
    try:
        value = int(source.get("line") or annotation.get("line") or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def build_ui_coder_context(
    current_files: dict[str, str],
    target_files: list[str],
    annotations: list[dict[str, Any]],
    *,
    budget_tokens: int = UI_CONTEXT_TOKEN_BUDGET,
    full_file_token_limit: int = UI_FULL_FILE_TOKEN_LIMIT,
    diff_token_threshold: int = UI_DIFF_TOKEN_THRESHOLD,
    include_annotations: bool = True,
    include_history: bool = True,
) -> UiCoderContext:
    """组装 UI Coder 的极简上下文。

    多文件时优先保障第一个目标文件；其余文件如果让总预算超限，会被
    明确标记为“未加载，禁止修改”，并要求模型只输出已加载文件的局部 diff。

    ``include_history=False`` 用于 FrontendCoder：只保留最终意图，剥离
    被用户否定过的历史指令，避免旧样式被重新写回代码。
    """
    compacted = (
        compact_annotations(annotations, include_history=include_history)
        if include_annotations
        else []
    )
    annotations_text = json.dumps(
        compacted,
        ensure_ascii=False,
        default=str,
    )
    if not annotations_text:
        annotations_text = "无"

    ordered = [path for path in target_files if path in current_files]
    omitted: list[str] = [
        path for path in target_files if path and path not in current_files
    ]
    windows: dict[str, SourceWindow] = {}
    rendered: list[str] = []
    truncated = False

    def render_file(path: str, *, max_tokens: int, max_lines: int) -> tuple[str, int]:
        source = current_files.get(path, "")
        source_tokens = count_tokens(source)
        annotation = next(
            (item for item in annotations if _annotation_file(item) == path),
            None,
        )
        if source_tokens <= min(full_file_token_limit, max_tokens):
            window = SourceWindow(
                text=source,
                start_line=1,
                end_line=max(len(source.splitlines()), 1),
                total_lines=max(len(source.splitlines()), 1),
                truncated=False,
            )
        else:
            window = extract_jsx_window(
                source,
                selector=_annotation_selector(annotation) if annotation else None,
                component=_annotation_component(annotation) if annotation else None,
                line=_annotation_line(annotation) if annotation else None,
                max_lines=max_lines,
                max_tokens=max_tokens,
            )
        windows[path] = window
        block = f"### {path}（{window.location}）\n```jsx\n{window.text}\n```"
        return block, count_tokens(block)

    per_file_budget = max(600, budget_tokens // max(1, len(ordered)))
    for path in ordered:
        block, tokens_used = render_file(
            path,
            max_tokens=per_file_budget,
            max_lines=UI_WINDOW_MAX_LINES,
        )
        candidate = "\n\n".join([*rendered, block])
        if rendered and count_tokens(candidate) > budget_tokens:
            omitted.append(path)
            truncated = True
            continue
        rendered.append(block)

    if not rendered and ordered:
        # 极端情况：单文件窗口仍超预算。缩小窗口，保证至少有一个可编辑文件。
        block, _ = render_file(ordered[0], max_tokens=1200, max_lines=90)
        rendered.append(block)
        truncated = True

    files_text = "\n\n".join(rendered) or "无可用源码（请只依据标注生成局部补丁）"
    total_tokens = count_tokens(annotations_text) + count_tokens(files_text)
    any_window_truncated = any(window.truncated for window in windows.values())
    force_diff = (
        truncated or any_window_truncated or total_tokens > diff_token_threshold
    )
    return UiCoderContext(
        annotations_text=annotations_text,
        files_text=files_text,
        tokens=total_tokens,
        truncated=truncated or any_window_truncated,
        force_diff=force_diff,
        included_files=tuple(windows),
        omitted_files=tuple(dict.fromkeys(omitted)),
        windows=windows,
    )


__all__ = [
    "SourceWindow",
    "UiCoderContext",
    "build_ui_coder_context",
    "compact_annotations",
    "extract_jsx_window",
]
