# 面试战报 · 5 个踩坑与自愈案例

> 这份文档不是功能清单，而是**答辩素材**。
> 每个案例都按「现象 → 根因 → 处置 → 验证 → 追问」组织，重点讲清楚：
> **为什么这个问题难，为什么这个方案对，怎么证明它真的对了。**

---

## 0. 90 秒电梯稿

> 我做的是一个 AI 全栈研发引擎：用户在画布上圈选页面元素，用自然语言提出修改，系统经过契约澄清、多智能体编排、Hybrid RAG 检索、Docker 沙盒构建与一致性校验，最终交付可运行、可回滚、可部署的全栈项目。
>
> 它最难的不是“让模型生成代码”，而是**让不确定的模型输出进入确定性的工程闭环**：抽象目标由确定性门禁拦截，结构错误回传模型重试，构建失败自动修复，前后端路由不一致自动补齐，代码与对话记忆通过 Git checkpoint 原子回滚。
>
> 当前 316 项 Python 单测全绿，10 条 Eval 用例通过，产物可在纯净容器中构建并完成云端发布模拟。

---

## 案例一：Dockerfile 权限 Bug —— 非 root 不是加一行 `USER` 就完事

### 30 秒版本

容器里同时存在四个身份视角：构建期 root、运行期 `app`、bind mount 的宿主属主、`group_add` 注入的 docker 组。
只改其中一个，就会出现“镜像能构建、容器起不来”或“本地能跑、挂载就 PermissionError”。我的做法是：**入口脚本以 root 做最小范围 chown，然后 `setpriv` 立即降权，并保留非 root 附加组**。

### 现象

- `python-ai` 启动后，上下文记忆目录无法创建，`global_summary.md` 静默写入失败。
- Chroma 模型预热时报缓存目录不可写；某些环境下 `pip install` 报 `/root/.local`、`/tmp/apphome` 权限拒绝。
- 自定义 Agent 的 Manifest 无法在运行时新建，抛 `PermissionError`。
- 镜像一度膨胀到 **1.84GB**，其中一层重复拷贝了 175MB 模型缓存。

### 根因

1. **构建期与运行期身份不一致**：镜像由 root 构建，文件默认 `root:root`；入口降权到 `app(999)` 后，应用没有写权限。
2. **bind mount 属主由宿主机决定**：宿主目录可能被 Docker 以 root 创建（755），容器内 `app` 无法写入。
3. **`HOME` 仍指向 `/root`**：ONNX / HuggingFace 缓存默认写到 `$HOME/.cache`，非 root 用户不可写。
4. **附加组被清空**：简单 `su app` 会丢掉 `group_add` 注入的 docker 组，导致无法访问 `/var/run/docker.sock`。
5. **Docker 层叠加**：`chown -R` 单独一个 `RUN` 会把整个模型目录复制进新层，镜像体积翻倍。

### 处置

```dockerfile
ENV HOME=/app
RUN mkdir -p /app/.cache/chroma \
    && python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction; DefaultEmbeddingFunction()(['warmup'])" \
    && chown -R app:app /app/.cache
```

```sh
# entrypoint.sh：root 只做最小范围修正，随后立即降权
if [ "$(id -u)" = "0" ]; then
  mkdir -p /app/data/project_memory
  chown -R app:app /app/data
  [ -d /app/config/custom_agents ] && chown -R app:app /app/config/custom_agents

  GROUPS="$(id -G | tr ' ' '\n' | grep -v '^0$' | paste -sd, - || true)"
  exec setpriv --reuid=app --regid=app --groups "${GROUPS}" -- "$@"
fi
exec "$@"
```

关键点：

- `HOME=/app`，模型缓存、pip 用户目录都有确定的可写位置；
- 预热与 `chown` 合并进同一个 `RUN`，避免复制 175MB 缓存层；
- `setpriv` 保留非 root 附加组，docker socket 权限不丢；
- chown 只覆盖运行时真正需要写的目录，不做全盘 `chown -R /app`。

### 验证

- 容器 `healthy`，模型预热后运行期离线可用；
- 316 项单测全绿；
- 镜像从 **1.84GB → 1.5GB**；
- `data/chroma`、`project_memory`、`custom_agents` 均由 `app` 可写。

### 面试追问

**Q：为什么不直接 `USER app`？**
A：构建期安装依赖、bind mount 属主修正都需要 root。正确姿势是“入口短暂 root → 最小 chown → 立即降权”，而不是全程 root 或全程非 root。

