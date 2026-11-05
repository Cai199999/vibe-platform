# 3 分钟面试演示脚本

> 目标：在 3 分钟内让面试官看到「精准修改 → 全局架构 → 专属 Agent 秒级接入」
> 三段递进能力，并用 Supervisor 决策可视化证明系统不是黑盒。
>
> 演示前 5 分钟完成：`docker compose up -d`，打开 <http://127.0.0.1:5173>，
> 模型选择「本地预览（Mock）」保证节奏稳定；网络与 API Key 正常时可切
> 「DeepSeek V3」展示真实生成。

---

## 0:00–0:20 开场：一句话说明

> “这是一个可视化 Vibe Coding 平台。用户圈选页面元素，用自然语言描述意图，
> 平台会自动判断这是局部修改、全局架构，还是两者都要，再交给不同 Agent
> 在隔离沙盒里执行并预览。”

打开工作台，指向顶部工具栏：

- 左侧：模型选择、标注数量
- 右侧：**✨ 我的专属 Agent**、**下一步：构建全栈方案**、**提交**
- 画布下方：执行时间轴与预览

---

## 0:20–1:00 第一幕：精准局部修改

**点击顺序**

1. 在画布上圈选「提交」按钮。
2. 在浮层输入：`把提交按钮改成蓝色`。
3. 点击浮层里的 **发送**。
4. 等待右侧时间轴出现 `run.succeeded`，切到 **预览**。

**让面试官看的点**

- 顶部出现 **Supervisor 决策条**：
  `🤖 正在分析意图…` → `🎯 判定为局部 UI 修改`
- 右侧时间轴逐条出现 `plan → patch → build → verify → commit`
- 预览中的按钮变蓝，代码已提交到沙盒 Git checkpoint

**一句话解释**

> “这里没有让大模型直接改文件。Supervisor 只做路由，UI Graph 负责规划、
> 打补丁、构建验证，失败会进入有界修复循环，成功后在沙盒内自动 commit。”

---

## 1:00–2:10 第二幕：全局架构 + both 强门控（最惊艳）

**点击顺序**

1. 保留刚才的按钮标注（或重新圈选任意元素）。
2. 点击顶部 **下一步：构建全栈方案**。
3. 观察顶部决策条变为：
   `🎯 判定为全栈构建（架构 + 前端）`
4. 观察两阶段时间轴：

```text
【阶段 1 · 架构澄清】 进行中 · 先确认全栈实施契约
          │
【阶段 2 · 前端修改】 等待门控释放
```

5. 在右侧 Architect 面板回答 2–3 轮澄清（应用形态、业务实体、接口约束）。
6. 生成《全栈实施契约》后，点击 **确认契约，进入生成**。
7. 观察时间轴自动切换：

```text
【阶段 1 · 架构澄清】 ✓ 契约已确认，门控可释放
          │
【阶段 2 · 前端修改】 进行中 · 正在执行界面修改
```

8. 等待 UI Run 成功，展示预览。

**让面试官看的点**

- both 模式下 UI Graph **不会提前启动**，提交按钮与画布被冻结；
- 契约未确认时，后端 release 返回 `409 SUPERVISOR_CONTRACT_NOT_CONFIRMED`；
- 契约确认后前端自动调 `POST /gates/{gateId}/release`，只启动一个 UI Run；
- 时间轴从「等待门控」变为「执行中」，不是伪造的 Loading。

**一句话解释**

> “全栈需求最怕前后端各做各的。这里用项目级强门控保证契约先行：Architect
> 先产出可确认的实施契约，UI 才能拿到放行信号。项目锁保证同一项目不会并发
> 启动两个互相覆盖的 Run。”

---

## 2:10–2:50 第三幕：专属 Agent 秒级接入

**点击顺序**

1. 点击顶部 **✨ 我的专属 Agent**。
2. 展示下拉中的 **Excel 报表专家**：
   - 能力：`excel_report_generation`
   - 关键词：`数据报表 / 销售数据 / Excel生成 …`
   - 标注：**仅路由 · M3 执行**
3. 关闭菜单，圈选画布任意元素。
4. 在浮层输入：`帮我导出一份本月的销售数据`。
5. 点击 **发送**。

**预期结果**

- 决策条显示：`🎯 判定为我的专属 Agent`
- 出现紫色专属 Agent 卡片：
  `Excel 报表专家 · 任务类型 excel_report_generation · 命中 销售数据`
- 只调用 `POST /api/v1/supervisor/route`，**不调用 dispatch、不创建沙盒**
- 卡片明确说明：运行时将在 M3 进入独立受限沙盒

**让面试官看的点**

打开 `services/orchestrator/config/custom_agents/excel_report_agent.json`：

```json
{
  "agentId": "excel_report_agent",
  "displayName": "Excel 报表专家",
  "taskTypes": ["excel_report_generation"],
  "keywords": ["数据报表", "Excel生成", "销售数据"],
  "systemPromptFile": "prompts/excel_report_agent.md",
  "ragKnowledgeDir": "knowledge/excel_report"
}
```

然后展示重扫接口：

```bash
curl -X POST http://127.0.0.1:8100/api/v1/internal/supervisor/agents/reload
```

**一句话解释**

> “新增一个企业 Agent 不需要改 Supervisor、Java 或前端代码，只要放一份
> Manifest。注册中心启动时自动扫描，关键词命中就能路由。Manifest 是纯声明，
> 不执行代码；真正的执行会在 M3 的独立沙盒里跑，避免 Prompt 注入和越权。”

---

## 2:50–3:00 收尾：一张图总结

指向 README 的 Mermaid 架构图，用三句话收尾：

1. **Supervisor 只做路由**：不写代码、不改子图状态，能力可无限挂载；
2. **声明式 Manifest**：Agent / Tool / Skill 分层，企业 Agent 秒级接入；
3. **强门控 + 沙盒**：契约先行、单项目单门控、所有生成代码只在 Docker 沙盒执行。

---

## 演示节奏与兜底

| 情况 | 处理 |
|---|---|
| DeepSeek 响应慢 / 不稳定 | 切「本地预览（Mock）」，全链路仍真实执行，仅代码生成走确定性模板 |
| 沙盒构建超过 30 秒 | 先讲架构图与 Supervisor 决策条，构建完成后再切预览 |
| 网络不可用 | 直接展示 `docs/screenshots/supervisor-mixed-gate-locked.png`、`supervisor-mixed-gate-released.png`、`custom-agent-routed.png` |
| 面试官追问“自定义 Agent 真的执行了吗” | 明确回答：M2.5 只完成注册与路由，执行在 M3 独立沙盒；不夸大能力 |
| 面试官追问记忆 | 当前只有 LangGraph 线程级短期记忆，长期记忆与用户画像刻意留到 M3 |

## 可量化指标（可选展示）

```bash
# 单元测试
docker run --rm -v "$PWD":/repo -w /repo/services/orchestrator \
  -e PYTHONPATH=/repo/services/orchestrator vibe-platform/python-ai:dev \
  python -m unittest discover -s tests -v

# API + 浏览器 E2E
python3 scripts/supervisor_e2e_test.py
python3 scripts/supervisor_both_e2e_test.py
/tmp/vibe-browser/bin/python scripts/supervisor_mixed_ui_e2e_test.py
/tmp/vibe-browser/bin/python scripts/custom_agent_e2e_test.py
```
