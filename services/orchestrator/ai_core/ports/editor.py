"""代码编辑端口（CodeEditor）。

为什么需要这层抽象
------------------
M2 的 Coder 以“锚点字符串替换”为主，但全栈生成阶段会出现两类完全不同的
编辑需求：

1. **新建文件**：生成 ``pom.xml``、``*.java``、迁移脚本、``docker-compose``
   片段等此前不存在的文件，没有锚点可依赖，只能全量写入；
2. **跨文件结构化编辑**：修改实体类后同步 Mapper / DTO / 前端类型，需要按
   AST 定位符号而不是匹配字符串。

如果 Graph、ToolRegistry 直接调用 ``str.replace``，上述能力会被写死。因此
这里定义稳定的 ``CodeEditor`` 协议与 ``CodeEdit`` 指令：

    Graph / Tool 层  ->  CodeEdit  ->  EditorRegistry  ->  具体编辑器
                                                          ├─ AnchorCodeEditor (M2)
                                                          ├─ FileCreator (全量文件)
                                                          └─ TreeSitterEditor (预留)

编辑器只负责“如何把一次编辑落到文件内容”，不负责沙盒读写、Git checkpoint、
构建与回滚；这些仍由 Applier / Verifier 负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable


class EditorError(RuntimeError):
    """编辑指令无法安全落地。

    典型场景：锚点不存在、锚点出现次数不符合预期、路径越界（``..``）、
    新建文件已存在等。上层应把它当作 ``apply_failed`` 处理并进入修复循环，
    而不是静默返回原文，避免“看起来成功但实际没改”。
    """

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.path = path


class EditorNotAvailableError(EditorError):
    """编辑器尚未实现（例如 M2 阶段的 TreeSitterEditor）。"""


class EditKind(str, Enum):
    """一次编辑的语义类型，用于路由到具体编辑器。"""

    # 锚点字符串编辑（M2）
    REPLACE_ANCHOR = "replace_anchor"
    REPLACE_ALL = "replace_all"
    INSERT_BEFORE = "insert_before"
    INSERT_AFTER = "insert_after"

    # 全量文件写入（全栈 FileCreator）
    CREATE_FILE = "create_file"
    REWRITE_FILE = "rewrite_file"

    # 基于 AST / 语言服务器的结构化编辑（TreeSitterEditor，预留）
    STRUCTURED = "structured"


class EditLayout(str, Enum):
    """插入内容的排版方式。"""

    INLINE = "inline"
    """直接在锚点字符处拼接，适合插入到 ``"}</style>"`` 这类紧邻位置。"""

    BLOCK = "block"
    """以整行插入：定位锚点所在行的行首/行尾，并按 ``indent`` 缩进。"""


@dataclass(frozen=True)
class CodeEdit:
    """与具体编辑器无关的一次编辑指令。

    Attributes:
        kind: 编辑语义，决定由哪个 ``CodeEditor`` 处理。
        path: 工作区内的相对路径（``src/App.jsx``）。
        content: 替换文本、插入文本或完整文件内容。
        anchor: 锚点字符串，仅锚点类编辑使用。
        occurrence: 锚点出现序号。正数从 1 开始，负数表示从末尾倒数
            （``-1`` 即最后一次出现）。LLM 的修改应尽量保证锚点唯一。
        layout: 插入排版方式，见 ``EditLayout``。
        indent: ``BLOCK`` 插入时使用的缩进；``None`` 表示沿用锚点所在行的缩进。
        expected_matches: 期望锚点出现次数，用于防御“改错位置”。
        label: 可读说明，写入事件 / 日志，便于审计。
        metadata: 编辑器特有的扩展参数，保证协议无需频繁变更。
    """

    kind: EditKind
    path: str
    content: str = ""
    anchor: str = ""
    occurrence: int = 1
    layout: EditLayout = EditLayout.INLINE
    indent: str | None = None
    expected_matches: int | None = None
    label: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EditResult:
    """一次编辑的结果。

    ``files`` 只包含本次发生变化的文件（相对路径 -> 完整内容），调用方直接
    合并进工作区即可。这样 TreeSitterEditor 将来一次修改多个文件时，协议
    不需要变化。
    """

    editor: str
    files: Mapping[str, str]
    changed: bool
    summary: str = ""


@runtime_checkable
class CodeEditor(Protocol):
    """所有编辑器必须实现的协议。"""

    name: str

    def supports(self, kind: EditKind) -> bool:
        """是否处理该类型的编辑。"""

    def apply(self, edit: CodeEdit, files: Mapping[str, str]) -> EditResult:
        """在不修改入参的前提下返回编辑后的文件内容。"""
