"""领域层通用值对象与序列化基类。

领域层是整个系统的“通用语言”：
  - 只允许依赖 Python 标准库与 Pydantic；
  - 禁止导入 FastAPI、LangGraph、LangChain、Docker、数据库驱动或任何
    具体大模型 SDK；
  - 不包含 I/O、网络、容器操作与环境变量读取。
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict


def to_camel(name: str) -> str:
    """把 snake_case 字段名转为 camelCase。"""
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


def iso_datetime(value: datetime | None) -> str | None:
    """把 ``datetime`` 统一序列化为 UTC ISO 8601（毫秒 + ``Z``）。

    领域模型可能同时接收前端 ``Z`` 字符串与 Python ``datetime``；任何
    要进入 ``json.dumps`` / SSE / Prompt 的字段都必须先经过本函数，
    避免 ``Object of type datetime is not JSON serializable``。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class CamelModel(BaseModel):
    """同时接受 camelCase 与 snake_case，序列化默认 camelCase。

    对外 API 契约使用 camelCase，Python 内部使用 snake_case；该基类让
    两种写法可以互操作，同时允许扩展字段（extra="allow"）以兼容灰度期
    客户端。
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="allow",
    )


__all__ = ["CamelModel", "to_camel"]
