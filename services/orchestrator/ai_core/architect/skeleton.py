"""抽象需求的确定性“骨架模式”。

对话前置的 Architect 最容易犯的错误，是把用户一句“做个工作台”脑补成
登录、简历上传、职位详情等一整套业务。本模块用确定性规则兜底：

1. 只包含抽象容器名词（工作台 / 仪表盘 / 后台 / 系统……）且没有任何
   业务信号时，判定为“抽象需求”；
2. 抽象需求不走 LLM，直接生成一份最小契约：Header + 侧边栏 + 主内容区，
   主内容区只放一个 ``<Placeholder title="待定区域" />``；
3. 契约显式标记 ``skeletonMode=True``，FrontendCoder 据此只渲染干净骨架，
   不渲染任何猜测出来的业务表单或卡片。

规则必须保持保守：宁可让用户多说一句，也不能替用户编造业务组件。
"""

from __future__ import annotations

import re
from typing import Any

from ai_core.architect.llm import ContractDraft
from ai_core.domain.architect import ArchitectImplementationPhase

# 纯粹的“容器名词”：它们只说明产品形态，不包含任何业务对象。
_ABSTRACT_SUBJECTS = (
    "工作台",
    "仪表盘",
    "看板",
    "控制台",
    "管理后台",
    "后台管理系统",
    "后台",
    "管理系统",
    "系统",
    "平台",
    "网站",
    "网页",
    "页面",
    "应用",
    "app",
    "dashboard",
    "console",
    "portal",
    "首页",
    "主页",
    "门户",
)

# 出现任意一个业务信号，就说明用户已经表达了具体业务，不能再走骨架模式。
_BUSINESS_SIGNALS = (
    "登录",
    "注册",
    "用户",
    "账号",
    "权限",
    "角色",
    "订单",
    "商品",
    "产品",
    "库存",
    "支付",
    "结算",
    "退款",
    "发票",
    "账单",
    "财务",
    "客户",
    "联系人",
    "线索",
    "合同",
    "项目",
    "任务",
    "待办",
    "日程",
    "日历",
    "会议",
    "审批",
    "流程",
    "工单",
    "客服",
    "消息",
    "通知",
    "聊天",
    "评论",
    "文章",
    "博客",
    "帖子",
    "简历",
    "招聘",
    "职位",
    "面试",
    "员工",
    "考勤",
    "请假",
    "课程",
    "学生",
    "教师",
    "成绩",
    "图书",
    "借阅",
    "医院",
    "医生",
    "挂号",
    "预约",
    "会员",
    "积分",
    "优惠券",
    "营销",
    "广告",
    "报表",
    "图表",
    "统计",
    "分析",
    "数据",
    "上传",
    "下载",
    "导入",
    "导出",
    "搜索",
    "筛选",
    "表格",
    "表单",
    "列表",
    "详情",
    "编辑",
    "删除",
    "新建",
    "api",
    "接口",
    "数据库",
    "mysql",
    "postgres",
    "redis",
    "模型",
    "算法",
    "脚本",
    "爬虫",
    "文件",
    "图片",
    "视频",
    "音频",
    "地图",
    "定位",
    "物流",
    "仓库",
    "采购",
    "销售",
    "供应商",
)

# 只表达“帮我做一个”的动词 / 语气词，可以从文本中剥离后再判断主语。
_GENERIC_PHRASES = (
    "帮我",
    "请",
    "我想",
    "我要",
    "我需要",
    "需要",
    "希望",
    "做一个",
    "做个",
    "搭一个",
    "搭建",
    "开发",
    "创建",
    "实现",
    "生成",
    "写一个",
    "写个",
    "设计",
    "一个",
    "一款",
    "简单",
    "简洁",
    "好看",
    "漂亮",
    "能用",
    "可用",
    "的",
    "可以",
)

# 这些槽位一旦有值，说明用户已经给出了具体业务，禁止骨架模式。
_CONCRETE_SLOTS = {
    "ui.primary_flow",
    "business.key_flows",
    "data.entities",
    "api.style",
    "script.input",
    "script.output",
    "script.logic",
    "script.runtime",
}

_PUNCTUATION = re.compile(
    r"[\s,，。.!！?？、;；:：\"'“”‘’()（）\[\]【】<>《》/\\|+*#@_-]+"
)


def latest_user_text(history: list[dict[str, Any]] | None) -> str:
    """返回最后一条用户消息；忽略 assistant / system 事件。"""
    for item in reversed(list(history or [])):
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "") != "user":
            continue
        content = str(item.get("content") or "").strip()
        if content:
            return content
    return ""


def user_texts(history: list[dict[str, Any]] | None) -> list[str]:
    """按时间顺序返回用户文本。"""
    texts: list[str] = []
    for item in list(history or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "") != "user":
            continue
        content = str(item.get("content") or "").strip()
        if content:
            texts.append(content)
    return texts


def is_abstract_brief(text: str) -> bool:
    """判断一句话是否只是“做个工作台”这类无业务信息的抽象需求。

    判定顺序：
    1. 含任何业务信号 -> 否；
    2. 剥掉“帮我 / 做一个 / 简单的”等通用词后，仍包含容器名词 -> 是；
    3. 其余情况（包括“你好”“随便看看”这类闲聊）-> 否。
    """
    raw = str(text or "").strip()
    if not raw:
        return False
    normalized = _PUNCTUATION.sub("", raw.lower())
    if not normalized:
        return False
    if any(signal in normalized for signal in _BUSINESS_SIGNALS):
        return False

    cleaned = normalized
    for phrase in _GENERIC_PHRASES:
        cleaned = cleaned.replace(phrase, "")
    if not cleaned:
        return False

    return any(subject in cleaned for subject in _ABSTRACT_SUBJECTS)


