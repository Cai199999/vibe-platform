# 面试技术叙事 · vibe-platform

> 目标公司：苍穹数码（GIS 平台 / 遥感测绘）。
> 叙事一句话：**我不是在演示一个低代码产品，我在演示我交付一个 GIS 业务应用的默认工作流。**
>
> 工作流：需求收敛 → 实施契约 → V0 画布对齐 → 冻结 → 沙盒生成 → 圈选/自然语言修改 → 交付。

---

## 1. 真实需求起点

以「测绘任务与成果管理平台」为例：

- 角色：项目经理、作业员、质检员；
- 页面：项目总览、任务看板、成果质检、影像上传；
- 实体：Project、Task、Deliverable、QCRecord；
- 流程：建项目 → 拆任务派发 → 上传影像成果 → 质检合格/返工 → 总览更新进度；
- 地图：内联 SVG 示意底图，真实项目替换为 Cesium / OpenLayers；
- 复用：先查 GeoAI 开源方案，能复用就 REUSE，能改就 ADAPT，都不行才 BUILD。

这不是“让 AI 随便生成一个页面”，而是**一个要交付、要改、要回溯的业务系统**。

---

## 2. 解决的痛点

| 真实痛点 | 平台做法 | 结果 |
|---|---|---|
| 生成很快，但改不动；一改就碰坏别的组件 | 源码身份（data-oid + source map）+ Direct Ops 直接改 AST | 小改动不走全量重生成，能定位到源码 |
| 前端、后端、数据库各做各的，最后对不上 | 契约先行；V0 冻结后才能进入 BACKEND_BINDING；未确认契约 release 返回 409 | 项目级门控，契约是唯一事实源 |
| AI 写的代码“看起来对”，但跑不起来 | Docker 沙盒真实构建 + AST 门禁 + 一致性校验 + 有界自愈 + Git checkpoint | 失败可定位、可回滚，不把坏代码当成功 |
| 需求还没收敛，架构师先问技术栈 | 需求与目的优先；先问项目规模/质量分级；技术选型必须给取舍 | 问题更像业务评审，不像技术考试 |
| 多 Agent 互相甩锅、反馈不可追踪 | Supervisor 只路由；A2A 允许矩阵 + 统一 feedback envelope + 运行日志 | 谁给谁反馈、卡在哪一步可回放 |
| 每次都重复造轮子 | 本地 GeoAI 复用目录 + RAG 命中 + 契约强制 REUSE/ADAPT/BUILD | 复用建议带 URL、许可证、集成成本 |
| 刷新/重开就丢状态 | localStorage（项目/文档）+ sessionStorage（Run 快照）+ IndexedDB（草稿）+ MinIO/MySQL（源码） | 刷新后能继续改，不丢沙盒与草稿 |
| 用户随手删根节点，整页没了 | V0 页面根作为隐式容器，不渲染删除入口；删除函数拒绝根节点 | 只能删组件，不能误删整页 |

---

## 3. 全栈技术栈

| 层 | 技术 | 用途 |
|---|---|---|
| 交互层 | React 18.3 + Vite 5 + React Router 6 | 工作台、V0 画布、live 画布 |
| 交互层 | Zustand 5 | 画布文档、草稿等轻量状态 |
| 交互层 | dnd-kit | 组件拖拽、排序、落点 |
| 交互层 | 自定义 CSS | 平台 UI；生成应用使用 Tailwind |
| BFF | Java 21 + Spring Boot 3.3.5 | 统一入口、SSE 代理、错误信封、项目/源码 API |
| BFF | Spring Web + WebFlux + Validation + AOP | Servlet 主栈 + SSE 流式代理 + 参数校验 + 审计切面 |
| BFF | JDBC + MySQL Connector | 项目元数据、源码快照索引 |
| BFF | MinIO Java Client | 源码正文对象存储 |
| AI Core | Python 3.12 + FastAPI 0.115 | 内部 API、SSE、Pydantic 校验 |
| AI Core | LangGraph 0.2 | Architect / Supervisor / UI / Fullstack 显式状态图 |
| AI Core | DeepSeek V3（OpenAI 兼容） | 分析、契约、代码生成；可切本地 mock |
| RAG | Markdown 切块 + 自研 BM25 + Chroma 1.0 + RRF | 中文词项 + 向量语义混合召回 |
| 沙盒 | Docker + Node + Vite + React | 生成应用真实构建、HMR、预览 |
| 沙盒 | @babel/parser + 自定义 AST 编辑 | 确定性改 JSX，不靠正则/模型重写 |
| 沙盒 | inspect_plugin / inspect_sdk + source map | 构建期注入 data-oid，运行时回传锚点 |
| 沙盒 | Git | 每次成功写入都有 checkpoint，可回滚 |
| 数据 | MySQL（平台元数据 + 生成应用） | 项目、文件树、业务表 |
| 数据 | MinIO | 源码正文、快照 |
| 数据 | 浏览器 localStorage / sessionStorage / IndexedDB | 项目、Run 快照、草稿、文档中心 |
| 中间件 | 复用 `~/Workspace/infra` 的 MySQL / Redis / MinIO | 不重复部署 |
| 测试 | Vitest 82 / Python unittest 795 / JUnit 23 / Playwright E2E | 单元 + 接口 + 浏览器闭环 |
| 交付 | Docker Compose、`scripts/ci_local.sh` | 本地一键起服务与串行 CI |

