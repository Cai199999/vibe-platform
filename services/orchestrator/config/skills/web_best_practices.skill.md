---
skillId: web.best_practices
name: Web 开发规范与安全编码
agentId: backend_coder
agentIds:
  - backend_coder
description: 强制 RESTful API 设计、输入校验、ORM 参数化查询、Bcrypt 密码哈希、最小权限与安全响应规范。
version: 1.0.0
enabled: true
priority: 20
tools:
  - read_file
  - create_file
  - apply_code_edit
keywords:
  - restful
  - api设计
  - 安全编码
  - sql注入
  - bcrypt
  - 密码
  - 鉴权
  - 输入校验
---

# Web 开发规范与安全编码（强制）

本 Skill 强制挂载给 BackendCoder。生成任何 REST API、数据库访问、认证
或表单处理代码时，以下规则是**验收条件**，不是建议。

## 1. RESTful API 设计

- 资源使用复数名词与稳定路径：`/api/v1/resumes`、`/api/v1/jobs`；
  禁止动词路径（`/getResume`、`/createJob`）。
- 方法语义固定：
  - `GET` 查询，不产生副作用；
  - `POST` 创建 / 执行非幂等动作，成功返回 `201`；
  - `PUT` / `PATCH` 更新，资源不存在返回 `404`；
  - `DELETE` 删除，成功返回 `200` 或 `204`。
- 列表接口必须支持 `page` / `size` 分页，并返回
  `{ items, total, page, size }`；`size` 上限 100。
- 错误响应统一为 `{ code, message }`；参数错误 `400`、未认证 `401`、
  无权限 `403`、不存在 `404`、冲突 `409`、服务异常 `500`。
- 路由参数统一 `{name}`（契约）/ `:name`（Express），不得混用冒号与花括号。
- 前后端 Method + Path 必须逐字符一致；静态集合路由与详情路由必须成对出现。

## 2. SQL 注入防护

- 所有数据库访问必须使用 ORM（Sequelize / SQLAlchemy）或参数化查询；
  禁止把用户输入拼接进 SQL 字符串。
- 原生 SQL 只允许用于迁移；迁移中的表名 / 列名必须来自契约白名单，
  不得直接透传请求参数。
- 动态排序、过滤字段必须映射到固定白名单：
  `const SORTABLE = new Set(['created_at', 'name'])`，未命中直接拒绝。
- 查询值一律走绑定参数：`WHERE id = ?` / `WHERE id = :id`，
  不得使用模板字符串 `WHERE id = '${id}'`。
- ORM 查询同样禁止 `sequelize.query()` 拼接字符串；必须传
  `replacements` / `bind`。

## 3. 密码与认证

- 密码只存哈希，使用 **Bcrypt**（Node: `bcryptjs`，Python: `bcrypt`）；
  禁止 MD5 / SHA1 / 明文 / 可逆加密。
- 哈希成本因子：Node `bcrypt.hash(password, 12)`；Python
  `bcrypt.hashpw(password, bcrypt.gensalt(rounds=12))`。
- 登录校验使用 `bcrypt.compare` / `bcrypt.checkpw`，失败统一返回
  `401 INVALID_CREDENTIALS`，不得区分“用户不存在 / 密码错误”。
- 密码、Token、API Key 不得写日志、不得出现在响应体、不得进入前端 bundle。
- 会话 Token 必须设置过期时间；生产环境通过 HttpOnly / Secure Cookie
  或 Authorization Header 传递，禁止 URL 参数携带。

## 4. 输入校验与输出安全

- 请求体必须做类型、长度、枚举与必填校验；拒绝未知的大量字段。
- 字符串长度设置硬上限（例如 8000 字符），数组长度设置上限。
- 文件上传必须限制 MIME、扩展名与大小；文件名重新生成，禁止使用
  用户提供的路径拼接磁盘路径。
- 返回给客户端的错误不得包含堆栈、SQL、连接串或第三方密钥。
- 前端渲染用户内容时使用 React 默认转义，禁止 `dangerouslySetInnerHTML`
  直接插入未清洗内容。

## 5. 依赖与配置

- 第三方 AI / 邮件 / 支付 SDK 只在服务端初始化，密钥从
  `process.env` 读取；缺失密钥返回 `503`，不得返回伪造结果。
- 新增依赖必须固定主版本并说明用途；不得引入来源不明的包。
- CORS 只允许显式白名单；生产环境禁止 `origin: '*'` 与凭据同时启用。

## 6. 交付前自检

1. 搜索是否存在字符串拼接 SQL、`sequelize.query(\`...\`)`。
2. 搜索密码字段是否使用 Bcrypt，是否存在明文密码。
3. 检查每个写接口是否有输入校验与统一错误结构。
4. 检查所有密钥是否只从环境变量读取。
5. 在总结中输出 `Web best practices:` 清单，逐项说明通过情况。
