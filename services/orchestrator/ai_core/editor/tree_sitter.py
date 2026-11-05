"""跨文件结构化编辑器（预留实现）。

全栈场景的典型编辑：

- 修改 ``Order`` 实体后，同步 ``OrderMapper``、``OrderDTO``、前端 ``types.ts``；
- 重命名后端 DTO 字段后，同步 API 契约与调用方；
- 新增接口后，在路由表 / OpenAPI 文档 / 前端 SDK 中注册。

这些操作依赖 AST 符号定位，而不是字符串锚点。本类只固定接口与边界，
M2 不引入 ``tree-sitter`` 依赖；接入时需保证：

1. 每种语言预先加载 grammar，禁止运行时从网络下载；
2. 只允许修改符号的精确范围（``node.start_byte`` / ``end_byte``），
   不做全文正则替换；
3. 跨文件编辑返回的 ``EditResult.files`` 可以包含多个文件；
4. 语法树解析失败必须显式报错，回退到 ``AnchorCodeEditor`` 需要上层决策，
   不能在编辑器内部静默降级。
"""

from __future__ import annotations

from typing import Mapping

from ai_core.editor.base import (
    CodeEdit,
    EditKind,
    EditResult,
    EditorNotAvailableError,
)


class TreeSitterEditor:
    """Tree-sitter 结构化编辑占位。"""

    name = "tree_sitter"

    def __init__(self, languages: tuple[str, ...] = ()) -> None:
        # 预留 grammar 配置；M2 不加载任何语言包。
        self.languages = languages

    def supports(self, kind: EditKind) -> bool:
        return kind is EditKind.STRUCTURED

    def apply(self, edit: CodeEdit, files: Mapping[str, str]) -> EditResult:
        raise EditorNotAvailableError(
            "TreeSitterEditor 尚未实现：M2 请使用 AnchorCodeEditor / FileCreator；"
            "全栈阶段接入 tree-sitter 后再路由 STRUCTURED 编辑",
            path=edit.path,
        )


__all__ = ["TreeSitterEditor"]