> 说明：Redis 依赖已接入 BFF，但核心链路当前不强依赖；平台登录仍是 Mock，生成应用暂未生成完整 JWT/RBAC。

---

## 4. 关键选型与对比

| 选型 | 替代方案 | 为什么选它 | 代价 / 放弃了什么 |
|---|---|---|---|
| React + Vite | Next.js / Vue | 工作台是重交互 SPA，不需要 SSR；Vite HMR 让沙盒预览反馈快 | 没有 SSR/SEO；首屏体积换开发反馈 |
| Spring Boot BFF | Node BFF / 前端直连 Python | 企业治理、统一错误信封、SSE 代理、后续鉴权/审计好落；团队 Java 背景 | 多一跳网络、DTO 要镜像；迭代比 Node 重 |
| FastAPI + LangGraph | LangChain Agent / 纯脚本 | 状态图显式、节点可测试、失败可重试；路由权在平台不在模型 | 编排代码自己写；不如通用 Agent 框架“开箱即用” |
| DeepSeek V3 | GPT / Claude | 成本低、中文好、OpenAI 兼容；国内网络可用 | JSON/工具调用稳定性弱一些，所以有 JSON 修复、Pydantic 校验、mock 兜底 |
| BM25 + Chroma + RRF | 纯向量 / 纯关键词 | 中文术语靠 BM25，语义靠向量，RRF 融合更稳 | 索引与切块要一致，复杂度更高 |
| Docker 沙盒 | 本地进程 / WASM | 真实 npm 构建、隔离、Git、HMR；失败不碰宿主 | 资源重；目前没有 gVisor，未上云 |
| Babel AST 编辑 | 正则替换 / 让模型重写整文件 | 确定性、可失败关闭、diff 小 | 只支持已建模的 JSX 模式；动态 className 要转 AI |
| data-oid + source map | file:line / CSS selector | 源码改动后身份稳定；跨文件调用点可区分 | 构建插件复杂；地图过期必须 fail-closed |
| Zustand | Redux / Context | 画布状态更新频繁，Zustand 轻、选择器细 | 约定少，团队协作要靠规范 |
| MySQL | PostgreSQL / PostGIS | 复用现有 infra，业务团队熟悉 | 空间能力弱；真实 GIS 应接 PostGIS + Cesium/OpenLayers |
| 浏览器存储 | 全部落后端草稿 | 即时、离线、无网络也能改 | 容量/跨浏览器限制；后端草稿是下一阶段 |
| Playwright | Cypress | 多 iframe、真实 Chromium、Node/Python 都能驱动 | 更重、启动慢 |

---

## 5. 真实踩过的问题与解决

1. **V0 契约超过 8192 token 被截断**
   - 现象：多实体产品在 V0 阶段 JSON 截断，契约出不来。
   - 解决：V0 不再让模型输出完整 VisualDocument，只出 routes/layout/components，虚拟树由 `build_visual_document_from_contract` 确定性生成；加输出预算。

2. **生成页面全堆在一页**
   - 原因：页面根被当成普通节点，布局没有页面边界。
   - 解决：页面根作为隐式容器；多页面 VisualDocument + 页面 Tab。

3. **用户能删除根节点，整页消失**
   - 解决：V0 不渲染根节点外壳/删除按钮；删除函数拒绝 `parent === null` 或 `type === page`。

4. **拖拽“成功但没动 / 自动吸到最近组件”**
   - 原因：按 DOM children 猜兄弟序号；源节点参与落点计算。
   - 解决：用 manifest.childOids 的 AST 兄弟序号；拖拽时源节点 fixed + stub 占位，只用指针与剩余兄弟几何计算。

5. **跨文件改 Props 改错文件**
   - 原因：组件定义和调用点混在一起。
   - 解决：importerGraph + callSiteOid 双索引；UPDATE_PROPS 只改调用点 JSX，不碰组件定义。

6. **拖拽成功后 HMR 又回弹**
   - 解决：drag-commit 延后到 Vite HMR 后再清 inline translate。