**Q：为什么不把数据目录改成 named volume？**
A：开发期需要直接在宿主机看 Chroma / 记忆文件；named volume 会牺牲可观测性。生产可以换 volume，但入口脚本的降权逻辑仍然需要。

---

## 案例二：Hybrid RAG —— RRF 融合的不是分数，是同一候选空间里的排名

### 30 秒版本

RRF 只对“同一候选空间”的排名有意义。我最初让 BM25 索引全栈规范、Chroma 索引企业长文档，结果两路排名不可比，无关的 BM25 第一名把真正答案挤出 Top-3。修复方式是**两路统一索引 `knowledge/`**，再用内容寻址 ID 做增量同步，并给单路故障加 fail-open。

### 现象

切换 Hybrid 后，问“JWT 过期时间设多久？”，Top-3 里没有“15 分钟”的答案段落；反而是一段全栈代码规范排在第一。
另外，Chroma 索引在进程重启后出现重复 Chunk；Markdown parser 还会产出只有标题、没有正文的空 Chunk。

### 根因

1. **跨语料 RRF 失真**：BM25 只索引 `knowledge/fullstack`，Chroma 只索引 `knowledge/enterprise_docs`。两路的 `rank=1` 含义不同，RRF 把两个不同分布的第一名直接相加，语义上不成立。
2. **重复写入**：初始化使用 `collection.add`，且没有扫描已有 ID；进程重启后同一段落被再次写入。
3. **空 Chunk 污染**：Markdown 标题后紧跟下一个标题时，parser 会 flush 出只有 heading 的块；它会被向量化并参与召回，稀释 Top-K。
4. **单路故障放大**：任一通道异常都会让整个检索失败，Planner 直接失去知识上下文。

### 处置

**统一候选空间**

```python
corpus_root = Path(settings.rag_knowledge_root)  # knowledge/
self._sparse = MarkdownRAGProvider(corpus_root)
self._dense = ChromaRAGProvider(
    corpus_root,
    collection_name=settings.chroma_hybrid_collection_name,  # knowledge_corpus
)
```

**内容寻址 + 增量同步**

```python
def _chunk_id(path: str, section: str, content: str) -> str:
    digest = hashlib.sha1(f"{path}\x00{section}\x00{content}".encode()).hexdigest()
    return f"doc-{digest}"
```

- 分页 `collection.get(limit=500, offset=...)` 扫描已有 ID；
- 新 Chunk 用 `upsert`，不重复；
- `existing_ids - expected_ids` 作为 stale，显式删除；
- 文档变化自然生成新 ID，旧块自动下线。

**Parser 去空块**

```python
body_lines = [
    line.strip()
    for line in current_lines
    if line.strip() and not _HEADING_RE.match(line.strip())
]
has_body = any(not line.startswith(">") for line in body_lines)
if not content or not has_body:
    current_lines = []
    return
```

**RRF + fail-open**

```python
entry.rrf_score += 1.0 / (self._rrf_k + rank)  # k=60
```

- 两路并行，各取 `max(10, top_k × 3)`；
- 按 `path + section` 去重；
- 单路异常只记录 warning，另一路继续；
- 两路都失败才抛 `RAGProviderError`。

### 验证

| 问题 | RRF Top-1 | 两路排名 |
|---|---|---|
| 我们公司的 API 用什么鉴权方式？ | 二、API 鉴权方式（OAuth 2.1 / IAM 签发 JWT） | bm25=1, chroma=1 |
| JWT 过期时间设多久？ | 三、JWT 令牌规范（访问令牌 15 分钟 / 刷新令牌 7 天） | bm25=2, chroma=1 |

- `test_reopen_persistent_index_does_not_duplicate` 验证重启不重复；
- `test_single_source_failure_degrades_gracefully` 验证降级；
- `test_both_sources_failure_raises` 验证双路失败边界；
- 生产容器默认 Provider 实测为 `hybrid`。

### 面试追问

**Q：为什么用 RRF 而不是加权分数融合？**
A：BM25 分数无界，cosine 相似度在 `[0,1]`，直接加权需要归一化且对 query 分布敏感；RRF 只用排名，鲁棒、无需调参，适合异构检索器。

**Q：为什么不用 Milvus？**
A：当前是单机、低运维场景，Chroma `PersistentClient` 足够；`RAGProvider` 是端口，后续换 Milvus / pgvector 不改上层。生产多租户再引入独立向量库。

