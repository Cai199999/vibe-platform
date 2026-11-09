"""DBA Agent（预留 Agent）。

当前只有声明式 Manifest，没有 Graph 与运行时；Supervisor 可以展示其
DDL / 迁移 / 索引能力，但显式路由会返回 ``AGENT_NOT_AVAILABLE``。
M3.5 接入契约驱动的数据库生成后，再在此目录实现 ``DbaAgentGraph``。
"""
