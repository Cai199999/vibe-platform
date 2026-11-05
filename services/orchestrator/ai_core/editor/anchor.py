"""锚点字符串编辑器（M2 默认实现）。

设计约束：
  - 任何锚点都必须显式出现，找不到即报错，禁止“悄悄返回原文”；
  - 支持 ``occurrence`` 与 ``expected_matches``，避免重复锚点误伤；
  - ``BLOCK`` 插入自动处理缩进与换行，避免生成 ``<button>`` 紧贴
    ``</section>`` 这类可读性差但语法合法的代码。

局限：
  - 不理解 JSX / Java 语法，锚点本身必须足够稳定；
  - 跨文件符号重命名应交给 ``TreeSitterEditor``，不要用本编辑器堆正则。
"""

from __future__ import annotations

from typing import Mapping

from ai_core.editor.base import (
    CodeEdit,
    EditKind,
    EditLayout,
    EditResult,
    EditorError,
)


class AnchorCodeEditor:
    """基于字符串锚点的确定性编辑器。"""

    name = "anchor"

    _SUPPORTED = frozenset(
        {
            EditKind.REPLACE_ANCHOR,
            EditKind.REPLACE_ALL,
            EditKind.INSERT_BEFORE,
            EditKind.INSERT_AFTER,
        }
    )

    def supports(self, kind: EditKind) -> bool:
        return kind in self._SUPPORTED

    # ------------------------------------------------------------------
    # CodeEditor 协议
    # ------------------------------------------------------------------
    def apply(self, edit: CodeEdit, files: Mapping[str, str]) -> EditResult:
        if edit.kind not in self._SUPPORTED:
            raise EditorError(
                f"AnchorCodeEditor 不支持 {edit.kind.value}", path=edit.path
            )
        if edit.path not in files:
            raise EditorError(f"目标文件不存在: {edit.path}", path=edit.path)

        source = files[edit.path]
        if edit.kind is EditKind.REPLACE_ANCHOR:
            updated = self.replace_anchor(
                source,
                edit.anchor,
                edit.content,
                occurrence=edit.occurrence,
                expected_matches=edit.expected_matches,
                path=edit.path,
            )
        elif edit.kind is EditKind.REPLACE_ALL:
            updated = self.replace_all(
                source,
                edit.anchor,
                edit.content,
                expected_matches=edit.expected_matches,
                path=edit.path,
            )
        elif edit.kind is EditKind.INSERT_BEFORE:
            updated = self.insert_before(
                source,
                edit.anchor,
                edit.content,
                occurrence=edit.occurrence,
                layout=edit.layout,
                indent=edit.indent,
                expected_matches=edit.expected_matches,
                path=edit.path,
            )
        else:  # INSERT_AFTER
            updated = self.insert_after(
                source,
                edit.anchor,
                edit.content,
                occurrence=edit.occurrence,
                layout=edit.layout,
                indent=edit.indent,
                expected_matches=edit.expected_matches,
                path=edit.path,
            )

        changed = updated != source
        summary = edit.label or (
            f"{edit.kind.value} @ {edit.anchor[:32]!r}"
            if edit.anchor
            else edit.kind.value
        )
        return EditResult(
            editor=self.name,
            files={edit.path: updated},
            changed=changed,
            summary=summary,
        )

    # ------------------------------------------------------------------
    # 基础操作：既可被 apply 调用，也可被 Mock / 工具直接复用
    # ------------------------------------------------------------------
    def replace_anchor(
        self,
        source: str,
        anchor: str,
        content: str,
        *,
        occurrence: int = 1,
        expected_matches: int | None = None,
        path: str = "",
    ) -> str:
        positions = self._match_positions(source, anchor, expected_matches, path)
        index = self._pick(positions, occurrence, anchor, path)
        return source[:index] + content + source[index + len(anchor) :]

    def replace_all(
        self,
        source: str,
        anchor: str,
        content: str,
        *,
        expected_matches: int | None = None,
        path: str = "",
    ) -> str:
        positions = self._match_positions(source, anchor, expected_matches, path)
        if not positions:
            raise EditorError(f"锚点未找到: {anchor!r}", path=path)
        return source.replace(anchor, content)

    def insert_before(
        self,
        source: str,
        anchor: str,
        content: str,
        *,
        occurrence: int = 1,
        layout: EditLayout = EditLayout.INLINE,
        indent: str | None = None,
        expected_matches: int | None = None,
        path: str = "",
    ) -> str:
        positions = self._match_positions(source, anchor, expected_matches, path)
        index = self._pick(positions, occurrence, anchor, path)
        if layout is EditLayout.INLINE:
            return source[:index] + content + source[index:]

        # BLOCK：插到锚点所在行的行首，保持整行结构。
        line_start = source.rfind("\n", 0, index) + 1
        block = self._format_block(
            content,
            indent if indent is not None else self._line_indent(source, index),
        )
        return source[:line_start] + block + source[line_start:]

    def insert_after(
        self,
        source: str,
        anchor: str,
        content: str,
        *,
        occurrence: int = 1,
        layout: EditLayout = EditLayout.INLINE,
        indent: str | None = None,
        expected_matches: int | None = None,
        path: str = "",
    ) -> str:
        positions = self._match_positions(source, anchor, expected_matches, path)
        index = self._pick(positions, occurrence, anchor, path) + len(anchor)
        if layout is EditLayout.INLINE:
            return source[:index] + content + source[index:]

        # BLOCK：插到锚点所在行的下一行行首。
        line_end = source.find("\n", index)
        if line_end < 0:
            line_end = len(source)
        else:
            line_end += 1
        block = self._format_block(
            content,
            (
                indent
                if indent is not None
                else self._line_indent(source, max(0, index - len(anchor)))
            ),
        )
        return source[:line_end] + block + source[line_end:]

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _match_positions(
        source: str,
        anchor: str,
        expected_matches: int | None,
        path: str,
    ) -> list[int]:
        if not anchor:
            raise EditorError("锚点不能为空", path=path)
        positions: list[int] = []
        start = 0
        while True:
            index = source.find(anchor, start)
            if index < 0:
                break
            positions.append(index)
            start = index + len(anchor)
        if expected_matches is not None and len(positions) != expected_matches:
            raise EditorError(
                f"锚点 {anchor[:48]!r} 期望出现 {expected_matches} 次，"
                f"实际 {len(positions)} 次",
                path=path,
            )
        return positions

    @staticmethod
    def _pick(
        positions: list[int],
        occurrence: int,
        anchor: str,
        path: str,
    ) -> int:
        if not positions:
            raise EditorError(f"锚点未找到: {anchor[:64]!r}", path=path)
        if occurrence == 0:
            raise EditorError("occurrence 不能为 0（正数从 1 开始，负数从末尾倒数）")
        try:
            return (
                positions[occurrence - 1] if occurrence > 0 else positions[occurrence]
            )
        except IndexError as exc:
            raise EditorError(
                f"锚点 {anchor[:48]!r} 只有 {len(positions)} 处，"
                f"无法定位第 {occurrence} 处",
                path=path,
            ) from exc

    @staticmethod
    def _line_indent(source: str, index: int) -> str:
        line_start = source.rfind("\n", 0, index) + 1
        prefix = source[line_start:index]
        return prefix[: len(prefix) - len(prefix.lstrip(" \t"))]

    @staticmethod
    def _format_block(content: str, indent: str) -> str:
        """把多行内容统一缩进，并保证以换行结束。"""
        lines = content.rstrip("\n").splitlines() or [""]
        rendered = "\n".join(
            f"{indent}{line}" if line.strip() else "" for line in lines
        )
        return f"{rendered}\n"


__all__ = ["AnchorCodeEditor"]
