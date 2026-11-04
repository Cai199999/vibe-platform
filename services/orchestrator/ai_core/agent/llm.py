"""Agent 使用的 LLM 适配层。

M1 支持两种实现：
  - MockAgentLLM：零密钥、确定性输出，用于闭环与 E2E；
  - DeepSeekAgentLLM：OpenAI 兼容接口，读取 DEEPSEEK_API_KEY。

一次 Run 最多包含 3 个画布标注。Planner 必须综合全部标注生成一个统一计划，
Coder 必须一次性输出包含全部修改的 patch / files；禁止只处理第一个标注。

模型只负责“计划”和“代码生成”，不直接操作沙盒；工具调用由 Graph 节点完成，
这样模型输出可以被结构化校验与重试。
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ai_core.agent.schemas import ExecuteRunRequest, RunOptions
from ai_core.config import Settings, get_settings


class AgentLLMError(Exception):
    """LLM 输出不可用。"""


class AgentLLMConfigurationError(AgentLLMError):
    """缺少模型密钥或配置。"""


@dataclass(frozen=True)
class PlanResult:
    plan: str


@dataclass(frozen=True)
class CodeResult:
    """代码生成结果：full files 与 unified diff 二选一。"""

    summary: str
    files: dict[str, str] = field(default_factory=dict)
    patch: str | None = None


class AgentLLM:
    """Planner / Coder 共用的模型接口。"""

    def plan(
        self,
        annotations: list[dict[str, Any]],
        current_files: dict[str, str],
        previous_errors: str | None = None,
    ) -> PlanResult:
        raise NotImplementedError

    def generate(
        self,
        annotations: list[dict[str, Any]],
        plan: str,
        current_files: dict[str, str],
        target_files: list[str],
        verify_result: dict[str, Any],
        retry_count: int,
    ) -> CodeResult:
        raise NotImplementedError


# ------------------------------------------------------------
# Mock
# ------------------------------------------------------------
_BROKEN_LINE = re.compile(r"^\s*const broken_\d+\s*=\s*;\s*$")

_COLOR_WORDS: tuple[tuple[str, str], ...] = (
    ("红色", "red"),
    ("蓝色", "blue"),
    ("绿色", "green"),
    ("紫色", "purple"),
    ("橙色", "orange"),
    ("黄色", "yellow"),
    ("粉色", "pink"),
    ("黑色", "black"),
    ("白色", "white"),
    ("red", "red"),
    ("blue", "blue"),
    ("green", "green"),
    ("purple", "purple"),
    ("orange", "orange"),
    ("yellow", "yellow"),
    ("pink", "pink"),
    ("black", "black"),
    ("white", "white"),
)

_TEXT_PATTERNS = (
    re.compile(
        r"(?:按钮|标题|文案|文字|文本|内容)\s*(?:改为|改成|换成)\s*"
        r"[“\"'‘]?([^”\"'’\n，。；]+?)[”\"'’]?\s*$"
    ),
    re.compile(r"(?:改为|改成|换成)\s*[“\"'‘]?([^”\"'’\n，。；]+?)[”\"'’]?\s*$"),
)


class MockAgentLLM(AgentLLM):
    """确定性 Mock。

    scenario:
      - fail_first : 首次输出语法错误，用于演示“验证失败 -> 自动修复”；
      - always_fail: 每次都错，用于验证 3 次重试硬上限；
      - none       : 直接输出正确结果。

    为了让无密钥 E2E 也能验证批量修改，Mock 会对每个标注解析最常见的
    颜色 / 文案 / 尺寸指令，并把所有修改合并到一次 files 写入中。
    它不是通用模型：任意布局指令请使用 DeepSeek。
    """

    def __init__(self, scenario: str = "fail_first") -> None:
        if scenario not in {"fail_first", "always_fail", "none"}:
            raise AgentLLMConfigurationError(f"未知 mock_scenario: {scenario}")
        self.scenario = scenario

    def plan(
        self,
        annotations: list[dict[str, Any]],
        current_files: dict[str, str],
        previous_errors: str | None = None,
    ) -> PlanResult:
        lines = [
            f"1. 读取目标文件，综合分析 {len(annotations)} 个标注需求：",
        ]
        for index, annotation in enumerate(annotations, start=1):
            lines.append(
                f"   - 标注 {index}（{_annotation_selector(annotation)}）："
                f"{_annotation_text(annotation)}"
            )
        lines.extend(
            [
                "2. 将全部标注合并为一次代码修改，禁止只处理第一个标注",
                "3. 一次性写入沙盒，执行 npm run build 验证",
                f"4. 目标文件：{', '.join(sorted(current_files))}",
            ]
        )
        if previous_errors:
            lines.append("5. 上一轮构建失败：先修复错误日志，再保证全部标注生效")
        return PlanResult(plan="\n".join(lines))

    def generate(
        self,
        annotations: list[dict[str, Any]],
        plan: str,
        current_files: dict[str, str],
        target_files: list[str],
        verify_result: dict[str, Any],
        retry_count: int,
    ) -> CodeResult:
        primary_file = (
            target_files[0]
            if target_files
            else next(iter(current_files), "src/App.jsx")
        )

        if self.scenario == "always_fail":
            broken = self._inject_syntax_error(
                current_files.get(primary_file, ""), attempt=retry_count + 1
            )
            files = dict(current_files)
            files[primary_file] = broken
            return CodeResult(
                summary="Mock：故意生成错误代码，用于验证重试上限",
                files=files,
            )

        if self.scenario == "fail_first" and not verify_result:
            broken = self._inject_syntax_error(
                current_files.get(primary_file, ""), attempt=1
            )
            files = dict(current_files)
            files[primary_file] = broken
            return CodeResult(
                summary="Mock：首次生成故意包含语法错误",
                files=files,
            )

        fixed = dict(current_files)
        applied: list[str] = []
        for index, annotation in enumerate(annotations, start=1):
            target_file = _annotation_target_file(annotation, fixed, primary_file)
            before = fixed.get(target_file, "")
            after = self._apply_instruction(before, annotation)
            fixed[target_file] = after
            if after != before:
                applied.append(f"标注 {index} -> {target_file}")

        # 清理历史错误行，保证重试后代码可以构建。
        for path, code in list(fixed.items()):
            fixed[path] = self._clean_broken_lines(code)

        summary = (
            "Mock：已一次性应用全部标注修改（"
            + ("；".join(applied) if applied else "未识别到可执行的样式 / 文案指令")
            + "）"
        )
        return CodeResult(summary=summary, files=fixed)

    @staticmethod
    def _inject_syntax_error(code: str, attempt: int) -> str:
        if not code.endswith("\n"):
            code += "\n"
        return f"const broken_{attempt} = ;\n{code}"

    @staticmethod
    def _clean_broken_lines(code: str) -> str:
        lines = [line for line in code.splitlines() if not _BROKEN_LINE.match(line)]
        cleaned = "\n".join(lines)
        return cleaned + "\n" if code.endswith("\n") else cleaned

    @classmethod
    def _apply_instruction(cls, code: str, annotation: dict[str, Any]) -> str:
        """把单条自然语言指令映射为确定性 Mock 修改。"""
        instruction = _annotation_text(annotation)
        selector = _annotation_selector(annotation)
        if not selector or not instruction:
            return code

        color = _detect_color(instruction)
        if color:
            # “文字 / 字体颜色”作用在 color；其余（按钮 / 卡片）默认背景色。
            textual = _is_textual_target(selector) or _is_text_color(instruction)
            prop = "color" if textual else "background-color"
            return _append_css_rule(code, f"{selector}{{{prop}:{color} !important;}}")

        factor = _detect_scale(instruction)
        if factor is not None:
            if "字号" in instruction or "字体" in instruction:
                size = "2em" if factor >= 1 else "0.5em"
                declaration = f"font-size:{size} !important;"
            else:
                declaration = (
                    f"transform:scale({factor:g}) !important;"
                    "transform-origin:center;"
                )
            return _append_css_rule(code, f"{selector}{{{declaration}}}")

        new_text = _detect_text(instruction)
        if new_text:
            return _replace_element_text(code, selector, new_text)
        return code

    @staticmethod
    def _unified_diff(old: str, new: str, target_file: str) -> str:
        if old == new:
            return ""
        return "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{target_file}",
                tofile=f"b/{target_file}",
            )
        )


# ------------------------------------------------------------
# DeepSeek
# ------------------------------------------------------------
class DeepSeekAgentLLM(AgentLLM):
    """DeepSeek Chat（OpenAI 兼容协议）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.deepseek_api_key:
            raise AgentLLMConfigurationError(
                "DEEPSEEK_API_KEY 未配置；请设置密钥或使用 llm_provider=mock"
            )
        self._model = ChatOpenAI(
            model=self.settings.deepseek_model,
            api_key=self.settings.deepseek_api_key,
            base_url=self.settings.deepseek_base_url,
            temperature=0,
            timeout=120,
            max_retries=2,
        )

    def plan(
        self,
        annotations: list[dict[str, Any]],
        current_files: dict[str, str],
        previous_errors: str | None = None,
    ) -> PlanResult:
        system = (
            "你是资深前端架构师。一次请求包含 1~3 个画布标注，你必须综合分析"
            "全部标注，输出一份统一、可执行的修改计划。\n"
            "要求：\n"
            "- 逐条列出每个标注的目标组件、要修改的属性（颜色 / 文案 / 字号 /"
            " 间距 / 布局 / 结构等）与验收标准；\n"
            "- 明确指出多个标注之间是否可能冲突，以及最终以什么结果为准；\n"
            "- 只描述要改什么、保持什么不变、如何验证；不要输出代码。"
        )
        files_block = _format_files(current_files, 16000)
        human = (
            f"画布标注（AnnotationEnvelope[]）:\n"
            f"{json.dumps(annotations, ensure_ascii=False)}\n\n"
            f"目标文件当前内容:\n{files_block}\n\n"
            f"上一轮错误（如有）:\n{_truncate(previous_errors or '无', 4000)}"
        )
        content = self._invoke(system, human)
        return PlanResult(plan=content.strip())

    def generate(
        self,
        annotations: list[dict[str, Any]],
        plan: str,
        current_files: dict[str, str],
        target_files: list[str],
        verify_result: dict[str, Any],
        retry_count: int,
    ) -> CodeResult:
        system = (
            "你是前端代码修改 Agent。一次请求最多包含 3 个画布标注，你必须"
            "一次性完成全部标注对应的修改，禁止只处理第一个标注。\n"
            "支持任意 UI 修改：颜色、文字、字号、间距、布局、响应式样式、"
            "组件结构与少量交互；不要把自己限制为“把按钮变红”。\n"
            "只输出一个 JSON 对象，不要输出 Markdown 代码围栏或解释文字。\n"
            "JSON 格式二选一：\n"
            '1) {"mode":"diff","patch":"--- a/<file>\\n+++ b/<file>\\n...",'
            '"summary":"..."}\n'
            '2) {"mode":"files","files":{"<file>":"完整文件内容"},"summary":"..."}\n'
            "硬性要求：\n"
            "- 多个标注 / 多个文件时优先使用 mode=files，files 必须包含每个目标"
            "文件的完整内容；\n"
            "- diff 必须是可被 git apply 应用的标准 unified diff；\n"
            "- 不要修改无关文件、路由、未提及的文案与样式；\n"
            "- 如果多个标注作用在同一元素，按标注顺序叠加修改，后写的属性覆盖"
            "先写的同名属性；\n"
            "- 如果上一轮构建失败，必须针对错误日志修复，同时保证全部标注仍然"
            "生效，而不是重复原方案。"
        )
        human = (
            f"目标文件列表: {json.dumps(target_files, ensure_ascii=False)}\n\n"
            f"画布标注（AnnotationEnvelope[]）:\n"
            f"{json.dumps(annotations, ensure_ascii=False)}\n\n"
            f"修改计划:\n{plan}\n\n"
            f"当前文件内容:\n{_format_files(current_files, 24000)}\n\n"
            f"上一轮验证结果:\n{json.dumps(verify_result, ensure_ascii=False)}\n\n"
            f"当前重试次数: {retry_count}"
        )
        content = self._invoke(system, human)
        return self._parse_code_result(content, target_files)

    def _invoke(self, system: str, human: str) -> str:
        try:
            response = self._model.invoke(
                [SystemMessage(content=system), HumanMessage(content=human)]
            )
        except Exception as exc:  # noqa: BLE001 - 统一转换为领域异常
            raise AgentLLMError(f"DeepSeek 调用失败: {exc}") from exc
        content = response.content
        if isinstance(content, list):
            content = "".join(str(part) for part in content)
        return str(content)

    def _parse_code_result(self, raw: str, target_files: list[str]) -> CodeResult:
        payload = _extract_json(raw)
        summary = str(payload.get("summary", "模型生成代码修改"))
        mode = str(payload.get("mode", "")).lower()

        if mode == "diff" or (not mode and payload.get("patch")):
            patch = payload.get("patch")
            if not isinstance(patch, str) or not patch.strip():
                raise AgentLLMError("模型返回 diff 模式但 patch 为空")
            missing = [path for path in target_files if path not in _diff_paths(patch)]
            if missing:
                raise AgentLLMError(
                    f"模型 diff 未覆盖全部目标文件: {missing}；"
                    "请一次性修改所有标注，或改用 mode=files"
                )
            return CodeResult(summary=summary, patch=patch)

        files = payload.get("files")
        if isinstance(files, dict) and files:
            normalized = _normalize_files(files)
            missing = [path for path in target_files if path not in normalized]
            if missing:
                raise AgentLLMError(
                    f"模型返回的 files 缺少目标文件 {missing}；"
                    "必须一次性返回全部目标文件的完整内容"
                )
            return CodeResult(summary=summary, files=normalized)

        raise AgentLLMError("模型输出既不含 diff 也不含 files")


