# CustomAgent Manifest 目录

AI Core 启动时扫描本目录下的 `*.json`，只登记能力，不执行任何代码。

部署企业专属 Agent 时，把 Manifest 放到这里，并确保：

- `agentId` 唯一，不能覆盖内置 Agent；
- `systemPromptFile` / `ragKnowledgeDir` 必须是本目录内的相对路径；
- `taskTypes` 至少一项；
- `enabled` 为 `true`。

示例文件名以 `.example` 结尾，不会被扫描；复制为 `.json` 才会生效。

```bash
cp custom-agent.example.json.example custom-agent.example.json
```

当前阶段 Supervisor 只返回 `handoff_custom` 路由计划；CustomAgent Runtime
（Prompt / RAG 沙盒执行）在后续阶段接入。
