"""应用层端口（Ports）。

端口是应用层与外部世界之间的抽象契约，不包含任何具体实现：
  - ``ModelClient``：大模型调用；
  - ``SandboxProvider``：代码沙盒生命周期；
  - ``RAGProvider``：知识检索；
  - ``CheckpointerProvider``：LangGraph 状态持久化。
"""

from ai_core.ports.checkpoint import CheckpointerProvider
from ai_core.ports.editor import CodeEdit, CodeEditor, EditKind, EditLayout, EditResult
from ai_core.ports.model import ModelClient, ModelClientError
from ai_core.ports.rag import RAGProvider, RAGProviderError, RetrievedChunk
from ai_core.ports.sandbox import (
    SandboxError,
    SandboxGitError,
    SandboxHandle,
    SandboxNotFoundError,
    SandboxPatchError,
    SandboxProvider,
    SandboxStartupError,
    SandboxValidationError,
    VerifyResult,
)

__all__ = [
    "CheckpointerProvider",
    "CodeEdit",
    "CodeEditor",
    "EditKind",
    "EditLayout",
    "EditResult",
    "ModelClient",
    "ModelClientError",
    "RAGProvider",
    "RAGProviderError",
    "RetrievedChunk",
    "SandboxError",
    "SandboxGitError",
    "SandboxHandle",
    "SandboxNotFoundError",
    "SandboxPatchError",
    "SandboxProvider",
    "SandboxStartupError",
    "SandboxValidationError",
    "VerifyResult",
]
