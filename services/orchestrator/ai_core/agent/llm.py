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
import logging
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable, TypeVar

from ai_core.adapters.model.deepseek import DeepSeekClient
from ai_core.agent.memory import fallback_summary
from ai_core.agent.preferences import (
    CORE_BUSINESS,
    IMPLEMENTATION_DETAIL,
    build_clarification_options,
    infer_decision_impact,
    normalize_decision_impact,
)
from ai_core.config import Settings, get_settings
from ai_core.domain.actions import (
    CREATE_NEW,
    MODIFY_EXISTING,
    is_create_action,
)
from ai_core.domain.agent import ExecuteRunRequest, RunOptions
from ai_core.ports.model import ModelClient, ModelClientError
from ai_core.skill_loader import skill_prompts_to_system
from ai_core.tools.move_component_tool import build_move_rule
from ai_core.editor import (
    CodeEdit,
    EditKind,
    EditLayout,
    default_editor_registry,
)

logger = logging.getLogger(__name__)


class AgentLLMError(Exception):
    """LLM 输出不可用。"""


class AgentLLMConfigurationError(AgentLLMError):
    """缺少模型密钥或配置。"""


@dataclass(frozen=True)
class PlanResult:
    """Planner 输出。

    ``confidence_score`` 与 ``clarification`` 是 HITL 的唯一触发依据；
    Graph 不再靠自然语言关键词猜测是否该中断。
    """

    plan: str
    confidence_score: float = 1.0
    decision_impact: str = IMPLEMENTATION_DETAIL
    needs_clarification: bool = False
    clarification: dict[str, Any] | None = None


@dataclass(frozen=True)
class CodeResult:
    """代码生成结果：full files 与 unified diff 二选一。"""

    summary: str
    files: dict[str, str] = field(default_factory=dict)
    patch: str | None = None


