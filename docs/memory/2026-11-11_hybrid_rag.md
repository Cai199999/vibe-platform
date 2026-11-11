# 2026-11-11 · 混合 RAG（BM25 + Chroma + RRF）

## 目标

把纯 Markdown/BM25 知识检索升级为本地混合 RAG，在不引入 Milvus 等
独立服务的前提下，让企业长文档进入语义检索。

## 已完成

1. **本地向量库**
   - 依赖 `chromadb==1.0.15`，使用 `PersistentClient`；
   - Embedding 为内置 ONNX MiniLM，无需 API Key / PyTorch；
   - Dockerfile 构建阶段预热模型，运行期离线；
   - 切块规则与 BM25 完全一致，内容寻址 ID + 增量 upsert / 删除。
2. **HybridRAGProvider**
   - BM25 与 Chroma 并行召回，各取 `top_k×3`；
   - RRF（k=60）融合，按 `path + section` 去重；
   - 单路失败降级，两路失败才抛错；
   - metadata 保留两路排名，便于排查。
3. **接入**
   - Architect 默认 `hybrid`，前端默认提交 `hybrid`；
   - Planner 初始状态注入 `rag_provider/rag_top_k`，检索结果写入
     `rag_context/rag_citations` 并追加到计划；
   - 纯拖拽跳过 RAG。
4. **语料**
   - 双路统一索引 `knowledge/`（fullstack 规范 + enterprise_docs）；
   - 新增《企业内部安全开发规范与 JWT 鉴权指南》约 2400 字。

## 验证

- `demo/rag_hybrid_demo.py` 实测两问：
  - “API 用什么鉴权方式？” → RRF #1 “二、API 鉴权方式”，返回 OAuth 2.1 / JWT；
  - “JWT 过期时间设多久？” → RRF #1 “三、JWT 令牌规范”，返回 15 分钟 / 7 天。
- 生产容器默认 Provider 为 `hybrid`，Top-3 均由企业文档命中。
- Python 单测 **316 项全绿**（新增 10 项）。

## 边界

- Embedding 仍是英文 MiniLM，中文短句可用但非最优；
- Chroma 单机嵌入式，无多副本/扩缩容；
- Planner RAG 默认 Top-3，失败不阻断代码修改。