# ------------------------------------------------------------
# 通用工具
# ------------------------------------------------------------
def _annotation_text(annotation: dict[str, Any]) -> str:
    payload = annotation.get("payload") or {}
    text = str(payload.get("text") or "").strip()
    if text:
        return text
    intent = annotation.get("intent") or {}
    ui = intent.get("ui") or {}
    return str(ui.get("summary") or "").strip() or "按用户标注修改界面"


def _annotation_selector(annotation: dict[str, Any]) -> str:
    target = annotation.get("target") or {}
    selector = str(target.get("selector") or "").strip()
    if selector:
        return selector
    source = target.get("source") or {}
    marker = str(source.get("target") or "").strip()
    if marker:
        return f"[data-vibe-target='{marker}']"
    return ""


def _annotation_target_file(
    annotation: dict[str, Any],
    current_files: dict[str, str],
    fallback: str,
) -> str:
    target = annotation.get("target") or {}
    source = target.get("source") or {}
    path = str(source.get("file") or "").strip()
    if path in current_files:
        return path
    return fallback


def _detect_color(text: str) -> str | None:
    for word, color in _COLOR_WORDS:
        if word.isascii():
            if re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE):
                return color
        elif word in text:
            return color
    return None


def _is_text_color(text: str) -> bool:
    return any(
        token in text
        for token in ("文字颜色", "字体颜色", "字色", "文字改成", "文字改为")
    )


