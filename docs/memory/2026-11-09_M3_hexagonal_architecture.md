# 2026-11-09 M3.1：六边形架构提纯（Ports & Adapters）

## 目标

只调整结构，不改业务逻辑：把“按 Agent 角色平铺”的 AI Core 收敛为
`domain → ports → adapters → application`，降低 Agent 与具体框架 / 厂商
SDK 的耦合。

## 新目录

```text
ai_core/
├── domain/                 # 纯领域层：只依赖标准库 + Pydantic
│   ├── common.py           # CamelModel / to_camel
│   ├── agent.py            # AnnotationEnvelope、Run / HITL 契约
│   ├── architect.py        # ArchitectContract、澄清 / 快照契约
│   ├── supervisor.py       # SupervisorDecision、Handoff、Gate
│   ├── state.py            # AgentState / ArchitectState / VerifyState
│   └── skills.py           # SkillDescriptor
├── ports/                  # 应用层端口：只定义契约
│   ├── model.py            # ModelClient.generate(task, system, human)
│   ├── sandbox.py          # SandboxProvider
│   ├── rag.py              # RAGProvider
│   ├── checkpoint.py       # CheckpointerProvider
│   └── editor.py           # CodeEditor
├── adapters/               # 具体实现
│   ├── model/              # DeepSeekModelClient + ModelRouter
│   ├── sandbox/            # LocalDockerSandbox + 工厂
│   ├── rag/                # MarkdownRAG / ChromaRAG + 工厂
│   └── checkpoint.py       # Memory / Redis Checkpointer
├── agent/                  # 应用层：ui_graph 编排
├── architect/              # 应用层：architect_graph 编排
└── supervisor/             # 应用层：路由 / Handoff / Gate
```

## 依赖规则

- `domain` 禁止导入 `langgraph`、`fastapi`、`docker`、`langchain*`、
  `openai`、`redis`、`minio`、`httpx`、`sqlalchemy`；
- `ports` 禁止导入 `ai_core.adapters`；
- Graph / Service 只依赖端口类型；默认适配器由组合根注入；
- 旧路径保留为 re-export shim，例如：
  - `ai_core.agent.schemas` → `ai_core.domain.agent`
  - `ai_core.architect.schemas` → `ai_core.domain.architect`
  - `ai_core.sandbox.base` → `ai_core.ports.sandbox`
  - `ai_core.rag.base` → `ai_core.ports.rag`
  - `ai_core.sandbox.local_docker` → `ai_core.adapters.sandbox.local_docker`

## 关键改造

- `DeepSeekClient(ModelClient)` 封装 `ModelRouter + ChatOpenAI`；
  `DeepSeekAgentLLM` / `DeepSeekArchitectLLM` 只持有 `ModelClient`，
  支持注入假客户端做单测。
- `ArchitectGraphRunner` 新增 `rag_provider_factory` 注入点。
- `LocalDockerSandbox`、`MarkdownRAGProvider`、`ChromaRAGProvider`
  分别实现对应端口；`CheckpointerProvider` 改为不依赖 LangGraph 的
  `Protocol`，具体 Saver 留在适配器。

## 验证

- `tests/test_hexagonal_architecture.py`：9 项边界测试
  （AST 禁止依赖扫描、端口纯度、适配器继承、兼容路径恒等）。
- 全量单测 **138 passed**。
- README 新增六边形架构 Mermaid 图与依赖规则表。
