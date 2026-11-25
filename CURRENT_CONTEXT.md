# vibe-platform · 当前状态简报

> 2026-11-18 · 历史细节见 docs/memory/

## 架构
React+Vite → Java21 BFF → Python3.12 FastAPI/LangGraph → Docker 沙盒；中间件复用 ~/Workspace/infra；六边形分层。

## 已闭环
- 画布/Direct Ops：圈选、拖拽、HITL、Diff、回滚；NodeIdentity/ScopePolicy、错误信封。
- 生成链路：Architect 契约、多 Agent、语义路由、RAG、审计门禁。
- 离线可视化：多页面 VisualDocument + 页面 Tab + 高保真/模拟预览 + 冻结/审查。
- 阶段三 CodeGen：Tailwind JSX + node-id/source；Babel 门禁；自带 inspect SDK 与 /api 代理；写回源码、v0 快照、ChangeLog。
- 存量只读投影：Babel 分析 → 导入 .jsx/.tsx 只读树；不转虚拟树。
- 双轨制：前后端分流；新建强制选从零/导入；空工作区可直达导入；Resolver 以 files/sandbox 事实判定（仅声明存量不再生效）；Fullstack Run 带 projectMode，存量禁止 frontend/fullstack CodeGen，仅后端增量；Architect 首屏切换为存量文案。
- 源码持久化：导入先 PUT BFF，MySQL 存元数据/文件树，MinIO 存正文；重开/清缓存从后端回灌，ReadOnly 投影可直接补拉。
- 写回闭环：Direct Ops Git commit 成功后自动同步快照；sourceSync 异步 + 持久化队列，commit 仍同步完成。Git add/commit/rev-parse 合并为单次容器执行。失败不回滚代码，队列后台重试。
- 删除快路径：安全叶子点击即乐观删除 + 左下角撤回；共享/循环/定义节点仍先确认再 execute，HITL/失败自动恢复。
- 交互规范：`docs/design/15_交互语义与节点身份规范.md` 冻结身份优先级、L0-L3 分级与操作矩阵。
- 两阶段：V0 只出虚拟树 → 画布 delta → BACKEND_BINDING；最终契约带冻结树。
- 审查门禁：FROZEN repeat 未绑定仅 warning；CodeGen 用示例数据；二次 Architect 全量审查交互/repeat+delta。
- 方案文档 01–05；质量分级一次；禁物理外键、分层。
- 源码级可视化：前端 Agent 语义工具复用 Direct Ops（文本/移动/增删/撤回），不 fork Utopia。
- 构建期身份：inspect_plugin / CodeGen 注入 data-oid + vibe-source-map.json；SDK 与 resolver oid 优先，旧 file/line 仅兜底，地图过期 fail-closed。
- 跨文件调用点：importerGraph + callSiteOid 双索引；UPDATE_PROPS 只改调用点 JSX（App.jsx），不触碰组件定义（UserCard.jsx）。
- 高级编辑只走前端 Agent 工具：`update_props` / `update_class` / `update_style`；画布不再暴露 ⚙/🎨/🖌 按钮。
- 低级移动落位使用 manifest.childOids 的 AST 兄弟序号，不再按 DOM children 猜；DOM/AST 不一致时回退。SDK 回传 parentOid，服务端也可从 `[data-oid]` 选择器回补，修复“移动成功但没动 / 找不到落点”。
- 多调用点消歧：优先 React dev `_debugSource` 精确定位；无 Fiber 时按源码顺序 + 运行时实例序号，仍不确定保持 HITL。
- 多实例：SDK 上报 instancePath（父 oid 链 + 同模板序号）；模板/重复实例修改返回 HITL_REQUIRED + 范围选项。
- 数据源删除：静态数组（字面量/同文件变量）可确定性删除对应元素；动态数据仍明确 HANDOFF_TO_AI。
- 容器作用域：Source Map 标注 containerScope=local/component_definition/repeated_template/call_site_slot；local 与 call_site_slot 直接落位，跨文件仍拒绝。仅被 `src/main.*` / `src/index.*` 渲染的入口组件按 local（页面根）处理，存量项目拖拽不再误触 HITL。
- 低级操作不转 Agent：父容器删除/移动连同子树直接执行；共享/循环/多实例不再 handoff，由 SDK 悬停高亮全部子节点提示影响范围。
- SourceSync 边界：删除文件从快照移除；失败任务落盘 JSON 队列，后台指数退避重试。
- 本地 CI：`scripts/ci_local.sh` 串行跑 Python / 前端 + build / BFF；`RUN_E2E=1` 浏览器 E2E、`RUN_CANVAS_E2E=1` 画布套件、`RUN_CHAOS_E2E=1` 生产故障、`RUN_PLATFORM_FAULTS=1` 平台故障矩阵。
- 终极路线：`docs/design/16_平台终极目标与路线图.md` 固化六维目标；M1 闭环进行中。
- 拖拽容错：落点出容器/缝隙时按 48px 几何距离吸附最近容器，不再直接“未能定位父容器”。
- 旧工程兼容：SDK 支持 `file:line` / `file:line:column` 两段与三段锚点，缺失时向上找源码祖先；页面根兜底取真实容器，不再报“标注缺少目标文件”。
- 拖拽实时预览：拖动中按指针求候选父容器与 AST 视觉缝隙，画虚线框 + 插入线；松手复用同一计算，所见即所写。
- 澄清可靠性：Architect 输出上限 8192、JSON 修复重试 2 次，`finish_reason=length` 留告警；LLM 失败文案不再声称“已保留进度”。V0 契约在 validate_draft 校验 visual_document，缺失/非法走结构化打回而非内部错误。
- 两阶段守卫：BACKEND_BINDING 缺冻结树服务端 422 `ARCHITECT_VISUAL_REQUIRED`；前端缺冻结树时不消费审查包并阻断，旧骨架入口按 `outputPhase=BACKEND_BINDING` 门禁，不再误报缺 frontendRoutes/components。
- 代码自愈收敛：Applier 诊断带 source 归属，db.js 缺陷回 DBACoder 重建（不再空转 BackendCoder），index.js 缺陷走 LLM；修复以被拦截候选为基线并只读注入 db.js，Coder 输出上限 8192。
- 生成画布可编辑性：Visual CodeGen 全栈模式不再注入无源码锚点的 FullstackDataPanel / ApiErrorBanner；生成工程自带 inspect_plugin（新增组件文件也会获得 data-oid/source map）；DOM 兜底跨容器移动修正 siblingIndex off-by-one；resolver 支持仅 `data-node-id` 的节点用 manifest.nodeId 反查文件；drag-commit 延后到 Vite HMR 后再清 inline translate，避免“成功后又回弹”。
- 循环模板：CodeGen 的 repeat 样本改为静态数组常量，删除单项走 DELETE_DATA_ITEM 只移除一个元素；map 内删除/移动改为 HITL（只删数据项 / 删除整模板 / 交给 AI），不再“删一个全删”或移动后回退。
- Applier 别名比较：URL 段（job-descriptions）与 Sequelize alias（job_descriptions/jobdescriptions）按去分隔符归一后比较，修复真实 LLM 运行中“2 处 JOIN 别名缺陷反复修不掉”的门禁误报。
- 低级操作免弹窗：静态数组循环项删除自动转 DELETE_DATA_ITEM 只删一条；动态循环/多实例删除与所有循环项移动直接返回可执行错误，不再弹“安全路径”决策面板。
- 视觉所见即所得：CREATE_NEW 生成按 relativeTarget.widthPx/heightPx 近似匹配圈选尺寸（±25%），并沿用当前控件/页面圆角字号颜色；Tailwind skill 不再强制 rounded-full。
- 同父容器排序：拖到兄弟容器边缘按排序处理（共同父容器 + AST 缝隙），只有落在兄弟中央才嵌套；E2E 验证首位顺序按鼠标位置变化。
- 回退根因：grid 只按 y 判定（横向位置被忽略）→ 按行/列阅读顺序；插件 oid 依赖兄弟序号，移动后身份漂移 → 用 data-*/id/name/key 稳定 oid；resolver 的 callSiteOid/file/range 不再覆盖节点自身 oid，组件定义内部移动/删除不再误判跨文件。
- 循环项编辑：CodeGen repeat 包装层带 data-repeat-template/index；拖拽循环项自动转 MOVE_DATA_ITEM 重排静态数据数组，resolver 回传 instance 序号；E2E 验证列表项拖到首位 [丙,甲,乙] 源码与 HMR 一致。
- 拖拽落位（Onlook 式）：拖拽开始即把源节点 `position:fixed` 脱离文档流，原位置放同尺寸 stub 占位；落点几何排除源节点与 stub，只用指针与剩余兄弟的中点/网格缝隙计算序号，修复“抓上半部向下拖永远回原位 / 自动吸到最近组件”。drag-end 后恢复文档流并保留 inline translate，仍由 drag-commit 在 HMR 后清零。
- 新增组件尺寸契约：ADD_COMPONENT 接收圈选 `size.widthPx/heightPx`，写入 `w-[Npx] h-[Npx] min-h-[30px]`；无圈选时按类型兜底（Container 120 / Card 96 / 其他 30），`visual.css` 补 `.vibe-direct-*` 基础 min-height 与 padding，杜绝空 div 长条。
- SET_SIZE 尺寸模式：画布悬浮工具栏提供 Hug / Fill / Fixed；后端按父容器 display/flex-direction 映射 `w-fit`、`w-full`、`flex-1 min-w-0`、`self-stretch`、`min-h-full`、`h-full`，先移除冲突尺寸类；动态 className 返回 `ROUTE_TO_LLM`。
- Alt 自由定位：SDK 按住 Alt 拖拽跳过流式落点，按源节点视觉矩形与最近源码父容器算 `freeLeft/freeTop`；新增 `MOVE_FREE` + AST `position_free`，一次写节点 `position:absolute; left/top; width/height; transform:none; margin:0` 与父容器 `position:relative`；跨文件 / 循环项拒绝，仅该动作放行绝对定位，UPDATE_STYLE 仍禁止 `position:absolute`。
- A2A 双向反馈：新增统一 `a2a.feedback` 事件与运行级日志；Applier/ConsistencyVerifier 把诊断回传 BackendCoder/DBACoder，契约矛盾或代码自愈 2/2 耗尽时自动向 Architect 发 `REVISE_CONTRACT/REVIEW_CONTRACT`；Architect 修订开始与契约确认时回传 BackendCoder；失败事件带完整 `a2aFeedback`，前端错误卡片展示交互时间线并可一键触发契约修订。
- JOIN 别名校验收敛：只收集真正传给 `listRelatedRecords/createRelatedRecord/include.as` 的别名，`/:id/crawl` 等动作端点不再被误报；代码别名若来自契约嵌套路由但缺 relation，直接判 `CONTRACT_DEFECT`；诊断与候选产物同时与上一轮完全一致时短路重试并升级 Architect，避免 2/2 空转。
- Direct Ops 离线缓冲：删除 / 拖拽移动先入队（SDK 乐观隐藏 / 保留 translate），点【提交修改】按入队顺序逐条提交，每条重新取 revision；成功后由 HMR 生效，不再逐条重建 iframe；【撤销】恢复隐藏节点与位移。缓冲与暂存布局共用同一保存动作，仍不改变各 Direct Op 后端逻辑。
- 草稿/预览收敛：`begin_draft` 的 clean-tree 检查排除构建期生成物 `vibe-source-map.json`（`.gitignore` / `.git/info/exclude` 同步补充）；prepare 阶段失败时保留上一次有效 `sandboxId/previewUrl/commitHash`，不再把画布冲成空白。
- 样式预设取色：`style.*` 澄清里的“和现有主按钮一致 / 和登录按钮一样”等 quick option 不再 `skipReference`，进入既有 reference 取色流程，Coder 获得真实 `tailwindClasses/computed style`；普通业务 quick option 仍一键提交。
- 架构师默认样式：契约新增 `componentStyles`，确认时从 VisualDocument 用 CodeGen 的 `_tailwind_classes` 抽取 `primary_button/secondary_button/login_button/input...`；前端样式 quick option 优先直接生成结构化 reference（无需再点选），无默认时回退取色。修复 clarification answer 的 `reference` 在 projectActions 映射中被丢弃的问题。
- V0 大契约：V0 不再让模型输出完整 VisualDocument，改为只出 frontend_routes/page_layout/components，虚拟树由 `build_visual_document_from_contract` 确定性生成；输出预算（markdown≤1200 字、components≤12、routes≤5），修复多实体产品在 8192 token 处截断。实测 AI 求职助理 MVP（3 实体、5 页面、17 路由）完成 V0→契约→前端 CodeGen→全栈生成。
- 生产级容灾加固：BFF 三档超时（健康 3s / 交互 15s / Run 300s）+ 三条独立舱壁/熔断通道（fast-fail `AI_CORE_BUSY` / `AI_CORE_CIRCUIT_OPEN`），traceId 全链路透传；Supervisor/Architect 状态操作改走 15s 交互客户端。Python 侧全局 Run 并发舱壁（`MAX_CONCURRENT_RUNS`）、LLM 连接错误纳入瞬态重试、visual-agent 模型不可用改 503 `VISUAL_LLM_UNAVAILABLE`、停止沙盒容器 TTL GC、MinIO→MySQL 失败补偿删除。
- 运行态持久化 Phase 2：`RunEventBuffer` 事件异步落 Redis（TTL 24h，默认内存回退），Run 快照与已确认契约落 Redis（7d）；进程重启后 SSE 可从持久化事件回放，缺终态时补发 `RUN_INTERRUPTED`，`GET /runs/{id}` 回退快照并把进行中状态收敛为 FAILED；`find_architect_contract` 支持 Redis 回源。BFF `run→sandbox` 映射写 Redis（7d）。项目写锁支持 Redis SET NX + TTL 续期（崩溃后最多 300s 释放），Redis 不可用自动回退本地锁。

