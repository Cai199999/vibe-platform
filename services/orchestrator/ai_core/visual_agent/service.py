"""局部 VisualNode 生成 Agent。

设计边界：
  - 输入是选中容器的 VisualNode JSON 与自然语言需求；
  - 输出只能是 VisualNode JSON 数组；
  - 不读写源码、不启动沙盒、不调用 Fullstack / Direct Ops；
  - 所有 id 由服务端重新生成，避免模型碰撞与注入。
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

from ai_core.adapters.model.deepseek import DeepSeekClient
from ai_core.config import Settings, get_settings
from ai_core.domain.visual_agent import (
    VisualNodeGenerationError,
    VisualNodeGenerationRequest,
    VisualNodeGenerationResponse,
)
from ai_core.domain.visual_document import (
    CONTAINER_NODE_TYPES,
    MAX_VISUAL_DEPTH,
    VisualDocument,
    VisualNode,
    validate_visual_document,
)
from ai_core.ports.model import ModelClient, ModelClientError

logger = logging.getLogger(__name__)

MAX_GENERATED_NODES = 80
MAX_GENERATED_DEPTH = 6
MAX_CONTEXT_CHARS = 30000
_FORBIDDEN_COORDINATE_KEYS = {
    "x",
    "y",
    "absoluteX",
    "absoluteY",
    "absolute_x",
    "absolute_y",
}
_FORBIDDEN_LAYOUT_KEYS = {
    "position",
    "left",
    "top",
    "right",
    "bottom",
    "inset",
    "translate",
    "translateX",
    "translateY",
}
_FORBIDDEN_TAILWIND_PREFIXES = (
    "absolute",
    "fixed",
    "inset-",
    "top-",
    "left-",
    "right-",
    "bottom-",
    "translate-",
)
_MARKUP_RE = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")

SYSTEM_PROMPT = """你是一个 VisualNode JSON 生成器，只服务离线可视化画布。

【输出铁律】
1. 只输出一个 JSON 数组（顶层必须是 []），数组元素是 VisualNode 对象。
2. 严禁输出 JSX、HTML、TSX、Markdown 代码围栏、解释文字或任何代码标记。
3. 严禁输出 page 节点；不要重复外层选中容器，只生成应插入其 children 的新节点。
4. 严禁绝对坐标：不得出现 x、y、absoluteX、absoluteY、position、left、top、
   inset、translate，也不得使用 absolute/fixed/inset-* 等 Tailwind 类。
5. 文本只能放在 props.text / props.placeholder 等纯字符串字段中，不能含标签。

【节点结构】
{
  "type": "container|grid|stack|section|text|button|input|image|list|table|form|modal|component",
  "name": "节点名",
  "props": {},
  "layout": {
    "display": "block|flex|grid", "direction": "row|column",
    "gap": "12px", "padding": "16px",
    "align": "start|center|end|stretch",
    "justify": "start|center|between|end",
    "wrap": false, "columns": 1,
    "minWidth": "", "maxWidth": ""
  },
  "style": {"tokenRefs": [], "className": "", "tailwind": []},
  "children": [],
  "repeat": null,
  "semantics": {"role": "", "purpose": "", "importance": "normal",
                 "businessEntity": ""},
  "a11y": {"label": "", "description": ""},
  "state": {"defaultVisible": true, "disabled": false},
  "codegen": {"mode": "primitive", "componentName": "",
              "extractComponent": false, "imports": []},
  "provenance": {"createdBy": "agent", "revision": 1, "changeId": ""},
  "locked": false
}

【约束】
- 非容器节点不得有 children；只有 list/table 可以使用 repeat。
- 单个输出最多 40 个节点、深度最多 6。
- 常用映射：表单用 form + input/button；卡片列表用 list；指标区用 grid；
  普通布局用 section/stack/container。
