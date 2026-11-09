"""领域层通用值对象与序列化基类。

领域层是整个系统的“通用语言”：
  - 只允许依赖 Python 标准库与 Pydantic；
  - 禁止导入 FastAPI、LangGraph、LangChain、Docker、数据库驱动或任何
    具体大模型 SDK；
  - 不包含 I/O、网络、容器操作与环境变量读取。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


def to_camel(name: str) -> str:
    """把 snake_case 字段名转为 camelCase。"""
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


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