def _is_textual_target(selector: str) -> bool:
    lowered = selector.lower()
    return any(
        token in lowered
        for token in ("title", "copy", "kicker", "heading", "paragraph")
    ) or bool(re.search(r"\b(h1|h2|h3|p|span)\b", lowered))


def _detect_scale(text: str) -> float | None:
    if any(token in text for token in ("缩小一半", "缩小一倍", "缩小", "变小")):
        return 0.5
    if any(token in text for token in ("放大一倍", "放大两倍", "放大", "变大", "更大")):
        return 2.0
    return None


def _detect_text(text: str) -> str | None:
    for pattern in _TEXT_PATTERNS:
        match = pattern.search(text)
        if match:
            value = match.group(1).strip().strip("。！!，,；;")
            if value and _detect_color(value) is None:
                return value
    return None


def _append_css_rule(code: str, rule: str) -> str:
    """把 CSS 规则追加到 JSX 的 <style> 字符串中。"""
    marker = '"}</style>'
    if marker in code:
        return code.replace(marker, f"{rule}{marker}", 1)
    if "</style>" in code:
        return code.replace("</style>", f"{rule}</style>", 1)
    style_block = f"<style>{{`{rule}`}}</style>"
    for closing in ("</section>", "</main>"):
        if closing in code:
            return code.replace(closing, f"{style_block}\n{closing}", 1)
    return code