- id 可以省略或随意填写，服务端会统一重写。
"""


def _new_node_id() -> str:
    return "nd_" + uuid.uuid4().hex[:20]


def _extract_nodes_payload(raw: str) -> list[dict[str, Any]]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    candidates = [text]
    array_start = text.find("[")
    array_end = text.rfind("]")
    if array_start >= 0 and array_end > array_start:
        candidates.append(text[array_start : array_end + 1])
    object_start = text.find("{")
    object_end = text.rfind("}")
    if object_start >= 0 and object_end > object_start:
        candidates.append(text[object_start : object_end + 1])
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            nodes = payload.get("nodes")
            if isinstance(nodes, list):
                return nodes
            if payload.get("type"):
                return [payload]
        last_error = ValueError("顶层必须是 VisualNode 数组")
    raise ValueError(f"模型未返回合法 JSON 数组: {last_error}")


def _reject_coordinates(node: dict[str, Any]) -> None:
    forbidden = sorted(_FORBIDDEN_COORDINATE_KEYS.intersection(node))
    if forbidden:
        raise ValueError("节点包含绝对坐标字段: " + ", ".join(forbidden))
    layout = node.get("layout")
    if isinstance(layout, dict):
        bad_layout = sorted(_FORBIDDEN_LAYOUT_KEYS.intersection(layout))
        if bad_layout:
            raise ValueError("layout 包含绝对定位字段: " + ", ".join(bad_layout))
    style = node.get("style")
    if isinstance(style, dict):
        tailwind = style.get("tailwind")
        if isinstance(tailwind, list):
            for item in tailwind:
                text = str(item or "").strip().lower()
                if text and any(
                    text == prefix or text.startswith(prefix)
                    for prefix in _FORBIDDEN_TAILWIND_PREFIXES
                ):
                    raise ValueError(f"tailwind 包含定位类: {item}")
    props = node.get("props")
    if isinstance(props, dict):
        for key, value in props.items():
            if str(key) in _FORBIDDEN_COORDINATE_KEYS | _FORBIDDEN_LAYOUT_KEYS:
                raise ValueError(f"props 包含定位字段: {key}")
            if isinstance(value, str) and _MARKUP_RE.search(value):
                raise ValueError(f"props.{key} 含 JSX/HTML 标记")


def _sanitize_node(
    raw: Any,
    *,
    depth: int,
    counter: list[int],
    max_nodes: int,
) -> VisualNode:
    if not isinstance(raw, dict):
        raise ValueError("数组元素必须是 VisualNode 对象")
    if depth > MAX_GENERATED_DEPTH:
        raise ValueError(f"节点深度超过上限 {MAX_GENERATED_DEPTH}")
    counter[0] += 1
    if counter[0] > max_nodes:
        raise ValueError(f"生成节点数超过上限 {max_nodes}")
    _reject_coordinates(raw)
    node_type = str(raw.get("type") or "").strip()
    if node_type == "page":
        raise ValueError("局部生成不允许返回 page 根节点")
    node = dict(raw)
    node["id"] = _new_node_id()
    children = node.get("children")
    if children is None:
        children = []
    if not isinstance(children, list):
        raise ValueError("children 必须是数组")
    if children and node_type not in CONTAINER_NODE_TYPES:
        raise ValueError(f"节点类型 {node_type} 不能包含 children")
    node["children"] = [
        _sanitize_node(
            child,
            depth=depth + 1,
            counter=counter,
            max_nodes=max_nodes,
        ).model_dump(mode="json", by_alias=True)
        for child in children
    ]
    # 模型常见的语义枚举漂移：high/medium/low → core/normal。
    semantics = node.get("semantics")
    if isinstance(semantics, dict):
        importance = str(semantics.get("importance") or "").strip().lower()
        semantics = dict(semantics)
        semantics["importance"] = {
            "high": "core",
            "medium": "normal",
            "low": "normal",
            "critical": "critical",
            "core": "core",
            "normal": "normal",
        }.get(importance, "normal")
        node["semantics"] = semantics
    return VisualNode.model_validate(node)


class VisualNodeGenerator:
    """轻量 Coder：严格 JSON Array → VisualNode[]。"""

    def __init__(
        self,
        client: ModelClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client or DeepSeekClient(self._settings)

    def generate(
        self,
        request: VisualNodeGenerationRequest,
    ) -> VisualNodeGenerationResponse:
        started = time.perf_counter()
        context_text = json.dumps(
            request.context,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(context_text) > MAX_CONTEXT_CHARS:
            raise VisualNodeGenerationError(
                "VISUAL_CONTEXT_TOO_LARGE",
                "选中节点上下文过大，请选择更小的容器",
                status_code=422,
            )
        human = (
            "【选中容器 VisualNode】\n"
            f"{context_text}\n\n"
            "【用户需求】\n"
            f"{request.instruction}\n\n"
            "【现有 nodeId（仅供避让，服务端会重写 id）】\n"
            f"{json.dumps(request.existing_ids[:200], ensure_ascii=False)}\n\n"
            "请只输出要插入该容器 children 的 VisualNode JSON 数组。"
        )
        attempt = 0
        last_error = ""
        last_error_is_model = False
        while attempt < 2:
            attempt += 1
            try:
                raw = self._client.generate("visual_node", SYSTEM_PROMPT, human)
                payload = _extract_nodes_payload(raw)
                if not payload:
                    raise ValueError("模型返回了空数组")
                counter = [0]
                nodes = [
                    _sanitize_node(
                        item,
                        depth=1,
                        counter=counter,
                        max_nodes=min(request.max_nodes, MAX_GENERATED_NODES),
                    )
                    for item in payload
                ]
                root = VisualNode(
                    id=_new_node_id(),
                    type="page",
                    name="Agent 生成临时根",
                    children=nodes,
                )
                document = VisualDocument(
                    docId="vdoc_agent_validation",
                    projectId=request.project_id,
                    revision=0,
                    status="DRAFT",
                    root=root,
                )
                report = validate_visual_document(document)
                if not report.ok:
                    raise ValueError("；".join(report.errors[:5]))
                if report.max_depth > MAX_GENERATED_DEPTH + 1:
                    raise ValueError("生成树层级过深")
                return VisualNodeGenerationResponse(
                    nodes=[
                        node.model_dump(mode="json", by_alias=True) for node in nodes
                    ],
                    model=self._settings.model_fast_name
                    or self._settings.deepseek_model,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    repaired=attempt > 1,
                    trace_id="trc_" + uuid.uuid4().hex[:20],
                )
            except (ValueError, ModelClientError) as exc:
                last_error = str(exc)
                last_error_is_model = isinstance(exc, ModelClientError)
                logger.warning(
                    "visual node generation attempt=%s failed model=%s: %s",
                    attempt,
                    last_error_is_model,
                    last_error,
                )
                # 模型连接失败不是“输出格式不合法”，继续追加 schema 纠正
                # 提示只会浪费一次请求；直接进入下一次（有界）尝试。
                if not last_error_is_model:
                    human += (
                        "\n\n上一次输出不合法，请修正后只输出 JSON 数组。"
                        f"\n错误：{last_error[:1200]}"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("visual node generation crashed")
                raise VisualNodeGenerationError(
                    "VISUAL_NODE_GENERATION_FAILED",
                    f"局部生成失败（{type(exc).__name__}）",
                    status_code=500,
                    diagnostic={"error": str(exc)[:500]},
                ) from exc

        if last_error_is_model:
            raise VisualNodeGenerationError(
                "VISUAL_LLM_UNAVAILABLE",
                "模型服务暂时不可用，请稍后重试",
                status_code=503,
                diagnostic={"error": last_error[:1000]},
            )
        raise VisualNodeGenerationError(
            "VISUAL_NODE_INVALID",
            "局部生成未通过 VisualDocument Schema 校验，请换一种描述重试",
            status_code=422,
            diagnostic={"error": last_error[:1000]},
        )


__all__ = [
    "MAX_GENERATED_DEPTH",
    "MAX_GENERATED_NODES",
    "SYSTEM_PROMPT",
    "VisualNodeGenerator",
]
