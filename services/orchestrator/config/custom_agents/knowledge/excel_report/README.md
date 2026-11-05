# Excel 报表知识库（演示）

演示用私有 RAG 目录。M3 阶段由 CustomAgent Runtime 在隔离沙盒中加载，
当前仅用于证明 Manifest 的 `ragKnowledgeDir` 为合法相对路径。

## 企业报表规范

- 标题：`{业务域} {月份} 报表`
- 明细页：业务日期、订单号、客户、区域、数量、单价、金额
- 汇总页：按区域与月份聚合，保留金额合计
- 文件命名：`reports/{business}-{YYYY-MM}.xlsx`
- 所有金额单位统一为人民币元
