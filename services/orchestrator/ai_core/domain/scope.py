"""记忆 / Checkpoint 的物理命名空间。

多智能体系统里，``threadId``、``runId`` 都只是“业务内唯一”，不能假设它们
在租户、项目、Agent 之间全局唯一。任何持久化键（LangGraph thread_id、
ThreadMemoryStore 的 key）都必须先经过本模块加作用域前缀，形成：

    <kind>:<tenant+project 哈希>:<业务 ID>

这样即使两个项目意外生成相同的 ``threadId`` / ``runId``，也不会读写到同一
份记忆。函数保持纯计算，方便领域层与测试复用。
"""

from __future__ import annotations

import hashlib

# 未显式传租户时，与前端默认值保持一致；绝不能把空租户当成“跨租户”。
DEFAULT_TENANT = "tenant_local"
_SEPARATOR = "\x1f"


def memory_scope(tenant_id: str | None, project_id: str | None) -> str:
    """返回租户 + 项目的稳定短哈希；没有项目时返回空串（兼容旧调用）。"""
    project = (project_id or "").strip()
    if not project:
        return ""
    tenant = (tenant_id or DEFAULT_TENANT).strip() or DEFAULT_TENANT
    payload = f"{tenant}{_SEPARATOR}{project}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def scoped_key(
    kind: str,
    identifier: str,
    *,
    tenant_id: str | None,
    project_id: str | None,
) -> str:
    """生成带项目作用域的存储键。

    ``project_id`` 为空时退化为 ``kind:identifier``，仅用于旧测试 / 单机脚本；
    生产请求的 ``project_id`` 是必填字段，不会走到该分支。
    """
    scope = memory_scope(tenant_id, project_id)
    if not scope:
        return f"{kind}:{identifier}"
    return f"{kind}:{scope}:{identifier}"


def scoped_thread_key(
    thread_id: str,
    *,
    tenant_id: str | None,
    project_id: str | None,
) -> str:
    """ThreadMemoryStore 的物理键：项目隔离后的标注会话。"""
    return scoped_key(
        "thread",
        thread_id,
        tenant_id=tenant_id,
        project_id=project_id,
    )


def scoped_checkpoint_key(
    kind: str,
    run_id: str,
    *,
    tenant_id: str | None,
    project_id: str | None,
) -> str:
    """LangGraph Checkpointer 的物理键。

    ``kind`` 区分 ``ui_run`` 与 ``architect_run``，保证两个 Agent 即使拿到
    相同 runId 也不会共享 checkpoint。
    """
    return scoped_key(
        kind,
        run_id,
        tenant_id=tenant_id,
        project_id=project_id,
    )


__all__ = [
    "DEFAULT_TENANT",
    "memory_scope",
    "scoped_checkpoint_key",
    "scoped_key",
    "scoped_thread_key",
]