def _replace_element_text(code: str, selector: str, new_text: str) -> str:
    """把带 data-vibe-target 的元素内部文本替换为 new_text。"""
    marker_match = re.search(r"data-vibe-target\s*=\s*['\"]([^'\"]+)['\"]", selector)
    if marker_match:
        marker = re.escape(marker_match.group(1))
        pattern = re.compile(
            rf"(<([a-zA-Z][\w-]*)\b"
            rf"(?=[^>]*data-vibe-target=['\"]{marker}['\"])[^>]*>)"
            rf"([\s\S]*?)(</\2>)",
            re.MULTILINE,
        )
        replaced, count = pattern.subn(
            lambda match: f"{match.group(1)}{new_text}{match.group(4)}",
            code,
            count=1,
        )
        if count:
            return replaced

    tag = selector.strip().lower()
    if re.fullmatch(r"[a-z][\w-]*", tag):
        pattern = re.compile(rf"(<{tag}\b[^>]*>)([\s\S]*?)(</{tag}>)", re.IGNORECASE)
        replaced, count = pattern.subn(
            lambda match: f"{match.group(1)}{new_text}{match.group(3)}",
            code,
            count=1,
        )
        if count:
            return replaced
    return code


def _extract_json(raw: str) -> dict[str, Any]:
    """从模型输出中提取第一个 JSON 对象，容忍代码围栏与前缀文字。"""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise AgentLLMError(f"模型输出不是合法 JSON: {_truncate(raw, 500)}")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AgentLLMError(f"模型 JSON 解析失败: {exc}") from exc
    if not isinstance(payload, dict):
        raise AgentLLMError("模型 JSON 顶层必须是对象")
    return payload