## 最新验证
生产级容灾加固后：Python 836 项、BFF 32 项、前端 82 项全通过。运行态持久化实测：Python health `run_store=redis`；跨进程写入/读取事件、快照与已确认契约成功；真实 Architect Run 写入 72 条事件与 AWAITING_CLARIFICATION 快照；模拟重启的第二实例 GET 快照返回 `FAILED / RUN_INTERRUPTED`，SSE 回放包含 `architect.failed + RUN_INTERRUPTED`；Redis 项目锁跨进程互斥验证通过（holder=redis，contender=ProjectBusyError）；Chroma 模型缓存已挂载命名卷，避免重建重复下载。故障注入（暂停 python-ai，220 并发请求 Supervisor）：BFF 健康检查 3.0s 返回 503 DEGRADED、纯 MySQL 接口 0.29s 返回，64 个请求占用舱壁后恢复为 200，其余 156 个 0ms 快速失败 503 `AI_CORE_BUSY`；不再出现 200 线程耗尽。短通道连续 4 次失败后熔断打开（第 5 次 7ms 返回），冷却后半开探测成功自动 CLOSED。LLM DNS 黑洞下 Architect 连接错误按瞬态重试 3 次后以 `ARCHITECT_LLM_ERROR retryable=true` 收敛；`MAX_CONCURRENT_RUNS=8` 已在 Python health 暴露为 `run_capacity=0/8`。
前端 72 项 Vitest；Python 795 项全通过；BFF 23 项 Maven 测试；浏览器 E2E PASS：OID 4/4、删除预览消失 257ms、刷新回灌 3.9s、跨文件 Props 命中 App.jsx 调用点且 UserCard.jsx 未改、Direct MOVE 由真实 SDK 拖段落算出 siblingIndex=2，源码与预览同步重排；拖动中候选父容器框与插入线几何断言通过，松手后提示清零。两阶段全链路 E2E PASS：V0 → 冻结 → BACKEND_BINDING 澄清 → 确认（无旧骨架入口）→ Visual CodeGen → live 画布 → 删除组件 + 跨父容器拖拽均 SUCCEEDED（HMR 后父容器已切换）→ 模拟前端工程师新增组件并被 Direct Ops 编辑生效 → 刷新回灌；call-site E2E 增加定义映射完成等待。Direct MOVE E2E 新增上半部抓取回归：`MOVE_E2E_GRAB_RATIO=0.25` 向下拖到末尾仍得到 siblingIndex=2，拖拽中 stub 存在且 pointer-events:none。Direct MOVE E2E 追加尺寸闭环：尺寸面板发出 Fill/Fixed 消息，SET_SIZE 写入 `w-full h-[80px]`，ADD_COMPONENT 带圈选尺寸写入 `w-[200px] h-[50px] min-h-[30px]`；Alt 拖拽发出 `free:true + mode:free`，MOVE_FREE 写入节点 absolute 与父容器 relative。A2A：Applier→DBACoder / BackendCoder、Consistency→Coder、Coder→Architect（契约修订/复核）、Architect→BackendCoder（修订开始/契约确认）均有单测覆盖，失败事件携带 `a2aFeedback`。别名校验：动作端点不产生 JOIN 别名；契约嵌套路由缺 relation 判 CONTRACT_DEFECT；诊断+候选双指纹相同才短路重试。Direct MOVE E2E 追加离线缓冲：删除入队 → 提交修改 → 源码已删且 iframe marker 未丢（noReload=true），证明不再全量重建。草稿检查：真实沙盒中修改 `vibe-source-map.json` 后 raw status 为 M、带 exclude 的 status 为空，确认不再阻塞 begin_draft。

