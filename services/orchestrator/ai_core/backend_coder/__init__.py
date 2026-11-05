"""BackendCoder（预留 Agent）。

当前只有声明式 Manifest，没有 Graph 与运行时；Supervisor 可以识别并展示
该能力，但显式路由会返回 ``AGENT_NOT_AVAILABLE``。M3 接入契约驱动的
Spring Boot 生成后，再在此目录实现 ``BackendCoderGraph``。
"""