def should_use_skeleton(
    *,
    history: list[dict[str, Any]] | None,
    slots: dict[str, Any] | None,
    source_files: dict[str, str] | None,
    project_summary: str = "",
    latest_changes: str = "",
) -> bool:
    """是否应该进入“空白骨架”模式。

    只对“空白新项目 + 抽象目标”生效：
    - 已有源码 / 项目摘要 / 快照改动 -> 是既有项目，按正常流程分析；
    - 槽位里已有具体业务 -> 用户已经说过，不再骨架化；
    - 任意一轮用户消息出现业务信号 -> 不再骨架化。
    """
    if source_files:
        return False
    if str(project_summary or "").strip():
        return False
    if str(latest_changes or "").strip():
        return False

    for key, value in dict(slots or {}).items():
        if key in _CONCRETE_SLOTS and value not in (None, "", [], {}):
            return False

    texts = user_texts(history)
    if not texts:
        return False
    combined = "\n".join(texts)
    normalized = _PUNCTUATION.sub("", combined.lower())
    if any(signal in normalized for signal in _BUSINESS_SIGNALS):
        return False
    # 只要对话中出现过“工作台 / 后台 / 系统”这类抽象目标，且后续补充
    # 始终没有业务信号（例如用户只回答“没想好 / 先这样”），就应当停在
    # 骨架阶段，而不是让模型用追问耗尽后开始脑补。
    return any(is_abstract_brief(text) for text in texts)


def _extract_subject(text: str) -> str:
    """从抽象目标中提取一个适合做页面标题的名词。"""
    normalized = _PUNCTUATION.sub("", str(text or "").lower())
    for subject in _ABSTRACT_SUBJECTS:
        if subject in normalized:
            # app 等英文词转成中文展示。
            return {
                "app": "应用",
                "dashboard": "工作台",
                "console": "控制台",
                "portal": "门户",
            }.get(subject, subject)
    return "工作台"


def build_skeleton_draft(
    *,
    project_name: str = "",
    goal: str = "",
) -> ContractDraft:
    """生成最小可确认契约：只有布局骨架与一个占位组件。"""
    subject = _extract_subject(goal)
    title = str(project_name or "").strip()
    if not title or title in {"未命名项目", "新建项目", "新项目"}:
        title = subject
    app_title = title
    placeholder_name = "待定区域"
    markdown = f"""# {app_title} · 初始骨架契约

## 目标

用户当前只给出了抽象目标“{goal or subject}”，尚未说明业务对象、核心操作、
数据字段或权限要求。本契约只交付一个干净、可继续演进的应用骨架，不猜测
任何未提及的业务功能。

## 页面结构

- Header：品牌名称、当前页面标题与全局操作占位；
- Sidebar：后续业务模块的导航入口占位；
- Main：空白主内容区，显示欢迎语与一个待定占位组件。

## 占位组件

主内容区只包含一个虚拟占位节点：

- nodeId：由 Architect 生成；
- type：`container`；
- 文案：待定区域；
- 视觉样式：Tailwind 占位类；
- 不含任何 React / JSX 源码。

禁止在该阶段生成登录表单、简历上传、职位详情、数据表格等用户没有明确
要求的业务组件。

## 验收标准

1. 页面具有完整的 Header + Sidebar + Main 三栏骨架；
2. 主区域显示 “Welcome to your new app” 与待定占位组件；
3. 组件使用响应式布局，小屏不重叠，内容区可滚动；
4. 用户确认契约后，再通过对话或画布圈选逐步补充业务功能。
"""
    layout = [
        {
            "region": "header",
            "title": "顶部导航",
            "description": "品牌、页面标题与全局操作占位。",
        },
        {
            "region": "sidebar",
            "title": "侧边栏",
            "description": "后续业务模块的导航入口占位。",
        },
        {
            "region": "main",
            "title": "主内容区",
            "description": "空白工作区，仅显示欢迎语与待定占位组件。",
        },
    ]
    components = [
        {
            "name": placeholder_name,
            "role": "placeholder",
            "section": "主内容区",
            "description": "需求尚未明确；确认业务后再生成具体组件。",
            "isPlaceholder": True,
            "placeholder": '<Placeholder title="待定区域" />',
        }
    ]
    return ContractDraft(
        contract_markdown=markdown,
        tech_stack={
            "frontend": "React 18 + Vite",
            "ui": "原生 CSS + 响应式 Grid",
            "frontendTitle": app_title,
        },
        api_routes=[],
        database_ddl="",
        component_interactions=[],
        frontend_routes=[
            {
                "path": "/",
                "name": subject,
                "description": "空白工作区；待补充具体业务后逐步扩展。",
            }
        ],
        page_layout=layout,
        components=components,
        ui_logic=[
            {
                "type": "placeholder",
                "description": "骨架阶段不实现业务交互，等待用户逐步补充。",
            }
        ],
        frontend_data_model=[],
        script_spec={},
        implementation_path=[
            ArchitectImplementationPhase(
                phase="1",
                title="生成空白骨架",
                tasks=[
                    "生成 Header + 侧边栏 + 主内容区",
                    "渲染 Welcome 与待定占位组件",
                    "等待用户确认后逐步添加业务功能",
                ],
            )
        ],
        assumptions=[
            "用户只提供了抽象目标，未明确业务对象、操作或数据字段。",
            "本契约不生成登录、简历上传、职位详情等未提及组件。",
            "占位区域使用 <Placeholder> 表示，后续通过对话或画布圈选填充。",
        ],
        skeleton_mode=True,
    )


__all__ = [
    "build_skeleton_draft",
    "is_abstract_brief",
    "latest_user_text",
    "should_use_skeleton",
    "user_texts",
]
