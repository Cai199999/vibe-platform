"""兼容层：文件创建编辑器已迁移至 ``ai_core.adapters.editor``。"""

from ai_core.adapters.editor.file_creator import FileCreator, normalize_workspace_path

__all__ = ["FileCreator", "normalize_workspace_path"]
