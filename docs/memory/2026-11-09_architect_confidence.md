# 2026-11-09 · Architect 置信度耗尽（CONFIDENCE_EXHAUSTED）专项修复

## 复现

新增 `scripts/architect_confidence_debug_test.py`，模拟：

1. 画“立即抢购”按钮，点击扣库存并跳转，下一步构建全栈方案；
2. 用户补充：Java Spring Boot + MySQL；
3. 用户补充：核心实体是订单和商品表；
4. 用户补充：不需要登录鉴权；
5. 用户补充：API 风格 RESTful。

修复前结果：

- **Mock**：5 轮 analyze 全部返回 `raw=0.90`、`cap=0.60`，
  `missing=[backend, database, business, data]` 从未变化；第 5 轮路由到
  `CONFIDENCE_EXHAUSTED`。Mock 完全不读取 `ui_history`，用户补充无效。
- **DeepSeek**：`raw=0.93`，但 `data.entities` 一直缺失，`cap=0.70`；
  即使槽位已齐，`assumptions` 又会把分数压到 `0.84`（低于 0.85）；
  LLM 自报的 `auth.required` 还可能作为额外 missing 参与封顶。

## 根因

1. **Prompt 缺少完成度规则**：模型要求实体必须带完整字段，用户只列
   “订单和商品表”时不写入 `data.entities`，反复追问同一槽位。
2. **确定性规则过严**：
   - LLM 自报的可选槽位（`auth.required` / `api.style`）被并入 missing，
     未知槽位按 0.78 封顶；
   - `assumptions` 一律封顶 0.84，与“允许默认技术栈”的目标冲突。
3. **问题排序缺陷**：`_normalize_questions` 先塞满 LLM 的 3 个可选问题，
   真正缺失的必需槽位排不进去；用户连续回答登录 / API 风格，必需槽位
   永远不补。
4. **Mock 剧本不会学习**：`MockArchitectLLM` 只看 `context.slots`，
   忽略历史对话里的技术栈 / 实体描述。

## 修复

- `architect/slots.py`：新增确定性 `infer_slots()`，从自然语言识别
  Spring Boot / FastAPI / NestJS、MySQL / PostgreSQL / SQLite、实体清单、
  业务动作、登录鉴权、RESTful；只在槽位为空时回填。
- `architect/graph.py`：`clarify_gate` 对结构化回答与“补充说明”都执行
  槽位回填，用户说过的内容不再依赖 LLM 自觉写入。
- `architect/llm.py`：
  - `MockArchitectLLM.analyze()` 从 `ui_history` 回填槽位，分数随信息
    完整度自然提升；
  - DeepSeek System Prompt 增加“槽位填充与完成度规则”：实体名即可算
    已填、缺失字段写 assumptions、已回答槽位不得重复追问、可选槽位不阻塞、
    必需槽位齐全时 `confidence_score >= 0.86`。
- `architect/confidence.py`：
  - 只有当前形态的必需槽位参与封顶与路由，可选槽位不再扣分；
  - `assumptions` 在槽位齐全时封顶 0.90（可进入契约），仅在仍有必需
    槽位缺失时保持 0.84；
  - `_normalize_questions()` 改为必需槽位优先，LLM 可选问题只能在剩余
    名额内追加。

## 验证

- 诊断脚本 `--provider both`：Mock 第 3 轮达到 `0.94`，DeepSeek 第 2 轮
  达到 `0.90`，均生成契约，无 `CONFIDENCE_EXHAUSTED`。
- Python 全量：**182 tests OK**（新增可选槽位、问题优先级、Mock 动态
  提取、comment 回填 4 例）。
- `architect_m2_5_e2e_test.py` 与 `architect_ui_e2e_test.py` 均 PASS。
