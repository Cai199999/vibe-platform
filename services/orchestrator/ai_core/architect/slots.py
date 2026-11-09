"""从自然语言回答中确定性回填 Architect 槽位。

LLM 偶尔会把“订单和商品表”“Java Spring Boot + MySQL”识别成技术栈描述，
却不愿意写入 ``slots``，导致已经回答过的必需槽位反复被追问。这里提供一套
保守的关键词抽取：

  - 只在文本明确出现技术栈 / 实体 / 业务流程关键词时回填；
  - 调用方使用 ``setdefault`` 语义，绝不覆盖用户或 LLM 已确认的值；
  - 无法识别时返回空字典，不臆造内容。

该模块同时服务 Mock 剧本与 ``clarify_gate`` 的自由文本回答，保证
“用户说了什么，系统就记住什么”，而不是完全依赖模型自觉。
"""

from __future__ import annotations

import re
from typing import Any

_BACKEND_RULES = (
    ("java_spring_boot", (r"spring\s*boot", r"java\s*后端", r"后端.*java")),
    ("python_fastapi", (r"fastapi", r"python\s*后端", r"后端.*python")),
    ("node_nestjs", (r"nestjs", r"nest\.js", r"node\.?js\s*后端", r"后端.*node")),
)

_DATABASE_RULES = (
    ("postgresql", (r"postgres(?:ql)?", r"pg\s*库")),
    ("sqlite", (r"sqlite",)),
    ("mysql", (r"mysql", r"maria\s*db")),
)

_ENTITY_HINTS = (
    "订单",
    "商品",
    "库存",
    "用户",
    "账户",
    "支付",
    "购物车",
    "课程",
    "文章",
    "评论",
    "任务",
    "项目",
    "客户",
    "员工",
    "产品",
)

_FLOW_HINTS = (
    "点击",
    "提交",
    "跳转",
    "扣减",
    "扣除",
    "创建",
    "编辑",
    "删除",
    "指派",
    "拖拽",
    "下单",
    "支付",
    "结算",
    "库存",
    "流程",
)

_AUTH_NO_HINTS = (
    "不需要登录",
    "无需登录",
    "不用登录",
    "不需要鉴权",
    "无需鉴权",
    "不做鉴权",
    "no auth",
    "without auth",
)
_AUTH_YES_HINTS = ("需要登录", "需要鉴权", "需要权限", "租户隔离", "rbac")

# 应用形态推断：对话前置模式不再向用户展示三选一，Architect 必须从目标
# 描述里主动判断。规则保持保守：只有出现明确信号才推断，避免把普通
# 单页工具误升级成全栈系统。
_SCRIPT_HINTS = (
    "脚本",
    "定时任务",
    "爬虫",
    "命令行",
    "批处理",
    "无ui",
    "无 ui",
    "cron",
)
_LIGHTWEIGHT_HINTS = (
    "纯前端",
    "小工具",
    "计算器",
    "单页",
    "浏览器存储",
    "localstorage",
    "local storage",
    "无需登录",
    "不需要登录",
    "个人使用",
)
_FULLSTACK_HINTS = (
    # 用户直接说“全栈 / 前后端”时同样是明确形态信号。
    "全栈",
    "前后端",
    "团队",
    "协作",
    "多人",
    "多用户",
    "登录",
    "权限",
    "账号",
    "角色",
    "数据库",
    "后端",
    "服务端",
    "持久化",
    "管理系统",
    "看板",
    "订单",
    "库存",
    "客户",
    "审批",
    "预约",
)


def _first_match(
    text: str, rules: tuple[tuple[str, tuple[str, ...]], ...]
) -> str | None:
    for value, patterns in rules:
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            return value
    return None


def _clip(text: str, limit: int = 240) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit]


def infer_app_shape(text: str) -> str | None:
    """从自然语言目标推断应用形态。

    优先级：自动化脚本 → 明确的轻量前端 → 含协作/持久化信号的全栈。
    没有明确信号时返回 ``None``，由 Architect 继续用自由文本追问，
    而不是替用户做技术选型。
    """
    raw = _clip(text)
    if not raw:
        return None
    lowered = raw.lower()
    if any(hint in lowered for hint in _SCRIPT_HINTS):
        return "script_automation"
    if any(hint in raw for hint in _LIGHTWEIGHT_HINTS):
        return "lightweight_web"
    if any(hint in raw for hint in _FULLSTACK_HINTS):
        return "full_stack_system"
    return None


def infer_slots(text: str) -> dict[str, Any]:
    """从一段自然语言中抽取可确定的槽位。

    返回的 key 与 Architect 槽位名一致；调用方按 ``setdefault`` 合并。
    """
    raw = _clip(text)
    if not raw:
        return {}

    lowered = raw.lower()
    inferred: dict[str, Any] = {}

    shape = infer_app_shape(raw)
    if shape:
        inferred["app.shape"] = shape

    backend = _first_match(raw, _BACKEND_RULES)
    if backend:
        inferred["backend.framework"] = backend

    database = _first_match(raw, _DATABASE_RULES)
    if database:
        inferred["database.engine"] = database

    # 实体：必须同时出现“实体/表/字段”语境与至少一个业务对象，避免
    # 把“订单页跳转”误判为数据实体。
    entity_hits = [hint for hint in _ENTITY_HINTS if hint in raw]
    if entity_hits and any(key in raw for key in ("实体", "表", "字段", "模型")):
        inferred["data.entities"] = raw

    # 业务流：用户描述点击 / 扣库存 / 跳转等动作时，整句就是最好的槽位值。
    if any(hint in raw for hint in _FLOW_HINTS) and (
        "点击" in raw or "流程" in raw or "->" in raw or "→" in raw
    ):
        inferred["business.key_flows"] = raw

    if any(hint in lowered for hint in _AUTH_NO_HINTS):
        inferred["auth.required"] = "no"
    elif any(hint in lowered for hint in _AUTH_YES_HINTS):
        inferred["auth.required"] = "yes"

    if "restful" in lowered or "rest api" in lowered:
        inferred["api.style"] = "RESTful"

    return inferred


def merge_inferred_slots(slots: dict[str, Any], text: str) -> dict[str, Any]:
    """按“已有值优先”合并推断结果，返回新的槽位字典。"""
    merged = dict(slots)
    for key, value in infer_slots(text).items():
        if key not in merged or merged[key] in (None, "", [], {}):
            merged[key] = value
    return merged


__all__ = ["infer_slots", "merge_inferred_slots"]