7. **删除组件偶发失败 / 删除后组件消失但代码还在**
   - 原因一：待提交删除按 selector 记账，同 class 兄弟互清记录。
   - 原因二：删除分析是异步的，期间切选目标会串。
   - 原因三：循环项 HITL 取消后，iframe 里节点仍隐藏。
   - 解决：按 `oid + instance` 记账；分析响应绑定发起时 target；HITL 先恢复乐观隐藏节点。
   - 验证：同一缓冲连删 2 个同 selector 兄弟 + 刷新后再删 1 个，均 SUCCEEDED、noReload、pageErrors 0。

8. **模型 JSON 不合法 / 契约字段缺失**
   - 解决：JSON 修复重试、Pydantic 校验、结构化打回；`finish_reason=length` 留告警。

9. **JOIN 别名门禁误报，反复修不掉**
   - 原因：把动作端点也当关联查询；URL 段与 Sequelize alias 写法不同。
   - 解决：只收集真正传给关联 API 的别名；别名去分隔符归一；契约嵌套路由缺 relation 直接判 CONTRACT_DEFECT。

10. **生成物把主工作树弄脏，下一次 begin_draft 被拒**
    - 解决：clean-tree 检查排除 `vibe-source-map.json`，并同步 `.gitignore` / `.git/info/exclude`。

11. **带未保存草稿关闭 Tab 卡住**
    - 原因：IndexedDB hydration 可能不返回；草稿守卫 z-index 被面板遮住。
    - 解决：1.5s hydration 超时兜底；守卫 z-index 提到 200。

12. **质量分级在契约确认时丢失**
    - 原因：BFF DTO 没字段；BACKEND_BINDING 用了旧闭包。
    - 解决：补 `qualityTier` 字段；`architectStatesRef` + sessionStorage 按项目续传。

13. **架构师重复问已确认的数据库**
    - 解决：Project Summary 注入 analyze/contract prompt；多轮对话只问未确认项。真实 E2E：2 轮、MySQL 在首版和修订版都保留。

14. **GeoAI 复用目录写不进 data 目录**
    - 原因：目录属主不是当前用户。
    - 解决：catalog 放 `knowledge/geoai_reuse`，完整 README 放 `docs/reuse/geoai`；knowledge 只读挂载进 AI Core。

---

## 6. 已验证的结果

- 全栈生成：GIS 应用 fullstack 7/7 PASS；
- G1 任务集：mock 3/3 PASS，P50 65.7s，P95 73.6s；
- G4 上下文：真实 DeepSeek 多轮，2 轮出契约，MySQL 在首版/修订版都保留；
- 画布编辑：V0 文案/增删/拖拽；live 删除、样式、移动、Alt 自由定位；
- 删除稳定性：连续删除 + 刷新后删除 PASS；
- 复用：GIS 查询 RAG Top-4 命中 GeoAI 目录；真实契约输出 REUSE/ADAPT/BUILD、URL、MIT 许可证、集成成本；
- 测试：前端 82、Python 795、BFF 23。

---

## 7. 面试话术

### 30 秒版

> 我做的不是低代码，而是把“需求 → 契约 → 界面 → 代码 → 修改 → 交付”串成一条可控流水线。
> 生成只是入口，真正难的是生成后还能安全地改：所以我做了源码身份、契约门禁、Docker 沙盒、AST 确定性编辑和 Git 回滚。
> 今天用测绘任务平台演示，是因为它刚好覆盖 GIS 业务里最常见的项目、任务、成果、质检闭环。

### 2 分钟版

> 我平时接一个 GIS 业务需求，不会先搭脚手架。我先把需求收敛成实施契约，再用 V0 画布和业务方对齐界面；冻结后才让 AI 在沙盒里写代码。
> 代码生成完不是结束，而是开始：每个节点有稳定身份，我可以圈选组件直接改 AST，也可以让前端 Agent 改；跨文件、循环模板、多实例这些容易改错的情况，平台会要求我确认或转给 AI。
> 前后端通过契约和 A2A 反馈对齐，生成失败会自愈，耗尽后回传架构师修订契约；每次成功都有 Git checkpoint。
> 这套东西的价值不是“生成得多快”，而是“从 1 到 N 能可控地改、能回滚、能交付”。

---

## 8. 边界（面试别说满）

- 地图是内联 SVG 示意底图，不是 GIS 引擎；真实项目接 Cesium / OpenLayers；
- 存量项目当前是只读投影 + 后端增量，前端 AST 直改是下一阶段；
- 平台登录是 Mock；生成应用暂未生成完整 JWT/RBAC；
- 沙盒没有 gVisor，未上云；生产要独立沙盒节点 / K8s + gVisor；
- 不要承诺“直接改老系统前端”“生产可用”。
