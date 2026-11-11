"""平台内置 MCP Server。

当前提供 SQLite Schema MCP：在构建前把生成 DDL 真正执行到临时数据库，
再查询 sqlite_master / PRAGMA table_info，用于 ConsistencyVerifier 的
真实结构校验，而不是静态文本比对。
"""