**Q：知识库文档会不会造成 Prompt Injection？**
A：检索结果只作为“数据”注入，明确分隔并附带引用；不执行文档中的指令，工具调用仍受白名单约束。

---

## 案例三：LLM 幻觉拦截 —— 不让模型自己证明自己是对的

### 30 秒版本

模型自检和生成是同一个分布，靠 Prompt 说“不要幻觉”基本无效。我的策略是**确定性校验优先**：抽象目标直接拦截不调模型；结构错误回传原始输出重试；构建必须真实通过；前后端路由逐条比对，缺路由只允许后端补，补两次仍失败就阻断。

### 现象

- 用户说“做个工作台”，模型直接脑补出完整业务模块、假数据、假接口，代码看起来完整但不可用。
- Planner / Coder 偶发输出非 JSON、字段缺失、路径越界。
- 前端生成了 `onClick` 和 `/api/orders` 调用，后端根本没有这个路由；构建能过，运行时 404。
- 契约写“用户点击提交后展示成功状态”，生成页面只有静态按钮，没有 Loading / Error / Success。

### 根因

1. **抽象目标缺少可验收信息**，模型只能用训练分布补全，越补越像“样板房”。
2. **自由文本输出不可校验**，Prompt 约束不是契约。
3. **前端与后端由不同节点生成**，没有跨产物一致性检查。
4. **构建通过 ≠ 业务可用**，`npm run build` 不检查接口是否存在。

### 处置：四道闸门

**闸门 1：确定性骨架门禁**

```text
抽象目标（如“做个工作台”）
  → should_use_skeleton 确定性拦截
  → 不调用 LLM
  → 契约 skeletonMode=true
  → FrontendCoder 只渲染 Header + Sidebar + Main + 唯一占位
```

- 组件全部 `isPlaceholder=true`；
- 响应式 Grid；
- 不生成假业务、假接口、假数据；
- 先让用户确认骨架，再逐步补业务。

**闸门 2：结构化输出 + 错误回传**

- Planner / Coder 强制 JSON；
- Pydantic 校验失败时，把**原始输出 + 校验错误**一起回传模型重试；
- `AGENT_JSON_RETRY_LIMIT=2`；
- Planner 耗尽后降级为文本计划，不让整个 Run 崩掉；
- 文件路径拒绝 `..` / 绝对路径，只允许沙盒目标文件。

**闸门 3：真实构建验证**

```text
planner → coder → applier → verifier → evaluator
            ↑                    │
            └──── retry ≤ 3 ─────┘
```

- Verifier 在沙盒内执行真实 `npm run build`；
- 失败日志写入状态，Coder 整体重试；
- 3 次预算耗尽才返回 `FAILED`；
- Mock 的 `fail_first` 场景每次都能稳定复现“首次失败 → 自动修复 → 成功”。

**闸门 4：ConsistencyVerifier**

```text
前端 fetch(method, path, payload)
        ⇅ 逐条比对
后端 api-routes.json.routes
        │
        ├── 一致 → 继续构建
        └── 缺失 → BackendCoder 补路由（≤2 次）→ 重新校验
                              └── 仍缺失 → FULLSTACK_CONSISTENCY_FAILED
```

- 只允许后端补路由，**绝不改前端到无关接口**；
- `MAX_CONSISTENCY_REPAIRS = 2`；
- 契约中的每个业务动作（抓取 / 改写 / 投递 / 对话）都有独立 Express 路由与 Mock 处理，不折叠成通用 CRUD。

**附加：RAG Prompt 约束**

- 内部知识库优先级高于模型先验；
- 没有检索依据时不得编造条款；
- 输出附 `rag_citations`，可追溯到 `path + section`。

### 验证

- 抽象目标 E2E：完全不调用 LLM，契约 `skeletonMode=true`，页面只有骨架与占位；
- 全栈生成 E2E：前端 Method / Path / Payload 与后端路由一致，缺失路由可自愈；
- `test_hexagonal_architecture`、316 项单测全绿；
- 业务 Eval 覆盖纯前端 / CRUD / 上传，真实 DeepSeek 契约 + 产物语义断言。

### 面试追问

**Q：为什么不让 LLM 做自检？**
A：自检与生成同分布，容易“自信地确认自己的错误”。确定性检查（schema、构建、路由比对）成本低、可复现，应该放在 LLM 自检之前。

**Q：一致性修复为什么只允许后端改？**
A：前端承载用户已确认的交互契约，改前端会让用户看到的行为漂移；后端补路由是更小、更可控的修复面。

