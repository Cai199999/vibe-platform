package com.vibeplatform.bff.sandbox;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * 创建快照请求。
 *
 * <p>备注必填，BFF 只做长度与空白校验；真正的 Git 提交由 Python AI Core
 * 在沙盒内执行。备注最终会写入 commit message，因此限制 200 字。</p>
 *
 * @param message 用户填写的快照备注，如“完成了登录页样式”
 * @param projectId 可选项目 ID，用于关联 local/global 上下文记忆
 * @param tenantId 可选租户 ID，与 projectId 一起定位记忆目录
 * @param latestChanges 本次保存涵盖的改动说明，用于生成项目全局摘要
 */
public record SnapshotCreateRequest(
        @NotBlank(message = "快照备注不能为空")
        @Size(max = 200, message = "快照备注最多 200 字")
        String message,
        @Size(max = 64, message = "projectId 最多 64 字符")
        String projectId,
        @Size(max = 64, message = "tenantId 最多 64 字符")
        String tenantId,
        @Size(max = 4000, message = "latestChanges 最多 4000 字符")
        String latestChanges
) {
}
