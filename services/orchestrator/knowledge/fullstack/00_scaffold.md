# 全栈脚手架规范

## 1. 应用形态优先

架构设计必须首先判断应用形态，禁止默认所有需求都需要后端与数据库。

- `lightweight_web`：纯前端 React / H5，数据可放 LocalStorage、IndexedDB 或 Serverless 云函数。
- `script_automation`：无 UI，只有 Python / Node 脚本，核心是输入、输出、运行环境与依赖。
- `full_stack_system`：需要持久化、多用户 / 多端协同、权限、事务或复杂查询的完整系统。

判定原则：

1. 用户只描述页面交互、视觉、单机数据 → 优先 `lightweight_web`。
2. 用户只描述定时任务、批量处理、调用 API、生成文件 → 优先 `script_automation`。
3. 出现“登录、订单、库存、支付、多人协作、保存到数据库、管理后台”等关键词 → 评估 `full_stack_system`。
4. 无法判断时，第一轮必须询问用户，而不是默认全栈。

## 2. React 轻量 Web 脚手架

- 构建：Vite + React。
- 状态：组件内 `useState` / `useReducer`；跨页面可用 Context。
- 持久化：
  - 少量配置 → `localStorage`，必须带版本键，例如 `vibe.app.v1`；
  - 结构化数据 → `IndexedDB`；
  - 需要跨设备同步 → 使用 Serverless 函数 + 托管数据库，再升级为全栈。
- 不允许为了“以后可能扩展”而提前引入 Spring Boot / MySQL。

## 3. Java 全栈脚手架

```text
apps/
  platform-web/          # React 前端
  platform-bff/          # Spring Boot BFF / 业务 API
services/
  orchestrator/          # Python AI Core
```

- Java 21 + Spring Boot 3.x。
- 分层：Controller → Service → Mapper / Repository。
- 参数校验使用 Jakarta Validation。
- 错误响应统一：`code` + `message` + `details`。
- 数据库访问使用 MyBatis-Plus 或 Spring Data JPA，禁止在 Controller 拼接 SQL。
- 配置通过环境变量注入，禁止提交密钥。

## 4. Python 脚本自动化脚手架

```text
scripts/
  main.py                # 入口
  requirements.txt       # 依赖锁定
  README.md              # 运行方式、环境变量、示例
```

- 运行环境必须明确 Python 版本。
- 输入输出必须明确：命令行参数、文件、HTTP API、数据库或标准输出。
- 外部调用设置超时、重试与错误退出码。
- 只读凭证从环境变量读取；脚本不得在日志中打印密钥。

## 5. 通用约束

- 禁止物理外键；使用 `tenant_id`、索引和业务一致性校验。
- 所有写操作必须可幂等；接口使用业务唯一键或幂等键。
- 先契约后实现：API、数据模型、组件交互必须同时出现在实施契约中。
- 契约应包含实施路径与验收标准，而不只是技术名词。
