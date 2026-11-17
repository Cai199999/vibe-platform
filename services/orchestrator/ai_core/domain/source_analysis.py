"""存量 React 源码分析视图的领域契约。

该模型只服务“只读投影”：输入一个源码文件，输出 Babel 分析报告。
它不参与写回、不生成 VisualDocument、不触发沙盒或 Git。
"""

from __future__ import annotations

from pydantic import Field

from ai_core.domain.common import CamelModel

MAX_SOURCE_CHARS = 500_000


class SourceAnalysisRequest(CamelModel):
    file_name: str = Field(default="App.jsx", min_length=1, max_length=240)
    source: str = Field(min_length=1, max_length=MAX_SOURCE_CHARS)


__all__ = ["MAX_SOURCE_CHARS", "SourceAnalysisRequest"]
