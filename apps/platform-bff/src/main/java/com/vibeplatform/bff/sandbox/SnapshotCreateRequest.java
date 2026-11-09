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
 */
public record SnapshotCreateRequest(
        @NotBlank(message = "快照备注不能为空")
        @Size(max = 200, message = "快照备注最多 200 字")
        String message
) {
}
