"""兼容层：代码编辑器实现已迁移至 ``ai_core.adapters.editor``。"""

from ai_core.adapters.editor import (
    AnchorCodeEditor,
    CodeEdit,
    CodeEditor,
    EditKind,
    EditLayout,
    EditResult,
    EditorError,
    EditorNotAvailableError,
    EditorRegistry,
    FileCreator,
    TreeSitterEditor,
    default_editor_registry,
    normalize_workspace_path,
)

__all__ = [
    "AnchorCodeEditor",
    "CodeEdit",
    "CodeEditor",
    "EditKind",
    "EditLayout",
    "EditResult",
    "EditorError",
    "EditorNotAvailableError",
    "EditorRegistry",
    "FileCreator",
    "TreeSitterEditor",
    "default_editor_registry",
    "normalize_workspace_path",
]