def _normalize_files(files: dict[Any, Any]) -> dict[str, str]:
    """校验模型返回的文件路径，拒绝绝对路径与目录穿越。"""
    normalized: dict[str, str] = {}
    for path, content in files.items():
        if not isinstance(content, str):
            continue
        raw_path = str(path).strip()
        while raw_path.startswith("./"):
            raw_path = raw_path[2:]
        pure = PurePosixPath(raw_path)
        if not raw_path or pure.is_absolute() or ".." in pure.parts:
            raise AgentLLMError(f"模型返回了非法文件路径: {path}")
        normalized[raw_path] = content
    if not normalized:
        raise AgentLLMError("模型返回的 files 为空或内容不是字符串")
    return normalized


def _diff_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("+++ "):
            continue
        path = line[4:].strip().split("\t", 1)[0]
        if path.startswith("b/"):
            path = path[2:]
        if path and path != "/dev/null" and path not in paths:
            paths.append(path)
    return paths


def _format_files(files: dict[str, str], limit: int) -> str:
    blocks: list[str] = []
    for path in sorted(files):
        blocks.append(f"### {path}\n```\n{_truncate(files[path], limit)}\n```")
    return "\n\n".join(blocks)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"...[truncated]...\n{text[-limit:]}"


def build_agent_llm(
    request: ExecuteRunRequest,
    settings: Settings | None = None,
) -> AgentLLM:
    """按请求选项与密钥可用性选择 LLM。"""
    settings = settings or get_settings()
    options: RunOptions = request.options
    provider = options.llm_provider

    if provider == "auto":
        provider = "deepseek" if settings.deepseek_api_key else "mock"

    if provider == "mock":
        return MockAgentLLM(scenario=options.mock_scenario)
    return DeepSeekAgentLLM(settings)
