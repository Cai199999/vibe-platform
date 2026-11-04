package com.vibeplatform.bff.run;

import com.fasterxml.jackson.databind.JsonNode;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import java.util.Map;

/**
 * 前端画布提交的 Run 请求。
 *
 * <p>BFF 不重复实现 AnnotationEnvelope 的全部字段校验：annotation / options
 * 以 {@link JsonNode} 透传给 Python AI Core，由 Python 侧单一真源校验。
 * BFF 只保证路由必需字段存在，避免契约在两个语言里漂移。</p>
 *
 * @param runId      可选，前端生成；缺省由 Python 生成
 * @param tenantId   可选，M1 单租户
 * @param projectId  项目 ID
 * @param annotation 画布标注（AnnotationEnvelope）
 * @param files      沙盒初始文件
 * @param options    LLM / 重试 / 沙盒保留选项
 */
public record CreateRunRequest(
        String runId,
        String tenantId,
        @NotBlank String projectId,
        @NotNull JsonNode annotation,
        Map<String, String> files,
        JsonNode options
) {
}