## 真假边界
DeepSeek 真实调用；鉴权 Mock；BFF 仅源码快照持久化（Run/指标仍无持久化）；沙盒无 gVisor；未云端部署。

## 下一步
P0：多调用点运行时消歧、rename/add 全量同步、身份模型正式文档；P1：class/style、wrap、预览/构建验证；存量 Phase 2；Redis/云端。

## 面试演示（2026-11-23 · 苍穹数码）
- 叙事：以“日常开发工作流”演示新建 GIS 业务应用；主脚本 `docs/DEMO_GIS_5MIN.md`。
- 预生成：`docs/demo/gis-visual-document.json`（260 节点，4 页：总览/任务看板/成果质检/影像上传）；V0 状态 `docs/demo/demo-v0-storage.json`，live 状态 `docs/demo/demo-storage.json`。
- 离线资产：`docs/demo/vibe-architecture.html`、`docs/demo/vibe-request-flow.html`（Archify，Apache-2.0，validate/deliver/check/browser-check 全通过）。
- 打开：`node docs/demo/open-demo.mjs`；用 `DEMO_STATE`/`DEMO_PATH` 选择 V0 或 live。
- 边界：地图为内联 SVG 示意底图；存量模式仍只读；鉴权 Mock；未上云/无 gVisor。
- Architect 文档中心：本地 localStorage（按项目隔离，最多 10 份 / 单份 200KB / 最多 3 份启用），入口在面板右上角与「追加修改需求」区；上传时规则优先 + DeepSeek 兜底判断长期复用 / 一次性 / 无明确语义，长期入库、一次性仅本次、无明确语义询问；启用的文档随 start / REVISE 发送 `knowledgeDocuments`，Python 按 Markdown 分节 + 中文 bigram 合并进 `retrieved_chunks`。示例文档 `docs/demo/gis-requirements-sample.md`。
- V0 页面根：画布不再渲染根节点的外壳 / 删除按钮，页面根只作为隐式容器与 drop target；`deleteVisualNodeFromDocument` 拒绝删除根节点，默认选中首个子节点，点页面空白处取消选中。
- GIS 真实 E2E：V0 虚拟树编辑 PASS（文案修改、39→40→39 增删、localStorage 校验）；live 缓冲删除 `DELETE_NODE SUCCEEDED`（noReload）；`UPDATE_STYLE` 200 SUCCEEDED；全链路生成 fullstack 7/7 PASS。旧 `architect_visual_chain_e2e.mjs` 的 Direct Ops 仍按即时响应编写，需改成离线缓冲流程。
- 删除组件稳定性：Direct Ops 待提交删除按 `oid+instance` 记账（不再按 selector，避免同 class 兄弟互清记录）；分析响应绑定发起时的 target；提交遇 HITL 先恢复乐观隐藏节点。E2E：同一缓冲连续删除 2 个同 selector 兄弟 + 刷新后再删 1 个，均 SUCCEEDED、noReload、pageErrors 0；前端 82 项测试通过。注意 `sandbox_template/inspect_sdk.js` 改动要 `docker compose up -d --build python-ai` 才持久（本次用 `docker cp` 热更新验证）。
- A2A 协议 Phase 1：`docs/design/17_A2A协议与问题叙事.md`；允许矩阵 + 4 组前后端共用 fixtures + 只读校验器 `ai_core/a2a_contract.py` + 前端纯函数 `a2aNarrative.js`；5 Python / 6 前端测试；默认不接运行时，不影响 V0/live。
- G1 任务集：`scripts/g1_task_suite_e2e.mjs`；mock 3/3 PASS（P50 65.7s / P95 73.6s），已扩到 5 个任务（ToDo/博客/CRM/库存/报销）。
- G4 上下文：`tests/test_g4_context.py` 3 项 PASS（project summary 进入 analyze/contract prompt，不重复问已确认数据库）；`scripts/g4_context_e2e.mjs` 真实多轮 E2E。
- Architect 需求优先：prompt 增加“需求与目的优先、技术选型给优缺点、分层契约 Markdown、请求拆解”；未新增 schema 字段，Supervisor 路由不变。
- 项目规模 / 质量分级（不可跳过）移到用户输入需求之后：Architect 起始必选 L1/L2/L3；`architect.start` 带 `qualityTier`，`architect.started` 返回 `qualityTierRequired`；BACKEND_BINDING 用 sessionStorage 按项目续传；契约确认不再重复询问。
- 项目 Tab 关闭：IndexedDB 草稿 hydration 加 1.5s 兜底；草稿守卫 z-index 200，避免被 Architect 面板遮住；带未保存草稿关闭会弹「放弃并关闭」，E2E PASS。
- 前端 Agent 新增组件：UI Agent `CREATE_NEW` run SUCCEEDED；沙箱 `src/App.jsx` 真实写入 `button.vibe-added-button`（红色「提交」）。注意演示项目没有 sandbox 时「保存布局」会提示“当前页面尚未生成代码”，新增组件必须在 live 项目测试；新增 E2E 的状态 overlay 选择器仍待更新。
- GeoAI 复用目录：`scripts/fetch_geoai_reuse_catalog.py` 拉取 8 个 GitHub 项目（opengeos/geoai、geoai.js、raster-vision、eo-learn、golem、qgis-samgeo、torchgeo、geospatial-ml 清单），生成 `knowledge/geoai_reuse/catalog.md`；`knowledge/` 只读挂载进 python-ai；GIS 查询 RAG Top-2/4 命中该目录；prompt 增加 REUSE/ADAPT/BUILD 与契约复用建议。真实 DeepSeek 单次契约验证：契约第 6 节「复用建议」输出 BUILD/ADAPT/REUSE，含 geoai.js URL、MIT 许可证与集成成本。4 项 catalog/prompt 测试通过；完整 README 存 `docs/reuse/geoai/readmes/`。
- G2 鉴权分两条线：平台登录仍 Mock（`docs/design/18_G2_JWT与审计.md`）；生成应用目前只生成 `server/security.js` 的 bcrypt/sanitize，没有 users 表、`/auth/login`、JWT 校验或 RBAC（`docs/design/19_生成应用JWT与RBAC.md`）。不要把两者混为一谈。

