# vibe-platform · 当前状态简报

> 2026-11-16 · 历史细节见 docs/memory/

## 架构
React+Vite → Java21 BFF → Python3.12 FastAPI/LangGraph → Docker 沙盒；中间件复用 ~/Workspace/infra；AI Core 六边形分层。

## 已闭环
- 画布圈选/ADD/Inspect/批量拖拽、双屏同步、HITL 澄清、Diff 审批、取消、Git 快照回滚。
- 全栈生成：Architect 契约、多 Agent、语义路由自愈、SQLite/MySQL 验收、导入导出。
- 混合 RAG、分级决策、Reflection、Token 成本、骨架防脑补、Agent/知识库管理。
- AST/ORM/DDL 门禁、Applier 隔离、契约审计、缺陷归因、风险门禁 L1/L2/L3。
- Phase1：确定性 Resolver + apply_ast_edit 硬门禁；Phase2：AWAITING_DECISION + decision.required + 3 轮上限。
- 暂存组件自由拖拽：视口 clamp、SDK stage-hit 父容器高亮、Drop 写 placement；同父容器连续插入按拖入顺序确定性定序，inside_index 由服务端计算（不采信模型序号）；拖拽中 Esc / 路由切换均清空幽灵。
- 孤儿 Draft 回收：启动即扫 + 6h 周期，7 天 TTL；begin_draft 前 60s 宽限回收同沙盒残留 worktree，避免重启后 5174 端口被占。
- 契约资源/动作分离：Prompt 禁止 /auth、/session、/account 作为集合根；审计自动映射 users / 跳过动作端点。修复死锁改为 conflict_gate 中断 → AWAITING_DECISION，前端三选一（映射已有表 / 新建表 / 取消），决策后确定性改写契约并复验。
- Phase1 错误信封：Python ErrorRegistry + 全局异常处理器统一 code/userMessage/errorOptions/traceId；SSE 建连后异常转 terminal 事件；BFF 全局 Advice 透传 + SSE onErrorResume 兜底，禁止堆栈泄漏。
- Direct Ops 地基：DirectActionPayload / Risk / ErrorCode / ChangeLogEntry；项目写锁覆盖 AI promote+commit、Fullstack、Rollback 与 Direct 写入；baseRevision 乐观校验；direct.* 事件命名空间。
- UPDATE_TEXT 闭环：服务端 Resolver → 公共组件拦截 → Babel update_text → apply_patch → Git commit → ChangeLog；前端 Inspect 悬浮 ➕❌✏️，Phase1 仅 ✏️ 落盘。
- ChangeLog → Architect：成功 Direct Op 写入内存 ChangeLog；Architect 初始状态读取最近 10 条并注入 System Prompt，真实 DeepSeek Helper 可正确提及用户刚改的文案。
- MOVE_NODE：同文件容器内移动，placement.siblingIndex 为移除后的最终序号；节点数量守恒校验；公共组件/跨文件转 HITL/AI；前端 ↕ 移到容器末尾。
- ADD_COMPONENT：Button/Input/Text/Card 白名单模板，AST insert + 节点增量校验 + import 补全；前端 ➕ 下拉直接插入。
- ChangeLog 持久化：JSONL 落盘 `data/changelog.jsonl`，重启后 Architect 仍可读取最近记录。
- DELETE_NODE 闭环：静态分析 safe=false 前端拦截；safe=true 二次 Revision 校验 + AST 删除 + 子树节点数守恒校验 + commit + ChangeLog。Direct Ops 四项动作 ✏️/↕/➕/❌ 全部闭环。
- 契约审计命名归一化：URL kebab-case 与 DDL snake_case 统一后比对；嵌套路由系统父表补推导 JOIN；repair feedback 明确指出命名规范。
- 内联 Direct Ops：沙盒 SDK 内联 ➕ / 悬浮 ❌ / 双击文本编辑，平台全局消息直连后端；InspectDraftCard 直操工具栏已移除。新增组件继承同级 className / inline style。
- 骨架布局：ui.layout 成为必需槽位并提供三选一澄清；Prompt 强制 Grid/Flex 响应式多列，禁止全量 flex-col。
- FailureRouter：白名单新增组件固定走 Direct 模板，目录外组件返回 DIRECT_REQUIRES_LLM 交给 AI。HITL 新增 RETRY（最多 2 次，不调 LLM）与 VISUAL_ONLY（不写代码、不 commit，写 applied=false ChangeLog）。
- 拖拽直连：SDK 松手计算父容器 + 最终子序号，handleDirectAction 完整透传 placement；支持 root-level 重排（祖先容器/顶层根节点）。成功 commit + ChangeLog，失败 rollback，不再经过前端 Agent / LLM。
- 沙盒镜像已重建，新增/删除节点数校验、根级拖拽与错误码 Toast 已通过新沙盒 E2E。
- 画布性能阶段一：Inspect/Direct hover 使用 rAF 合并 postMessage；高亮框改为 ref 直接写 DOM，React 仅在目标身份变化时更新；拖拽失败展示真实 code/message 并打印 placement/source。
- 拖拽/删除修复：共享组件 MOVE 使用 callSite 锚点；direct 消息携带 projectId，避免多 Tab 误用 activeTabId 导致 TARGET_NOT_FOUND；新建组件删除跳过确认框。UNSAFE 错误输出 source/parent/callSite；Resolver 失败自动重试一次并返回底层错误诊断。
- AST stage 隔离：每次 resolve/apply 使用唯一临时目录，消除并发 analyze-delete 的 spec.json ENOENT；SDK 删除增加 pending 防重与失效目标重查；TOOL_FAILED 返回异常类型/底层 error。20 路并发 analyze 全 200。
- 共享组件 callSite 删除：删除 `<Card />` 实例时改写 App.jsx 调用点，不动 Card.jsx；analyze 不再因定义文件顶层根节点误拦截。真实 E2E 已删除一个实例。
- 拖拽索引对齐：SDK 改用全部 DOM children 计算 siblingIndex，避免自定义组件/无 data-source 节点导致 Babel insertIndex 越界。错误信封保留 toolCode/resolverError 等 diagnostic，前端 Toast 可显示。

- v0 基线：骨架写入后静默保存 `v0: Architect Skeleton`；快照成功才把 bootstrap 切 ready/进入画布。架构师节点无特殊保护标记，仅按 shared/map/根节点风险分类；Resolver 用 importers 兜底 callSite。
- 澄清解耦：Architect 源码采集限时 5s，超时回退请求 files；启动 Architect 时前端注入快照 baseline Diff + latestChanges，后端注入 ChangeLog。

## 最新验证
前端 16 项 Vitest、Python 606 项 unittest 通过；BFF 单测通过。真实沙盒 Direct Ops（含样式继承 ADD 1559ms）正常；新 SDK 已打入 sandbox 镜像。终极 E2E：UPDATE_TEXT 1604ms、ADD 1523ms、DELETE 1603ms；ChangeLog 重启可读，DeepSeek Helper 可复述直接操作。

## 真假边界
DeepSeek 真实调用；鉴权 Mock；BFF 无持久化；沙盒无 gVisor；未云端部署。

## 下一步
Redis checkpoint；云端部署。