---

## 案例四：Java → Python 的 POST body 离奇消失 —— 协议协商也是契约

### 30 秒版本

GET 健康检查正常，Java 一 POST 到 FastAPI 就 422 missing body；curl 直连 Python 却完全正常。根因是 JDK HttpClient 默认尝试 `h2c Upgrade`，Uvicorn 不支持升级，请求体在升级过程中丢失。修复是显式锁定 HTTP/1.1，并把跨语言失败语义统一为 `503 + DEGRADED`。

### 现象

- `GET /api/v1/health` 从 Java 代理到 Python 正常；
- `POST /api/v1/internal/runs/execute` 偶发 422，FastAPI 日志显示 body 为空；
- 同一个请求用 `curl` 直连 Python 完全正常；
- 前端表现为“提交标注后无反应”，BFF 日志里是反序列化失败。

### 根因

1. JDK `HttpClient` 默认使用 HTTP/2，明文场景会先发 `Upgrade: h2c`；
2. Uvicorn 不支持 h2c 升级；
3. 部分请求在升级握手与降级回退之间丢失 body；
4. 跨语言调用只校验了 JSON 字段，没有校验**传输层协议**。

### 处置

```java
HttpClient.newBuilder()
    .version(HttpClient.Version.HTTP_1_1)
    .connectTimeout(Duration.ofSeconds(5))
    .build();
```

- 强制 HTTP/1.1；
- 长读超时 300s，匹配 Agent + 沙盒构建耗时；
- Python 不可达时返回 `503 + DEGRADED`，不抛 500；
- Python 业务失败返回 `200 + status=FAILED`，HTTP 错误只用于参数 / 基础设施问题。

### 验证

- `scripts/smoke-test.sh` 验证 Java → Python 健康检查；
- `scripts/platform_e2e_test.py` 真实走 Vite 代理 → BFF → AI Core → 沙盒 → 构建；
- 三服务健康检查中 `dependencies.pythonAi.status=UP`；
- 316 项单测不依赖真实网络，跨语言链路由 E2E 兜底。

### 面试追问

**Q：为什么不是 FastAPI 的 bug？**
A：FastAPI 按 HTTP/1.1 正常解析；问题出在客户端发起了服务端不支持的协议升级。跨语言联调要把“协议版本、超时、错误码”也当成契约的一部分。

**Q：为什么业务失败返回 200？**
A：Run 的状态机失败是**业务结果**，不是传输错误；前端需要拿到完整状态、错误与重试信息。4xx/5xx 只留给参数错误与基础设施不可用。

---

## 案例五：沙盒 Git Checkpoint —— 代码回滚了，记忆没回滚，等于没回滚

### 30 秒版本

沙盒是项目唯一真源。每次成功 Run 生成一个全局 Git commit，同时为每个标注线程保存 `{commitHash, chatHistory, runs, files}` checkpoint。回滚时必须在**同一个请求内**完成 `git reset --hard` 与 `ThreadMemoryStore` 恢复，否则代码和对话记忆会分叉；预览 URL 还要带 `?v=` 强制刷新 iframe。

### 现象

- 删除一个标注后，代码回到了基线，但该圈的聊天历史还在，下一轮模型继续引用已删除的上下文；
- 回滚后预览 iframe 仍显示旧页面，用户以为回滚失败；
- 多标注批量 Run 只生成一个全局 commit，按单个标注回滚时定位不到正确基线；
- 浮层发送后立即关闭，迟到的 SSE 结果仍可能写回已销毁的会话。

### 根因

1. **代码与记忆是两个存储**：Git 管源码，`ThreadMemoryStore` 管对话 / runs；只回滚一个就会分叉。
2. **全局 commit 与线程 checkpoint 不是一一对应**：一个批量 Run 包含多个标注，每个标注有自己的 `turnCount`。
3. **iframe 缓存**：预览 URL 不变，浏览器不会重新加载。
4. **异步竞态**：请求已发出，前端浮层已关闭，迟到事件没有归属。

### 处置

**沙盒创建即基线**

```text
git init
git commit -m "baseline: sandbox init"
```

**每次成功 Run**

```text
git add .
git commit -m "run: {runId} - turn: {turnCount}"
```

响应返回：

```json
{
  "commitHash": "...",
  "baselineCommit": "...",
  "commits": [{ "threadId": "...", "turnCount": 2, "commitHash": "..." }]
}
```

**回滚原子化**

