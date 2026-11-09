"""适配器层（Adapters）。

具体实现外部依赖并实现 ``ai_core.ports`` 中的端口：
  - ``adapters.model``：DeepSeek / OpenAI 兼容模型调用；
  - ``adapters.sandbox``：本地 Docker 沙盒；
  - ``adapters.rag``：Markdown BM25 / Chroma 检索；
  - ``adapters.checkpoint``：LangGraph Memory / Redis checkpoint。

应用层（agent / architect / supervisor）不得直接构造这些实现。
"""