class AgentLLM:
    """Planner / Coder 共用的模型接口。"""

    # 项目长期记忆（用户画像 + 全局摘要），由 RunExecutionService 在
    # 构造 LLM 后注入；Mock 实现不读取，但保留同一接口。
    _memory_context: str = ""
    # Reflection 沉淀的稳定实现偏好（例如主色=紫色）。与项目记忆分开注入，
    # 便于测试与审计：只有达到证据阈值的软默认值才会出现在这里。
    _preference_context: str = ""

    def set_memory_context(self, text: str) -> None:
        """注入项目级长期记忆；空字符串表示不注入。"""
        self._memory_context = str(text or "").strip()

    def set_preference_context(self, text: str) -> None:
        """注入 Reflection 得到的稳定实现偏好；空字符串表示不注入。"""
        self._preference_context = str(text or "").strip()

    def _memory_block(self) -> str:
        """把长期记忆包装成 system prompt 片段。

        必须明确声明「只读参考」，否则模型可能把摘要里的历史要求误当成
        当前轮的新指令，导致重复执行或方向漂移。
        """
        text = str(getattr(self, "_memory_context", "") or "").strip()
        if not text:
            return ""
        return (
            "\n\n【项目长期记忆（只读参考）】\n"
            "以下内容来自历史对话摘要与用户画像，仅用于保持风格和约束一致；"
            "它不是本轮用户的新指令。若与本轮明确指令冲突，以本轮为准。\n"
            f"{text[:3000]}"
        )

    def _preference_block(self) -> str:
        """稳定实现偏好：只作为默认值，本轮显式指令优先。"""
        text = str(getattr(self, "_preference_context", "") or "").strip()
        if not text:
            return ""
        return f"\n\n{text[:1500]}"

    def plan(
        self,
        annotations: list[dict[str, Any]],
        current_files: dict[str, str],
        previous_errors: str | None = None,
        clarification_answers: list[dict[str, Any]] | None = None,
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

    def summarize_history(self, text: str, max_chars: int = 200) -> str:
        """把长对话压缩为核心需求与约束。子类必须实现。"""
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
        if scenario not in {"fail_first", "always_fail", "add_button", "none"}:
            raise AgentLLMConfigurationError(f"未知 mock_scenario: {scenario}")
        self.scenario = scenario

    def plan(
        self,
        annotations: list[dict[str, Any]],
        current_files: dict[str, str],
        previous_errors: str | None = None,
        clarification_answers: list[dict[str, Any]] | None = None,
    ) -> PlanResult:
        lines = [
            f"1. 读取目标文件，综合分析 {len(annotations)} 个标注需求：",
        ]
        for index, annotation in enumerate(annotations, start=1):
            action = _annotation_action(annotation)
            action_label = (
                "新增组件（位置后置）" if is_create_action(action) else "修改已有元素"
            )
            lines.append(
                f"   - 标注 {index}（{action_label} · "
                f"{_annotation_selector(annotation) or '画布空白区域'}）："
                f"{_annotation_text(annotation)}"
            )
        history_count = sum(
            len(((annotation.get("threadContext") or {}).get("chatHistory") or []))
            for annotation in annotations
        )
        lines.extend(
            [
                "2. 将全部标注合并为一次代码修改，禁止只处理第一个标注",
                "3. 一次性写入沙盒，执行 npm run build 验证",
                f"4. 目标文件：{', '.join(sorted(current_files))}",
            ]
        )
        if history_count:
            lines.append(
                f"5. 参考同圈多轮对话历史 {history_count} 条，"
                "以最新用户指令为准做增量修改"
            )
        if previous_errors:
            lines.append(
                f"{6 if history_count else 5}. 上一轮构建失败："
                "先修复错误日志，再保证全部标注生效"
            )

        # 分级决策：Mock 也必须遵守与 DeepSeek 相同的产品规则。
        # IMPLEMENTATION_DETAIL（样式 / 基础 UI / 变量命名等）绝不中断；
        # 只有 CORE_BUSINESS 且缺少拍板信息时才生成澄清。
        decision_impact = infer_decision_impact(annotations)
        lines.insert(0, f"0. 决策影响面：{decision_impact}")
        clarification = (
            None
            if clarification_answers
            else _mock_clarification(annotations, decision_impact)
        )
        if clarification:
            lines.insert(1, "0.1 该需求会改变业务结果或接口契约，需要用户拍板")
            return PlanResult(
                plan="\n".join(lines),
                confidence_score=0.55,
                decision_impact=CORE_BUSINESS,
                needs_clarification=True,
                clarification=clarification,
            )

        if clarification_answers:
            lines.append(
                "6. 用户已澄清："
                + "；".join(
                    f"{item.get('questionId') or item.get('question_id')}: "
                    f"{item.get('value') or item.get('text')}"
                    for item in clarification_answers
                )
            )
        return PlanResult(
            plan="\n".join(lines),
            confidence_score=0.92,
            decision_impact=decision_impact,
        )

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
            after = self._apply_instruction(before, annotation, target_file)
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

    def summarize_history(self, text: str, max_chars: int = 200) -> str:
        """Mock 用确定性截取，保证离线 E2E 也能验证压缩链路。"""
        return fallback_summary(text, max_chars)

    def _apply_instruction(
        self,
        code: str,
        annotation: dict[str, Any],
        path: str = "src/App.jsx",
    ) -> str:
        """把单条自然语言指令映射为确定性 Mock 修改。

        ADD 分支优先于 MODIFY：action=ADD、mock_scenario=add_button 或
        指令包含“添加 / 新建 / 插入”时，直接向最合适的容器末尾插入一个
        完整 JSX 按钮，而不是覆盖原有元素。

        除 ``_replace_element_text`` 外，所有写入都经过 ``CodeEditor`` 抽象
        （``default_editor_registry()``）；Mock 只是把自然语言映射成
        ``CodeEdit``，不直接依赖字符串拼接实现。
        """
        instruction = _annotation_text(annotation)
        if not instruction:
            return code

        # 坐标回写：用户在预览中拖拽生成的组件后，前端发送 MODIFY_EXISTING
        # + payload.position。这里只改定位 CSS，绝不重新生成组件主体。
        position = _annotation_position(annotation)
        if position:
            return _apply_position(code, annotation, position, path)

        # 移动意图走标准 Tool：只生成 transform 位移，不触碰宽高。
        # 这样“新建组件后自动进入拖拽模式”的旧链路可以被完全移除，
        # 用户选中目标后直接说“往右挪一点”即可。
        move = _detect_move(instruction)
        selector = _annotation_selector(annotation)
        if (
            move
            and selector
            and not is_create_action(_annotation_action(annotation))
            and not _is_add_intent(instruction)
        ):
            try:
                rule = build_move_rule(selector, move[0], move[1])
            except ValueError:
                rule = ""
            if rule:
                return _append_css_rule(code, rule, path)

        if (
            self.scenario == "add_button"
            or is_create_action(_annotation_action(annotation))
            or _is_add_intent(instruction)
        ):
            return _insert_button(code, annotation, instruction, path)

        selector = _annotation_selector(annotation)
        if not selector:
            return code

        color = _detect_color(instruction)
        if color:
            # “文字 / 字体颜色”作用在 color；其余（按钮 / 卡片）默认背景色。
            textual = _is_textual_target(selector) or _is_text_color(instruction)
            prop = "color" if textual else "background-color"
            return _append_css_rule(
                code, f"{selector}{{{prop}:{color} !important;}}", path
            )

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
            return _append_css_rule(code, f"{selector}{{{declaration}}}", path)

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

    def __init__(
        self,
        settings: Settings | None = None,
        client: ModelClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if not self.settings.deepseek_api_key:
            raise AgentLLMConfigurationError(
                "DEEPSEEK_API_KEY 未配置；请设置密钥或使用 llm_provider=mock"
            )
        # 应用层只依赖 ModelClient 端口；具体厂商调用由适配器完成。
        # 测试或企业网关可注入自定义 ModelClient，无需修改 Planner/Coder。
        self._client = client or DeepSeekClient(self.settings)
        # config/skills/*.skill.json 中挂载到 ui_graph 的 Prompt。
        # 只读取服务端静态文件，不接受请求参数注入。
        self._skill_system = skill_prompts_to_system(
            "ui_graph",
            self.settings.skills_dir,
        )

    def plan(
        self,
        annotations: list[dict[str, Any]],
        current_files: dict[str, str],
        previous_errors: str | None = None,
        clarification_answers: list[dict[str, Any]] | None = None,
    ) -> PlanResult:
        system = (
            "你是资深前端架构师。一次请求包含 1~3 个画布标注，你必须综合分析"
            "全部标注，输出一份统一、可执行的修改计划。\n"
            "只输出一个 JSON 对象，不要输出 Markdown 代码围栏或解释文字。\n"
            "JSON 格式：\n"
            '{"plan":"逐条说明每个标注要改什么、保持什么不变、如何验证",'
            '"confidence_score":0.0到1.0,'
            '"decision_impact":"CORE_BUSINESS 或 IMPLEMENTATION_DETAIL",'
            '"needs_clarification":false,'
            '"clarification":null或{"reason":"core_business_decision",'
            '"decisionImpact":"CORE_BUSINESS","decisionSummary":"一句话说明'
            '需要拍板的业务问题","annotationId":"...",'
            '"questions":[{"questionId":"q1","type":"single_choice",'
            '"prompt":"...","required":true,"options":[{"value":"ui_only",'
            '"label":"只做前端校验","description":"不新增接口"}],'
            '"defaultValue":"ui_only"}],'
            '"clarificationOptions":[{"optionId":"ui_only","label":"只做前端校验",'
            '"description":"不新增接口","recommended":true,'
            '"questionId":"q1","value":"ui_only"}]}}\n\n'
            "要求：\n"
            "- 逐条列出每个标注的目标组件、要修改的属性（颜色 / 文案 / 字号 /"
            " 间距 / 布局 / 结构等）与验收标准；\n"
            "- 每个标注必须区分两种动作：\n"
            "  · CREATE_NEW（旧协议别名 ADD）：新增组件。只规划组件主体、"
            "插入父容器与可辨识的 data-vibe-created 标记；不要依赖 absolute "
            "坐标，也不要要求 Coder 把圈选坐标写死。插入到父容器自然文档流"
            "即可，位置调整由用户后续自然语言指令触发 move_component。\n"
            "  · MODIFY_EXISTING（旧协议别名 MODIFY）：修改已有元素。\n"
            "- **移动规划铁律**：用户说“挪 / 移动 / 往左 / 往右 / 上移 / 下移”"
            "时，计划只能写“给目标元素追加 transform: translate3d(dx,dy,0)”，"
            "禁止规划 width / height / left / top / position / margin 变更；"
            "拖拽浮层是画布交互，不是最终代码的一部分。\n"
            "- **API 路径规划铁律**：前端请求路径必须与后端 Express 路由逐字符"
            "一致（/api/v1 前缀、静态集合根路径）；禁止把 `:id` / `{id}` / "
            "`/stream` 路由当作集合接口。\n"
            "- **导航规划铁律**：侧边栏 / 顶部导航必须规划真实内容切换"
            "（react-router-dom 或 useState 页面状态），禁止只做静态 DOM 高亮。\n"
            "- CREATE_NEW 仍可能带 relativeTarget（旧客户端）：归一化 x/y 与"
            "像素 relativeX/relativeY/widthPx/heightPx/parentWidth/"
            "parentHeight 只用于判断应插入哪个父容器 / 相对顺序，不得据此"
            "生成绝对定位；target.selector 指向父容器。禁止把新增内容伪装"
            "成“修改某个已有元素”；\n"
            "- 旧版 payload.position 只作为历史审计信息，不再规划 left/top 定位；\n"
            "- 每个标注可能带 threadContext.chatHistory（同一圈的多轮对话）和"
            " previousRuns（上一轮修改记录）。必须理解上下文，例如用户先说"
            "“改成红色”，再说“不，我要蓝色”，最终结果只能是蓝色；不要同时保留"
            "两个冲突方案；\n"
            "- 历史修改已经体现在当前源码中。本轮只需生成相对当前源码的增量修改，"
            "不得回退用户没有要求撤销的其它属性；\n"
            "- 明确指出多个标注之间是否可能冲突，以及最终以什么结果为准；\n"
            "- 只描述要改什么、保持什么不变、如何验证；不要输出代码。\n\n"
            "分级决策规则（最高优先级）：\n"
            "- 必须先输出 decision_impact，且只能是 CORE_BUSINESS 或 "
            "IMPLEMENTATION_DETAIL；\n"
            "- CORE_BUSINESS：新增数据库表 / 依赖、修改 API 契约、核心业务分支"
            "（发邮件还是扣库存、支付/退款/权限/库存）、破坏性删除。只有这类"
            "需求在信息缺失或存在多种合理方案时，才 needs_clarification=true；\n"
            "- IMPLEMENTATION_DETAIL：样式微调（hex 颜色、圆角、内外边距）、"
            "基础代码实现（变量命名、日志格式、JSX 嵌套、组件拆分）、登录框等"
            "纯 UI 结构。必须自主按当前项目风格与最佳实践决定，"
            "**needs_clarification 必须为 false**，不得因为用户没指定具体样式"
            "而打断；\n"
            "- 低置信度只对 CORE_BUSINESS 生效；IMPLEMENTATION_DETAIL 即使不"
            "完全确定，也要选择合理默认值继续执行，让用户通过沙盒预览事后圈选；\n"
            "- 核心决策每轮 1~3 个问题，优先提供 2~3 个可执行选项，并把推荐"
            "方案标记 recommended=true；必须同时输出 clarificationOptions，"
            "字段为 optionId/label/description/recommended/questionId/value；\n"
            "- 问题字段固定为 questionId/type/prompt/required/options/"
            "defaultValue；type 只允许 single_choice / multi_choice / free_text。\n\n"
            "全栈引导：\n"
            "- 登录界面本身属于 IMPLEMENTATION_DETAIL；但密码校验放前端还是"
            "后端接口、是否新增用户表 / 认证服务属于 CORE_BUSINESS；\n"
            "- 缺少业务范围时 needs_clarification=true，问题必须引导用户确认："
            "本次先生成前端界面，还是同时生成后端接口和数据库？\n"
            "- 当前能力只有前端时，不得擅自承诺或生成后端 / 数据库代码；"
            "用户明确要求全栈时在 plan 中标注 fullstack_requested=true，"
            "由上层能力开关决定是否继续。"
            f"{self._skill_system}"
            f"{self._memory_block()}"
            f"{self._preference_block()}"
        )
        files_block = _format_files(current_files, 16000)
        answers_block = (
            json.dumps(clarification_answers, ensure_ascii=False, default=str)
            if clarification_answers
            else "无"
        )
        human = (
            f"画布标注（AnnotationEnvelope[]）:\n"
            f"{json.dumps(annotations, ensure_ascii=False, default=str)}\n\n"
            f"目标文件当前内容:\n{files_block}\n\n"
            f"用户已澄清的回答:\n{answers_block}\n\n"
            f"上一轮错误（如有）:\n{_truncate(previous_errors or '无', 4000)}"
        )
        # 真实模型偶发输出 Markdown 围栏或截断 JSON。先按严格 JSON 解析，
        # 失败时把错误回传模型自修复；自修复仍失败才降级为纯文本计划，
        # 保证产品不会因为一次格式漂移直接中断。
        return self._generate_with_repair(
            task="planner",
            system=system,
            human=human,
            parse=lambda raw: _parse_plan_result(raw, annotations, strict=True),
            label="修改计划",
            fallback=lambda raw: _parse_plan_result(raw, annotations),
        )

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
            "- 同一标注可能带 threadContext：当前文件已经包含上一轮修改。必须"
            "基于历史对话与当前源码做增量修改，以最新用户指令为准；用户否定"
            "上一轮方案时（如“不，我要蓝色”），最终代码只能体现新方案，同时"
            "保留其它未被否定的属性；\n"
            "- 标注的 action 字段决定操作类型：\n"
            "  · CREATE_NEW（旧协议别名 ADD）：新增组件。必须保留原有 DOM "
            "结构，不得删除或覆盖已有节点；在 target.selector 指向的父容器"
            "内部插入完整、可编译的 JSX。**只生成组件主体，不要写 position:"
            "absolute / left / top，也不要根据旧 relativeTarget 写死坐标**；"
            "按父容器现有 flex/grid 自然流插入即可。每个新增节点带 "
            'data-vibe-created="<annotationId>" 仅用于审计，禁止再引入拖拽层。'
            "绝不能把新增内容塞进 button / p / h1 等叶子元素内部；target 为空"
            "时，从页面主容器（main / 根 section）末尾插入；\n"
            "  · MODIFY_EXISTING（旧协议别名 MODIFY）：修改已有元素。\n"
            "- **移动指令铁律**：当指令表达“挪 / 移动 / 往左 / 往右 / 上移 / "
            "下移 / 拖到某处”时，只允许给目标元素追加 CSS "
            "`transform: translate3d(dx, dy, 0) !important`（或等价 transform），"
            "禁止修改 width / height / max-width / min-width / left / top / "
            "position / margin 来模拟位移。移动是可叠加的相对位移，量级参考："
            "一点≈24px，一些≈48px，未给方向时默认向右。画布拖拽只负责生成"
            "这条自然语言指令，最终代码中绝不能出现辅助线、虚线框或拖拽浮层。\n"
            "- **前端 API 路径铁律**：任何 fetch / axios 请求的 URL 必须与后端 "
            "Express 路由逐字符一致（包含 /api/v1 前缀与静态集合根路径）。"
            "禁止把带路径参数的 `:id` / `{id}` / `/stream` 路由当成集合接口；"
            "数据面板默认只请求集合根路径，例如后端 "
            "`app.get('/api/v1/resumes', ...)` 时前端必须请求 `/api/v1/resumes`。"
            "若项目中存在 server/api-routes.json，以其中的 collection / item 为准。\n"
            "- **侧边栏路由铁律**：如果页面包含侧边栏或顶部导航，必须实现真实的"
            "内容切换：使用 react-router-dom，或至少用 useState 维护 activePath，"
            "并为每个导航项渲染独立的页面 / 区块内容。禁止只生成静态 DOM 让点击"
            "仅改变高亮；每个导航项都要有可验证的内容差异。\n"
            "- CREATE_NEW 生成的新元素必须有可辨识的文本与样式（如 className /"
            " style / 已有 CSS 类），新增按钮需保证点击区域可见；同一 thread "
            "多轮 CREATE_NEW 时不要重复插入完全相同的组件，除非用户明确要求"
            "再来一个；\n"
            "- 兼容旧客户端：relativeX / relativeY 等像素字段只能用于判断 "
            "flex/grid 插入顺序或 margin 粗调，不得写死绝对坐标；旧版 "
            "payload.position 仅作为历史审计信息，不再驱动 left/top 定位。\n"
            "- 如果上一轮构建失败，必须针对错误日志修复，同时保证全部标注仍然"
            "生效，而不是重复原方案。"
            f"{self._skill_system}"
            f"{self._memory_block()}"
            f"{self._preference_block()}"
        )
        human = (
            f"目标文件列表: {json.dumps(target_files, ensure_ascii=False, default=str)}\n\n"
            f"画布标注（AnnotationEnvelope[]）:\n"
            f"{json.dumps(annotations, ensure_ascii=False, default=str)}\n\n"
            f"修改计划:\n{plan}\n\n"
            f"当前文件内容:\n{_format_files(current_files, 24000)}\n\n"
            f"上一轮验证结果:\n{json.dumps(verify_result, ensure_ascii=False, default=str)}\n\n"
            f"当前重试次数: {retry_count}"
        )
        # Coder 输出必须同时满足：合法 JSON、目标文件全覆盖、patch 可用。
        # 任一校验失败都把「原始输出 + 校验错误」回传模型自修复；连续失败
        # 才抛 AgentLLMError，交给 Graph 的构建错误修复循环处理。
        return self._generate_with_repair(
            task="coder",
            system=system,
            human=human,
            parse=lambda raw: self._parse_code_result(raw, target_files),
            label="代码修改",
        )

    def summarize_history(self, text: str, max_chars: int = 200) -> str:
        """用 FAST 模型把历史对话压缩为最多 max_chars 个字符。"""
        system = (
            "你是对话记忆压缩器。只保留与当前开发任务相关的核心需求、"
            "已确认决策、约束与禁止项；丢弃寒暄、重复解释和中间代码。\n"
            f"只输出一段纯文本摘要，不超过 {max_chars} 字，不要 Markdown。"
        )
        human = f"历史对话：\n{_truncate(text, 24000)}"
        summary = self._invoke("summary", system, human).strip()
        return _truncate(summary, max_chars)

    def _generate_with_repair(
        self,
        *,
        task: str,
        system: str,
        human: str,
        parse: Callable[[str], Any],
        label: str,
        fallback: Callable[[str], Any] | None = None,
    ) -> Any:
        """调用模型并解析结构化输出，失败时带错误上下文自修复。

        真实模型的格式漂移（Markdown 围栏、截断 JSON、缺目标文件、空
        patch）不是网络错误，重发原 Prompt 往往得到同样结果。因此每次
        重试都把「上一轮原始输出 + 具体校验错误」追加到 Human Prompt，
        要求模型只修复指出的字段。

        Args:
            parse: 严格解析器；抛 ``AgentLLMError`` / ``ValueError`` 等
                即视为输出不合法。
            fallback: 所有重试耗尽后的降级解析器。Planner 用它保证
                纯文本计划仍可继续；Coder 不传，必须失败。
        """
        retries = max(0, int(self.settings.agent_json_retry_limit))
        current_human = human
        last_raw = ""
        last_error = "未知错误"
        for attempt in range(retries + 1):
            last_raw = self._invoke(task, system, current_human)
            try:
                return parse(last_raw)
            except (AgentLLMError, ValueError, TypeError, KeyError) as exc:
                last_error = str(exc)
                logger.warning(
                    "agent %s 结构化输出校验失败 attempt=%s/%s task=%s: %s",
                    label,
                    attempt + 1,
                    retries + 1,
                    task,
                    last_error,
                )
                if attempt >= retries:
                    break
                current_human = (
                    f"{human}\n\n"
                    f"【上一次输出】\n{_truncate(last_raw, 8000)}\n\n"
                    f"【校验错误】\n{_truncate(last_error, 2000)}\n\n"
                    "请只输出修正后的完整 JSON 对象：保留已经正确的字段，"
                    "修复校验错误指出的字段，不要输出 Markdown 围栏或解释。"
                )

        if fallback is not None:
            logger.warning(
                "agent %s 自修复耗尽，使用降级解析 task=%s: %s",
                label,
                task,
                last_error,
            )
            return fallback(last_raw)
        raise AgentLLMError(f"{label}连续 {retries + 1} 次输出不合法: {last_error}")

    def _invoke(self, task: str, system: str, human: str) -> str:
        try:
            return self._client.generate(task, system, human)
        except ModelClientError as exc:
            raise AgentLLMError(f"DeepSeek 调用失败: {exc}") from exc

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
def _parse_plan_result(
    raw: str,
    annotations: list[dict[str, Any]],
    *,
    strict: bool = False,
) -> PlanResult:
    """解析 Planner JSON。

    ``strict=True`` 时，非 JSON 输出直接抛错，供自修复重试捕获；
    ``strict=False``（最终降级）时，按纯文本计划继续执行：模型偶发不按
    JSON 输出时，宁可继续执行已有计划，也不能因为解析失败把每个请求都
    变成澄清，导致产品不可用。
    """
    try:
        payload = _extract_json(raw)
    except AgentLLMError:
        if strict:
            raise
        return PlanResult(
            plan=raw.strip(),
            confidence_score=0.8,
            decision_impact=infer_decision_impact(annotations),
        )

    plan = str(payload.get("plan") or raw).strip()
    try:
        confidence = float(payload.get("confidence_score", 0.8))
    except (TypeError, ValueError):
        confidence = 0.8
    confidence = max(0.0, min(1.0, confidence))
    decision_impact = normalize_decision_impact(
        payload.get("decision_impact")
        or payload.get("decisionImpact")
        or payload.get("impact")
    ) or infer_decision_impact(annotations)
    needs = bool(payload.get("needs_clarification"))
    clarification = payload.get("clarification")
    if not needs or not isinstance(clarification, dict):
        return PlanResult(
            plan=plan,
            confidence_score=confidence,
            decision_impact=decision_impact,
            needs_clarification=False,
        )

    normalized = _normalize_clarification(
        clarification,
        annotations,
        decision_impact=decision_impact,
    )
    if normalized is None:
        return PlanResult(
            plan=plan,
            confidence_score=confidence,
            decision_impact=decision_impact,
        )
    return PlanResult(
        plan=plan,
        confidence_score=confidence,
        decision_impact=decision_impact,
        needs_clarification=True,
        clarification=normalized,
    )


_ALLOWED_QUESTION_TYPES = {
    "single_choice",
    "multi_choice",
    "free_text",
    "entity_fields",
    "schema_fields",
}


def _normalize_clarification(
    clarification: dict[str, Any],
    annotations: list[dict[str, Any]],
    *,
    decision_impact: str = CORE_BUSINESS,
) -> dict[str, Any] | None:
    """把模型输出收敛为 API 契约；问题不合法时返回 None。"""
    questions: list[dict[str, Any]] = []
    raw_questions = clarification.get("questions")
    if not isinstance(raw_questions, list):
        return None
    for index, raw in enumerate(raw_questions[:3], start=1):
        if not isinstance(raw, dict):
            continue
        question_type = str(raw.get("type") or raw.get("kind") or "").strip()
        if question_type not in _ALLOWED_QUESTION_TYPES:
            question_type = "free_text"
        prompt = str(raw.get("prompt") or raw.get("question") or "").strip()
        if not prompt:
            continue
        options: list[dict[str, str]] = []
        for option in raw.get("options") or []:
            if not isinstance(option, dict):
                continue
            value = str(option.get("value") or "").strip()
            label = str(option.get("label") or value).strip()
            if value:
                options.append(
                    {
                        "value": value,
                        "label": label,
                        "description": str(option.get("description") or "").strip()[
                            :200
                        ],
                    }
                )
        questions.append(
            {
                "questionId": str(
                    raw.get("questionId") or raw.get("question_id") or f"q{index}"
                ),
                "type": question_type,
                "prompt": prompt[:300],
                "required": bool(raw.get("required", True)),
                "options": options,
                "defaultValue": raw.get("defaultValue", raw.get("default_value")),
            }
        )
    if not questions:
        return None

    annotation_id = clarification.get("annotationId") or clarification.get(
        "annotation_id"
    )
    if not annotation_id and annotations:
        annotation_id = _annotation_id(annotations[0])

    # 一键决策选项：优先使用模型显式输出；缺失时从第一个单选题派生，
    # 保证旧模型 / Mock 也能被前端渲染成按钮。
    normalized_options: list[dict[str, Any]] = []
    raw_options = clarification.get("clarificationOptions") or clarification.get(
        "clarification_options"
    )
    if isinstance(raw_options, list):
        for index, option in enumerate(raw_options[:3], start=1):
            if not isinstance(option, dict):
                continue
            value = option.get("value")
            label = str(option.get("label") or value or "").strip()
            if value is None or not label:
                continue
            normalized_options.append(
                {
                    "optionId": str(option.get("optionId") or f"opt{index}"),
                    "label": label[:80],
                    "description": str(option.get("description") or "").strip()[:200],
                    "recommended": bool(option.get("recommended", False)),
                    "questionId": str(
                        option.get("questionId")
                        or (questions[0].get("questionId") if questions else "")
                    ),
                    "value": value,
                }
            )
    if not normalized_options:
        normalized_options = build_clarification_options(questions)

    normalized_impact = (
        normalize_decision_impact(
            clarification.get("decisionImpact") or clarification.get("decision_impact")
        )
        or normalize_decision_impact(decision_impact)
        or CORE_BUSINESS
    )
    summary = str(
        clarification.get("decisionSummary")
        or clarification.get("decision_summary")
        or (questions[0]["prompt"] if questions else "")
    ).strip()[:300]
    return {
        "reason": str(clarification.get("reason") or "core_business_decision")[:120],
        "decisionImpact": normalized_impact,
        "decisionSummary": summary,
        "annotationId": annotation_id,
        "questions": questions,
        "clarificationOptions": normalized_options,
    }


def _annotation_id(annotation: dict[str, Any]) -> str | None:
    value = annotation.get("annotationId") or annotation.get("annotation_id")
    return str(value) if value else None


def _mock_clarification(
    annotations: list[dict[str, Any]],
    decision_impact: str = IMPLEMENTATION_DETAIL,
) -> dict[str, Any] | None:
    """Mock 的核心业务决策澄清。

    与旧版“模糊形容词就打断”不同，这里只在 ``CORE_BUSINESS`` 时返回问题：
    - 密码 / 登录校验：前端、后端接口、已有认证服务三选一；
    - 其它接口 / 数据 / 支付 / 库存 / 通知：前端先行、后端契约、已有服务；
    - 破坏性操作：确认执行或取消。
    """
    if decision_impact != CORE_BUSINESS:
        return None
    text = " ".join(_annotation_text(item) for item in annotations)
    annotation_id = _annotation_id(annotations[0]) if annotations else None

    if any(word in text for word in ("密码校验", "登录校验", "鉴权", "认证")):
        question = {
            "questionId": "q_password_check",
            "type": "single_choice",
            "prompt": "密码校验放在哪里？这会决定是否需要新增后端接口与数据契约。",
            "required": True,
            "options": [
                {"value": "ui_only", "label": "只做前端格式校验"},
                {"value": "backend_api", "label": "新增后端密码校验接口"},
                {"value": "existing_auth", "label": "对接已有认证服务"},
            ],
            # 涉及密码校验时，默认推荐补齐后端接口，避免前端假校验
            # 被误当成真实安全能力。
            "defaultValue": "backend_api",
        }
    elif any(
        word in text for word in ("删除整个", "移除整个", "删掉整个", "删表", "删库")
    ):
        question = {
            "questionId": "q_destructive_confirm",
            "type": "single_choice",
            "prompt": "这是破坏性操作，确认如何处理？",
            "required": True,
            "options": [
                {"value": "cancel", "label": "取消操作，保留现状"},
                {"value": "confirm", "label": "确认删除并回滚到安全状态"},
            ],
            "defaultValue": "cancel",
        }
    else:
        question = {
            "questionId": "q_core_scope",
            "type": "single_choice",
            "prompt": "这项改动会改变业务结果或接口契约，你希望怎么处理？",
            "required": True,
            "options": [
                {"value": "ui_only", "label": "先完成前端界面与交互"},
                {"value": "backend_api", "label": "同时设计后端接口与数据变更"},
                {"value": "existing_service", "label": "对接现有服务与规则"},
            ],
            "defaultValue": "backend_api",
        }
    return {
        "reason": "core_business_decision",
        "decisionImpact": CORE_BUSINESS,
        "decisionSummary": question["prompt"],
        "annotationId": annotation_id,
        "questions": [question],
        "clarificationOptions": build_clarification_options([question]),
    }


_VAGUE_TOKENS = (
    "好看",
    "大气",
    "高级",
    "美观",
    "漂亮",
    "美化",
    "润色",
    "优化一下",
    "调整一下",
    "改一下",
    "弄一下",
    "改改",
)
_CONCRETE_TOKENS = (
    "颜色",
    "红色",
    "蓝色",
    "绿色",
    "紫色",
    "橙色",
    "黄色",
    "粉色",
    "黑色",
    "白色",
    "字号",
    "字体",
    "大小",
    "间距",
    "布局",
    "文案",
    "文字",
    "文本",
    "按钮",
    "添加",
    "新增",
    "插入",
    "删除",
    "隐藏",
    "显示",
    "放大",
    "缩小",
    "宽度",
    "高度",
    "圆角",
    "背景",
    "对齐",
    "居中",
    "换行",
    "图标",
    "图片",
    "链接",
    "输入框",
    "卡片",
    "标题",
    "边框",
    "阴影",
    "动画",
)
_COMPONENT_TOKENS = (
    "按钮",
    "卡片",
    "输入框",
    "标题",
    "文案",
    "文字",
    "图片",
    "链接",
    "图标",
    "表格",
    "列表",
    "弹窗",
    "表单",
    "导航",
    "标签",
)


def _is_vague_instruction(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return True
    if not any(token in lowered for token in _VAGUE_TOKENS):
        return False
    return not any(token in lowered for token in _CONCRETE_TOKENS)


def _has_component_type(text: str) -> bool:
    return any(token in text for token in _COMPONENT_TOKENS)


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


def _selector_marker(selector: str) -> tuple[str, str]:
    """从属性选择器中提取标记属性与值。

    前端新契约使用 ``data-source``，M1 旧源码仍可能是
    ``data-vibe-target``；Mock 的两条路径都必须兼容。
    """
    match = re.search(r"data-(source|vibe-target)\s*=\s*['\"]([^'\"]+)['\"]", selector)
    if not match:
        return "", ""
    attribute = "data-source" if match.group(1) == "source" else "data-vibe-target"
    return attribute, match.group(2)


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


def _annotation_action(annotation: dict[str, Any]) -> str:
    """读取操作类型；保留原始值，统一由 domain.actions 判断语义。"""
    return str(annotation.get("action") or "MODIFY").upper()


def _annotation_position(annotation: dict[str, Any]) -> dict[str, Any] | None:
    """读取 Drag 松手后回写的坐标 Patch。

    只有前端确认拖拽结果后才携带 ``payload.position``：
      - x / y：相对父容器左上角的 CSS 像素；
      - parentSelector：父容器选择器；
      - selector：需要移动的已生成节点选择器。
    """
    payload = annotation.get("payload") or {}
    position = payload.get("position") or annotation.get("position")
    if not isinstance(position, dict):
        return None
    if position.get("x") is None or position.get("y") is None:
        return None
    return position


def _apply_position(
    code: str,
    annotation: dict[str, Any],
    position: dict[str, Any],
    path: str = "src/App.jsx",
) -> str:
    """把“拖拽后的绝对坐标”写成 CSS，而不是重新生成组件。

    采用 CSS 规则而不是直接重写 JSX 的 style 属性：
      - 生成的组件可能来自 DeepSeek，inline style 结构不可预测；
      - 追加 ``position:absolute; left/top`` 与父容器
        ``position:relative`` 即可稳定复现用户落点；
      - 不触碰组件内容与其它样式，保留多轮对话的增量语义。
    """
    selector = str(position.get("selector") or "").strip() or (
        _annotation_selector(annotation)
    )
    if not selector:
        return code
    parent_selector = (
        str(position.get("parentSelector") or "").strip()
        or str(
            (annotation.get("relativeTarget") or {}).get("parentSelector") or ""
        ).strip()
        or '[data-source="page"]'
    )
    left = float(position.get("x") or 0)
    top = float(position.get("y") or 0)
    rules = (
        f"{parent_selector}{{position:relative !important;}}"
        f"{selector}{{position:absolute !important;"
        f"left:{left:.2f}px !important;top:{top:.2f}px !important;"
        "margin:0 !important;z-index:20;}"
    )
    return _append_css_rule(code, rules, path)


_ADD_INTENT_TOKENS = ("添加", "新增", "新建", "插入", "加一个", "加个", "add ")


def _is_add_intent(text: str) -> bool:
    lowered = text.lower()
    return any(
        token in lowered if token.isascii() else token in text
        for token in _ADD_INTENT_TOKENS
    )


def _detect_button_label(text: str) -> str:
    """从“添加一个红色的提交按钮”中提取按钮文案“提交”。"""
    patterns = (
        re.compile(
            r"(?:添加|新增|新建|插入|加)(?:一个|个|一条|一段)?\s*"
            r"[^，。；\n]{0,10}?(?:的)?([^，。；\n]{1,12}?)按钮"
        ),
        re.compile(
            r"(?:按钮|文字|文案)[^，。；\n]{0,6}?"
            r"[：:为是]?\s*[“\"'‘]([^”\"'’]+)[”\"'’]"
        ),
    )
    for pattern in patterns:
        match = pattern.search(text)
        if not match:
            continue
        label = match.group(1).strip()
        # “加个按钮”这类口语中，量词可能被正则的惰性匹配吞进文案，
        # 先剔除以避免生成只有“个”的按钮。
        label = re.sub(r"^(?:一个|一条|一段|个)", "", label).strip()
        for word, _color in _COLOR_WORDS:
            if word.isascii():
                label = re.sub(rf"\b{re.escape(word)}\b", "", label, flags=re.I)
            else:
                label = label.replace(word, "")
        label = label.strip().strip("的").strip()
        if label:
            return label[:12]
    if "提交" in text:
        return "提交"
    return "新增"


def _next_added_button_id(code: str) -> str:
    """复用已经存在的 data-vibe-target 命名，避免多次新增时冲突。"""
    if 'data-vibe-target="added-button"' not in code:
        return "added-button"
    index = 2
    while f'data-vibe-target="added-button-{index}"' in code:
        index += 1
    return f"added-button-{index}"


def _line_indent(code: str, index: int) -> str:
    """返回 index 所在行的前导缩进。"""
    line_start = code.rfind("\n", 0, index) + 1
    match = re.match(r"[ \t]*", code[line_start:])
    return match.group(0) if match else ""


def _edit_source(
    code: str,
    path: str,
    kind: EditKind,
    anchor: str,
    content: str,
    **kwargs: Any,
) -> str:
    """把一次 ``CodeEdit`` 交给默认注册表执行。

    Mock 只负责“指令 -> 编辑描述”的映射；锚点定位、缩进、重复锚点校验
    全部由 ``CodeEditor`` 实现负责，保证 Mock 与真实 Coder 走同一条编辑通路。
    """
    edit = CodeEdit(
        kind=kind,
        path=path,
        anchor=anchor,
        content=content,
        **kwargs,
    )
    result = default_editor_registry().apply(edit, {path: code})
    return result.files.get(path, code)


def _insert_button(
    code: str,
    annotation: dict[str, Any],
    instruction: str,
    path: str = "src/App.jsx",
) -> str:
    """向目标容器末尾（无法确定容器时插到目标元素之后）插入完整 JSX 按钮。

    插入点选择原则：
      1. 命中 card / section 等内容容器：插到容器闭合标签之前，成为父子关系；
      2. 命中 button / p / h1 等叶子元素：插到该元素闭合标签之后，成为兄弟；
      3. 没有 target（用户在空白区域圈选）：回退到页面主容器末尾。
    这样不会把 `<button>` 塞进 `<p>` / `<h1>` 内部，保证 JSX 结构合法。
    """
    label = _detect_button_label(instruction)
    color = _detect_color(instruction) or "#2563eb"
    button_id = _next_added_button_id(code)
    annotation_id = _annotation_id(annotation) or button_id
    # data-vibe-created 是前端拖拽悬浮层的定位锚点：生成完成后，父页面
    # 通过 inspect SDK 按该属性测量真实 rect，再把拖拽坐标回写成 CSS。
    # 未经用户拖拽前不写任何 position / left / top。
    button = (
        f'<button className="vibe-added-button" data-vibe-target="{button_id}" '
        f'data-vibe-created="{annotation_id}" '
        f"type=\"button\" style={{{{ backgroundColor: '{color}', color: '#fff', "
        "border: 0, borderRadius: '999px', padding: '12px 24px', "
        "marginTop: '16px', fontSize: '14px', fontWeight: 600, "
        f"cursor: 'pointer' }}}}>{label}</button>"
    )

    selector = _annotation_selector(annotation)
    attribute, marker = _selector_marker(selector)

    # 1) 内容容器：优先插入到容器内部末尾。
    if marker in {"card", ""}:
        for closing_tag in ("</section>", "</main>"):
            closing = code.rfind(closing_tag)
            if closing < 0:
                continue
            indent = _line_indent(code, closing) + "  "
            return _edit_source(
                code,
                path,
                EditKind.INSERT_BEFORE,
                closing_tag,
                button,
                occurrence=-1,
                layout=EditLayout.BLOCK,
                indent=indent,
                label="插入按钮到容器末尾",
            )

    # 2) 叶子元素：插到目标闭合标签之后，成为兄弟节点。
    if marker:
        opening = re.search(
            r"<([a-zA-Z][\w-]*)[^>]*data-vibe-target\s*=\s*['\"]"
            + re.escape(marker)
            + r"['\"][^>]*>",
            code,
        )
        if opening:
            tag = opening.group(1)
            closing_tag = f"</{tag}>"
            close_index = code.find(closing_tag, opening.end())
            if close_index >= 0:
                # 目标闭合标签在全文中的序号（1-based），确保插到同一元素之后。
                occurrence = code.count(closing_tag, 0, close_index) + 1
                indent = _line_indent(code, opening.start())
                return _edit_source(
                    code,
                    path,
                    EditKind.INSERT_AFTER,
                    closing_tag,
                    f"\n{indent}{button}",
                    occurrence=occurrence,
                    label="插入按钮到叶子元素之后",
                )

    # 3) 最终兜底：插到最后一个 </section> 或 </main> 之前。
    for closing_tag in ("</section>", "</main>"):
        closing = code.rfind(closing_tag)
        if closing >= 0:
            indent = _line_indent(code, closing) + "  "
            return _edit_source(
                code,
                path,
                EditKind.INSERT_BEFORE,
                closing_tag,
                button,
                occurrence=-1,
                layout=EditLayout.BLOCK,
                indent=indent,
                label="插入按钮到主容器末尾",
            )
    return code


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


_MOVE_RIGHT = ("向右", "往右", "右移", "右边", "右侧")
_MOVE_LEFT = ("向左", "往左", "左移", "左边", "左侧")
_MOVE_DOWN = ("向下", "往下", "下移", "底部", "下方", "下面")
_MOVE_UP = ("向上", "往上", "上移", "顶部", "上方", "上面")


def _move_distance(text: str) -> float:
    """解析位移距离：优先显式 px，其次按口语程度取保守默认值。"""
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:px|像素)", text, re.IGNORECASE)
    if match:
        return max(1.0, min(2000.0, float(match.group(1))))
    if any(token in text for token in ("一点", "稍微", "轻轻")):
        return 24.0
    if any(token in text for token in ("一些", "几下", "大点", "多一点")):
        return 48.0
    return 48.0


def _detect_move(text: str) -> tuple[float, float] | None:
    """把移动类自然语言解析为 ``(dx, dy)`` 像素偏移。

    “移动端 / 移动应用”是产品名，不触发位移；只有出现方向词或明确的
    移动动词（挪 / 拖动 / 位移）时才返回偏移。
    """
    normalized = str(text or "").strip()
    if not normalized or "移动端" in normalized or "移动应用" in normalized:
        return None
    has_verb = any(token in normalized for token in ("挪", "移动", "拖到", "拖拽到"))
    dx = 0.0
    dy = 0.0
    distance = _move_distance(normalized)
    if any(token in normalized for token in _MOVE_RIGHT):
        dx += distance
    if any(token in normalized for token in _MOVE_LEFT):
        dx -= distance
    if any(token in normalized for token in _MOVE_DOWN):
        dy += distance
    if any(token in normalized for token in _MOVE_UP):
        dy -= distance
    if dx == 0 and dy == 0:
        if not has_verb:
            return None
        # “挪一下 / 移动一点”没有方向时，按向右微调处理，避免空操作。
        dx = distance
    return dx, dy


def _detect_text(text: str) -> str | None:
    for pattern in _TEXT_PATTERNS:
        match = pattern.search(text)
        if match:
            value = match.group(1).strip().strip("。！!，,；;")
            if value and _detect_color(value) is None:
                return value
    return None


def _jsx_style_block(rule: str) -> str:
    """把 CSS 规则包装成合法的 JSX ``<style>`` 表达式。

    规则会进入模板字符串，因此必须转义反斜杠、反引号与 ``${``；属性
    选择器里的双引号不需要转义，放在模板字符串里是安全的。
    """
    escaped = rule.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
    return f"<style>{{`{escaped}`}}</style>"


def _append_css_rule(code: str, rule: str, path: str = "src/App.jsx") -> str:
    """把 CSS 规则追加到 JSX 的 ``<style>`` 中，且不破坏 JSX 语法。

    通过 ``AnchorCodeEditor`` 实现，锚点优先级：
      1. ``"}</style>"``：规则直接拼进双引号字符串；
      2. `` `}</style>` ``：规则拼进模板字符串，并转义特殊字符；
      3. 其他 ``<style>{表达式}</style>``：在标签后新增一个兄弟
         ``<style>{`规则`}</style>``。生成骨架常用 ``GLOBAL_CSS`` 变量，
         把裸 CSS 插到 ``</style>`` 前会变成 JSX 非法内容。
      4. 没有 style 块：在页面主容器末尾新建一个 ``<style>`` 块。
    """
    # 双引号字符串：属性选择器里的双引号必须转义，否则会提前截断。
    js_marker = '"}</style>'
    if js_marker in code:
        escaped = rule.replace("\\", "\\\\").replace('"', '\\"')
        return _edit_source(
            code,
            path,
            EditKind.INSERT_BEFORE,
            js_marker,
            escaped,
            label="追加 CSS 规则",
        )
    template_marker = "`}</style>"
    if template_marker in code:
        escaped = _jsx_style_block(rule)[len("<style>{`") : -len("`}</style>")]
        return _edit_source(
            code,
            path,
            EditKind.INSERT_BEFORE,
            template_marker,
            escaped,
            label="追加 CSS 规则",
        )
    if "</style>" in code:
        return _edit_source(
            code,
            path,
            EditKind.INSERT_AFTER,
            "</style>",
            _jsx_style_block(rule),
            layout=EditLayout.BLOCK,
            label="追加 CSS 规则",
        )
    style_block = _jsx_style_block(rule)
    for closing_tag in ("</section>", "</main>"):
        if closing_tag in code:
            return _edit_source(
                code,
                path,
                EditKind.INSERT_BEFORE,
                closing_tag,
                style_block,
                layout=EditLayout.BLOCK,
                label="新建 style 块",
            )
    return code


def _replace_element_text(code: str, selector: str, new_text: str) -> str:
    """把带 data-vibe-target 的元素内部文本替换为 new_text。

    这是 Mock 的选择器启发式（正则匹配标签范围），暂不走 ``CodeEditor``：
    真实 Coder 输出的是锚点 / 结构化编辑；跨文件、跨标签的文本替换应由
    ``TreeSitterEditor`` 基于 AST 完成。保留此函数只服务于零依赖 E2E。
    """
    attribute, raw_marker = _selector_marker(selector)
    if attribute and raw_marker:
        marker = re.escape(raw_marker)
        pattern = re.compile(
            rf"(<([a-zA-Z][\w-]*)\b"
            rf"(?=[^>]*{attribute}=['\"]{marker}['\"])[^>]*>)"
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


# ------------------------------------------------------------
# 供 Graph / Tool 短路使用的公开辅助函数
# ------------------------------------------------------------
# Graph 需要在调用 LLM 之前判断“这是一次纯移动请求”，从而直接调用标准
# Tool `move_component`。这里只暴露稳定的函数名，内部实现仍复用同一套
# 解析 / 锚点编辑逻辑，避免 Graph 与 Mock Coder 产生两套行为。
def detect_move_offset(text: str) -> tuple[float, float] | None:
    return _detect_move(text)


def is_move_only_instruction(text: str) -> bool:
    """判断指令是否只要求移动，不夹带颜色 / 文案 / 字号等其它修改。

    保守策略：只要出现任何非移动类修改信号，就返回 False，交给完整
    LLM 流程处理，避免“移动并把按钮改成红色”被短路成一次纯位移。
    """
    if _detect_move(text) is None:
        return False
    normalized = str(text or "").strip()
    if _detect_color(normalized) is not None or _detect_text(normalized) is not None:
        return False
    non_move_tokens = (
        "字号",
        "字体",
        "背景",
        "边框",
        "圆角",
        "阴影",
        "间距",
        "padding",
        "margin",
        "加粗",
        "斜体",
        "隐藏",
        "显示",
        "删除",
        "新增",
        "添加",
        "替换",
        "改成",
        "变为",
        "颜色",
    )
    return not any(token in normalized.lower() for token in non_move_tokens)


def annotation_selector(annotation: dict[str, Any]) -> str:
    return _annotation_selector(annotation)


def annotation_text(annotation: dict[str, Any]) -> str:
    return _annotation_text(annotation)


def annotation_target_file(
    annotation: dict[str, Any],
    current_files: dict[str, str],
    fallback: str,
) -> str:
    return _annotation_target_file(annotation, current_files, fallback)


def append_css_rule(code: str, rule: str, path: str = "src/App.jsx") -> str:
    return _append_css_rule(code, rule, path)