```text
POST /api/v1/runs/rollback
  { sandboxId, threadId, commitHash, runId?, turnCount? }
        │
        ├── 校验 40 位 commit（防注入）
        ├── git reset --hard <commit>
        ├── ThreadMemoryStore 恢复 chatHistory / runs / files
        └── previewUrl + ?v=<revision>
```

- 两步必须在同一请求内完成；
- 删除标注回到该圈创建前基线；
- 「清除全部」回到最早执行圈的基线；
- 预览 URL 带版本号，强制 iframe 刷新。

**竞态处理**

- 浮层关闭时中止请求；
- 丢弃迟到结果；
- 预览不因迟到事件报错；
- E2E 覆盖“发送后立即关闭浮层”。

### 验证

- `scripts/rollback_e2e_test.py`：Git 提交顺序、逐轮回滚、回滚后续写；
- `scripts/annotation_delete_rollback_e2e_test.py`：删除标注回到创建前基线、清除全部；
- `scripts/popover_close_race_e2e_test.py`：迟到结果丢弃、预览不报错；
- 316 项单测覆盖状态机与边界。

### 面试追问

**Q：为什么不用数据库事务？**
A：代码在 Git，记忆在进程内存储，跨存储事务成本高。当前用“同请求顺序执行 + 幂等 + 版本号”保证一致性；M2 计划用 Event Sourcing 重放未删除标注的意图。

**Q：为什么批量 Run 只生成一个 commit？**
A：一次批量提交是用户的一次原子操作，生成一个全局 commit 才能保证代码快照一致；每个标注的 `turnCount` 与 checkpoint 单独记录，用于细粒度回滚。

---

## 答辩数据面板

| 指标 | 数值 |
|---|---|
| Python 单测 | **316 项全绿** |
| 离线 Eval | 10 条核心用例，约 40ms |
| 真实沙盒 Eval | 10/10，约 35s |
| 一致性自愈上限 | 2 次 |
| 构建自愈上限 | 3 次 |
| Architect 澄清上限 | 10 轮 |
| Chroma 融合 | RRF k=60，双路各取 `top_k×3` |
| 5 个沙盒常驻内存 | 约 426 MiB |
| python-ai 镜像 | 约 1.5GB |
| Hybrid RAG 实测 | 两问 Top-1 精准命中 OAuth 2.1 / 15 分钟 |

---

## 面试官可能追问的 8 个问题

1. **“你怎么保证生成代码能跑？”**
   真实沙盒 `npm run build`，失败回传错误重试 3 次；构建通过后再做前后端路由一致性检查。

2. **“模型胡编接口怎么办？”**
   ConsistencyVerifier 逐条比对 Method / Path / Payload；缺路由只让后端补，最多 2 次，仍缺失就阻断。

3. **“为什么不用多 Agent 自由对话？”**
   Supervisor 只路由，子图职责固定；自由对话会放大不确定性和 Token 成本，工程闭环需要确定性边界。

4. **“RAG 为什么不用纯向量？”**
   硬性规范依赖关键词精确命中（字段名、接口名、条款号）；纯向量会漏精确匹配。Hybrid + RRF 兼顾两者。

5. **“Chroma 挂了怎么办？”**
   单路 fail-open，BM25 继续；两路都挂才抛错。Planner 侧检索失败不阻断代码修改。

6. **“沙盒安全吗？”**
   本地开发挂 Docker Socket，等价 root，仅限受信环境；容器 `cap_drop=ALL`、`no-new-privileges`、资源限额、端口只绑 `127.0.0.1`。生产需独立节点 + gVisor。

7. **“产物真的能部署吗？”**
   验收脚本完成云端发布模拟：未迁移 503 → 独立 migrate → 校验表 / checksum → CRUD → 重复迁移幂等；纯净容器构建通过。

8. **“这个项目最大的技术难点是什么？”**
   不是模型调用，而是**跨语言、跨存储、跨进程的一致性**：Java / Python 协议契约、Git / 记忆原子回滚、前端 / 后端路由一致、构建失败自愈。

---

## 诚实边界

- 平台尚未实际部署到 Render / 阿里云，云端发布为本地模拟验收；
- Docker Socket 挂载不适合生产直接复用；
- Chroma 单机嵌入式，无多副本 / 扩缩容；
- 默认 MiniLM 对中文非最优，BGE-M3 为预留升级路径；
- BFF 当前无业务数据库持久化，生成产物才走真实数据库与迁移。
