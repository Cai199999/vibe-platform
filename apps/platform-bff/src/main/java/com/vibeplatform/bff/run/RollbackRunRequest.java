package com.vibeplatform.bff.run;

import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;

/**
 * 单标注回滚请求。
 *
 * <p>BFF 只做路由级校验：commit hash 必须是 40 位十六进制，避免任意字符串
 * 进入沙盒 Git 命令；thread / turn 的语义由 Python AI Core 的 checkpoint
 * 存储最终裁决。</p>
 *
 * @param sandboxId  目标沙盒
 * @param tenantId   可选租户，缺省由 AI Core 使用 tenant_local
 * @param projectId  项目作用域；AI Core 用它隔离同名 threadId 的会话记忆
 * @param threadId   标注圈对应的 Agent thread
 * @param commitHash 该轮成功 Run 生成的 Git commit
 * @param runId      可选的 Run ID，用于精确定位 checkpoint
 * @param turnCount  可选的轮次，用于精确定位 checkpoint
 */
public record RollbackRunRequest(
        @NotBlank String sandboxId,
        String tenantId,
        String projectId,
        @NotBlank String threadId,
        @NotBlank
        @Pattern(regexp = "^[0-9a-f]{40}$", message = "commitHash 必须是 40 位小写十六进制")
        String commitHash,
        String runId,
        @Min(1) Integer turnCount
) {
}