## 2026-11-24 · 方案侦察 + GIS 基线 + 领域定制层
- 澄清前新增方案侦察：`/architect/solutions/discover`（GitHub + 本地复用目录 + 自研；用户选择后以 `solutionChoice` 进入现有链路），前端 ArchitectPanel 增加方案卡片；Python 11 / 前端 6 测试通过。
- 领域定制层：`config/domain_profiles/*.yaml`（行业词/检索词/本地目录/排序/mock）、`config/codegen/stack_dependencies.yaml`（techStack→依赖）、`config/architect/brief_signals.yaml`（抽象需求信号）；核心 `ai_core` 不再含 GIS 词，`tests/test_no_domain_hardcoding.py` 守卫。
- GIS 基线：Iter0 69.8 C → Iter1 90.3 A → Iter2 98.0 A → **Iter3/6 100.0 A**（`samples/gis-baseline-iter6/`）。关键修复：契约 interactions→binding、techStack/正文→依赖、package.json 合并、可执行栈注入、config-driven 地图扩展、页面级 DataList、核心 CRUD 对齐 `/api/v1/facilities`、README/docker-compose。
- 评测集：`config/gis_eval/launch_checklist.yaml` + `scripts/gis_launch_eval.py`；冒烟 `scripts/gis_smoke_e2e.py`；分析 `reports/gis/baseline-analysis.md`。
- 平台自评：`config/platform_eval/checklist.yaml` + `scripts/platform_eval.py`；基线 **90.0 A**（架构/契约/溯源/门禁/确定性/交付/安全/可观测全 100；生产就绪 0：多实体 CRUD、JWT/RBAC、地图打点、云端部署未完成）。
- 画布地基：`config/canvas_eval/checklist.yaml` + `scripts/canvas_foundation_e2e.mjs`；真实 DeepSeek 生成新 GIS（不导出），live 画布 **6/6 PASS**（identity/select/delete-undo/delete-commit/move-commit/identity-after；0 page errors；nodeIdDrift=0）；mock 6/6。一次 DeepSeek 契约非法（logs 子表缺失）被门禁拦截，记入 `reports/canvas/baseline-analysis.md`。
- 画布套件：`scripts/canvas_eval_suite.sh`（foundation + cross-file-call-site + repeat-grid-size-free）mock 全 PASS；add-component SKIP（待 Python Playwright）；CI `RUN_CANVAS_E2E=1 ./scripts/ci_local.sh`，真实 GIS 用 `CANVAS_PROVIDER=auto`。
- 生产基线/故障注入：Iter6 杀 Express → 100% 错误、手动恢复 4.07s；修复 dev.mjs API 1s 自动重启、compose `restart: unless-stopped`、DataList 1s/2s 退避重试 → Iter7 自动恢复 **1.28s**、恢复后 0 错误；`reports/production/baseline-analysis.md`；78 项定向测试通过。边界：仍只有 facilities 真实 CRUD，未做 JWT/地图打点/云端。
- 故障矩阵：浏览器 DataList 后端崩溃后 **1.38s** 自动恢复 20 行、无错误页；DB 独占锁写错误 16.7%→0（`busy_timeout` 5s→15s，写 P95 5.7s，模板回归已加断言）；Redis 不可用 health 200/`run_store=memory`；LLM 不可达 `architect.failed + ARCHITECT_LLM_ERROR`。脚本：`gis_resilience_browser_e2e.mjs`、`gis_db_lock_chaos.py`、`platform_fault_matrix.py`；已接入 CI：`RUN_CHAOS_E2E=1`（含浏览器与 DB 锁）、`RUN_PLATFORM_FAULTS=1`（Redis/LLM，一次性容器）。
- 冒烟 E2E：Iter6 preview PASS（真实地图、DataList、`POST 201` 后 `GET` 命中）。诚实边界：只有 facilities 核心表是真实 CRUD，工单/影像仍是 Mock；JWT/RBAC 未做完整中间件；地图未按设施数据打点；未真正上云。下一步 Iter7：多实体 CRUD + 鉴权 + 地图打点 + 云端部署。
- 证据链：`ai_core/evidence_chain.py` + `scripts/evidence_chain.py`，只读比对 discovery / V0 / 最终契约 / 冻结画布 / source map，输出 ok/mock/weak/missing；`--strict` 可做 CI 门禁。Iter6 基线 **GAPS**（critical 9 / warning 11）：V0 的 `/login`、`/facilities`、`/tasks` 与 3 个工单组件在最终契约丢失；13 API 中 8 个 Mock；方案选择未写入契约。已修 `assemble_contract`：BACKEND_BINDING 用冻结树确定性补齐 routes/components（Iter6 数据恢复 4 路由 / 10 组件），回归测试覆盖。报告 `reports/traceability/baseline-analysis.md`。
- 证据链复验（Iter9 codegen replay）：真实 BACKEND_BINDING 重放 **4 路由 / 10 组件、漂移 0**；契约重放 + 代码生成证据链 **PASS**（critical 0，API 真实/ Mock 10/2，源码映射 60/60）。本轮修复：`contract_audit` 接受 relation alias；多实体真实 CRUD（独立顶层表 + 主从嵌套 + 真实 `/api/v1/health`）；核心表按 CRUD 密度评分，避免 `GET /users` 抢走 facilities；方案选择写入契约正文。Node 冒烟：facilities 200、inspection GET 200/ POST 201、images 200。
- JWT/RBAC 补完：契约含 login/register/me 时生成 `server/auth.js`（Bcrypt + JWT + Bearer 门禁 + `requireWriteRole`）、前端 `src/auth.jsx` 登录门禁与全局 Authorization 注入；登录/注册/健康检查公开，viewer 只读、admin/inspector 可写；首次登录懒创建 admin/admin123（`ADMIN_PASSWORD`/`JWT_SECRET` 可覆盖）。Iter6 契约重新生成 Mock 数 **8→0**；冒烟：无 token 401、admin 登录 200、token 访问 200、viewer GET 200/ POST 403、admin POST 201、register 无 token 401/ admin token 201。剩余：多实体前端 DataList、上传 POST 前端调用、完整沙盒 E2E。报告 `reports/traceability/iter9-codegen-check-analysis.md`。
- Iter9 评估（`samples/gis-baseline-iter9`，codegen replay）：平台自评 **97.0 A**（生产就绪 70%：多实体 + JWT/RBAC 已补，地图打点/云端仍缺）；GIS 可上线评测 **100 A**；证据链 **PASS**（critical 0 / warning 1：影像上传 POST 前端未接）；API 真实 13 / Mock 0；前端 `vite build` 通过。证据链已修正同路径 GET/POST 混淆（上传类 change 优先核对 POST）。报告 `reports/iter9-evaluation.md`；Python **882** + 前端 87 + BFF，`ci_local.sh` PASS。
- 六维终极目标评估（`docs/design/16...`）：G1 70 / G2 57 / G3 58 / G4 67 / G5 70 / G6 85，综合 **68**（2026-11-25 更新 G1）。强项 G5/G6（可观测/人机协同），相对弱项 G2/G3（平台级鉴权/租户/审计、可协商规划）。报告 `reports/six-goals-evaluation.md`。
- G1 真实评测（已完成，2026-11-25）：真实 DeepSeek 5 任务单轮 **5/5 PASS**、自动验收 5/5，P50/P95 **161.8s / 224.7s**，Architect 成本合计 **0.676568 CNY**（均 0.135314）。库存别名门禁修复（`check_orm_schema` 接受 childTable）在全量运行中复验通过。数据：`reports/g1/runs/2026-11-25T00-57-16-592-auto/`（含 `manifest.sha256`）；迭代记录 `reports/g1/real-evaluation.md`。诚实边界：人工接受率 PENDING、回滚率未触发、仅单轮、未接 CI 回归。`ci_local.sh` PASS（Python 884 / 前端 87 / BFF 32）。
- Agent 协作原型（2026-11-25）：`ai_core/collaboration/` 新增 AgentReport、ArchitectInbox、澄清桥、风险门禁、动作台账、A2A 改道、**快速通道**（Architect 预授权标准边，零异常直连且每步仍报摘要；错误/重试/澄清/风险拦截立即收回）。**已接入 fullstack 运行时**：`_publish_a2a` 同步写入 `ARCHITECT_HUB` 收件箱并发 `agent.report`；`_write_bundle` 把 Applier 写操作写入动作台账并发 `agent.action`。**查询接口**：Python `GET /api/v1/internal/collaboration/runs/{runId}`、BFF `GET /api/v1/architect/collaboration/runs/{runId}`，返回 inbox / actions / fastPathGrants。**持久化**：hub 每次写入后用现有 `RunStore.save_snapshot("collaboration", runId)` 落 Redis/内存，查询时按需回灌（完整载荷不持久化）。**快速通道自动化**：Fullstack Run 启动时由 `ARCHITECT_HUB` 预授权（`COLLABORATION_FASTPATH_ENABLED/TTL/MAX_STEPS`），**白名单来源优先级**：契约 `agentPolicy.fastPathEdges`（Architect 确认时从 YAML 冻结）> `FullstackRunOptions.fastPathEdges` > `config/collaboration/fastpath_edges.yaml` > 内置默认；**YAML 支持 mtime 热加载**（`FastPathPolicy.reload()`，授权时自动检查，失败保留旧白名单）。`_publish_a2a` 带 grant 与 retry 状态，Run 结束收回；`COLLABORATION_FAIL_CLOSED=1` 可让报告失败阻断。容器实测 `contract_edges=6`。**前端**：ArchitectPanel 新增“协作时间线”（`agent.report/action/fastpath` 事件归一，最近 8 条），支持“从服务端加载完整记录”并语义去重合并；刷新/切换 Run 自动拉取一次，支持全部/汇报/操作/快速通道筛选；保留 A2A 区块。ContractViewer 概览新增“Agent 协作策略”，只读展示 `agentPolicy`（澄清/风险/用户语言 + 快速通道白名单）；契约确认区可勾选白名单子集，`CONFIRM` 请求携带 `agentPolicy`，服务端在确认时写回契约。证据链新增 `agent policy` 项（未冻结为 weak）。前端 **94 项 + build**；`ci_local.sh` 全量 **PASS**（Python 全量 / 前端 94 / BFF 32）。`test_collaboration.py` 25 项 + API 2 项、fullstack 71 项、前端 **93 项 + build** 通过（此前组合回归 115 项）。
- 全流程原型（2026-11-25）：`ai_core/prototype/` + `scripts/full_flow_prototype.py` 串起需求→契约→拓扑回写→多产物生成→快速通道→迁移→真实消费/幂等→Node 语法→交付打包（Helm/SBOM/manifest）→飞轮；实测 `status=ok`、28 个产物、工单分类 `IT/财务`、重复事件只写回一次、Helm/SBOM 校验通过。`test_full_flow_prototype.py` 4 项，相关回归 **30 项通过**。边界：未接真实 LLM、未启动 compose 容器、前端仅静态壳、飞轮为候选判定。
- 多服务 compose 沙盒（2026-11-25，已完成）：`scripts/compose_sandbox_e2e.py` 真实 `docker compose build/up` 启动 migration + **Redpanda(Kafka)** + api + consumer + agent + web + otel，API `POST /api/tickets` → Kafka `ticket.created` → consumer 消费 → Agent 分类 → SQLite 写回；Kafka 重复消息验证幂等（before/after 均 1/1）；Helm/SBOM/manifest 打包校验。文件事件卷保留为本地回退（`KAFKA_BROKERS` 为空时）。实测 `ok=True`，阶段 `compose_up / health / generated_tests / consume / idempotency / otel_spans / delivery`，无残留容器。报告 `reports/compose-e2e/<ts>/report.json`。
- 测试生成（2026-11-25）：生成 `tests/test_app.py` unittest，覆盖分类、写回与幂等；Dockerfile 复制 tests，compose E2E 在沙盒内真实执行 `unittest discover` 并作为独立阶段。进度图 `test_gen` 更新为“部分实现”。
- 镜像发布链路（2026-11-25）：`scripts/registry_delivery_e2e.py` 构建 api/consumer/agent/web 四个镜像，推送本地 `registry:2`，校验 tags 与 digest，并回写 `dist/manifest.json` 的 `registry` 字段。实测 `ok=True`，四个镜像 digest 均已记录；报告 `reports/registry-e2e/<ts>/report.json`。边界：本地 registry，未接企业镜像仓库/签名/扫描策略。
- K8s / OTel 交付清单（2026-11-25）：生成物新增 Helm `Service / Job / ConfigMap / PVC / OTel Collector`，迁移以 pre-install/pre-upgrade Job 执行；`scripts/k8s_manifest_check.py` 结构校验通过（Deployment=2 / ConfigMap=2 / Job=1 / Service=1 / PVC=1）。**OTel SDK 已注入**：Python consumer/agent 上报 `consume_event/classify`，Node API 上报 `POST /api/tickets`；compose 增加 Collector，E2E 阶段 `otel_spans` 实测命中 `ticket-consumer`、`consume_event`、`ticket-api`。边界：未装 helm/kubectl、未做真实集群部署；调试容器 `debugotel/debug2/debug3/debug4` 因守卫规则未自动清理。
- 平台鉴权底座（2026-11-25）：BFF 新增可开关 OIDC Resource Server（`VIBE_OIDC_ENABLED` + issuer/JWKS，启用缺配置启动失败）、`TenantContext/TenantFilter`（**JWT claim `tenant_id` 优先**，与 `X-Tenant-Id` 不一致返回 403；校验/回写/清理）、`AuditFilter + vibe_audit_event` MySQL 只追加审计（POST/PUT/PATCH/DELETE，不记请求体，失败仅告警）、**审计查询 API** `GET /api/v1/audit/events`（管理员、按租户、limit≤200）。BFF **37 项测试通过**；实机 POST 422 后 MySQL 记录 `tenant_audit / user_audit / POST /api/v1/architect/documents/classify / 422`。边界：无 RLS、未接真实 IdP、审计无导出/保留策略。设计见 `docs/design/22_平台鉴权租户与审计.md`。
