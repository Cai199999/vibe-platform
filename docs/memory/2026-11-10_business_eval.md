# 2026-11-10 · 路由加固、业务语义断言与 Business Eval

## 目标

把验收从“文件存在 + 能构建”推进到“AI 是否真的理解业务”。拆成三层：

1. 路由解析加固；
2. 导出包业务语义断言；
3. 独立业务评测集与通过率报告。

## 路由解析加固

`scripts/acceptance_test.py`：

- 后端：`parse_express_routes` 支持 `router.get/post/...`、
  `app.route('/x').get(...).post(...)`、`app.use('/api', router)` +
  `router.use('/v1', child)` 二级/多级前缀链；仍合并
  `server/api-routes.json`。
- 前端：覆盖 `fetch`、`axios.get/post`、`axios({method,url})`、
  自定义 HTTP client、路径常量、模板字符串、`apiUrl(CONST)`；
  `INTERACTIONS` 优先整体 JSON 解析，避免 `{task_id}` 干扰。
- 动态段统一归一化 `:param`，REST 更新允许 PUT/PATCH 二选一。

## 业务语义断言

`check_business_semantics()` 由契约驱动，仅对 `full_stack_system` 生效：

- 契约每个 `apiRoutes` 必须出现在后端实际路由表；
- 契约 DDL 每张表必须写入 `server/schema.sql`；
- 契约出现上传/附件/multipart 时：
  - `server/index.js` 必须包含 multer/formidable/busboy 等中间件；
  - `server/package.json` 必须声明该依赖；
  - DDL 必须存在 file/upload/attachment/resume 类字段；
- 存在真实 API 交互时，前端必须有成功态提示
  （`setStatus('success')` / `setSuccess(` / `成功` / `alert(`）。

`acceptance_test.py` 新增第 5 阶段“业务语义断言”。

## Business Eval Suite

- `config/eval_suite/business_cases.yaml`：4 个用例
  - `biz_001` 纯前端作品集（无 API/DDL）；
  - `biz_002` 客户 CRUD；
  - `biz_003` 简历上传 + 列表；
  - `biz_004` 待办 CRUD。
- `scripts/business_eval.py`：真实 DeepSeek 驱动 Architect SSE，
  自动回答澄清（YAML `clarifications` 优先，其余按槽位/选项兜底），
  断言形态、路由、表、字段；`run_fullstack` 用例继续生成、导出并跑
  路由一致性与业务语义检查。
- 区分三个指标：业务通过率、AI 契约理解准确率、产物语义通过率；
  输出 `docs/business_eval_report.md`，聚合 token / cost。

## 发现并修复的产物缺陷

首次 `biz_003` 契约 100% 正确，但产物失败：
`server/index.js` 没有 multer。进一步发现沙盒重启不会安装新增依赖。

修复：

1. `build_upload_support`：契约有上传意图时注入 `multer@2.5.0`、
   磁盘存储（5MB 限制、随机文件名、`/uploads` 静态目录），
   并把 `file_name/file_path/file_size` 映射到 DDL 对应列；
   普通 CRUD 契约不注入。
2. `SandboxProvider.verify` 新增 `install`：
   根项目 + `server/` 分别 `npm install`；
   `BuildVerifier` 在全栈构建前先安装依赖。

## 验证

- `business_eval.py` 全量实跑：4/4 = 100%，
  契约理解 4/4，产物语义 3/3；36,586 tokens / ¥0.102416 / 123.5s。
- `acceptance_test.py` 仍全绿：导出 69.8KB、路由一致、npm build、
  docker build、无 DB env 下 SQLite CRUD 通过。
- `tests.test_fullstack`：28 tests OK（新增 upload 用例）。
- `black` 已执行。

## 注意

- 4 个用例是小样本，100% 只代表当前覆盖范围；后续应补充
  “需求含糊 / 多表关联 / 鉴权 / 分页筛选”等更难用例。
- 业务语义是静态断言，不等于运行时端到端业务测试；
  上传用例目前验证中间件、依赖与字段映射，尚未在验收中真实发 multipart 请求。
