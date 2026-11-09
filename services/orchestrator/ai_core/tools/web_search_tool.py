"""DuckDuckGo Web 搜索 Tool。

为什么需要它：
  - 用户在标注里说“参考官网最新文档 / 查一下这个库怎么用”时，模型必须
    先获得外部事实，而不是凭训练数据猜测；
  - 该 Tool 无状态、只读、无 API Key，适合作为 LangGraph 的第一个真实
    Tool 调用示例。

实现边界：
  - 只访问 DuckDuckGo HTML / Lite 两个公开端点，不抓取任意 URL；
  - 超时、结果数、User-Agent 全部由服务端配置，不接受请求方覆盖；
  - 返回结构化 JSON 文本（title / url / snippet），便于模型引用来源；
  - 网络失败时返回可读错误，不抛异常阻断 Agent Run。

注意：DuckDuckGo 没有官方稳定 API，HTML 结构可能变化。生产环境应替换
为具备 SLA 的搜索服务；本实现刻意把解析逻辑与 Tool 包装分离，便于替换。
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from langchain_core.tools import BaseTool, tool

from ai_core.config import Settings
from ai_core.tools.base import ToolDescriptor, ToolRegistry

logger = logging.getLogger(__name__)

_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.DOTALL | re.IGNORECASE)
_SNIPPET_RE = re.compile(
    r"<(?:a|td|div)\b[^>]*class\s*=\s*['\"][^'\"]*"
    r"(?:result__snippet|result-snippet)[^'\"]*['\"][^>]*>(.*?)</(?:a|td|div)>",
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


class WebSearchError(ValueError):
    """搜索请求失败。"""


def _clean_text(value: str) -> str:
    text = _TAG_RE.sub(" ", value or "")
    text = html_lib.unescape(text)
    text = _SPACE_RE.sub(" ", text).strip()
    # 内联标签替换成空格后，标点前会残留空格（如 "documentation ."）。
    # 这里只收紧 ASCII 标点，避免破坏中文全角标点与正文。
    return re.sub(r"\s+([,.;:!?%\)\]}])", r"\1", text)


def _parse_attributes(raw: str) -> dict[str, str]:
    """解析 <a> 标签属性；不依赖属性顺序。"""
    attributes: dict[str, str] = {}
    for match in re.finditer(
        r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(['\"])(.*?)\2",
        raw,
        re.DOTALL,
    ):
        attributes[match.group(1).lower()] = match.group(3)
    return attributes


def _unwrap_redirect(url: str) -> str:
    """DuckDuckGo 的搜索结果通常包一层 /l/?uddg= 跳转。"""
    value = html_lib.unescape((url or "").strip())
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlparse(value)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return value


def parse_duckduckgo_html(html: str, *, max_results: int = 5) -> list[dict[str, str]]:
    """从 DuckDuckGo HTML / Lite 页面提取结果。

    两类页面结构不同，但结果链接都带 result__a / result-link class；
    摘要都带 result__snippet / result-snippet。按出现顺序配对即可，
    比引入 BeautifulSoup 更轻量，也不会执行页面脚本。
    """
    links: list[tuple[str, str]] = []
    for match in _ANCHOR_RE.finditer(html or ""):
        attributes = _parse_attributes(match.group(1))
        classes = attributes.get("class", "")
        if "result__a" not in classes and "result-link" not in classes:
            continue
        href = attributes.get("href", "")
        title = _clean_text(match.group(2))
        if not href or not title:
            continue
        links.append((title, _unwrap_redirect(href)))

    snippets = [_clean_text(item) for item in _SNIPPET_RE.findall(html or "")]
    results: list[dict[str, str]] = []
    for index, (title, url) in enumerate(links[:max_results]):
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": snippets[index] if index < len(snippets) else "",
            }
        )
    return results


def _fetch(endpoint: str, query: str, settings: Settings) -> str:
    headers = {
        "User-Agent": settings.tool_web_search_user_agent,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    with httpx.Client(
        timeout=settings.tool_web_search_timeout_seconds,
        follow_redirects=True,
        headers=headers,
    ) as client:
        response = client.get(endpoint, params={"q": query})
        response.raise_for_status()
        return response.text


def search_duckduckgo(query: str, settings: Settings) -> list[dict[str, str]]:
    """先请求 HTML 端点，解析为空时自动回退到 Lite 端点。"""
    endpoints = [
        settings.tool_web_search_endpoint,
        settings.tool_web_search_fallback_endpoint,
    ]
    errors: list[str] = []
    for endpoint in dict.fromkeys(item for item in endpoints if item):
        try:
            page = _fetch(endpoint, query, settings)
        except Exception as exc:  # noqa: BLE001 - 网络错误统一转为可读信息
            errors.append(f"{endpoint}: {exc}")
            continue
        results = parse_duckduckgo_html(
            page,
            max_results=settings.tool_web_search_max_results,
        )
        if results:
            return results
        errors.append(f"{endpoint}: 页面未解析到结果")
    raise WebSearchError("；".join(errors) or "未配置搜索端点")


def build_web_search_tool(settings: Settings) -> BaseTool:
    """构造 LangChain Tool；闭包捕获 Settings，避免全局状态。"""

    @tool("search_web")
    def search_web(query: str, max_results: int = 5) -> str:
        """使用 DuckDuckGo 搜索外部资料，返回标题 / URL / 摘要。

        适用于“查最新文档、官网地址、库的用法、外部事实”等需要联网的
        请求。query 应为简洁关键词；max_results 最多由服务端配置限制。
        """
        normalized = (query or "").strip()
        if not normalized:
            return "search_web 失败：query 不能为空"
        if len(normalized) > 300:
            return "search_web 失败：query 过长（最多 300 字符）"
        if not settings.tool_web_search_enabled:
            return "search_web 已注册但被服务端禁用" "（TOOL_WEB_SEARCH_ENABLED=false）"
        limit = max(
            1,
            min(
                int(max_results or settings.tool_web_search_max_results),
                settings.tool_web_search_max_results,
            ),
        )
        try:
            results = search_duckduckgo(normalized, settings)[:limit]
        except WebSearchError as exc:
            logger.warning("search_web failed: %s", exc)
            return f"search_web 失败：{exc}"
        if not results:
            return f"search_web 没有找到结果：{normalized}"
        return json.dumps(
            {
                "query": normalized,
                "engine": "duckduckgo",
                "results": results,
            },
            ensure_ascii=False,
        )

    return search_web


def register_web_search(registry: ToolRegistry, settings: Settings) -> None:
    """注册 search_web；关闭时仍注册，调用返回禁用提示。"""
    registry.register(
        ToolDescriptor(
            name="search_web",
            description="DuckDuckGo 外部搜索（只读、无密钥）",
            danger_level="read",
            metadata={
                "engine": "duckduckgo",
                "enabled": settings.tool_web_search_enabled,
                "timeoutSeconds": settings.tool_web_search_timeout_seconds,
                "maxResults": settings.tool_web_search_max_results,
            },
        ),
        build_web_search_tool(settings),
    )
