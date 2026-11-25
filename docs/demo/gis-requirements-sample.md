# 测绘任务与成果管理平台 · 需求摘要

## 角色与权限

- 项目经理：创建项目、拆分任务、派发任务、查看项目进度。
- 作业员：只能查看和提交自己名下的任务；上传影像成果。
- 质检员：查看项目全部成果；标记合格 / 返工；返工必须追加 QCRecord，保留历史。
- 管理员：租户内用户与角色管理，不参与业务审批。

## 业务流程

项目创建 -> 任务拆分 -> 派发 -> 作业员上传成果 -> 质检员审核 -> 合格归档 / 返工重提。

## 成果质检规则

- 每个项目至少抽检 20% 的成果；重点成果全检。
- 返工后必须追加一条 QCRecord，不得覆盖原记录。
- 项目结项要求所有成果质检合格。

## 影像上传

- 支持 TIFF / JPEG / ZIP；单文件不超过 2GB。
- 上传后自动关联任务编号与坐标系。
- 文件正文存对象存储，元数据存 MySQL。

## 数据实体

```text
Project(id, name, region, managerId, status, progress)
Task(id, projectId, title, assigneeId, deadline, status)
Deliverable(id, taskId, fileName, uploaderId, uploadedAt, qcStatus, storagePath)
QCRecord(id, deliverableId, reviewerId, result, comment, createdAt)
```
