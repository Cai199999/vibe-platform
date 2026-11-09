"""纯领域层（Domain）。

包含整个系统的通用语言：标注信封、运行契约、架构契约、会话状态与
Skill 描述。领域层只依赖标准库与 Pydantic，禁止导入 LangGraph、FastAPI、
Docker、LangChain 或任何具体大模型 SDK。

具体模块按需导入，避免包初始化时产生循环依赖。
"""
