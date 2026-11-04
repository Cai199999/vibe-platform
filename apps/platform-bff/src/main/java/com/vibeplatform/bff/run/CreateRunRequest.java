package com.vibeplatform.bff.run;

import com.fasterxml.jackson.annotation.JsonIgnore;
import com.fasterxml.jackson.databind.JsonNode;
import jakarta.validation.constraints.AssertTrue;
import jakarta.validation.constraints.NotBlank;
import java.util.Map;

/**
 * 前端画布提交的 Run 请求。
 *
 * <p>BFF 不重复实现 AnnotationEnvelope 的全部字段校验：annotations / annotation
 * 与 options 以 {@link JsonNode} 透传给 Python AI Core，由 Python 侧单一真源校验。
 * BFF 只保证路由必需字段与“至少有一个标注”存在，避免契约在两个语言里漂移。</p>
 *
 * <p>M1 升级后首选 {@code annotations: AnnotationEnvelope[]}（最多 3 个）。
 * 旧的单标注字段 {@code annotation} 继续保留，Python 侧会自动归一化。</p>
 *
 * @param runId       可选，前端生成；缺省由 Python 生成
 * @param tenantId    可选，M1 单租户
 * @param projectId   项目 ID
 * @param annotations 批量画布标注（最多 3 个）
 * @param annotation  旧协议：单个画布标注
 * @param sandboxId   可选：同一标注多轮修改时复用当前沙盒
 * @param files       沙盒初始文件
 * @param options     LLM / 重试 / 沙盒保留选项
 */
public record CreateRunRequest(
        String runId,
        String tenantId,
        @NotBlank String projectId,
        JsonNode annotations,
        JsonNode annotation,
        String sandboxId,
        Map<String, String> files,
        JsonNode options
) {

    /**
     * 至少提供一个标注。
     *
     * <p>只做存在性校验；数量上限、坐标与意图字段由 Python Pydantic 模型校验。
     * {@link JsonIgnore} 防止该派生属性被再次序列化进发往 AI Core 的请求体。</p>
     */
    @JsonIgnore
    @AssertTrue(message = "annotations 或 annotation 至少提供一个")
    public boolean isAnnotationsPresent() {
        boolean hasBatch = annotations != null
                && annotations.isArray()
                && !annotations.isEmpty();
        boolean hasSingle = annotation != null
                && !annotation.isNull();
        return hasBatch || hasSingle;
    }
}
