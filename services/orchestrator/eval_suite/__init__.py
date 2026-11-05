"""vibe-platform 离线自动化评测套件（Eval Suite）。

入口：``python -m eval_suite``。核心模型与加载器不依赖 LangGraph，
只有执行器（engine）会加载 AI Core 的 Supervisor 与 MockAgentLLM。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
