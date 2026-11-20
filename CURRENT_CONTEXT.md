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
- 本地 CI：`scripts/ci_local.sh` 串行跑 Python 739 / 前端 68 + build / BFF 23；`RUN_E2E=1` 可追加浏览器 E2E。
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

## 最新验证
前端 72 项 Vitest；Python 795 项全通过；BFF 23 项 Maven 测试；浏览器 E2E PASS：OID 4/4、删除预览消失 257ms、刷新回灌 3.9s、跨文件 Props 命中 App.jsx 调用点且 UserCard.jsx 未改、Direct MOVE 由真实 SDK 拖段落算出 siblingIndex=2，源码与预览同步重排；拖动中候选父容器框与插入线几何断言通过，松手后提示清零。两阶段全链路 E2E PASS：V0 → 冻结 → BACKEND_BINDING 澄清 → 确认（无旧骨架入口）→ Visual CodeGen → live 画布 → 删除组件 + 跨父容器拖拽均 SUCCEEDED（HMR 后父容器已切换）→ 模拟前端工程师新增组件并被 Direct Ops 编辑生效 → 刷新回灌；call-site E2E 增加定义映射完成等待。Direct MOVE E2E 新增上半部抓取回归：`MOVE_E2E_GRAB_RATIO=0.25` 向下拖到末尾仍得到 siblingIndex=2，拖拽中 stub 存在且 pointer-events:none。Direct MOVE E2E 追加尺寸闭环：尺寸面板发出 Fill/Fixed 消息，SET_SIZE 写入 `w-full h-[80px]`，ADD_COMPONENT 带圈选尺寸写入 `w-[200px] h-[50px] min-h-[30px]`；Alt 拖拽发出 `free:true + mode:free`，MOVE_FREE 写入节点 absolute 与父容器 relative。A2A：Applier→DBACoder / BackendCoder、Consistency→Coder、Coder→Architect（契约修订/复核）、Architect→BackendCoder（修订开始/契约确认）均有单测覆盖，失败事件携带 `a2aFeedback`。别名校验：动作端点不产生 JOIN 别名；契约嵌套路由缺 relation 判 CONTRACT_DEFECT；诊断+候选双指纹相同才短路重试。Direct MOVE E2E 追加离线缓冲：删除入队 → 提交修改 → 源码已删且 iframe marker 未丢（noReload=true），证明不再全量重建。草稿检查：真实沙盒中修改 `vibe-source-map.json` 后 raw status 为 M、带 exclude 的 status 为空，确认不再阻塞 begin_draft。

## 真假边界
DeepSeek 真实调用；鉴权 Mock；BFF 仅源码快照持久化（Run/指标仍无持久化）；沙盒无 gVisor；未云端部署。

## 下一步
P0：多调用点运行时消歧、rename/add 全量同步、身份模型正式文档；P1：class/style、wrap、预览/构建验证；存量 Phase 2；Redis/云端。
