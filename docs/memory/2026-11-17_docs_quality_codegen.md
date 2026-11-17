# 2026-11-17 方案文档投影、质量分级与 CodeGen 门禁修正

## 方案文档投影（模拟文档）

- 新增前端 `workspace/projectDocs.js`：
  - 从 contract / VisualDocument 确定性投影 5 份文档：
    01-需求与功能模块清单、02-技术选型与架构设计、
    03-数据库设计文档、04-API接口设计清单、
    05-最终设计方案与开发规范；
  - `simulated=true`，不写回、不调用 LLM、不作为第二事实源；
  - 文档包含 docId / revision / contentHash / status / sections。
- 新增 `ProjectDocsDrawer`：
  - Architect 面板“📄 方案文档”按钮；
  - 01–05 Tab、元信息、单份/全部 Markdown 导出；
  - 明确标注“模拟文档 · 由当前契约自动生成”。
- 文档名称与章节作为后续 Agent 的统一引用规范；未来可替换为正式文档生成器。

## 质量分级只问一次

- 新增 `quality_tier_selected` 状态：
  - 首次契约确认前为 false；
  - 用户选择并 CONFIRM / REVISE 后为 true；
  - 契约修订、重新确认沿用该选择。
- `qualityTierRequired` 使用该标志，不再被默认 L2 误判。
- 模型生成的 quality-tier 澄清问题被确定性过滤。

## CodeGen 失败修正

- 根因：`repeat.source` 未绑定被 CodeGen 硬门禁拦截，错误被包装成
  “前端工程师失败 / 通用内部错误”。
- 修正：
  - `_assert_repeats_bound` 改为 `_unbound_repeat_warnings`；
  - CodeGen 使用 `VisualRepeat.sampleCount` 示例数据渲染；
  - 未绑定 repeat 写入 compile warnings，继续构建；
  - 不再阻断静态原型生成。
- 真实 Docker 沙盒 E2E：
  - 多页面冻结文档（含未绑定 ghost_items）；
  - npm install / build / dev 全部成功；
  - App.jsx 含 VISUAL_PAGES + 页面导航；
  - Playwright 实际点击“用户页”切换成功，0 报错。

## 全局工程规范

- Architect Prompt 与 LLM BackendCoder Prompt 新增：
  - DDL 禁止物理外键，关联用 `<parent>_id` + 应用层事务；
  - 前端 feature / shared / hooks / api 分层；
  - 后端 route / service / repository / dto 分层；
  - 依赖单向、禁止循环依赖与巨石文件；
  - 方案文档固定 01–05 命名。

## 验证

- 前端 65 项 Vitest，构建通过；
- Python 676 项全量，仅 7 项既有环境失败；
- 真实沙盒多页面 CodeGen + Playwright PASS；
- V0 模拟预览 E2E PASS（页面 Tab、模拟数据、delta、BACKEND_BINDING）。
