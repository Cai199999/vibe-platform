"""画布标注的操作语义。

新协议使用 ``CREATE_NEW`` / ``MODIFY_EXISTING``，旧协议仍可能发送
``ADD`` / ``MODIFY``。所有业务判断都必须经过这里，避免不同模块各写一套
字符串比较，导致新动作在某个分支被当成“修改”。

语义边界：
  - ``CREATE_NEW``      只生成组件主体，不要求绝对定位；位置由用户在
                        预览里拖拽后，再通过新的 MODIFY_EXISTING Patch 写回。
  - ``MODIFY_EXISTING`` 修改、移动或重新定位已有元素。
"""

from __future__ import annotations

from typing import Any

CREATE_NEW = "CREATE_NEW"
MODIFY_EXISTING = "MODIFY_EXISTING"

# 旧客户端 / 旧 checkpoint 中的动作别名。
_LEGACY_CREATE = frozenset({"ADD"})
_LEGACY_MODIFY = frozenset({"MODIFY"})

CREATE_ACTIONS = frozenset({CREATE_NEW, *_LEGACY_CREATE})
MODIFY_ACTIONS = frozenset({MODIFY_EXISTING, *_LEGACY_MODIFY})


def normalize_action(value: Any) -> str:
    """把动作统一为 ``CREATE_NEW`` / ``MODIFY_EXISTING``。

    未知值保守地按“修改已有元素”处理，避免误把任意历史数据当成新增，
    从而意外向页面插入重复组件。
    """
    action = str(value or "").strip().upper()
    if action in CREATE_ACTIONS:
        return CREATE_NEW
    return MODIFY_EXISTING


def is_create_action(value: Any) -> bool:
    return normalize_action(value) == CREATE_NEW


def is_modify_action(value: Any) -> bool:
    return normalize_action(value) == MODIFY_EXISTING


__all__ = [
    "CREATE_ACTIONS",
    "CREATE_NEW",
    "MODIFY_ACTIONS",
    "MODIFY_EXISTING",
    "is_create_action",
    "is_modify_action",
    "normalize_action",
]
