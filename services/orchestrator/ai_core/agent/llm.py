"""Agent 使用的 LLM 适配层。

M1 支持两种实现：
  - MockAgentLLM：零密钥、确定性输出，用于闭环与 E2E；
  - DeepSeekAgentLLM：OpenAI 兼容接口，读取 DEEPSEEK_API_KEY。

模型只负责“计划”和“代码生成”，不直接操作沙盒；工具调用由 Graph 节点完成，
这样模型输出可以被结构化校验与重试。
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
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
        annotation: dict[str, Any],
        current_code: str,
        previous_errors: str | None = None,
    ) -> PlanResult:
        raise NotImplementedError

    def generate(
        self,
        annotation: dict[str, Any],
        plan: str,
        current_code: str,
        target_file: str,
        verify_result: dict[str, Any],
        retry_count: int,
    ) -> CodeResult:
        raise NotImplementedError


# ------------------------------------------------------------
# Mock
# ------------------------------------------------------------
_BROKEN_LINE = re.compile(r"^\s*const broken_\d+\s*=\s*;\s*$")


class MockAgentLLM(AgentLLM):
    """确定性 Mock。

    scenario:
      - fail_first : 首次输出语法错误，用于演示“验证失败 -> 自动修复”；
      - always_fail: 每次都错，用于验证 3 次重试硬上限；
      - none       : 直接输出正确 diff。
    """

    def __init__(self, scenario: str = "fail_first") -> None:
        if scenario not in {"fail_first", "always_fail", "none"}:
            raise AgentLLMConfigurationError(f"未知 mock_scenario: {scenario}")
        self.scenario = scenario

    def plan(
        self,
        annotation: dict[str, Any],
        current_code: str,
        previous_errors: str | None = None,
    ) -> PlanResult:
        summary = (
            annotation.get("intent", {}).get("ui", {}).get("summary")
            or annotation.get("payload", {}).get("text")
            or "按用户标注修改界面"
        )
        return PlanResult(
            plan=(
                f"1. 读取目标组件，理解标注需求：{summary}\n"
                "2. 仅修改目标按钮样式，保持现有结构与文案不变\n"
                "3. 输出 unified diff，交由沙盒应用并执行 npm run build 验证"
            )
        )

    def generate(
        self,
        annotation: dict[str, Any],
        plan: str,
        current_code: str,
        target_file: str,
        verify_result: dict[str, Any],
        retry_count: int,
    ) -> CodeResult:
        fixed_code = self._make_button_red(current_code)

        if self.scenario == "always_fail":
            broken = self._inject_syntax_error(current_code, attempt=retry_count + 1)
            return CodeResult(
                summary="Mock：故意生成错误代码，用于验证重试上限",
                files={target_file: broken},
            )

        if self.scenario == "fail_first" and not verify_result:
            broken = self._inject_syntax_error(current_code, attempt=1)
            return CodeResult(
                summary="Mock：首次生成故意包含语法错误",
                files={target_file: broken},
            )

        patch = self._unified_diff(current_code, fixed_code, target_file)
        if not patch:
            # 当前代码已是目标状态：返回全量文件，保证 applier 有明确动作。
            return CodeResult(
                summary="Mock：代码已满足要求",
                files={target_file: fixed_code},
            )
        return CodeResult(
            summary="Mock：将按钮背景色改为红色",
            patch=patch,
        )

    @staticmethod
    def _inject_syntax_error(code: str, attempt: int) -> str:
        if not code.endswith("\n"):
            code += "\n"
        return f"const broken_{attempt} = ;\n{code}"

    @staticmethod
    def _make_button_red(code: str) -> str:
        # 先清理历史错误行，再应用目标样式。
        lines = [line for line in code.splitlines() if not _BROKEN_LINE.match(line)]
        cleaned = "\n".join(lines)
        if code.endswith("\n"):
            cleaned += "\n"

        if "backgroundColor: 'red'" in cleaned or 'backgroundColor: "red"' in cleaned:
            return cleaned
        if "<button" in cleaned:
            return cleaned.replace(
                "<button",
                "<button style={{ backgroundColor: 'red' }}",
                1,
            )
        if "backgroundColor: 'blue'" in cleaned:
            return cleaned.replace(
                "backgroundColor: 'blue'", "backgroundColor: 'red'", 1
            )
        return cleaned

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
        annotation: dict[str, Any],
        current_code: str,
        previous_errors: str | None = None,
    ) -> PlanResult:
        system = (
            "你是资深前端架构师。根据用户标注和当前代码，输出一份简洁、可执行的"
            "修改计划。只描述要改什么、保持什么不变、如何验证；不要输出代码。"
        )
        human = (
            f"标注 JSON:\n{json.dumps(annotation, ensure_ascii=False)}\n\n"
            f"目标文件当前内容:\n```\n{_truncate(current_code, 16000)}\n```\n\n"
            f"上一轮错误（如有）:\n{_truncate(previous_errors or '无', 4000)}"
        )
        content = self._invoke(system, human)
        return PlanResult(plan=content.strip())

    def generate(
        self,
        annotation: dict[str, Any],
        plan: str,
        current_code: str,
        target_file: str,
        verify_result: dict[str, Any],
        retry_count: int,
    ) -> CodeResult:
        system = (
            "你是前端代码修改 Agent。严格执行计划，只输出一个 JSON 对象，不要输出"
            " Markdown 代码围栏或解释文字。\n"
            "JSON 格式二选一：\n"
            '1) {"mode":"diff","patch":"--- a/<file>\\n+++ b/<file>\\n...","summary":"..."}\n'
            '2) {"mode":"files","files":{"<file>":"完整文件内容"},"summary":"..."}\n'
            "硬性要求：\n"
            "- diff 必须是可被 git apply 应用的标准 unified diff；\n"
            "- 不要修改目标文件以外的文件，除非计划明确要求；\n"
            "- 保持现有路由、文案和无关样式不变；\n"
            "- 如果上一轮构建失败，必须针对错误日志修复，而不是重复原方案。"
        )
        human = (
            f"目标文件: {target_file}\n"
            f"标注 JSON:\n{json.dumps(annotation, ensure_ascii=False)}\n\n"
            f"修改计划:\n{plan}\n\n"
            f"当前代码:\n```\n{_truncate(current_code, 24000)}\n```\n\n"
            f"上一轮验证结果:\n{json.dumps(verify_result, ensure_ascii=False)}\n\n"
            f"当前重试次数: {retry_count}"
        )
        content = self._invoke(system, human)
        return self._parse_code_result(content, target_file)

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

    def _parse_code_result(self, raw: str, target_file: str) -> CodeResult:
        payload = _extract_json(raw)
        summary = str(payload.get("summary", "模型生成代码修改"))
        mode = str(payload.get("mode", "")).lower()

        if mode == "diff" or (not mode and payload.get("patch")):
            patch = payload.get("patch")
            if not isinstance(patch, str) or not patch.strip():
                raise AgentLLMError("模型返回 diff 模式但 patch 为空")
            return CodeResult(summary=summary, patch=patch)

        files = payload.get("files")
        if isinstance(files, dict) and files:
            normalized = {
                str(path): str(content)
                for path, content in files.items()
                if isinstance(content, str)
            }
            if target_file not in normalized:
                raise AgentLLMError(f"模型返回的 files 缺少目标文件 {target_file}")
            return CodeResult(summary=summary, files=normalized)

        raise AgentLLMError("模型输出既不含 diff 也不含 files")


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
