"""DevOps Agent（预留 Agent）。

当前只有声明式 Manifest，没有 Graph 与运行时；Supervisor 可以展示其
容器化、CI/CD 与部署配置能力，但显式路由会返回
``AGENT_NOT_AVAILABLE``。M4 接入构建与发布流水线后，再在此目录实现
``DevopsAgentGraph``。
"""
