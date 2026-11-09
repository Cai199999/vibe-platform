"""IntegrationVerifier（预留 Agent）。

当前只有声明式 Manifest，没有 Graph 与运行时；Supervisor 可以展示其
端到端联调、契约一致性与失败归因能力，但显式路由会返回
``AGENT_NOT_AVAILABLE``。M4 接入多服务沙盒后，再在此目录实现
``IntegrationVerifierGraph``。
"""
